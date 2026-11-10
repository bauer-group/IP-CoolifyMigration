"""A resource that does not run on the plan's source server is refused.

The plan's source server is the FIRST resource's (planner.build_plan), and the
run discovers, stops and copies every resource on it. An environment that spans
servers would put the others through all of that on the wrong host: no
container found, so no volume migrated, an empty container list read as
stopped, an empty target — and a delete finalize removing the source with its
data. So every resource records its own server at PLAN, and the plan refuses
each one on another, derived on every read like the same-server refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain.kinds import BuildPack, ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
    foreign_server_refusal,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.ui import report

OLD = ServerRef(uuid="s1", name="old", ip="10.0.0.1")
NEW = ServerRef(uuid="s2", name="new", ip="10.0.0.2")


def _resource(name: str, server_uuid: str | None) -> ResourcePlan:
    snapshot = ResourceSnapshot(
        uuid=f"{name}-uuid",
        name=name,
        collection="applications",
        kind=ResourceKind.APP_DOCKERIMAGE,
        server_uuid=server_uuid,
    )
    return ResourcePlan(snapshot=snapshot, strategy=Strategy.RECREATE_ONLY)


def _plan(*resources: ResourcePlan) -> MigrationPlan:
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=OLD,
        target_server=NEW,
        resources=resources,
    )


class TestForeignServerRefusal:
    def test_another_server_is_refused_and_says_what_to_do(self) -> None:
        line = foreign_server_refusal("s9", source=OLD, target=NEW)
        assert line is not None
        assert "runs on server s9, not on the source server 'old'" in line
        assert "spans servers" in line and "delete finalize would remove it" in line
        assert "Migrate it on its own" in line

    def test_the_target_server_is_named(self) -> None:
        line = foreign_server_refusal("s2", source=OLD, target=NEW)
        assert line is not None and "runs on the target server 'new'" in line

    @pytest.mark.parametrize("server_uuid", ["s1", None, ""])
    def test_the_source_server_or_nothing_known_is_not(self, server_uuid: str | None) -> None:
        assert foreign_server_refusal(server_uuid, source=OLD, target=NEW) is None

    def test_a_source_of_unknown_uuid_proves_nothing(self) -> None:
        blank = ServerRef(uuid="", name="?", ip="")
        assert foreign_server_refusal("s9", source=blank, target=NEW) is None


class TestPlan:
    def test_the_resource_elsewhere_blocks_and_its_sibling_does_not(self) -> None:
        web, db = _resource("web", "s1"), _resource("db", "s9")
        plan = _plan(web, db)
        assert plan.is_blocked
        assert plan.blocked_resources == (db,)
        [reason] = plan.blocking_reasons
        assert reason.startswith("db: it runs on server s9")
        assert plan.blocking_reasons_of(web) == ()
        # The resource on its own is fine: only the plan knows its servers.
        assert not db.is_blocked

    def test_a_resource_on_the_target_server_is_refused(self) -> None:
        plan = _plan(_resource("web", "s1"), _resource("db", "s2"))
        assert any("runs on the target server 'new'" in r for r in plan.blocking_reasons)

    @pytest.mark.parametrize("policy", list(FinalizePolicy))
    def test_under_every_policy(self, policy: FinalizePolicy) -> None:
        plan = _plan(_resource("web", "s1"), _resource("db", "s9"))
        assert plan.model_copy(update={"finalize_policy": policy}).is_blocked

    def test_one_server_is_fine(self) -> None:
        assert not _plan(_resource("web", "s1"), _resource("db", "s1")).is_blocked

    def test_survives_plan_json(self) -> None:
        saved = _plan(_resource("web", "s1"), _resource("db", "s9")).model_dump_json()
        assert MigrationPlan.model_validate_json(saved).is_blocked

    def test_a_plan_saved_before_the_field_refuses_nothing(self) -> None:
        import json

        doc = json.loads(_plan(_resource("web", "s1"), _resource("db", "s9")).model_dump_json())
        for resource in doc["resources"]:
            del resource["snapshot"]["server_uuid"]
        assert not MigrationPlan.model_validate(doc).is_blocked

    def test_the_report_names_the_resource(self) -> None:
        text = report.plain_plan(_plan(_resource("web", "s1"), _resource("db", "s9")))
        assert "name=db" in text and "blocked=True" in text
        assert "  blocking: it runs on server s9" in text

    def test_no_offline_offer_for_a_resource_elsewhere(self) -> None:
        from bg_coolify_migrate.domain.compose import MountClass
        from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest

        anon = "c" * 64
        manifest = VolumeManifest(
            items=(
                VolumeItem(
                    mount_class=MountClass.ANONYMOUS,
                    decision=Decision.REFUSE,
                    reason="anonymous volume",
                    source_name=anon,
                    source_path=f"/var/lib/docker/volumes/{anon}/_data",
                    mount_path="/cache",
                ),
            )
        )
        elsewhere = _resource("db", "s9").model_copy(update={"manifest": manifest})
        assert elsewhere.offline_only
        assert _plan(elsewhere).offline_only_resources == ()
        here = _resource("db", "s1").model_copy(update={"manifest": manifest})
        assert _plan(here).offline_only_resources == (here,)


# ── the planner records each resource's server ───────────────────────────────


async def _plan_two(
    monkeypatch: pytest.MonkeyPatch,
    second: dict[str, Any],
    *,
    resolved: dict[str, dict[str, Any] | None] | None = None,
) -> MigrationPlan:
    """build_plan over ``web`` (resolved onto s1) and ``pgdb`` (GET ``second``)."""
    from bg_coolify_migrate.domain.manifest import VolumeManifest
    from bg_coolify_migrate.engine import planner
    from tests.test_engine_planner import _PlanApi

    gets = {"web": {"uuid": "w1", "name": "web"}, "pgdb": {"uuid": "d1", "name": "pgdb", **second}}
    servers: dict[str, dict[str, Any] | None] = {
        "web": {"uuid": "s1", "name": "old", "ip": "10.0.0.1", "settings": {}},
        "pgdb": None,
        **(resolved or {}),
    }

    async def fake_find_project(api: Any, project: str) -> dict[str, Any]:
        return {"uuid": "p1", "name": "shop"}

    async def fake_find_server(api: Any, name: str) -> dict[str, Any]:
        return {"uuid": "s2", "name": name}

    async def fake_env_resources(api: Any, project_uuid: str, environment: str) -> Any:
        return [("applications", {"uuid": "w1", "name": "web"}), ("applications", gets["pgdb"])]

    async def fake_snapshot(api: Any, host: Any, *, resource: Any, **kw: Any) -> Any:
        name = str(resource["name"])
        snapshot = ResourceSnapshot(
            uuid=str(resource["uuid"]),
            name=name,
            collection="applications",
            kind=ResourceKind.APP_DOCKERIMAGE,
            build_pack=BuildPack.DOCKERIMAGE,
        )
        return snapshot, [], gets[name]

    async def fake_resolve_server(api: Any, resource: Any) -> dict[str, Any] | None:
        return servers[str(resource["name"])]

    async def fake_manifest(host: Any, **kw: Any) -> VolumeManifest:
        return VolumeManifest()

    async def nothing(*args: Any, **kw: Any) -> Any:
        return None

    async def no_mounts(host: Any, containers: Any) -> list[Any]:
        return []

    monkeypatch.setattr(planner, "find_project", fake_find_project)
    monkeypatch.setattr(planner, "find_server", fake_find_server)
    monkeypatch.setattr(planner, "environment_resources", fake_env_resources)
    monkeypatch.setattr(planner, "snapshot_resource", fake_snapshot)
    monkeypatch.setattr(planner, "resolve_server", fake_resolve_server)
    monkeypatch.setattr(planner, "build_manifest", fake_manifest)
    monkeypatch.setattr(planner, "assess_drift", nothing)
    monkeypatch.setattr(planner, "inspect_all_mounts", no_mounts)

    return await planner.build_plan(
        _PlanApi(version="4.7.2", envs=[]),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        project="shop",
        environment="production",
        target_server="new",
    )


class TestPlannerWiring:
    async def test_a_resource_on_another_server_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A service-shaped server_uuid: read off the GET, no lookup needed.
        plan = await _plan_two(monkeypatch, {"server_uuid": "s9"})
        web, db = plan.resources
        assert (web.snapshot.server_uuid, db.snapshot.server_uuid) == ("s1", "s9")
        assert plan.blocked_resources == (db,)
        assert "runs on server s9, not on the source server 'old'" in plan.blocking_reasons[0]

    async def test_a_nested_destination_server_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_two(monkeypatch, {"destination": {"server": {"uuid": "s2"}}})
        assert plan.resources[1].snapshot.server_uuid == "s2"
        assert any("runs on the target server" in r for r in plan.blocking_reasons)

    async def test_one_server_is_not_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only destination.server_id: resolved like the source server.
        plan = await _plan_two(
            monkeypatch,
            {"destination": {"server_id": 1}},
            resolved={"pgdb": {"uuid": "s1", "name": "old", "ip": "10.0.0.1"}},
        )
        assert [r.snapshot.server_uuid for r in plan.resources] == ["s1", "s1"]
        assert not plan.is_blocked

    async def test_a_resource_no_server_can_be_read_for_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_two(monkeypatch, {})
        db = plan.resources[1]
        assert db.snapshot.server_uuid is None
        [line] = db.refusals
        assert "did not say which server this resource runs on" in line
        assert plan.blocked_resources == (db,)
