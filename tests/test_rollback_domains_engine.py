"""The domain hand-over against the API: what is called, and in which order.

The pure plan order (test_rollback_domains.py) cannot tell whether the restore
actually PATCHes anything. These tests run the REAL steps and compensations
against respx and record the call order: the target claims the source's hosts
and the source is never written at CREATE (make-before-break); after FINALIZE
the source's domains are PATCHed back before ``/restart`` queues the deploy that
bakes the stored labels into its container, and the target that holds them is
deleted only once the source holds them again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.kinds import GitAuth, ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import Compensation, FinalizePolicy, Outcome
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.compensations import build_compensations
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import RunResult, Saga
from bg_coolify_migrate.errors import CoolifyApiError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SHOP = "https://shop.example.com"


def _snapshot(uuid: str = "app1", name: str = "shop", **kw: Any) -> ResourceSnapshot:
    base: dict[str, Any] = {
        "uuid": uuid,
        "name": name,
        "collection": "applications",
        "kind": ResourceKind.APP_DOCKERIMAGE,
    }
    return ResourceSnapshot(**{**base, **kw})


def _plan(*snapshots: ResourceSnapshot) -> MigrationPlan:
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=tuple(
            ResourcePlan(snapshot=s, strategy=Strategy.COPY_DATA)
            for s in (snapshots or (_snapshot(),))
        ),
    )


def _host() -> FakeHost:
    """No volume pins, and the key revoke every target delete now runs first."""
    return (
        FakeHost()
        .on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        .on(r"authorized_keys\.bak", exit_status=0)
        .on(r"rm -rf /root/\.coolify-migrate/", exit_status=0)
    )


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_host(),  # type: ignore[arg-type]
        target_host=_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


def _conflict(*holders: str | None) -> httpx.Response:
    """A realistic 409: the shape checkIfDomainIsAlreadyUsedViaAPI returns."""
    return httpx.Response(
        409,
        json={
            "message": "Domain conflicts detected. Use force_domain_override=true to proceed.",
            "conflicts": [
                {
                    "domain": "shop.example.com",
                    "resource_name": "shop",
                    "resource_uuid": h,
                    "resource_type": "application",
                }
                for h in holders
            ],
            "warning": "Using the same domain for multiple resources can cause routing conflicts.",
        },
    )


class Calls:
    """Records the API calls that matter, in order."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    def patch(self, *responses: httpx.Response) -> Any:
        queue = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.read())
            self.bodies.append(body)
            if "name" in body:
                self.order.append("patch:name")
            elif body.get("force_domain_override"):
                self.order.append("patch+force")
            else:
                self.order.append("patch")
            return queue.pop(0) if len(queue) > 1 else queue[0]

        return handler

    def mark(self, label: str, response: httpx.Response | None = None) -> Any:
        def handler(_request: httpx.Request) -> httpx.Response:
            self.order.append(label)
            return response or httpx.Response(200, json={})

        return handler


class SourceRow:
    """The source application's row: a GET shows it, a successful PATCH changes it.

    The restore reads the source before it writes and the target delete reads it
    again to see it holds its hosts, so a static mock would hide exactly the
    ordering these tests lock.
    """

    def __init__(self, calls: Calls, *, fqdn: str = "", compose: dict[str, Any] | None = None):
        self.calls = calls
        self.state: dict[str, Any] = {"uuid": "app1", "name": "shop", "fqdn": fqdn}
        if compose is not None:
            self.state["docker_compose_domains"] = json.dumps(compose)

    def get(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.state)

    def patch(self, *responses: httpx.Response) -> Any:
        record = self.calls.patch(*responses)

        def handler(request: httpx.Request) -> httpx.Response:
            response = record(request)
            if response.status_code < 300:
                body = self.calls.bodies[-1]
                if "domains" in body:
                    self.state["fqdn"] = body["domains"]
                if "docker_compose_domains" in body:
                    self.state["docker_compose_domains"] = json.dumps(
                        {e["name"]: {"domain": e["domain"]} for e in body["docker_compose_domains"]}
                    )
            return response

        return handler


def _source(respx_mock: respx.Router, calls: Calls, row: SourceRow, *responses: httpx.Response) -> None:
    respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=row.get)
    respx_mock.patch(f"{BASE}/applications/app1").mock(
        side_effect=row.patch(*(responses or (httpx.Response(200, json={}),)))
    )


async def _rollback(ctx: MigrationContext) -> RunResult:
    saga = Saga(journal=ctx.journal, context=ctx, steps={}, compensations=build_compensations())
    return await saga.rollback()


def _journal_create(ctx: MigrationContext, parked: dict[str, Any] | None = None) -> None:
    ctx.journal.append(
        "step_completed",
        state="create_target",
        detail={
            "target_uuids": {"app1": "appT"},
            "parked_domains": {"app1": parked or {"domains": SHOP}},
        },
    )


class TestRollbackOrderAgainstTheApi:
    async def test_after_finalize_the_domains_are_restored_before_the_source_restarts(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """The regression lock: patch -> patch+force -> restart -> delete.

        FINALIZE released the source's custom host; the target still exists and
        holds it (it is deleted last), so the restore 409s on it and the scoped
        force applies. The target goes only once the source holds the host again.
        """
        _journal_create(ctx)
        ctx.journal.append("step_completed", state="quiesce", detail={})
        calls = Calls()
        _source(respx_mock, calls, SourceRow(calls, fqdn=""), _conflict("appT"), httpx.Response(200, json={}))
        respx_mock.post(f"{BASE}/applications/app1/restart").mock(side_effect=calls.mark("restart"))
        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=calls.mark("delete"))

        result = await _rollback(ctx)

        assert calls.order == ["patch", "patch+force", "restart", "delete"]
        assert calls.bodies[1] == {"domains": SHOP, "force_domain_override": True}
        assert result.outcome is Outcome.ROLLED_BACK

    async def test_before_finalize_the_source_is_not_written(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Make-before-break: the source never lost its host before FINALIZE, so
        # the restore compares and sends nothing — no PATCH, no force.
        _journal_create(ctx)
        ctx.journal.append("step_completed", state="quiesce", detail={})
        calls = Calls()
        _source(respx_mock, calls, SourceRow(calls, fqdn=SHOP))
        respx_mock.post(f"{BASE}/applications/app1/restart").mock(side_effect=calls.mark("restart"))
        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=calls.mark("delete"))

        result = await _rollback(ctx)

        assert calls.order == ["restart", "delete"]
        assert result.outcome is Outcome.ROLLED_BACK

    async def test_a_failed_restore_keeps_the_target_but_still_restarts(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The target is the last holder of the custom host: deleting it would let
        # Coolify delete a managed DNS record for good. It stays, and says so.
        _journal_create(ctx)
        ctx.journal.append("step_completed", state="quiesce", detail={})
        calls = Calls()
        _source(
            respx_mock,
            calls,
            SourceRow(calls, fqdn=""),
            httpx.Response(422, json={"message": "Validation failed."}),
        )
        restart = respx_mock.post(f"{BASE}/applications/app1/restart").mock(
            side_effect=calls.mark("restart")
        )
        delete = respx_mock.delete(f"{BASE}/applications/appT").mock(
            side_effect=calls.mark("delete")
        )

        result = await _rollback(ctx)

        assert restart.called, "a failed restore must not also leave the source down"
        assert not delete.called
        assert result.outcome is Outcome.ROLLBACK_FAILED
        assert result.exit_code == 8
        failed = dict(result.compensations_failed)
        # Names first: the executor keeps 300 characters.
        assert failed[Compensation.RESTORE_SOURCE_FQDN].startswith(
            f"domains NOT restored: shop -> {SHOP} (HTTP 422"
        )
        assert "KEPT" in failed[Compensation.DELETE_TARGET_RESOURCE]
        assert "shop's target appT" in failed[Compensation.DELETE_TARGET_RESOURCE]
        assert "rollback m1" in failed[Compensation.DELETE_TARGET_RESOURCE]
        assert Compensation.RESTART_SOURCE in result.compensations_run

    async def test_rollback_after_finalize_rename_restores_released_domains_before_restart(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # FINALIZE(rename) released the custom host (the last source mutation);
        # without the restore first the source came back with no Host rule.
        _journal_create(ctx)
        ctx.journal.append("step_completed", state="quiesce", detail={})
        ctx.journal.append("step_completed", state="start_target", detail={"started": ["appT"]})
        ctx.journal.append("step_completed", state="finalize", detail={"policy": "rename"})
        ctx.target_uuids["app1"] = "appT"
        calls = Calls()
        _source(respx_mock, calls, SourceRow(calls, fqdn=""))
        respx_mock.post(f"{BASE}/applications/appT/stop").mock(side_effect=calls.mark("stop"))
        respx_mock.post(f"{BASE}/applications/app1/restart").mock(side_effect=calls.mark("restart"))
        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=calls.mark("delete"))

        result = await _rollback(ctx)

        assert calls.order == ["patch:name", "stop", "patch", "restart", "delete"]
        assert result.outcome is Outcome.ROLLED_BACK

    async def test_compose_app_restores_docker_compose_domains_before_restart(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(
            _snapshot(
                kind=ResourceKind.APP_GIT_COMPOSE,
                git_repository="acme/shop",
                git_branch="main",
                git_auth=GitAuth.PUBLIC,
            )
        )
        body = {"docker_compose_domains": [{"name": "web", "domain": SHOP}]}
        _journal_create(ctx, parked=body)
        ctx.journal.append("step_completed", state="quiesce", detail={})
        calls = Calls()
        _source(respx_mock, calls, SourceRow(calls, compose={"web": {"domain": ""}}))
        respx_mock.post(f"{BASE}/applications/app1/restart").mock(side_effect=calls.mark("restart"))
        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=calls.mark("delete"))

        await _rollback(ctx)

        assert calls.order == ["patch", "restart", "delete"]
        assert calls.bodies == [body]  # verbatim — the journalled body is the restore


class TestRestoreSourceDomains:
    async def _restore(self, ctx: MigrationContext, parked: dict[str, Any], **kw: Any) -> None:
        await compensations.undo_restore_source_fqdn(
            ctx, {"parked_domains": parked, "target_uuids": kw.get("targets", {"app1": "appT"})}
        )

    def _released(self, respx_mock: respx.Router, uuid: str = "app1") -> None:
        respx_mock.get(f"{BASE}/applications/{uuid}").mock(
            return_value=httpx.Response(200, json={"uuid": uuid, "fqdn": None})
        )

    async def test_the_journalled_body_is_restored(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        self._released(respx_mock)
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )
        await self._restore(ctx, {"app1": {"domains": SHOP}})
        assert json.loads(route.calls[0].request.read()) == {"domains": SHOP}

    async def test_a_source_holding_its_domains_is_not_written(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={"uuid": "app1", "fqdn": f"{SHOP}/"})
        )
        await self._restore(ctx, {"app1": {"domains": SHOP}})
        assert [c.request.method for c in respx_mock.calls] == ["GET"]

    async def test_nothing_parked_makes_no_call(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        await compensations.undo_restore_source_fqdn(ctx, {"target_uuids": {"app1": "appT"}})
        assert not respx_mock.calls

    async def test_a_conflict_with_our_own_target_is_forced(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        self._released(respx_mock)
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            side_effect=[_conflict("appT"), httpx.Response(200, json={})]
        )
        await self._restore(ctx, {"app1": {"domains": SHOP}})
        assert route.call_count == 2
        assert "force_domain_override" not in json.loads(route.calls[0].request.read())
        assert json.loads(route.calls[1].request.read()) == {
            "domains": SHOP,
            "force_domain_override": True,
        }

    async def test_a_conflict_with_a_third_party_is_not_forced(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        self._released(respx_mock)
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=_conflict("appT", "someone-else")
        )
        with pytest.raises(RuntimeError, match=r"held by shop/appT, shop/someone-else") as exc:
            await self._restore(ctx, {"app1": {"domains": SHOP}})
        assert route.call_count == 1
        assert "not forcing" in str(exc.value)

    async def test_a_conflict_that_names_nobody_is_not_forced(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # One whose holders we cannot read could be anybody's — including the
        # instance's own fqdn.
        self._released(respx_mock)
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(409, json={"message": "Domain conflicts detected."})
        )
        with pytest.raises(RuntimeError, match="a holder Coolify did not name"):
            await self._restore(ctx, {"app1": {"domains": SHOP}})
        assert route.call_count == 1

    async def test_other_errors_raise_and_are_never_forced(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        self._released(respx_mock)
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(422, json={"message": "Unprocessable."})
        )
        with pytest.raises(RuntimeError, match=r"shop -> https://shop\.example\.com \(HTTP 422"):
            await self._restore(ctx, {"app1": {"domains": SHOP}})
        assert route.call_count == 1

    async def test_one_failure_does_not_stop_the_next_resource(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_snapshot(), _snapshot(uuid="app2", name="blog"))
        self._released(respx_mock)
        self._released(respx_mock, "app2")
        respx_mock.patch(f"{BASE}/applications/app1").mock(return_value=httpx.Response(500))
        second = respx_mock.patch(f"{BASE}/applications/app2").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(RuntimeError) as exc:
            await self._restore(
                ctx,
                {"app1": {"domains": SHOP}, "app2": {"domains": "https://blog.example.com"}},
                targets={"app1": "appT", "app2": "app2T"},
            )
        assert second.called
        assert "shop -> https://shop.example.com (HTTP 500)" in str(exc.value)
        assert "blog" not in str(exc.value)

    async def test_an_unknown_resource_is_a_failure_not_a_skip(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        with pytest.raises(RuntimeError, match="nope -> "):
            await self._restore(ctx, {"nope": {"domains": SHOP}})

    async def test_a_service_restores_its_urls_forcing_over_its_own_sub_application(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """A service 409 names the ServiceApplication, not the service.

        The restore path is generic: a body journalled for a service PATCHes
        /services/{uuid}, and its own target's sub-applications count as ours.
        """
        ctx.plan = _plan(
            _snapshot(
                uuid="svc1", name="wiki", collection="services", kind=ResourceKind.SERVICE_TEMPLATE
            )
        )
        respx_mock.get(f"{BASE}/services/svcT").mock(
            return_value=httpx.Response(
                200, json={"uuid": "svcT", "applications": [{"uuid": "svcAppT"}, "junk"]}
            )
        )
        # The source as FINALIZE left it: its sub-application released the host.
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "svc1", "applications": [{"name": "wiki", "fqdn": None}]}
            )
        )
        route = respx_mock.patch(f"{BASE}/services/svc1").mock(
            side_effect=[_conflict("svcAppT"), httpx.Response(200, json={})]
        )
        body = {"urls": [{"name": "wiki", "url": "https://wiki.example.com"}]}
        await self._restore(ctx, {"svc1": body}, targets={"svc1": "svcT"})
        assert json.loads(route.calls[1].request.read()) == {**body, "force_domain_override": True}

    async def test_unreadable_sub_applications_fall_back_to_the_targets(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(
            _snapshot(
                uuid="svc1", name="wiki", collection="services", kind=ResourceKind.SERVICE_TEMPLATE
            )
        )
        respx_mock.get(f"{BASE}/services/svcT").mock(return_value=httpx.Response(404))
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "svc1", "applications": [{"name": "wiki", "fqdn": None}]}
            )
        )
        respx_mock.patch(f"{BASE}/services/svc1").mock(return_value=_conflict("svcAppT"))
        body = {"urls": [{"name": "wiki", "url": "https://wiki.example.com"}]}
        with pytest.raises(RuntimeError, match="not forcing"):
            await self._restore(ctx, {"svc1": body}, targets={"svc1": "svcT"})


class TestDeleteTarget:
    async def test_delete_target_never_writes_source_domains(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={"uuid": "app1", "fqdn": SHOP})
        )
        route = respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_create_target(
            ctx, {"target_uuids": {"app1": "appT"}, "parked_domains": {"app1": {"domains": SHOP}}}
        )
        assert [c.request.method for c in respx_mock.calls] == ["GET", "DELETE"]
        assert route.called

    async def test_the_last_holder_of_a_custom_host_is_kept(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={"uuid": "app1", "fqdn": None})
        )
        route = respx_mock.delete(f"{BASE}/applications/appT")
        with pytest.raises(RuntimeError, match=r"KEPT.*does not hold shop\.example\.com"):
            await compensations.undo_create_target(
                ctx,
                {"target_uuids": {"app1": "appT"}, "parked_domains": {"app1": {"domains": SHOP}}},
            )
        assert not route.called

    async def test_an_unreadable_source_keeps_the_target(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/app1").mock(return_value=httpx.Response(500))
        route = respx_mock.delete(f"{BASE}/applications/appT")
        with pytest.raises(RuntimeError, match="its source could not be read: HTTP 500"):
            await compensations.undo_create_target(
                ctx,
                {"target_uuids": {"app1": "appT"}, "parked_domains": {"app1": {"domains": SHOP}}},
            )
        assert not route.called

    async def test_only_server_bound_hosts_need_no_check(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan().model_copy(
            update={
                "source_server": ServerRef(
                    uuid="s1", name="old", ip="10.0.0.1", wildcard_domain="app.src.example"
                )
            }
        )
        respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_create_target(
            ctx,
            {
                "target_uuids": {"app1": "appT"},
                "parked_domains": {"app1": {"domains": "https://x.app.src.example"}},
            },
        )
        assert [c.request.method for c in respx_mock.calls] == ["DELETE"]


def _mock_create_routes(respx_mock: respx.Router, *, source: dict[str, Any] | None = None) -> None:
    """Everything CREATE needs for one dockerimage app on a custom domain."""
    # No locked variables to read back (env_recovery runs before any write).
    respx_mock.get(f"{BASE}/applications/app1/envs").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(f"{BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
    )
    respx_mock.get(f"{BASE}/projects/p1").mock(
        return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
    )
    respx_mock.get(f"{BASE}/servers/s2/destinations").mock(
        return_value=httpx.Response(200, json=[{"uuid": "d1", "network": "coolify"}])
    )
    respx_mock.get(f"{BASE}/applications/app1").mock(
        return_value=httpx.Response(
            200,
            json=source
            or {
                "uuid": "app1",
                "name": "shop",
                "build_pack": "dockerimage",
                "docker_registry_image_name": "nginx",
                "docker_registry_image_tag": "1.27-alpine",
                "ports_exposes": "80",
                "fqdn": SHOP,
            },
        )
    )


def _mock_rest_of_create(respx_mock: respx.Router) -> None:
    respx_mock.get(f"{BASE}/applications/app1/storages").mock(
        return_value=httpx.Response(200, json={"persistent_storages": [], "file_storages": []})
    )
    respx_mock.get(f"{BASE}/applications/app1/tags").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(f"{BASE}/applications/app1/scheduled-tasks").mock(
        return_value=httpx.Response(200, json=[])
    )


class TestCreateTargetClaims:
    async def test_the_target_claims_after_it_is_built_and_the_source_is_never_written(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """Make-before-break: create without the host, claim it with scoped force."""
        _mock_create_routes(respx_mock)
        _mock_rest_of_create(respx_mock)
        source_writes = respx_mock.patch(f"{BASE}/applications/app1")
        create = respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(201, json={"uuid": "appT"})
        )
        claim = respx_mock.patch(f"{BASE}/applications/appT").mock(
            side_effect=[_conflict("app1"), httpx.Response(200, json={})]
        )
        respx_mock.get(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={"uuid": "appT", "fqdn": SHOP})
        )

        result = await steps.step_create_target(ctx)

        assert result == {"target_uuids": {"app1": "appT"}}
        assert not source_writes.called, "the source keeps its host until FINALIZE"
        created = json.loads(create.calls[0].request.read())
        assert "domains" not in created and created["autogenerate_domain"] is False
        assert [json.loads(c.request.read()) for c in claim.calls] == [
            {"domains": SHOP},
            {"domains": SHOP, "force_domain_override": True},
        ]
        assert ctx.journal.undo_info("create_target")["parked_domains"] == {
            "app1": {"domains": SHOP}
        }

    async def test_a_foreign_holder_refuses_the_claim_and_the_target_goes(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create_routes(respx_mock)
        _mock_rest_of_create(respx_mock)
        respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(201, json={"uuid": "appT"})
        )
        claim = respx_mock.patch(f"{BASE}/applications/appT").mock(
            return_value=_conflict("app1", "orphan9")
        )
        delete = respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(CoolifyApiError) as exc:
            await steps.step_create_target(ctx)

        assert exc.value.status_code == 409
        assert "application 'shop' (uuid orphan9)" in str(exc.value)
        assert "uuid app1" not in str(exc.value), "our own source is not 'held elsewhere'"
        assert claim.call_count == 1, "never forced over a holder that is not ours"
        assert delete.called

    async def test_a_target_that_does_not_hold_its_hosts_is_refused(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.errors import UnsupportedResource

        _mock_create_routes(respx_mock)
        _mock_rest_of_create(respx_mock)
        respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(201, json={"uuid": "appT"})
        )
        respx_mock.patch(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )
        # Coolify ignores `domains` when the target server's proxy should not run.
        respx_mock.get(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={"uuid": "appT", "fqdn": None})
        )
        delete = respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(UnsupportedResource, match="does not hold"):
            await steps.step_create_target(ctx)
        assert delete.called

    async def test_a_failed_create_never_writes_or_restarts_the_source(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create_routes(respx_mock)
        source_writes = respx_mock.patch(f"{BASE}/applications/app1")
        respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(422, json={"message": "Validation failed."})
        )
        restart = respx_mock.post(f"{BASE}/applications/app1/restart").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(CoolifyApiError) as exc:
            await steps.step_create_target(ctx)

        assert exc.value.status_code == 422  # the original speaks
        assert not source_writes.called
        assert not restart.called, "CREATE runs before QUIESCE: the source was never stopped"

    async def test_the_restore_body_is_journalled_before_the_create(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create_routes(respx_mock)
        respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            side_effect=httpx.ReadTimeout("slow")
        )

        with pytest.raises(CoolifyApiError, match="timed out"):
            await steps.step_create_target(ctx)

        assert ctx.journal.undo_info("create_target")["parked_domains"]["app1"] == {"domains": SHOP}

    async def test_noindex_flags_are_journalled_with_the_restore(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.journal.append("step_completed", state="preflight", detail={"coolify_version": "4.5.0"})
        _mock_create_routes(
            respx_mock,
            source={
                "uuid": "app1",
                "name": "shop",
                "build_pack": "dockerimage",
                "docker_registry_image_name": "nginx",
                "fqdn": SHOP,
                "noindex_domains": [SHOP],
            },
        )
        respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(422, json={})
        )
        with pytest.raises(CoolifyApiError):
            await steps.step_create_target(ctx)
        assert ctx.journal.undo_info("create_target")["parked_domains"]["app1"] == {
            "domains": SHOP,
            "noindex_domains": [SHOP],
        }


class TestFinalizeReleasesLast:
    """The source lets go of its custom hosts as FINALIZE's LAST source mutation."""

    def _finalize_routes(self, ctx: MigrationContext, respx_mock: respx.Router) -> list[str]:
        order: list[str] = []
        target: Any = ctx.target_host

        def patch(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.read())
            if "name" in body:
                order.append("rename")
            else:
                revoked = any("authorized_keys" in c for c in target.commands)
                journalled = ctx.journal.undo_info("finalize").get("releasing_domains")
                order.append(f"release revoked={revoked} journalled={journalled}")
            return httpx.Response(200, json={})

        respx_mock.get(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={"uuid": "app1", "fqdn": SHOP})
        )
        ctx.target_uuids["app1"] = "appT"
        respx_mock.get(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={"uuid": "appT", "fqdn": SHOP})
        )
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=patch)
        return order

    async def test_rename_then_revoke_then_release(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = ctx.plan.model_copy(update={"finalize_policy": FinalizePolicy.RENAME})
        order = self._finalize_routes(ctx, respx_mock)

        result = await steps.step_finalize(ctx)

        assert order == ["rename", "release revoked=True journalled=['app1']"]
        assert result["domains_released"] == ["app1"]

    async def test_keep_releases_too(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A kept source still holding the host would be a second router and ACME
        # requester the moment anyone restarts it.
        ctx.plan = ctx.plan.model_copy(update={"finalize_policy": FinalizePolicy.KEEP})
        order = self._finalize_routes(ctx, respx_mock)

        await steps.step_finalize(ctx)

        assert order == ["release revoked=True journalled=['app1']"]

    async def test_a_failed_release_is_a_notice_not_a_failed_finalize(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = ctx.plan.model_copy(update={"finalize_policy": FinalizePolicy.KEEP})
        respx_mock.get(f"{BASE}/applications/app1").mock(return_value=httpx.Response(500))

        result = await steps.step_finalize(ctx)

        assert result["domains_released"] == []
        assert "still holds its custom domains" in ctx.notices[0]

    async def test_a_host_the_target_lost_since_create_is_not_released(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A compose service renamed upstream while the run waited at the DNS
        # gate: the deploy dropped its domain from the target. Releasing it on
        # the source too would leave it held by nothing - and from 4.4 Coolify
        # then deletes the DNS record it manages.
        ctx.plan = ctx.plan.model_copy(update={"finalize_policy": FinalizePolicy.RENAME})
        order = self._finalize_routes(ctx, respx_mock)
        respx_mock.get(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={"uuid": "appT", "fqdn": None})
        )

        result = await steps.step_finalize(ctx)

        assert order == ["rename"]
        assert result["domains_released"] == []
        assert "no longer holds shop.example.com" in ctx.notices[0]

    def test_a_compose_entry_the_target_dropped_is_named(self) -> None:
        from bg_coolify_migrate.domain import domains

        snapshot = ResourceSnapshot(
            uuid="app1",
            name="shop",
            collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_repository="acme/shop",
            git_branch="main",
            git_auth=GitAuth.PUBLIC,
        )
        api = "https://api.example.com"
        source = {
            "docker_compose_domains": json.dumps(
                {"web": {"domain": SHOP}, "api": {"domain": api}}
            )
        }
        target = {"docker_compose_domains": json.dumps({"api": {"domain": api}})}
        assert domains.custom_hosts_not_held(
            snapshot, source, target, source_wildcard=None
        ) == ["shop.example.com"]

    async def test_a_target_that_cannot_be_read_releases_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = ctx.plan.model_copy(update={"finalize_policy": FinalizePolicy.KEEP})
        order = self._finalize_routes(ctx, respx_mock)
        respx_mock.get(f"{BASE}/applications/appT").mock(return_value=httpx.Response(500))

        result = await steps.step_finalize(ctx)

        assert order == [] and result["domains_released"] == []
        assert "could not be read" in ctx.notices[0]

    async def test_delete_releases_by_deleting(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = ctx.plan.model_copy(update={"finalize_policy": FinalizePolicy.DELETE})
        respx_mock.delete(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )

        result = await steps.step_finalize(ctx)

        assert result["domains_released"] == []
        assert [c.request.method for c in respx_mock.calls] == ["DELETE"]
