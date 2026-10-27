"""The rollback-domains fix against the API: what is called, and in which order.

The pure plan order (test_rollback_domains.py) cannot tell whether the restore
actually PATCHes anything — before the fix the plan carried a RESTORE that was a
no-op. These tests run the REAL compensations against respx and record the call
order, which is the lock that would have caught the bug: the source's domains
must be PATCHed back before ``/restart`` queues the deploy that bakes the stored
labels into its container.
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
from bg_coolify_migrate.domain.statemachine import Compensation, Outcome
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
    async def test_domains_are_restored_before_the_source_restarts(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """The regression lock: patch -> patch+force -> restart -> delete.

        The target still exists while the source takes its domain back (it is
        deleted last), so the restore 409s on it and the scoped force applies.
        """
        _journal_create(ctx)
        ctx.journal.append("step_completed", state="quiesce", detail={})
        calls = Calls()
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            side_effect=calls.patch(_conflict("appT"), httpx.Response(200, json={}))
        )
        respx_mock.post(f"{BASE}/applications/app1/restart").mock(side_effect=calls.mark("restart"))
        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=calls.mark("delete"))

        result = await _rollback(ctx)

        assert calls.order == ["patch", "patch+force", "restart", "delete"]
        assert calls.bodies[1] == {"domains": SHOP, "force_domain_override": True}
        assert result.outcome is Outcome.ROLLED_BACK

    async def test_restart_runs_even_when_the_domain_restore_fails(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _journal_create(ctx)
        ctx.journal.append("step_completed", state="quiesce", detail={})
        calls = Calls()
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            side_effect=calls.patch(httpx.Response(422, json={"message": "Validation failed."}))
        )
        restart = respx_mock.post(f"{BASE}/applications/app1/restart").mock(
            side_effect=calls.mark("restart")
        )
        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=calls.mark("delete"))

        result = await _rollback(ctx)

        assert restart.called, "a failed restore must not also leave the source down"
        assert result.outcome is Outcome.ROLLBACK_FAILED
        assert result.exit_code == 8
        comp, message = result.compensations_failed[0]
        assert comp is Compensation.RESTORE_SOURCE_FQDN
        # Names first: the executor keeps 300 characters.
        assert message.startswith(f"domains NOT restored: shop -> {SHOP} (HTTP 422")
        assert Compensation.RESTART_SOURCE in result.compensations_run
        assert Compensation.DELETE_TARGET_RESOURCE in result.compensations_run

    async def test_rollback_after_finalize_rename_restores_blanked_domains_before_restart(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # FINALIZE(rename) blanked the domains (release_fqdn), so before the fix
        # the source came back with no Host rule at all.
        _journal_create(ctx)
        ctx.journal.append("step_completed", state="quiesce", detail={})
        ctx.journal.append("step_completed", state="start_target", detail={"started": ["appT"]})
        ctx.journal.append("step_completed", state="finalize", detail={"policy": "rename"})
        ctx.target_uuids["app1"] = "appT"
        calls = Calls()
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            side_effect=calls.patch(httpx.Response(200, json={}))
        )
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
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            side_effect=calls.patch(httpx.Response(200, json={}))
        )
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

    async def test_the_parked_body_is_restored(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )
        await self._restore(ctx, {"app1": {"domains": SHOP}})
        assert json.loads(route.calls[0].request.read()) == {"domains": SHOP}

    async def test_nothing_parked_makes_no_call(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        await compensations.undo_restore_source_fqdn(ctx, {"target_uuids": {"app1": "appT"}})
        assert not respx_mock.calls

    async def test_a_conflict_with_our_own_target_is_forced(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
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
        # The old code forced any 409. One whose holders we cannot read could be
        # anybody's — including the instance's own fqdn.
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(409, json={"message": "Domain conflicts detected."})
        )
        with pytest.raises(RuntimeError, match="a holder Coolify did not name"):
            await self._restore(ctx, {"app1": {"domains": SHOP}})
        assert route.call_count == 1

    async def test_other_errors_raise_and_are_never_forced(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Flipped: this used to be swallowed, so the rollback reported success
        # while the source kept its parked domains.
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

        The restore path is generic: a body journalled for a service (by the
        service-domains work) PATCHes /services/{uuid}, and its own target's
        sub-applications count as ours.
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
    async def test_delete_target_never_touches_source_domains(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_create_target(
            ctx, {"target_uuids": {"app1": "appT"}, "parked_domains": {"app1": {"domains": SHOP}}}
        )
        assert [c.request.method for c in respx_mock.calls] == ["DELETE"]
        assert route.called


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


class TestCreateTargetParking:
    async def test_a_failed_create_unparks_the_source_and_never_restarts_it(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create_routes(respx_mock)
        patch = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )
        respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(422, json={"message": "Validation failed."})
        )
        restart = respx_mock.post(f"{BASE}/applications/app1/restart").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(CoolifyApiError) as exc:
            await steps.step_create_target(ctx)

        assert exc.value.status_code == 422  # the original speaks
        bodies = [json.loads(c.request.read()) for c in patch.calls]
        assert len(bodies) == 2
        parked = bodies[0]["domains"]
        assert parked.startswith("https://old-") and parked.endswith(".shop.example.com")
        assert bodies[1] == {"domains": SHOP}
        assert not restart.called, "CREATE runs before QUIESCE: the source was never stopped"

    async def test_the_restore_body_is_journalled_before_the_park_patch(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Every retry of the park PATCH timed out: Coolify may or may not have
        # applied it, and only the journal can say how to undo it.
        _mock_create_routes(respx_mock)
        patch = respx_mock.patch(f"{BASE}/applications/app1").mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={})]
        )

        with pytest.raises(CoolifyApiError, match="timed out"):
            await steps.step_create_target(ctx)

        assert ctx.journal.undo_info("create_target")["parked_domains"]["app1"] == {"domains": SHOP}
        assert json.loads(patch.calls[1].request.read()) == {"domains": SHOP}

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
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
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
