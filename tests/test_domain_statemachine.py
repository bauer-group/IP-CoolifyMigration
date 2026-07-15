"""Tests for the saga state machine.

The rollback plan is pure, so a crash at any state can be asserted without
touching a server. This is the property that makes the chaos suite feasible.
"""

from __future__ import annotations

import itertools

import pytest

from bg_coolify_migrate.domain.statemachine import (
    ORDER,
    Compensation,
    FinalizePolicy,
    State,
    can_resume_from,
    compensations_for,
    finalize_is_destructive,
    has_side_effects,
    is_irreversible,
    is_terminal,
    next_state,
    requires_source_restart,
    rollback_plan,
    states_through,
)


class TestOrdering:
    def test_create_target_precedes_quiesce(self) -> None:
        # A failed create then costs zero downtime, AND the target must exist
        # before volumes can be paired by mount_path.
        assert ORDER.index(State.CREATE_TARGET) < ORDER.index(State.QUIESCE)

    def test_discover_follows_quiesce(self) -> None:
        # Discovery before the stop is provisional: a running stack can still
        # create volumes.
        assert ORDER.index(State.QUIESCE) < ORDER.index(State.DISCOVER)

    def test_dns_gate_between_verify_and_start(self) -> None:
        # Earlier would waste a good transfer; later would already have caused
        # the ACME damage the gate exists to prevent.
        assert ORDER.index(State.VERIFY) < ORDER.index(State.DNS_GATE)
        assert ORDER.index(State.DNS_GATE) < ORDER.index(State.START_TARGET)

    def test_finalize_is_last_before_done(self) -> None:
        assert ORDER[-2:] == (State.FINALIZE, State.DONE)

    def test_copy_precedes_verify(self) -> None:
        assert ORDER.index(State.COPY) < ORDER.index(State.VERIFY)

    def test_every_state_appears_exactly_once(self) -> None:
        assert len(ORDER) == len(set(ORDER)) == len(State)

    def test_next_state_walks_the_order(self) -> None:
        for a, b in itertools.pairwise(ORDER):
            assert next_state(a) is b

    def test_next_state_on_terminal_raises(self) -> None:
        with pytest.raises(ValueError, match="terminal"):
            next_state(State.DONE)

    def test_is_terminal(self) -> None:
        assert is_terminal(State.DONE)
        assert not is_terminal(State.COPY)

    def test_states_through(self) -> None:
        assert states_through(State.PLAN) == (State.INIT, State.PREFLIGHT, State.PLAN)


class TestSideEffects:
    @pytest.mark.parametrize(
        "state",
        [State.INIT, State.PREFLIGHT, State.PLAN, State.DISCOVER, State.VERIFY, State.DNS_GATE],
    )
    def test_read_only_states_have_no_compensation(self, state: State) -> None:
        assert has_side_effects(state) is False
        assert compensations_for(state) == ()

    @pytest.mark.parametrize(
        "state", [State.CREATE_TARGET, State.QUIESCE, State.COPY, State.START_TARGET, State.FINALIZE]
    )
    def test_mutating_states_have_compensation(self, state: State) -> None:
        assert has_side_effects(state) is True
        assert compensations_for(state) != ()


class TestRollbackPlan:
    def test_nothing_completed_means_nothing_to_undo(self) -> None:
        assert rollback_plan([]) == ()

    def test_read_only_progress_means_nothing_to_undo(self) -> None:
        assert rollback_plan([State.INIT, State.PREFLIGHT, State.PLAN]) == ()

    def test_failure_after_create_target_deletes_it(self) -> None:
        plan = rollback_plan([State.INIT, State.PREFLIGHT, State.PLAN, State.CREATE_TARGET])
        assert [s.compensation for s in plan] == [Compensation.DELETE_TARGET_RESOURCE]

    def test_failure_after_quiesce_restarts_source_then_deletes_target(self) -> None:
        plan = rollback_plan([State.CREATE_TARGET, State.QUIESCE])
        # Reverse order: undo QUIESCE (restart source) before undoing CREATE_TARGET.
        assert [s.compensation for s in plan] == [
            Compensation.RESTART_SOURCE,
            Compensation.DELETE_TARGET_RESOURCE,
        ]

    def test_failure_mid_copy_drops_volumes_revokes_key_and_restarts(self) -> None:
        plan = rollback_plan([State.CREATE_TARGET, State.QUIESCE, State.COPY])
        assert [s.compensation for s in plan] == [
            Compensation.DROP_TARGET_VOLUMES,
            Compensation.REVOKE_EPHEMERAL_KEY,
            Compensation.RESTART_SOURCE,
            Compensation.DELETE_TARGET_RESOURCE,
        ]

    def test_ordering_stops_target_before_dropping_its_volumes(self) -> None:
        # A volume in use by a running container cannot be removed; and deleting
        # the resource before dropping volumes would leak them.
        plan = rollback_plan([State.CREATE_TARGET, State.QUIESCE, State.COPY, State.START_TARGET])
        order = [s.compensation for s in plan]
        assert order.index(Compensation.STOP_TARGET) < order.index(Compensation.DROP_TARGET_VOLUMES)
        assert order.index(Compensation.DROP_TARGET_VOLUMES) < order.index(
            Compensation.DELETE_TARGET_RESOURCE
        )

    def test_plan_is_insensitive_to_input_order(self) -> None:
        # A journal replayed after a crash may hand us states in any order.
        a = rollback_plan([State.CREATE_TARGET, State.QUIESCE, State.COPY])
        b = rollback_plan([State.COPY, State.CREATE_TARGET, State.QUIESCE])
        assert a == b

    def test_plan_tolerates_duplicates(self) -> None:
        a = rollback_plan([State.CREATE_TARGET, State.QUIESCE])
        b = rollback_plan([State.CREATE_TARGET, State.CREATE_TARGET, State.QUIESCE, State.QUIESCE])
        assert a == b

    def test_every_step_names_the_state_that_caused_it(self) -> None:
        # `because_of` is a plain str, not State: the same machinery drives F2's
        # own state machine. StrEnum compares equal to its value, so the
        # assertions stay readable.
        plan = rollback_plan([State.CREATE_TARGET, State.QUIESCE, State.COPY])
        assert all(step.because_of in {s.value for s in State} for step in plan)
        by_comp = {s.compensation: s.because_of for s in plan}
        assert by_comp[Compensation.RESTART_SOURCE] == State.QUIESCE
        assert by_comp[Compensation.DELETE_TARGET_RESOURCE] == State.CREATE_TARGET

    def test_full_run_rollback_restores_source_identity(self) -> None:
        plan = rollback_plan(list(ORDER))
        comps = [s.compensation for s in plan]
        assert Compensation.RESTORE_SOURCE_NAME in comps
        assert Compensation.RESTORE_SOURCE_FQDN in comps


class TestSourceRestart:
    def test_not_required_before_quiesce(self) -> None:
        assert requires_source_restart([State.CREATE_TARGET]) is False

    def test_required_after_quiesce(self) -> None:
        assert requires_source_restart([State.CREATE_TARGET, State.QUIESCE]) is True

    def test_required_after_copy(self) -> None:
        assert requires_source_restart([State.QUIESCE, State.COPY, State.VERIFY]) is True


class TestIrreversibility:
    def test_only_finalize_is_irreversible(self) -> None:
        irreversible = [s for s in State if is_irreversible(s)]
        assert irreversible == [State.FINALIZE]

    def test_copy_is_reversible(self) -> None:
        # The source is never destroyed until FINALIZE, so everything before it
        # can be undone. This is the backbone of the safety story.
        assert is_irreversible(State.COPY) is False


class TestResume:
    @pytest.mark.parametrize("state", [s for s in ORDER if s is not State.DONE])
    def test_every_non_terminal_state_is_resumable(self, state: State) -> None:
        assert can_resume_from(state) is True

    def test_done_is_not_resumable(self) -> None:
        assert can_resume_from(State.DONE) is False

    def test_dns_gate_is_resumable_by_design(self) -> None:
        # The gate exists to be resumed: flip DNS, then continue.
        assert can_resume_from(State.DNS_GATE) is True


class TestFinalizePolicy:
    def test_delete_is_destructive(self) -> None:
        assert finalize_is_destructive(FinalizePolicy.DELETE) is True

    @pytest.mark.parametrize("policy", [FinalizePolicy.KEEP, FinalizePolicy.RENAME])
    def test_others_are_not(self, policy: FinalizePolicy) -> None:
        assert finalize_is_destructive(policy) is False
