"""The deploy switches against the API: what is sent, and when.

The pure decisions are tests/test_deploy_triggers.py's. These run the real IO
shells (engine/deploy_triggers.py, api/resources.py) against respx: a target is
CREATED with auto-deploy off, and armed with its source's value only at
FINALIZE, never fatally.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.resources import Placement
from bg_coolify_migrate.domain.kinds import DatabaseEngine, GitAuth, ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine import deploy_triggers as engine
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"


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


# ── R1: FINALIZE arms the target ─────────────────────────────────────────────


class TestArmTargets:
    @pytest.mark.parametrize("auto", [True, False])
    async def test_the_target_gets_the_sources_value(
        self, ctx: MigrationContext, respx_mock: respx.Router, auto: bool
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        App("app1", auto=auto, previews=True).mount(respx_mock)
        target = App("appT", auto=False).mount(respx_mock)

        assert await engine.arm_targets(ctx) == ["appT"]

        # Previews stay Coolify's default: no create ever carried them.
        assert target.patches == [{"is_auto_deploy_enabled": auto}]

    async def test_an_unreadable_41_source_arms_coolifys_default(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        App("app1", auto=None).mount(respx_mock)
        target = App("appT", auto=None).mount(respx_mock)

        await engine.arm_targets(ctx)

        assert target.patches == [{"is_auto_deploy_enabled": True}]

    async def test_a_source_that_cannot_be_read_arms_coolifys_default(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        respx_mock.get(f"{BASE}/applications/app1").mock(return_value=httpx.Response(500))
        target = App("appT").mount(respx_mock)

        assert await engine.arm_targets(ctx) == ["appT"]

        assert target.patches == [{"is_auto_deploy_enabled": True}]

    async def test_a_failure_is_a_notice_never_a_failed_finalize(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=App("app1").get)
        respx_mock.patch(f"{BASE}/applications/appT").mock(return_value=httpx.Response(500))

        assert await engine.arm_targets(ctx) == []

        [notice] = ctx.notices
        assert notice.startswith("the target of shop (appT) was created with auto-deploy off")
        assert "switch 'Auto Deploy' on" in notice

    async def test_a_database_has_no_switch(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db())
        ctx.target_uuids["db1"] = "db2"
        assert await engine.arm_targets(ctx) == []
        assert not respx_mock.calls


class TestStepFinalize:
    async def test_finalize_arms_the_target(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(policy=FinalizePolicy.KEEP)
        ctx.target_uuids["app1"] = "appT"
        source = App("app1", auto=True).mount(respx_mock)
        target = App("appT", auto=False).mount(respx_mock)

        result = await steps.step_finalize(ctx)

        assert result["target_armed"] == ["appT"]
        assert target.patches == [{"is_auto_deploy_enabled": True}]
        assert source.patches == []
