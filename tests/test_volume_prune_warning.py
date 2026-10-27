"""The plan warns about a server whose Docker cleanup prunes unused volumes.

A warning, never a refusal: the migration pins what it exposes either way, and
whether a server should prune is the operator's call. What the operator must
hear is the part no pin covers — after a KEEP/RENAME finalize the stopped
source has no containers, so on such a server it is no fallback copy.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.plan import MigrationPlan, ServerRef
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine.planner import server_ref

PRUNES = {
    "delete_unused_volumes": True,
    "docker_cleanup_frequency": "0 0 * * *",
    "force_docker_cleanup": True,
    "docker_cleanup_threshold": 80,
}


class TestDockerCleanup:
    @pytest.mark.parametrize("value", [True, 1, "1", "true", "t", " TRUE "])
    def test_truthy_spellings_turn_it_on(self, value: object) -> None:
        on, _ = fidelity.docker_cleanup({"settings": {**PRUNES, "delete_unused_volumes": value}})
        assert on is True

    @pytest.mark.parametrize("value", [False, 0, "0", "false", "", None])
    def test_falsy_spellings_leave_it_off(self, value: object) -> None:
        assert fidelity.docker_cleanup(
            {"settings": {**PRUNES, "delete_unused_volumes": value}}
        ) == (False, "")

    def test_absent_settings_are_off(self) -> None:
        assert fidelity.docker_cleanup({}) == (False, "")
        assert fidelity.docker_cleanup({"settings": None}) == (False, "")

    def test_forced_runs_on_every_scheduled_run(self) -> None:
        assert fidelity.docker_cleanup({"settings": PRUNES}) == (
            True,
            "cron '0 0 * * *', on every run",
        )

    def test_unforced_runs_above_the_threshold(self) -> None:
        settings = {**PRUNES, "force_docker_cleanup": False, "docker_cleanup_threshold": 70}
        assert fidelity.docker_cleanup({"settings": settings}) == (
            True,
            "cron '0 0 * * *', when disk usage is at or above 70%",
        )

    def test_missing_schedule_details_still_say_something(self) -> None:
        assert fidelity.docker_cleanup({"settings": {"delete_unused_volumes": "1"}}) == (
            True,
            "its schedule, when disk usage crosses its threshold",
        )


def _server(uuid: str, name: str, *, prunes: bool) -> ServerRef:
    return ServerRef(
        uuid=uuid,
        name=name,
        ip="10.0.0.1",
        prunes_unused_volumes=prunes,
        docker_cleanup="cron '0 0 * * *', on every run" if prunes else "",
    )


def _plan(
    *, source: bool, target: bool, policy: FinalizePolicy = FinalizePolicy.RENAME
) -> MigrationPlan:
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=_server("s1", "old", prunes=source),
        target_server=_server("s2", "new", prunes=target),
        finalize_policy=policy,
    )


class TestVolumePruneNotes:
    @pytest.mark.parametrize("policy", [FinalizePolicy.RENAME, FinalizePolicy.KEEP])
    def test_a_pruning_source_hears_it_is_no_fallback_after_keep_or_rename(
        self, policy: FinalizePolicy
    ) -> None:
        warnings = _plan(source=True, target=False, policy=policy).warnings
        assert len(warnings) == 2
        assert "source server 'old' has 'Delete unused volumes' on" in warnings[0]
        assert "cron '0 0 * * *', on every run" in warnings[0]
        assert "bg-coolify-migrate-pin-" in warnings[0]
        assert f"stays stopped ({policy.value})" in warnings[1]
        assert "no fallback copy" in warnings[1]

    def test_delete_needs_no_fallback_note(self) -> None:
        warnings = _plan(source=True, target=False, policy=FinalizePolicy.DELETE).warnings
        assert len(warnings) == 1 and "source server 'old'" in warnings[0]

    def test_a_pruning_target_is_named(self) -> None:
        warnings = _plan(source=False, target=True).warnings
        assert len(warnings) == 1 and "target server 'new'" in warnings[0]

    def test_one_server_is_named_once(self) -> None:
        plan = _plan(source=True, target=False).model_copy(
            update={"target_server": _server("s1", "old", prunes=True)}
        )
        assert sum("has 'Delete unused volumes' on" in w for w in plan.warnings) == 1

    def test_no_pruning_no_notes(self) -> None:
        assert _plan(source=False, target=False).warnings == ()

    def test_follows_a_policy_swapped_after_planning(self) -> None:
        # cli.py swaps finalize_policy onto the built plan with model_copy, so the
        # note must be derived on read rather than frozen into `notes`.
        plan = _plan(source=True, target=False, policy=FinalizePolicy.RENAME)
        swapped = plan.model_copy(update={"finalize_policy": FinalizePolicy.DELETE})
        assert len(plan.warnings) == 2 and len(swapped.warnings) == 1


def test_a_plan_saved_before_these_fields_still_loads() -> None:
    saved = (
        '{"project": "shop", "environment": "production",'
        ' "source_server": {"uuid": "s1", "name": "old", "ip": "10.0.0.1"},'
        ' "target_server": {"uuid": "s2", "name": "new", "ip": "10.0.0.2"}}'
    )
    plan = MigrationPlan.model_validate_json(saved)
    assert plan.source_server.prunes_unused_volumes is False
    assert plan.warnings == ()


def test_server_ref_reads_the_setting_from_the_settings_relation() -> None:
    ref = server_ref({"uuid": "s1", "name": "old", "ip": "10.0.0.1", "settings": PRUNES})
    assert ref.prunes_unused_volumes is True
    assert ref.docker_cleanup == "cron '0 0 * * *', on every run"


async def test_build_plan_warns_about_a_pruning_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring: build_plan's server read reaches the plan's warnings."""
    from tests import test_engine_planner

    async def pruning_server(self: Any, uuid: str) -> dict[str, Any]:
        return {"uuid": uuid, "name": "new", "ip": "10.0.0.2", "settings": PRUNES}

    monkeypatch.setattr(test_engine_planner._PlanApi, "get_server", pruning_server)
    plan = await test_engine_planner._plan_one_app(monkeypatch, {})

    assert plan.target_server.prunes_unused_volumes
    assert any("target server 'new' has 'Delete unused volumes' on" in w for w in plan.warnings)
