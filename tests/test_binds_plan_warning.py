"""A host-wide agent is warned about in the plan, before anything is stopped.

A container that mounts the whole host writable (Portainer's agent, a host
backup) no longer refuses a bind migration; it is warned about instead, and the
remedy — make the mount read-only, or stop the container — is of use only before
QUIESCE. A run notice alone reaches the operator in the final run report, after
the copy it warns about. So the plan carries it in ``notes``, which
``MigrationPlan.warnings`` and the confirmation's warnings panel show, and
PREFLIGHT repeats the same text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import HostBind
from bg_coolify_migrate.domain.kinds import BuildPack, ResourceKind
from bg_coolify_migrate.domain.manifest import VolumeManifest
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from bg_coolify_migrate.engine import planner, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import TransferError
from bg_coolify_migrate.transfer import binds as transfer_binds
from bg_coolify_migrate.ui import report as report_mod
from tests.test_binds_engine import SRC, FsHost, _bind, _service, _src, bind_context
from tests.test_engine_planner import _PlanApi

AGENT = {
    "name": "/portainer_agent",
    "labels": {},
    "mounts": [{"Type": "bind", "Source": "/", "RW": True}],
}

PLANS = (_service(_bind(f"{SRC}/data"), _bind("/srv/files", "/files")),)


async def _notes(host: FsHost, plans: Any = PLANS) -> list[str]:
    return await planner.host_wide_writer_notes(
        host,  # type: ignore[arg-type]
        plans,
        project="shop",
        environment="production",
        server="old",
    )


class TestPlanNote:
    async def test_names_the_agent_the_server_the_paths_and_the_remedy(self) -> None:
        notes = await _notes(FsHost(containers=[AGENT]))
        assert len(notes) == 1
        assert "portainer_agent on old" in notes[0]
        assert f"{SRC}/data" in notes[0] and "/srv/files" in notes[0]
        assert "read-only" in notes[0] and "before the migration starts" in notes[0]

    async def test_a_plan_without_binds_asks_the_host_nothing(self) -> None:
        host = FsHost(containers=[AGENT])
        assert await _notes(host, plans=(_service(),)) == []
        assert host.commands == []

    async def test_the_plans_own_container_is_no_agent(self) -> None:
        own = {**AGENT, "name": "/store-svc1"}
        assert await _notes(FsHost(containers=[own])) == []

    async def test_a_read_only_root_is_no_writer(self) -> None:
        cadvisor = {
            "name": "/cadvisor",
            "labels": {},
            "mounts": [{"Type": "bind", "Source": "/", "RW": False}],
        }
        assert await _notes(FsHost(containers=[cadvisor])) == []

    async def test_an_unreadable_listing_is_a_plan_without_the_note(self) -> None:
        host = FsHost(containers=[AGENT])
        host.on(r"^sh -c ", exit_status=1, stderr="Cannot connect to the Docker daemon")
        assert await _notes(host) == []
        # ...and PREFLIGHT, which reads it again, fails closed on the same answer.
        with pytest.raises(TransferError, match="could not list"):
            await transfer_binds.host_binds(host)  # type: ignore[arg-type]

    async def test_preflight_repeats_the_plans_text(self, tmp_path: Path) -> None:
        ctx: MigrationContext = bind_context(tmp_path)
        try:
            _src(ctx).containers = [AGENT]
            planned = await _notes(_src(ctx), plans=ctx.plan.resources)
            await steps._assert_source_binds_safe(ctx)
            assert ctx.notices == planned
        finally:
            await ctx.api.aclose()


class TestForeignBinds:
    def test_keeps_only_containers_outside_the_plan(self) -> None:
        mine = HostBind("store-svc1", "/")
        labelled = HostBind("x", "/", labels={"coolify.projectName": "shop"})
        other = HostBind("agent", "/")
        found = binds.foreign_binds(
            [mine, labelled, other],
            uuids=["svc1"],
            label_sets=[{"coolify.projectName": "shop"}],
        )
        assert found == [other]


async def _plan_with_bind(monkeypatch: pytest.MonkeyPatch, host: FsHost) -> Any:
    """build_plan for one application whose manifest migrates a bind mount."""

    async def fake_find_project(api: Any, project: str) -> dict[str, Any]:
        return {"uuid": "p1", "name": "shop"}

    async def fake_find_server(api: Any, name: str) -> dict[str, Any]:
        return {"uuid": "s2", "name": name}

    async def fake_env_resources(api: Any, project_uuid: str, environment: str) -> Any:
        return [("applications", {"uuid": "a1", "name": "api"})]

    async def fake_snapshot(api: Any, source_host: Any, **kw: Any) -> Any:
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="api",
            collection="applications",
            kind=ResourceKind.APP_DOCKERIMAGE,
            build_pack=BuildPack.DOCKERIMAGE,
        )
        return snapshot, [], {}

    async def fake_resolve_server(api: Any, resource: Any) -> dict[str, Any]:
        return {"uuid": "s1", "name": "old", "ip": "10.0.0.1", "settings": {}}

    async def fake_manifest(source_host: Any, **kw: Any) -> VolumeManifest:
        return VolumeManifest(items=(_bind("/srv/files", "/files"),))

    async def no_drift(source_host: Any, snapshot: Any) -> None:
        return None

    async def no_mounts(source_host: Any, containers: Any) -> list[Any]:
        return []

    monkeypatch.setattr(planner, "find_project", fake_find_project)
    monkeypatch.setattr(planner, "find_server", fake_find_server)
    monkeypatch.setattr(planner, "environment_resources", fake_env_resources)
    monkeypatch.setattr(planner, "snapshot_resource", fake_snapshot)
    monkeypatch.setattr(planner, "resolve_server", fake_resolve_server)
    monkeypatch.setattr(planner, "build_manifest", fake_manifest)
    monkeypatch.setattr(planner, "assess_drift", no_drift)
    monkeypatch.setattr(planner, "inspect_all_mounts", no_mounts)

    return await planner.build_plan(
        _PlanApi(version="4.7.2", envs=[]),  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        project="shop",
        environment="production",
        target_server="new",
    )


class TestBuildPlan:
    async def test_the_confirmation_shows_it_before_anything_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_with_bind(monkeypatch, FsHost(containers=[AGENT]))
        [note] = [n for n in plan.notes if "portainer_agent" in n]
        assert "portainer_agent on old" in note and "/srv/files" in note
        assert note in plan.warnings
        assert not plan.is_blocked
        panel = report_mod.warnings_panel(plan)
        assert panel is not None

    async def test_no_agent_no_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_with_bind(monkeypatch, FsHost())
        assert not any("whole host" in n for n in plan.notes)
