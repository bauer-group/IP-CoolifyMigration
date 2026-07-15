"""The F1 migration state machine and its compensating actions.

PURE module: no IO. The engine in ``migration/`` executes what this module
decides; keeping the decision pure is what makes the chaos suite possible — we
can assert that a crash at any state produces the right rollback plan without
standing up a single container.

Ordering rationale (the non-obvious parts)
------------------------------------------
* ``CREATE_TARGET`` runs **before** ``QUIESCE``. Two reasons: a failed create
  then costs zero downtime, and — decisively — the target must exist before
  volumes can be paired, because pairing is by ``mount_path`` read back from the
  *created* target, not by string-replacing uuids.
* ``DISCOVER`` runs **after** ``QUIESCE``. Discovery before the stop is only ever
  provisional: a running stack can still create volumes. The authoritative
  manifest is the one taken when nothing can write any more.
* ``DNS_GATE`` runs **after** ``VERIFY`` and **before** ``START_TARGET``. Gating
  earlier would waste a transfer that is otherwise fine; gating later would have
  already caused the ACME damage the gate exists to prevent.
* ``FINALIZE`` is last and is the only irreversible step. Everything before it
  leaves the source intact, which is what makes rollback cheap — the one thing
  Geczy's script gets right, and the backbone of the whole safety story.

Compensation philosophy
-----------------------
Undo is **best-effort but loud**. A compensation that fails does not silently
pass: it raises ``RollbackError`` carrying the journal path, because the operator
now has a half-state that only they can adjudicate. We never attempt to
"compensate the compensation" — that way lies an unbounded recursion of guesses
about a system we have already lost track of.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class State(StrEnum):
    """States of an F1 project migration, in execution order."""

    INIT = "init"
    PREFLIGHT = "preflight"
    PLAN = "plan"
    CREATE_TARGET = "create_target"
    QUIESCE = "quiesce"
    DISCOVER = "discover"
    COPY = "copy"
    VERIFY = "verify"
    DNS_GATE = "dns_gate"
    START_TARGET = "start_target"
    HEALTHCHECK = "healthcheck"
    FINALIZE = "finalize"
    DONE = "done"


class Outcome(StrEnum):
    """Terminal outcomes. Distinct from :class:`State` because a run can stop at
    any state for several different reasons, and the report must say which."""

    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    """A deliberate, resumable stop (DNS gate, drift gate). NOT a failure."""
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    NEEDS_RESUME = "needs_resume"
    ROLLBACK_FAILED = "rollback_failed"


class FinalizePolicy(StrEnum):
    """What happens to the source once the target is verified healthy."""

    KEEP = "keep"
    """Leave the source stopped and untouched. Safest; you clean up by hand."""

    RENAME = "rename"
    """Rename to ``{name}-old-{stamp}`` and leave stopped. The default."""

    DELETE = "delete"
    """Delete the source and its volumes. IRREVERSIBLE — typed confirmation."""


class Compensation(StrEnum):
    """Undo actions, named so the journal is readable by a human at 3am.

    Shared by F1 and F2. StrEnum members hash equal to their string value, which
    is what lets one Saga drive two different state machines without generics.
    """

    # ── F1 (project migration) ───────────────────────────────────────────────
    DELETE_TARGET_RESOURCE = "delete_target_resource"
    DROP_TARGET_VOLUMES = "drop_target_volumes"
    STOP_TARGET = "stop_target"
    RESTART_SOURCE = "restart_source"
    REVOKE_EPHEMERAL_KEY = "revoke_ephemeral_key"
    RESTORE_SOURCE_NAME = "restore_source_name"
    RESTORE_SOURCE_FQDN = "restore_source_fqdn"

    # ── F2 (instance migration) ──────────────────────────────────────────────
    START_SOURCE_DOCKER = "start_source_docker"
    """The one that ends the outage. F2 stops the whole box."""
    UNFENCE_SOURCE = "unfence_source"
    WIPE_TARGET_DATA = "wipe_target_data"


#: Execution order. ``DONE`` is terminal and has no successor.
ORDER: tuple[State, ...] = (
    State.INIT,
    State.PREFLIGHT,
    State.PLAN,
    State.CREATE_TARGET,
    State.QUIESCE,
    State.DISCOVER,
    State.COPY,
    State.VERIFY,
    State.DNS_GATE,
    State.START_TARGET,
    State.HEALTHCHECK,
    State.FINALIZE,
    State.DONE,
)

#: What each state must undo if the run is rolled back after it completed.
#: A state absent from this map completed without side effects (PREFLIGHT, PLAN,
#: DISCOVER, VERIFY and DNS_GATE only read).
_COMPENSATION: dict[State, tuple[Compensation, ...]] = {
    State.CREATE_TARGET: (Compensation.DELETE_TARGET_RESOURCE,),
    State.QUIESCE: (Compensation.RESTART_SOURCE,),
    State.COPY: (Compensation.DROP_TARGET_VOLUMES, Compensation.REVOKE_EPHEMERAL_KEY),
    State.START_TARGET: (Compensation.STOP_TARGET,),
    State.FINALIZE: (Compensation.RESTORE_SOURCE_NAME, Compensation.RESTORE_SOURCE_FQDN),
}

#: States after which the source is no longer running. Used to decide whether a
#: rollback must restart it.
_STOPS_SOURCE = State.QUIESCE

#: The only state that cannot be undone. Reached only after an explicit typed
#: confirmation, and only with FinalizePolicy.DELETE.
_IRREVERSIBLE = State.FINALIZE


class RollbackStep(BaseModel):
    """One compensating action to run, with the state that necessitated it.

    ``because_of`` is a plain string rather than :class:`State` so the same
    machinery serves F2's own state machine (see ``server/statemachine.py``).
    """

    model_config = ConfigDict(frozen=True)

    compensation: Compensation
    because_of: str


def next_state(current: State) -> State:
    """The successor of ``current``.

    Raises:
        ValueError: If ``current`` is terminal.
    """
    idx = ORDER.index(current)
    if idx + 1 >= len(ORDER):
        raise ValueError(f"{current} is terminal")
    return ORDER[idx + 1]


def is_terminal(state: State) -> bool:
    return state is State.DONE


def states_through(state: State) -> tuple[State, ...]:
    """Every state from INIT up to and including ``state``."""
    return ORDER[: ORDER.index(state) + 1]


def has_side_effects(state: State) -> bool:
    """True if completing this state mutated anything outside our process."""
    return state in _COMPENSATION


def compensations_for(state: State) -> tuple[Compensation, ...]:
    """The undo actions for a single completed state."""
    return _COMPENSATION.get(state, ())


def rollback_plan_for(
    completed: Sequence[str],
    *,
    order: Sequence[str],
    compensation_map: Mapping[str, tuple[Compensation, ...]],
) -> tuple[RollbackStep, ...]:
    """Compensations for a set of completed states, in reverse completion order.

    Generic over the state machine so F1 and F2 share it.

    Reverse order is not cosmetic: the target resource must be stopped before its
    volumes are dropped, and its volumes must be dropped before the resource is
    deleted, or we leak volumes that nothing references.

    Deliberately tolerant of duplicates and of unordered input — a journal
    replayed after a crash may contain either.

    Args:
        completed: State values that finished successfully.
        order: The machine's execution order.
        compensation_map: State value -> its undo actions.

    Returns:
        Steps to execute in order. Empty if nothing had side effects.
    """
    seen = {str(s) for s in completed if str(s) in compensation_map}
    steps: list[RollbackStep] = []
    for state in reversed(list(order)):
        key = str(state)
        if key not in seen:
            continue
        for comp in compensation_map[key]:
            steps.append(RollbackStep(compensation=comp, because_of=key))
    return tuple(steps)


def rollback_plan(completed: Sequence[State]) -> tuple[RollbackStep, ...]:
    """F1's rollback plan. See :func:`rollback_plan_for`."""
    return rollback_plan_for(
        [s.value for s in completed],
        order=[s.value for s in ORDER],
        compensation_map={k.value: v for k, v in _COMPENSATION.items()},
    )


def requires_source_restart(completed: Sequence[State]) -> bool:
    """True if a rollback must restart the source because we stopped it."""
    return _STOPS_SOURCE in set(completed)


def is_irreversible(state: State) -> bool:
    """True if completing this state cannot be undone."""
    return state is _IRREVERSIBLE


def can_resume_from(state: State) -> bool:
    """True if a run stopped at ``state`` can be resumed rather than restarted.

    Everything up to and including ``DNS_GATE`` is resumable: the expensive work
    (the copy) is already done and the source is still intact. The DNS gate in
    particular is *designed* to be resumed — you flip DNS, then continue.
    """
    return state in ORDER and not is_terminal(state)


def finalize_is_destructive(policy: FinalizePolicy) -> bool:
    """True if this policy destroys the source. Gates the typed confirmation."""
    return policy is FinalizePolicy.DELETE
