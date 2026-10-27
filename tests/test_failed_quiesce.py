"""A QUIESCE that fails AFTER its stop request must still restart the source.

Coolify's stop removes the containers (``docker rm -f``), so once QUIESCE has
asked for a stop the source is down — and QUIESCE can still fail afterwards: a
SIGKILLed data holder (``_refuse_if_killed_data_holder``) or a stop gate that
timed out. QUIESCE never completed, so the rollback owed no RESTART_SOURCE and
the source stayed down while the run reported FAILED.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bg_coolify_migrate.domain.statemachine import (
    STOP_REQUESTED,
    Compensation,
    Outcome,
    State,
    owed_states,
    rollback_plan,
    source_was_stopped,
)
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import QuiesceError
from bg_coolify_migrate.journal.store import Journal

QUIESCE = State.QUIESCE.value
CREATE = State.CREATE_TARGET.value


class TestSourceWasStopped:
    def test_completed_quiesce(self) -> None:
        assert source_was_stopped([CREATE, QUIESCE], {}) is True

    def test_requested_stop(self) -> None:
        assert source_was_stopped([CREATE], {STOP_REQUESTED: True}) is True

    def test_neither(self) -> None:
        assert source_was_stopped([CREATE], {}) is False
        assert source_was_stopped([CREATE], {"pre_stop_mounts": {}}) is False

    def test_only_a_real_true_counts(self) -> None:
        assert source_was_stopped([CREATE], {STOP_REQUESTED: "yes"}) is False


class TestOwedAfterAStopRequest:
    def test_the_marker_makes_quiesce_owed(self) -> None:
        owed = owed_states([CREATE], markers={QUIESCE: {STOP_REQUESTED: True}})
        assert owed == (CREATE, QUIESCE)
        plan = rollback_plan([State(s) for s in owed])
        assert [s.compensation for s in plan] == [
            Compensation.RESTORE_SOURCE_FQDN,
            Compensation.RESTART_SOURCE,
            Compensation.DELETE_TARGET_RESOURCE,
            Compensation.RELEASE_SOURCE_PINS,
        ]

    def test_a_marker_on_another_state_means_nothing(self) -> None:
        assert owed_states([CREATE], markers={CREATE: {STOP_REQUESTED: True}}) == (CREATE,)

    def test_in_flight_quiesce_with_the_marker_is_owed(self) -> None:
        owed = owed_states([CREATE], in_flight=QUIESCE, markers={QUIESCE: {STOP_REQUESTED: True}})
        assert owed == (CREATE, QUIESCE)


def _recorder() -> tuple[list[Compensation], dict[Any, Any]]:
    ran: list[Compensation] = []

    def make(comp: Compensation) -> Any:
        async def undo(_ctx: Any, _info: dict[str, Any]) -> None:
            ran.append(comp)

        return undo

    return ran, {c: make(c) for c in Compensation}


def _steps(journal: Journal, *, mark: bool) -> dict[Any, Any]:
    async def ok(_ctx: Any) -> dict[str, Any]:
        return {}

    async def failing_quiesce(_ctx: Any) -> dict[str, Any]:
        if mark:
            journal.append("step_started", state=QUIESCE, detail={STOP_REQUESTED: True})
        raise QuiesceError("container(s) holding migrated data were SIGKILLed")

    out: dict[Any, Any] = {s: ok for s in State if s is not State.DONE}
    out[State.QUIESCE] = failing_quiesce
    return out


class TestTheRunRestartsTheSource:
    async def test_a_quiesce_failing_after_the_stop_restarts_the_source(
        self, tmp_path: Path
    ) -> None:
        journal = Journal.create(tmp_path, "q1")
        ran, comps = _recorder()
        result = await Saga(
            journal=journal, context=None, steps=_steps(journal, mark=True), compensations=comps
        ).run()

        assert result.outcome is Outcome.ROLLED_BACK
        assert ran == [
            Compensation.RESTORE_SOURCE_FQDN,
            Compensation.RESTART_SOURCE,
            Compensation.DELETE_TARGET_RESOURCE,
            Compensation.RELEASE_SOURCE_PINS,
        ]

    async def test_a_quiesce_failing_before_the_stop_leaves_the_source_alone(
        self, tmp_path: Path
    ) -> None:
        journal = Journal.create(tmp_path, "q2")
        ran, comps = _recorder()
        await Saga(
            journal=journal, context=None, steps=_steps(journal, mark=False), compensations=comps
        ).run()
        assert Compensation.RESTART_SOURCE not in ran
        assert ran == [
            Compensation.DELETE_TARGET_RESOURCE,
            Compensation.RESTORE_SOURCE_FQDN,
            Compensation.RELEASE_SOURCE_PINS,
        ]

    async def test_the_rollback_command_restarts_a_source_a_crash_left_down(
        self, tmp_path: Path
    ) -> None:
        journal = Journal.create(tmp_path, "q3")
        journal.append("step_completed", state=CREATE, detail={"target_uuids": {}})
        journal.append("step_started", state=QUIESCE)
        journal.append("step_started", state=QUIESCE, detail={STOP_REQUESTED: True})
        ran, comps = _recorder()
        await Saga(journal=journal, context=None, steps={}, compensations=comps).rollback()
        assert Compensation.RESTART_SOURCE in ran

    async def test_a_crash_before_the_stop_request_restarts_nothing(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "q4")
        journal.append("step_completed", state=CREATE, detail={"target_uuids": {}})
        journal.append("step_started", state=QUIESCE, detail={"pre_stop_mounts": {}})
        ran, comps = _recorder()
        await Saga(journal=journal, context=None, steps={}, compensations=comps).rollback()
        assert Compensation.RESTART_SOURCE not in ran


class TestStepQuiesceMarksTheRequest:
    async def test_the_marker_is_journalled_before_the_stop_is_requested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = Journal.create(tmp_path, "q5")
        seen_at_stop: list[bool] = []

        class Ctx:
            delete_previews = False
            plan = None
            source_host = None

        ctx: Any = Ctx()
        ctx.journal = journal
        ctx.settings = type("S", (), {"stop_timeout": 1.0})()

        async def capture(_ctx: Any) -> dict[str, list[str]]:
            return {}

        async def now_on(_host: Any) -> str:
            return "2026-10-27T00:00:00"

        async def request_stop(_ctx: Any) -> None:
            seen_at_stop.append(journal.undo_info(QUIESCE).get(STOP_REQUESTED) is True)

        async def gate_times_out(*_a: Any, **_kw: Any) -> Any:
            raise QuiesceError("stop gate timed out")

        async def no_pins(_ctx: Any) -> list[str]:
            return []

        monkeypatch.setattr(steps, "_capture_mounts", capture)
        monkeypatch.setattr(steps, "_pin_source", no_pins)
        monkeypatch.setattr(steps.quiesce, "now_on", now_on)
        monkeypatch.setattr(steps, "_request_stop", request_stop)
        monkeypatch.setattr(steps, "observed_labels", lambda _plan: {})
        monkeypatch.setattr(steps.quiesce, "wait_until_stopped", gate_times_out)

        with pytest.raises(QuiesceError):
            await steps.step_quiesce(ctx)

        assert seen_at_stop == [True]
        assert journal.in_flight_state() == QUIESCE
