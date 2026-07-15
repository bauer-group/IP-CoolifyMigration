"""The saga executor.

Walks the state machine, journals every transition, and on failure runs the
compensating actions in reverse order. The *decisions* are pure (
:mod:`bg_coolify_migrate.domain.statemachine`); this module only performs them.

Compensation philosophy
-----------------------
Undo is **best-effort but loud**. Each compensation is attempted; a failure is
recorded and the remaining compensations still run, because a failure to delete
the target should not also prevent restarting the source. If any compensation
failed we raise :class:`RollbackError` at the end carrying the journal path —
the operator now has a half-state only they can adjudicate, and pretending
otherwise would be the same lie coolify-mover tells when it warns and continues.

We never attempt to compensate a compensation. That way lies unbounded guessing
about a system we have already lost track of.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from bg_coolify_migrate.domain.statemachine import (
    ORDER,
    Compensation,
    Outcome,
    State,
    rollback_plan,
)
from bg_coolify_migrate.errors import (
    DnsGateBlocked,
    MigrationError,
    RebuildDriftBlocked,
    RollbackError,
)
from bg_coolify_migrate.journal.store import Journal

log = structlog.get_logger(__name__)


def _states_from_journal(journal: Journal) -> list[State]:
    """Completed states from a journal, tolerating unknown names.

    A journal written by a different version may name a state we no longer have.
    Skipping it is correct: we cannot compensate a step we do not understand, and
    guessing would be worse than declining. The unknown name is logged so the
    operator can see what we ignored rather than discovering it by its absence.
    """
    known = {s.value for s in State}
    out: list[State] = []
    for name in journal.completed_states():
        if name in known:
            out.append(State(name))
        else:
            log.warning("saga.unknown_state_in_journal", state=name, path=str(journal.path))
    return out


class StepFn(Protocol):
    """One state's work. Returns undo info to journal."""

    async def __call__(self, ctx: Any) -> dict[str, Any]: ...


class CompensationFn(Protocol):
    """One undo action."""

    async def __call__(self, ctx: Any, undo_info: dict[str, Any]) -> None: ...


@dataclass
class RunResult:
    """What happened."""

    outcome: Outcome
    reached: State
    error: MigrationError | None = None
    compensations_run: list[Compensation] = field(default_factory=list)
    compensations_failed: list[tuple[Compensation, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCEEDED

    @property
    def exit_code(self) -> int:
        if self.outcome is Outcome.SUCCEEDED:
            return 0
        if self.error is not None:
            return self.error.exit_code
        if self.outcome is Outcome.ROLLBACK_FAILED:
            return 8
        return 1


class Saga:
    """Executes one migration.

    Steps and compensations are injected rather than hardcoded so that the
    executor itself can be tested exhaustively — including the chaos cases —
    without a Coolify instance, an SSH server, or a Docker daemon.
    """

    def __init__(
        self,
        *,
        journal: Journal,
        context: Any,
        steps: dict[State, StepFn],
        compensations: dict[Compensation, CompensationFn],
        on_state: Callable[[State], Awaitable[None]] | None = None,
    ) -> None:
        self.journal = journal
        self.context = context
        self.steps = steps
        self.compensations = compensations
        self.on_state = on_state
        self._completed: list[State] = []

    async def run(self, *, start_from: State = State.INIT) -> RunResult:
        """Walk the state machine from ``start_from``.

        Args:
            start_from: Where to begin. ``resume`` passes the state after the
                last journalled completion.
        """
        self._completed = _states_from_journal(self.journal)
        self.journal.append("started", state=start_from.value)

        for state in ORDER:
            if ORDER.index(state) < ORDER.index(start_from):
                continue
            if state is State.DONE:
                break
            if state in self._completed:
                log.debug("saga.skip", state=state.value, reason="already completed")
                continue

            step = self.steps.get(state)
            if step is None:
                self._completed.append(state)
                continue

            if self.on_state is not None:
                await self.on_state(state)

            self.journal.append("step_started", state=state.value)
            log.info("saga.step", state=state.value)

            try:
                undo_info = await step(self.context)
            except (DnsGateBlocked, RebuildDriftBlocked) as exc:
                # A gate is a deliberate, resumable stop — NOT a failure. Nothing
                # is rolled back: the target is created and its data verified,
                # and the operator resumes after fixing the world.
                self.journal.append("blocked", state=state.value, detail={"reason": str(exc)[:500]})
                log.warning("saga.blocked", state=state.value)
                return RunResult(outcome=Outcome.BLOCKED, reached=state, error=exc)
            except MigrationError as exc:
                self.journal.append(
                    "step_failed", state=state.value, detail={"error": str(exc)[:500]}
                )
                log.error("saga.step_failed", state=state.value, error=str(exc)[:200])
                return await self._rollback(state, exc)
            except Exception as exc:
                wrapped = MigrationError(f"unexpected error in {state.value}: {exc}")
                self.journal.append(
                    "step_failed", state=state.value, detail={"error": str(exc)[:500]}
                )
                log.exception("saga.step_crashed", state=state.value)
                return await self._rollback(state, wrapped)

            self.journal.append("step_completed", state=state.value, detail=undo_info or {})
            self._completed.append(state)

        self.journal.append("finished")
        log.info("saga.finished")
        return RunResult(outcome=Outcome.SUCCEEDED, reached=State.DONE)

    async def rollback(self) -> RunResult:
        """Roll back a previously-failed or blocked run."""
        self._completed = _states_from_journal(self.journal)
        return await self._rollback(State.INIT, None)

    async def _rollback(self, failed_at: State, error: MigrationError | None) -> RunResult:
        plan = rollback_plan(self._completed)
        if not plan:
            self.journal.append("rolled_back", detail={"steps": 0})
            return RunResult(
                outcome=Outcome.FAILED if error else Outcome.ROLLED_BACK,
                reached=failed_at,
                error=error,
            )

        self.journal.append("rollback_started", detail={"steps": len(plan)})
        log.warning("saga.rollback", steps=len(plan), failed_at=failed_at.value)

        ran: list[Compensation] = []
        failed: list[tuple[Compensation, str]] = []

        for step in plan:
            fn = self.compensations.get(step.compensation)
            if fn is None:
                log.debug("saga.compensation.missing", compensation=step.compensation.value)
                continue
            undo_info = self.journal.undo_info(step.because_of.value)
            try:
                await fn(self.context, undo_info)
            except Exception as exc:
                # A failure to delete the target must not also prevent restarting
                # the source. Record and continue; report honestly at the end.
                failed.append((step.compensation, str(exc)[:300]))
                self.journal.append(
                    "rollback_step",
                    state=step.because_of.value,
                    detail={"compensation": step.compensation.value, "ok": False},
                )
                log.error(
                    "saga.compensation.failed",
                    compensation=step.compensation.value,
                    error=str(exc)[:200],
                )
                continue

            ran.append(step.compensation)
            self.journal.append(
                "rollback_step",
                state=step.because_of.value,
                detail={"compensation": step.compensation.value, "ok": True},
            )
            log.info("saga.compensated", compensation=step.compensation.value)

        self.journal.append("rolled_back", detail={"ok": not failed})

        if failed:
            names = ", ".join(c.value for c, _ in failed)
            return RunResult(
                outcome=Outcome.ROLLBACK_FAILED,
                reached=failed_at,
                error=RollbackError(
                    f"rollback incomplete: {names} failed",
                    hint=(
                        f"The journal is at {self.journal.path}. Some compensating actions "
                        "could not run, so the system is in a state only you can adjudicate. "
                        "The source has NOT been deleted — that only happens at finalize."
                    ),
                ),
                compensations_run=ran,
                compensations_failed=failed,
            )

        return RunResult(
            outcome=Outcome.ROLLED_BACK,
            reached=failed_at,
            error=error,
            compensations_run=ran,
        )
