"""Tests for the saga executor, including the chaos suite.

The chaos tests are the differentiator. Neither coolify-mover nor
Geczy/coolify-migration has any journal, resume or rollback at all: a failure
mid-run leaves a half-built resource that nothing will ever clean up, and
re-running creates a SECOND clone with a new uuid.

Because the executor takes its steps and compensations by injection, all of this
is verifiable without a Coolify instance, an SSH server or a Docker daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bg_coolify_migrate.domain.statemachine import Compensation, Outcome, State
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import DnsGateBlocked, MigrationError, TransferError
from bg_coolify_migrate.journal.store import Journal


class Recorder:
    """Records what ran, so a test can assert order rather than just outcome."""

    def __init__(self) -> None:
        self.steps: list[State] = []
        self.compensations: list[Compensation] = []
        self.undo_seen: dict[Compensation, dict[str, Any]] = {}


def build_steps(
    rec: Recorder,
    *,
    fail_at: State | None = None,
    error: MigrationError | None = None,
    undo_info: dict[State, dict[str, Any]] | None = None,
) -> dict[State, Any]:
    infos = undo_info or {}

    def make(state: State) -> Any:
        async def step(_ctx: Any) -> dict[str, Any]:
            rec.steps.append(state)
            if state is fail_at:
                raise error or TransferError(f"boom at {state.value}")
            return infos.get(state, {})

        return step

    return {s: make(s) for s in State if s is not State.DONE}


def build_compensations(rec: Recorder, *, fail: set[Compensation] | None = None) -> dict[Any, Any]:
    failing = fail or set()

    def make(comp: Compensation) -> Any:
        async def undo(_ctx: Any, undo_info: dict[str, Any]) -> None:
            rec.undo_seen[comp] = undo_info
            if comp in failing:
                raise RuntimeError(f"compensation {comp.value} failed")
            rec.compensations.append(comp)

        return undo

    return {c: make(c) for c in Compensation}


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal.create(tmp_path, "test-mig")


class TestHappyPath:
    async def test_runs_every_state_in_order(self, journal: Journal) -> None:
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec),
            compensations=build_compensations(rec),
        )
        result = await saga.run()
        assert result.ok
        assert result.outcome is Outcome.SUCCEEDED
        assert result.exit_code == 0
        assert rec.compensations == []
        assert rec.steps[0] is State.INIT
        assert rec.steps[-1] is State.FINALIZE

    async def test_journal_records_completion(self, journal: Journal) -> None:
        rec = Recorder()
        saga = Saga(
            journal=journal, context=None, steps=build_steps(rec), compensations=build_compensations(rec)
        )
        await saga.run()
        assert journal.is_finished
        assert State.COPY.value in journal.completed_states()


class TestFailureRollsBack:
    async def test_failure_at_copy_compensates_in_reverse(self, journal: Journal) -> None:
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.COPY),
            compensations=build_compensations(rec),
        )
        result = await saga.run()

        assert result.outcome is Outcome.ROLLED_BACK
        assert isinstance(result.error, TransferError)
        # CREATE_TARGET and QUIESCE completed; COPY failed before completing.
        assert rec.compensations == [
            Compensation.RESTART_SOURCE,
            Compensation.DELETE_TARGET_RESOURCE,
        ]

    async def test_failure_before_any_side_effect_has_nothing_to_undo(
        self, journal: Journal
    ) -> None:
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.PREFLIGHT),
            compensations=build_compensations(rec),
        )
        result = await saga.run()
        assert result.outcome is Outcome.FAILED
        assert rec.compensations == []

    async def test_failure_after_start_stops_target_before_dropping_volumes(
        self, journal: Journal
    ) -> None:
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.HEALTHCHECK),
            compensations=build_compensations(rec),
        )
        await saga.run()
        order = rec.compensations
        assert order.index(Compensation.STOP_TARGET) < order.index(
            Compensation.DROP_TARGET_VOLUMES
        )
        assert order.index(Compensation.DROP_TARGET_VOLUMES) < order.index(
            Compensation.DELETE_TARGET_RESOURCE
        )

    async def test_unexpected_exception_still_rolls_back(self, journal: Journal) -> None:
        # A bug in our own code must not leave a half-built resource behind.
        rec = Recorder()
        steps = build_steps(rec)

        async def crash(_ctx: Any) -> dict[str, Any]:
            raise ValueError("not a MigrationError")

        steps[State.COPY] = crash
        saga = Saga(
            journal=journal, context=None, steps=steps, compensations=build_compensations(rec)
        )
        result = await saga.run()
        assert result.outcome is Outcome.ROLLED_BACK
        assert Compensation.RESTART_SOURCE in rec.compensations

    async def test_compensations_receive_journalled_undo_info(self, journal: Journal) -> None:
        # The whole point of the journal: enough to compensate after a crash.
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(
                rec,
                fail_at=State.COPY,
                undo_info={State.CREATE_TARGET: {"target_uuid": "new-uuid-123"}},
            ),
            compensations=build_compensations(rec),
        )
        await saga.run()
        assert rec.undo_seen[Compensation.DELETE_TARGET_RESOURCE] == {"target_uuid": "new-uuid-123"}


class TestGatesAreNotFailures:
    async def test_dns_gate_blocks_without_rolling_back(self, journal: Journal) -> None:
        # A gate is a deliberate, resumable stop. Nothing is undone: the target
        # is created and its data verified — you flip DNS and continue.
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.DNS_GATE, error=DnsGateBlocked("dns not ready")),
            compensations=build_compensations(rec),
        )
        result = await saga.run()

        assert result.outcome is Outcome.BLOCKED
        assert rec.compensations == []
        assert result.exit_code == 3

    async def test_blocked_run_is_journalled_as_blocked_not_failed(self, journal: Journal) -> None:
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.DNS_GATE, error=DnsGateBlocked("x")),
            compensations=build_compensations(rec),
        )
        await saga.run()
        events = [r.event for r in journal.read()]
        assert "blocked" in events
        assert "rollback_started" not in events


class TestResume:
    async def test_resume_skips_completed_states(self, journal: Journal) -> None:
        # Crash after COPY...
        rec1 = Recorder()
        saga1 = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec1, fail_at=State.VERIFY),
            compensations={},  # no compensations registered -> nothing undone
        )
        await saga1.run()
        assert State.COPY in rec1.steps

        # ...resume: the expensive copy must not run again.
        rec2 = Recorder()
        saga2 = Saga(
            journal=journal, context=None, steps=build_steps(rec2), compensations=build_compensations(rec2)
        )
        result = await saga2.run()
        assert result.ok
        assert State.COPY not in rec2.steps
        assert State.VERIFY in rec2.steps

    async def test_resume_from_an_explicit_state(self, journal: Journal) -> None:
        rec = Recorder()
        saga = Saga(
            journal=journal, context=None, steps=build_steps(rec), compensations=build_compensations(rec)
        )
        await saga.run(start_from=State.DNS_GATE)
        assert State.COPY not in rec.steps
        assert State.DNS_GATE in rec.steps

    async def test_resume_after_dns_gate_completes_the_migration(self, journal: Journal) -> None:
        # The designed flow: gate blocks -> operator flips DNS -> resume.
        rec1 = Recorder()
        await Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec1, fail_at=State.DNS_GATE, error=DnsGateBlocked("x")),
            compensations=build_compensations(rec1),
        ).run()

        rec2 = Recorder()
        result = await Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec2),
            compensations=build_compensations(rec2),
        ).run()

        assert result.ok
        assert State.COPY not in rec2.steps  # not re-copied
        assert State.START_TARGET in rec2.steps


class TestRollbackOfRollback:
    async def test_a_failing_compensation_does_not_stop_the_others(
        self, journal: Journal
    ) -> None:
        # A failure to delete the target must not also prevent restarting the
        # source — that would turn one problem into two.
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.COPY),
            compensations=build_compensations(rec, fail={Compensation.DELETE_TARGET_RESOURCE}),
        )
        result = await saga.run()

        assert Compensation.RESTART_SOURCE in rec.compensations
        assert result.outcome is Outcome.ROLLBACK_FAILED

    async def test_rollback_failure_is_reported_honestly(self, journal: Journal) -> None:
        # Never pretend. coolify-mover warns and continues; we say what is broken.
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.COPY),
            compensations=build_compensations(rec, fail={Compensation.RESTART_SOURCE}),
        )
        result = await saga.run()

        assert result.outcome is Outcome.ROLLBACK_FAILED
        assert result.exit_code == 8
        assert result.compensations_failed
        assert "restart_source" in str(result.error)
        assert str(journal.path) in str(result.error)

    async def test_source_is_never_deleted_by_a_rollback(self, journal: Journal) -> None:
        # The backbone of the safety story: the source survives everything until
        # an explicit, confirmed finalize.
        rec = Recorder()
        saga = Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.HEALTHCHECK),
            compensations=build_compensations(rec),
        )
        await saga.run()
        assert Compensation.RESTART_SOURCE in rec.compensations
        assert all("delete_source" not in c.value for c in rec.compensations)


class TestExplicitRollback:
    async def test_rollback_command_undoes_a_blocked_run(self, journal: Journal) -> None:
        rec = Recorder()
        await Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec, fail_at=State.DNS_GATE, error=DnsGateBlocked("x")),
            compensations=build_compensations(rec),
        ).run()
        assert rec.compensations == []  # the gate itself undid nothing

        rec2 = Recorder()
        result = await Saga(
            journal=journal,
            context=None,
            steps={},
            compensations=build_compensations(rec2),
        ).rollback()

        assert result.outcome is Outcome.ROLLED_BACK
        assert Compensation.RESTART_SOURCE in rec2.compensations
        assert Compensation.DELETE_TARGET_RESOURCE in rec2.compensations


class TestJournalRobustness:
    async def test_unknown_state_in_journal_is_skipped_not_fatal(
        self, journal: Journal
    ) -> None:
        # A journal from another version may name a state we no longer have. We
        # cannot compensate what we do not understand, and guessing is worse.
        journal.append("step_completed", state="a_state_from_the_future")
        rec = Recorder()
        result = await Saga(
            journal=journal, context=None, steps=build_steps(rec), compensations=build_compensations(rec)
        ).run()
        assert result.ok

    async def test_on_state_hook_fires_for_the_ui(self, journal: Journal) -> None:
        seen: list[State] = []

        async def hook(state: State) -> None:
            seen.append(state)

        rec = Recorder()
        await Saga(
            journal=journal,
            context=None,
            steps=build_steps(rec),
            compensations=build_compensations(rec),
            on_state=hook,
        ).run()
        assert State.COPY in seen
