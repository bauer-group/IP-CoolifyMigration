"""The deploy switches against the API: what is sent, journalled and in which order.

The pure decisions are tests/test_deploy_triggers.py's. These run the real IO
shells (engine/deploy_triggers.py, api/resources.py, the client) against respx:

* a target is CREATED with auto-deploy off, and armed (plus its tags) only at
  FINALIZE, never fatally;
* QUIESCE disarms the source before the stop, journalling the originals first,
  and refuses before the stop while a source deployment is queued or running;
* the rollback gives the switches back, between the domain restore and the
  restart, and changes nothing when replayed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.resources import Placement
from bg_coolify_migrate.domain import deploy_triggers as dt
from bg_coolify_migrate.domain.kinds import DatabaseEngine, GitAuth, ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import (
    STOP_REQUESTED,
    Compensation,
    FinalizePolicy,
    Outcome,
)
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine import deploy_triggers as engine
from bg_coolify_migrate.engine.compensations import build_compensations
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import QuiesceError, SourceDeploymentPending
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
DEPLOY_URL = "/project/p1/environment/e1/application/{app}/deployment/{dep}"


def _app(uuid: str = "app1", name: str = "shop", **kw: Any) -> ResourceSnapshot:
    base: dict[str, Any] = {
        "uuid": uuid,
        "name": name,
        "collection": "applications",
        "kind": ResourceKind.APP_DOCKERIMAGE,
    }
    return ResourceSnapshot(**{**base, **kw})


def _db() -> ResourceSnapshot:
    return ResourceSnapshot(
        uuid="db1",
        name="postgres",
        collection="databases",
        kind=ResourceKind.DATABASE,
        engine=DatabaseEngine.POSTGRESQL,
        image="postgres:16",
    )


def _plan(
    *snapshots: ResourceSnapshot, policy: FinalizePolicy = FinalizePolicy.RENAME
) -> MigrationPlan:
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=tuple(
            ResourcePlan(snapshot=s, strategy=Strategy.COPY_DATA) for s in (snapshots or (_app(),))
        ),
        finalize_policy=policy,
        coolify_version="4.7.2",
    )


def _host() -> FakeHost:
    """No volume pins, and the key revoke a target delete runs first."""
    return (
        FakeHost()
        .on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        .on(r"authorized_keys\.bak", exit_status=0)
        .on(r"rm -rf /root/\.coolify-migrate/", exit_status=0)
        # The containment checkpoints (engine/containment.py) find nothing started.
        .quiet_docker()
    )


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path, deploy_timeout=10.0),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_host(),  # type: ignore[arg-type]
        target_host=_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "SETTLE_INTERVAL", 5.0)

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(engine.asyncio, "sleep", instant)


class App:
    """One application row: GET shows its settings, a PATCH changes them."""

    def __init__(self, uuid: str, *, auto: bool | None = True, previews: bool = False) -> None:
        self.uuid = uuid
        self.settings: dict[str, Any] | None = (
            None
            if auto is None
            else {"is_auto_deploy_enabled": auto, "is_preview_deployments_enabled": previews}
        )
        self.patches: list[dict[str, Any]] = []

    def get(self, _request: httpx.Request) -> httpx.Response:
        row: dict[str, Any] = {"uuid": self.uuid, "name": "shop", "fqdn": ""}
        if self.settings is not None:
            row["settings"] = dict(self.settings)
        return httpx.Response(200, json=row)

    def patch(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        self.patches.append(body)
        if self.settings is not None:
            self.settings.update({k: v for k, v in body.items() if k in self.settings})
        return httpx.Response(200, json={"uuid": self.uuid})

    def mount(self, respx_mock: respx.Router) -> App:
        respx_mock.get(f"{BASE}/applications/{self.uuid}").mock(side_effect=self.get)
        respx_mock.patch(f"{BASE}/applications/{self.uuid}").mock(side_effect=self.patch)
        return self


def _queue(*rows: tuple[str, str, str]) -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            {
                "deployment_uuid": dep,
                "status": status,
                "deployment_url": DEPLOY_URL.format(app=app, dep=dep),
                "logs": "SECRET=hunter2",
            }
            for dep, app, status in rows
        ],
    )


# ── R1: the target is created disarmed ───────────────────────────────────────


class TestCreateDisarmed:
    @pytest.mark.parametrize("version", [(4, 1, 2), (4, 7, 2), None])
    async def test_every_target_is_created_with_auto_deploy_off(
        self, respx_mock: respx.Router, version: tuple[int, int, int] | None
    ) -> None:
        create = respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(201, json={"uuid": "appT"})
        )
        source = {
            "uuid": "app1",
            "name": "shop",
            "build_pack": "dockerimage",
            "docker_registry_image_name": "nginx",
            # Whatever the source has: a push must not start the target early.
            "settings": {"is_auto_deploy_enabled": True},
        }
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            await api_resources.create_application(
                api,
                _app(),
                Placement(project_uuid="p1", environment_name="production", server_uuid="s2"),
                source,
                version=version,
            )
        body = json.loads(create.calls[0].request.read())
        assert body["is_auto_deploy_enabled"] is False
        assert "is_preview_deployments_enabled" not in body

    async def test_a_git_target_too(self, respx_mock: respx.Router) -> None:
        create = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "appT"})
        )
        snapshot = _app(
            kind=ResourceKind.APP_GIT_BUILD,
            git_repository="acme/shop",
            git_branch="main",
            git_auth=GitAuth.PUBLIC,
        )
        source = {
            "uuid": "app1",
            "git_repository": "acme/shop",
            "git_branch": "main",
            "build_pack": "nixpacks",
        }
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            await api_resources.create_application(
                api,
                snapshot,
                Placement(project_uuid="p1", environment_name="production", server_uuid="s2"),
                source,
                version=(4, 5, 0),
            )
        assert json.loads(create.calls[0].request.read())["is_auto_deploy_enabled"] is False


class TestSettingsPatch:
    async def test_previews_are_never_sent_before_42(self, respx_mock: respx.Router) -> None:
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            sent = await api_resources.update_application_settings(
                api,
                "app1",
                {"is_auto_deploy_enabled": False, "is_preview_deployments_enabled": False},
                version=(4, 1, 2),
            )
        assert sent == {"is_auto_deploy_enabled": False}
        assert json.loads(route.calls[0].request.read()) == {"is_auto_deploy_enabled": False}

    async def test_only_the_registry_rows_are_sent(self, respx_mock: respx.Router) -> None:
        route = respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            sent = await api_resources.update_application_settings(
                api, "app1", {"is_auto_deploy_enabled": True, "name": "x"}, version=(4, 1, 2)
            )
        assert sent == {"is_auto_deploy_enabled": True}
        assert json.loads(route.calls[0].request.read()) == {"is_auto_deploy_enabled": True}

    async def test_nothing_to_send_sends_nothing(self, respx_mock: respx.Router) -> None:
        # assert_all_mocked: any request would fail this test.
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            assert (
                await api_resources.update_application_settings(api, "app1", {}, version=(4, 7, 2))
                == {}
            )


class TestClientQueue:
    async def test_the_logs_never_leave_the_client(self, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/deployments").mock(
            return_value=_queue(("d1", "app1", "in_progress"))
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            rows = await api.list_active_deployments()
        assert rows == (
            dt.ActiveDeployment(uuid="d1", status="in_progress", application_uuid="app1"),
        )
        assert "hunter2" not in repr(rows)


# ── R7: QUIESCE disarms the source before the stop ───────────────────────────


class TestDisarm:
    async def test_the_originals_are_journalled_before_the_patch(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        app = App("app1", auto=True, previews=True)
        seen: list[Any] = []

        def patch(request: httpx.Request) -> httpx.Response:
            seen.append(ctx.journal.undo_info("quiesce").get(dt.JOURNAL_KEY))
            return app.patch(request)

        respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=app.get)
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=patch)

        sent = await engine.disarm_sources(ctx)

        assert seen == [{"app1": {"auto_deploy": True, "previews": True}}]
        assert app.patches == [
            {"is_auto_deploy_enabled": False, "is_preview_deployments_enabled": False}
        ]
        assert sent == {"app1": app.patches[0]}

    async def test_a_source_already_off_is_journalled_and_left_alone(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        app = App("app1", auto=False).mount(respx_mock)
        assert await engine.disarm_sources(ctx) == {}
        assert app.patches == []
        assert engine.recorded(ctx) == {"app1": dt.Triggers(False, False)}

    async def test_a_resumed_quiesce_keeps_the_first_originals(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The crash came after the PATCH: read now, the source shows what THIS
        # run wrote. The journal's original must survive, or the rollback would
        # "restore" auto-deploy off.
        ctx.journal.append(
            "step_started",
            state="quiesce",
            detail={dt.JOURNAL_KEY: {"app1": {"auto_deploy": True, "previews": False}}},
        )
        app = App("app1", auto=False).mount(respx_mock)

        await engine.disarm_sources(ctx)

        assert app.patches == []
        assert engine.recorded(ctx) == {"app1": dt.Triggers(True, False)}

    async def test_a_41_source_is_switched_off_without_previews(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = ctx.plan.model_copy(update={"coolify_version": "4.1.2"})
        app = App("app1", auto=None).mount(respx_mock)

        await engine.disarm_sources(ctx)

        assert app.patches == [{"is_auto_deploy_enabled": False}]
        assert engine.recorded(ctx) == {"app1": dt.Triggers(None, None)}

    async def test_databases_and_services_are_not_touched(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db())
        assert await engine.disarm_sources(ctx) == {}
        assert not respx_mock.calls

    async def test_a_failed_write_refuses_before_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=App("app1").get)
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(
                403, json={"message": "Missing required permissions: write"}
            )
        )
        with pytest.raises(
            QuiesceError, match="could not switch off the auto-deploy of shop"
        ) as err:
            await engine.disarm_sources(ctx)
        assert "Nothing was stopped" in str(err.value.hint)
        # Journalled first: the rollback still owes the restore.
        assert "app1" in engine.recorded(ctx)


class TestAwaitDeployments:
    async def test_an_empty_queue_passes_at_once(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get(f"{BASE}/deployments").mock(return_value=_queue())
        await engine.await_source_deployments(ctx)
        assert route.call_count == 1

    async def test_waits_until_the_sources_deployment_ended(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get(f"{BASE}/deployments").mock(
            side_effect=[
                _queue(("d1", "app1", "in_progress"), ("d9", "other", "queued")),
                _queue(("d9", "other", "queued")),
            ]
        )
        await engine.await_source_deployments(ctx)
        assert route.call_count == 2

    async def test_refuses_naming_the_deployment_that_did_not_end(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/deployments").mock(return_value=_queue(("d1", "app1", "queued")))
        with pytest.raises(SourceDeploymentPending) as err:
            await engine.await_source_deployments(ctx)
        assert "shop: deployment d1 is still queued after 10s" in str(err.value)
        assert "never cancels one" in str(err.value.hint)
        assert "hunter2" not in str(err.value)
        assert err.value.exit_code == 5

    async def test_an_unreadable_queue_refuses_at_the_deadline(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/deployments").mock(return_value=httpx.Response(500))
        with pytest.raises(SourceDeploymentPending, match="could not be read"):
            await engine.await_source_deployments(ctx)

    async def test_a_plan_without_applications_reads_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db())
        await engine.await_source_deployments(ctx)
        assert not respx_mock.calls


class TestStepQuiesce:
    """The insertion point: FIRST - before the preview delete, the mount capture,
    the pins, the stop marker and the stop. A deployment that ends replaces the
    containers, so everything that names them must read them after the wait."""

    @pytest.fixture
    def stubbed(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch, respx_mock: respx.Router
    ) -> list[str]:
        order: list[str] = []
        # The deployment record QUIESCE journals with the stop clock
        # (engine/containment.deployments_before_stop): nothing deployed yet.
        respx_mock.get(f"{BASE}/deployments/applications/app1").mock(
            return_value=httpx.Response(200, json={"count": 0, "deployments": []})
        )

        async def capture(_ctx: Any) -> dict[str, list[str]]:
            order.append("capture")
            return {}

        async def no_pins(_ctx: Any) -> list[str]:
            order.append("pin")
            return []

        async def now_on(_host: Any) -> str:
            return "2026-11-10T00:00:00"

        async def request_stop(_ctx: Any) -> None:
            marked = ctx.journal.undo_info("quiesce").get(STOP_REQUESTED) is True
            order.append(f"stop marked={marked}")

        async def stopped(*_a: Any, **_kw: Any) -> Any:
            return SimpleNamespace(elapsed=0.0)

        async def killed(*_a: Any, **_kw: Any) -> list[tuple[str, int]]:
            return []

        monkeypatch.setattr(steps, "_capture_mounts", capture)
        monkeypatch.setattr(steps, "_pin_source", no_pins)
        monkeypatch.setattr(steps.quiesce, "now_on", now_on)
        monkeypatch.setattr(steps, "_request_stop", request_stop)
        monkeypatch.setattr(steps.quiesce, "wait_until_stopped", stopped)
        monkeypatch.setattr(steps.quiesce, "killed_since", killed)
        return order

    async def test_disarm_and_wait_come_before_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router, stubbed: list[str]
    ) -> None:
        app = App("app1")

        def patch(request: httpx.Request) -> httpx.Response:
            stubbed.append("disarm")
            return app.patch(request)

        def queue(_request: httpx.Request) -> httpx.Response:
            stubbed.append("queue")
            return _queue()

        respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=app.get)
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=patch)
        respx_mock.get(f"{BASE}/deployments").mock(side_effect=queue)

        await steps.step_quiesce(ctx)

        assert stubbed == ["disarm", "queue", "capture", "pin", "stop marked=True"]

    async def test_the_mounts_are_captured_after_the_deployment_ended(
        self, ctx: MigrationContext, respx_mock: respx.Router, stubbed: list[str]
    ) -> None:
        # The rolling update a running deployment finishes starts a container
        # under a NEW name and removes the old one. Captured before the wait,
        # the SIGKILL guard would match the killed new container against the
        # old name and let a torn volume through.
        App("app1").mount(respx_mock)
        answers = iter([_queue(("d1", "app1", "in_progress")), _queue()])

        def queue(_request: httpx.Request) -> httpx.Response:
            stubbed.append("queue")
            return next(answers)

        respx_mock.get(f"{BASE}/deployments").mock(side_effect=queue)

        await steps.step_quiesce(ctx)

        assert stubbed == ["queue", "queue", "capture", "pin", "stop marked=True"]

    async def test_a_pending_deployment_refuses_before_anything_is_stopped(
        self, ctx: MigrationContext, respx_mock: respx.Router, stubbed: list[str]
    ) -> None:
        App("app1").mount(respx_mock)
        respx_mock.get(f"{BASE}/deployments").mock(
            return_value=_queue(("d1", "app1", "in_progress"))
        )
        with pytest.raises(SourceDeploymentPending) as err:
            await steps.step_quiesce(ctx)
        # Not even captured or pinned: so no pin note follows the "nothing was
        # stopped" hint to promise a restart that is not owed.
        assert stubbed == []
        assert STOP_REQUESTED not in ctx.journal.undo_info("quiesce")
        assert "pinned by" not in str(err.value.hint)

    async def test_previews_are_deleted_only_after_the_disarm_and_the_wait(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        stubbed: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Disarmed first, a pull request can no longer bring a deleted preview back.
        ctx.plan = _plan(_app(has_previews=True))
        ctx.delete_previews = True
        app = App("app1", previews=True)

        def patch(request: httpx.Request) -> httpx.Response:
            stubbed.append("disarm")
            return app.patch(request)

        def queue(_request: httpx.Request) -> httpx.Response:
            stubbed.append("queue")
            return _queue()

        def delete(_request: httpx.Request) -> httpx.Response:
            stubbed.append("delete preview")
            return httpx.Response(200, json={})

        async def containers(*_a: Any, **_kw: Any) -> list[Any]:
            return [
                steps.docker.Container(
                    id="c1",
                    name="app1-pr-7",
                    state="running",
                    labels={steps.docker.LABEL_PR_ID: "7"},
                )
            ]

        respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=app.get)
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=patch)
        respx_mock.get(f"{BASE}/deployments").mock(side_effect=queue)
        respx_mock.delete(f"{BASE}/applications/app1/previews/7").mock(side_effect=delete)
        monkeypatch.setattr(steps, "resource_containers", containers)

        await steps.step_quiesce(ctx)

        assert stubbed == [
            "disarm",
            "queue",
            "delete preview",
            "capture",
            "pin",
            "stop marked=True",
        ]
        assert app.patches == [
            {"is_auto_deploy_enabled": False, "is_preview_deployments_enabled": False}
        ]


# ── R1/R2/R8: FINALIZE arms the target, a kept source stays disarmed ─────────


def _journal_originals(ctx: MigrationContext, **originals: dt.Triggers) -> None:
    ctx.journal.append(
        "step_started", state="quiesce", detail={dt.JOURNAL_KEY: dt.serialise(originals)}
    )


def _no_tags(respx_mock: respx.Router, collection: str, uuid: str) -> None:
    respx_mock.get(f"{BASE}/{collection}/{uuid}/tags").mock(
        return_value=httpx.Response(200, json=[])
    )


class TestArmTargets:
    async def test_the_target_gets_the_sources_original_and_its_tags(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=dt.Triggers(True, True))
        target = App("appT", auto=False).mount(respx_mock)
        respx_mock.get(f"{BASE}/applications/app1/tags").mock(
            return_value=httpx.Response(200, json=[{"name": "prod"}])
        )
        tags = respx_mock.post(f"{BASE}/applications/appT/tags").mock(
            return_value=httpx.Response(201, json=[])
        )

        assert await engine.arm_targets(ctx) == ["appT"]

        # Previews stay Coolify's default: no create ever carried them.
        assert target.patches == [{"is_auto_deploy_enabled": True}]
        assert json.loads(tags.calls[0].request.read()) == {"tag_names": ["prod"]}

    async def test_a_source_with_auto_deploy_off_gives_a_target_with_it_off(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=dt.Triggers(False, False))
        target = App("appT", auto=True).mount(respx_mock)
        _no_tags(respx_mock, "applications", "app1")

        await engine.arm_targets(ctx)

        assert target.patches == [{"is_auto_deploy_enabled": False}]

    async def test_without_a_quiesce_record_the_source_is_read(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A journal from before sources were disarmed: this run never switched it.
        ctx.target_uuids["app1"] = "appT"
        App("app1", auto=False).mount(respx_mock)
        target = App("appT").mount(respx_mock)
        _no_tags(respx_mock, "applications", "app1")

        await engine.arm_targets(ctx)

        assert target.patches == [{"is_auto_deploy_enabled": False}]

    async def test_an_unreadable_41_source_arms_coolifys_default(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=dt.Triggers(None, None))
        target = App("appT", auto=None).mount(respx_mock)
        _no_tags(respx_mock, "applications", "app1")

        await engine.arm_targets(ctx)

        assert target.patches == [{"is_auto_deploy_enabled": True}]

    async def test_a_source_that_cannot_be_read_arms_coolifys_default(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # No QUIESCE record and the source will not answer: the cutover is done,
        # so FINALIZE arms what every target got before, never raises.
        ctx.target_uuids["app1"] = "appT"
        respx_mock.get(f"{BASE}/applications/app1").mock(return_value=httpx.Response(500))
        target = App("appT").mount(respx_mock)
        _no_tags(respx_mock, "applications", "app1")

        assert await engine.arm_targets(ctx) == ["appT"]

        assert target.patches == [{"is_auto_deploy_enabled": True}]

    async def test_a_failure_is_a_notice_never_a_failed_finalize(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=dt.Triggers(True, False))
        respx_mock.patch(f"{BASE}/applications/appT").mock(return_value=httpx.Response(500))
        # A tag route that does not exist (4.1.x) costs the tags and nothing else.
        respx_mock.get(f"{BASE}/applications/app1/tags").mock(
            return_value=httpx.Response(404, json={"message": "Not found."})
        )

        assert await engine.arm_targets(ctx) == []

        [notice] = ctx.notices
        assert notice.startswith("the target of shop (appT) was created with auto-deploy off")
        assert "switch 'Auto Deploy' on" in notice

    async def test_a_database_gets_its_tags_and_no_settings(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db())
        ctx.target_uuids["db1"] = "db2"
        respx_mock.get(f"{BASE}/databases/db1/tags").mock(
            return_value=httpx.Response(200, json=[{"name": "prod"}])
        )
        tags = respx_mock.post(f"{BASE}/databases/db2/tags").mock(
            return_value=httpx.Response(201, json=[])
        )
        assert await engine.arm_targets(ctx) == []
        assert tags.called

    async def test_the_tags_follow_after_the_cutover(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # POST /deploy?tag ignores auto-deploy: tags copied at CREATE let a tag
        # CI run start the target before the DNS gate.
        ctx.target_uuids["app1"] = "appT"
        App("app1").mount(respx_mock)
        App("appT", auto=False).mount(respx_mock)
        respx_mock.get(f"{BASE}/applications/app1/tags").mock(
            return_value=httpx.Response(200, json=[{"name": "prod"}, {"name": "x"}])
        )
        tags = respx_mock.post(f"{BASE}/applications/appT/tags").mock(
            return_value=httpx.Response(201, json=[])
        )

        await engine.arm_targets(ctx)

        # A one-character name would 422 the whole call: dropped (copy_tags).
        assert json.loads(tags.calls[0].request.read()) == {"tag_names": ["prod"]}

    async def test_a_missing_tag_route_costs_the_tags_and_nothing_else(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The 2.5.6 lesson: on 4.1.x the route does not exist.
        ctx.target_uuids["app1"] = "appT"
        App("app1").mount(respx_mock)
        target = App("appT", auto=False).mount(respx_mock)
        respx_mock.get(f"{BASE}/applications/app1/tags").mock(
            return_value=httpx.Response(404, json={"message": "Not found."})
        )

        assert await engine.arm_targets(ctx) == ["appT"]
        assert target.patches == [{"is_auto_deploy_enabled": True}] and ctx.notices == []

    async def test_a_tag_copy_that_breaks_never_fails_finalize(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.plan = _plan(_db())
        ctx.target_uuids["db1"] = "db2"

        async def broken(*_a: Any, **_kw: Any) -> int:
            raise ValueError("unexpected shape")

        monkeypatch.setattr(engine.api_resources, "copy_tags", broken)
        assert await engine.arm_targets(ctx) == []


class TestKeptSources:
    @pytest.mark.parametrize("policy", [FinalizePolicy.KEEP, FinalizePolicy.RENAME])
    def test_keep_and_rename_leave_the_source_disarmed(
        self, ctx: MigrationContext, policy: FinalizePolicy
    ) -> None:
        ctx.plan = _plan(_app(), _app("app2", "api"), policy=policy)
        _journal_originals(ctx, app1=dt.Triggers(True, False), app2=dt.Triggers(False, False))

        assert engine.note_kept_sources(ctx) == ["app1"]

        [notice] = ctx.notices
        assert notice.startswith("shop (the kept source)")
        assert "coolify-migrate rollback m1" in notice

    def test_delete_says_nothing(self, ctx: MigrationContext) -> None:
        ctx.plan = _plan(policy=FinalizePolicy.DELETE)
        _journal_originals(ctx, app1=dt.Triggers(True, False))
        assert engine.note_kept_sources(ctx) == [] and ctx.notices == []

    def test_nothing_disarmed_says_nothing(self, ctx: MigrationContext) -> None:
        ctx.plan = _plan(policy=FinalizePolicy.KEEP)
        assert engine.note_kept_sources(ctx) == [] and ctx.notices == []


class TestStepFinalize:
    async def test_keep_arms_the_target_and_reports_the_disarmed_source(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(policy=FinalizePolicy.KEEP)
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=dt.Triggers(True, False))
        source = App("app1", auto=False).mount(respx_mock)
        target = App("appT", auto=False).mount(respx_mock)
        _no_tags(respx_mock, "applications", "app1")

        result = await steps.step_finalize(ctx)

        assert result["target_armed"] == ["appT"]
        assert result["sources_left_disarmed"] == ["app1"]
        assert target.patches == [{"is_auto_deploy_enabled": True}]
        # The kept source is NOT switched back on.
        assert source.patches == []

    async def test_delete_gives_the_target_the_tags_create_read(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The policy loop deletes every source BEFORE the targets get their
        # tags, and a deleted source has none left to read (soft-deleted, so
        # its GET .../tags 404s). CREATE's journalled names are what counts.
        ctx.plan = _plan(_app(), _db(), policy=FinalizePolicy.DELETE)
        ctx.target_uuids.update({"app1": "appT", "db1": "db2"})
        _journal_originals(ctx, app1=dt.Triggers(True, False))
        ctx.journal.append(
            "step_started",
            state="create_target",
            detail={dt.TAGS_JOURNAL_KEY: {"app1": ["prod", "ci-deploy"], "db1": ["prod"]}},
        )
        deleted: list[str] = []

        def delete(request: httpx.Request) -> httpx.Response:
            deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"message": "Deletion request queued."})

        respx_mock.delete(url__regex=r".*/(applications/app1|databases/db1)(\?.*)?$").mock(
            side_effect=delete
        )
        gone = respx_mock.get(url__regex=r".*/(applications/app1|databases/db1)/tags$").mock(
            return_value=httpx.Response(404, json={"message": "Not found."})
        )
        target = App("appT", auto=False).mount(respx_mock)
        app_tags = respx_mock.post(f"{BASE}/applications/appT/tags").mock(
            return_value=httpx.Response(201, json=[])
        )
        db_tags = respx_mock.post(f"{BASE}/databases/db2/tags").mock(
            return_value=httpx.Response(201, json=[])
        )

        result = await steps.step_finalize(ctx)

        assert deleted == ["app1", "db1"]
        assert result["target_armed"] == ["appT"]
        assert target.patches == [{"is_auto_deploy_enabled": True}]
        assert json.loads(app_tags.calls[0].request.read()) == {"tag_names": ["prod", "ci-deploy"]}
        assert json.loads(db_tags.calls[0].request.read()) == {"tag_names": ["prod"]}
        assert not gone.called
        assert result["sources_left_disarmed"] == [] and ctx.notices == []


class TestReadSourceTags:
    """CREATE reads the names - and writes no tag anywhere."""

    async def test_every_sources_names_are_journalled(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_app(), _db())
        respx_mock.get(f"{BASE}/applications/app1/tags").mock(
            return_value=httpx.Response(200, json=[{"uuid": "t1", "name": "prod"}])
        )
        _no_tags(respx_mock, "databases", "db1")

        names = await engine.read_source_tags(ctx)

        assert names == {"app1": ["prod"], "db1": []}
        assert ctx.journal.undo_info("create_target")[dt.TAGS_JOURNAL_KEY] == names

    async def test_what_cannot_be_read_is_left_out_and_never_fails_create(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_app(), _app("app2", "api"), _db())
        # 4.1.x: no tag route. Then an API error, then a shape nobody expected.
        respx_mock.get(f"{BASE}/applications/app1/tags").mock(
            return_value=httpx.Response(404, json={"message": "Not found."})
        )
        respx_mock.get(f"{BASE}/applications/app2/tags").mock(return_value=httpx.Response(500))
        respx_mock.get(f"{BASE}/databases/db1/tags").mock(side_effect=RuntimeError("unexpected"))

        assert await engine.read_source_tags(ctx) == {}
        # Nothing to journal: FINALIZE asks each source again.
        assert dt.TAGS_JOURNAL_KEY not in ctx.journal.undo_info("create_target")

    async def test_finalize_adds_the_journalled_names_without_reading_the_source(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db(), policy=FinalizePolicy.KEEP)
        ctx.target_uuids["db1"] = "db2"
        ctx.journal.append(
            "step_started", state="create_target", detail={dt.TAGS_JOURNAL_KEY: {"db1": []}}
        )
        read = respx_mock.get(f"{BASE}/databases/db1/tags").mock(
            return_value=httpx.Response(200, json=[{"name": "stale"}])
        )
        post = respx_mock.post(f"{BASE}/databases/db2/tags").mock(
            return_value=httpx.Response(201, json=[])
        )

        assert await engine.arm_targets(ctx) == []

        # CREATE read no tags: nothing to add, and no reason to ask again.
        assert not read.called and not post.called


# ── R7: the rollback gives the switches back ─────────────────────────────────


class TestRestore:
    async def test_gives_back_only_what_was_taken_and_a_replay_sends_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_app(), _app("app2", "api"))
        _journal_originals(ctx, app1=dt.Triggers(True, True), app2=dt.Triggers(False, False))
        one = App("app1", auto=False, previews=False).mount(respx_mock)
        two = App("app2", auto=True).mount(respx_mock)  # switched on since: not ours

        await engine.restore_sources(ctx)
        await engine.restore_sources(ctx)

        assert one.patches == [
            {"is_auto_deploy_enabled": True, "is_preview_deployments_enabled": True}
        ]
        assert two.patches == []

    async def test_nothing_journalled_is_a_noop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        await engine.restore_sources(ctx)
        assert not respx_mock.calls

    async def test_a_source_the_plan_does_not_name_is_restored_by_uuid(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The journal is the truth after a crash; a failure still names it.
        _journal_originals(ctx, gone=dt.Triggers(True, False))
        respx_mock.get(f"{BASE}/applications/gone").mock(return_value=httpx.Response(404))
        with pytest.raises(RuntimeError, match=r"switched back on for: gone \(GET .*HTTP 404\)"):
            await engine.restore_sources(ctx)

    async def test_a_failure_is_loud_and_the_others_still_run(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_app(), _app("app2", "api"))
        _journal_originals(ctx, app1=dt.Triggers(True, False), app2=dt.Triggers(True, False))
        respx_mock.get(f"{BASE}/applications/app1").mock(return_value=httpx.Response(404))
        two = App("app2", auto=False).mount(respx_mock)

        with pytest.raises(RuntimeError, match=r"^auto-deploy NOT switched back on for: shop"):
            await engine.restore_sources(ctx)

        assert two.patches == [{"is_auto_deploy_enabled": True}]

    async def test_the_compensation_is_registered(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        handler = build_compensations()[Compensation.RESTORE_SOURCE_DEPLOY_TRIGGERS]
        assert handler is compensations.undo_restore_source_deploy_triggers
        _journal_originals(ctx, app1=dt.Triggers(True, False))
        app = App("app1", auto=False).mount(respx_mock)

        await handler(ctx, {})

        assert app.patches == [{"is_auto_deploy_enabled": True}]


class TestRollbackAgainstTheApi:
    """The real saga: domains, then the deploy switches, then the restart."""

    async def _rollback(self, ctx: MigrationContext) -> Any:
        saga = Saga(journal=ctx.journal, context=ctx, steps={}, compensations=build_compensations())
        return await saga.rollback()

    def _routes(self, ctx: MigrationContext, respx_mock: respx.Router) -> tuple[App, list[str]]:
        order: list[str] = []
        app = App("app1", auto=False)

        def patch(request: httpx.Request) -> httpx.Response:
            order.append("switches")
            return app.patch(request)

        def mark(label: str) -> Any:
            def handler(_request: httpx.Request) -> httpx.Response:
                order.append(label)
                return httpx.Response(200, json={})

            return handler

        respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=app.get)
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=patch)
        respx_mock.post(f"{BASE}/applications/app1/restart").mock(side_effect=mark("restart"))
        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=mark("delete"))
        return app, order

    async def test_after_a_failed_copy_the_switches_return_before_the_restart(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.journal.append(
            "step_completed", state="create_target", detail={"target_uuids": {"app1": "appT"}}
        )
        _journal_originals(ctx, app1=dt.Triggers(True, False))
        ctx.journal.append("step_started", state="quiesce", detail={STOP_REQUESTED: True})
        ctx.journal.append("step_completed", state="quiesce", detail={})
        app, order = self._routes(ctx, respx_mock)

        result = await self._rollback(ctx)

        assert result.outcome is Outcome.ROLLED_BACK
        assert order == ["switches", "restart", "delete"]
        assert app.settings == {
            "is_auto_deploy_enabled": True,
            "is_preview_deployments_enabled": False,
        }

    async def test_a_rollback_after_a_succeeded_keep_run_rearms_the_source(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(policy=FinalizePolicy.KEEP)
        ctx.journal.append(
            "step_completed", state="create_target", detail={"target_uuids": {"app1": "appT"}}
        )
        _journal_originals(ctx, app1=dt.Triggers(True, False))
        ctx.journal.append("step_started", state="quiesce", detail={STOP_REQUESTED: True})
        ctx.journal.append("step_completed", state="quiesce", detail={})
        ctx.journal.append("step_completed", state="finalize", detail={"policy": "keep"})
        app, order = self._routes(ctx, respx_mock)

        result = await self._rollback(ctx)

        assert result.outcome is Outcome.ROLLED_BACK
        assert order == ["switches", "restart", "delete"]
        assert app.settings is not None and app.settings["is_auto_deploy_enabled"] is True
