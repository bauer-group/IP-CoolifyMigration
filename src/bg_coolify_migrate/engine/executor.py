"""The saga executor.

Walks a state machine, journals every transition, and on failure runs the
compensating actions in reverse order. The *decisions* are pure
(:mod:`bg_coolify_migrate.domain.statemachine`); this module only performs them.

Generic over the machine: ``order`` and ``compensation_map`` default to F1's but
are parameters, so F2's whole-instance migration reuses this executor rather than
duplicating it. Keys are compared as strings — ``StrEnum`` members hash equal to
their value, so callers pass enum members and everything lines up.

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

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from bg_coolify_migrate.domain.statemachine import _COMPENSATION as F1_COMPENSATION
from bg_coolify_migrate.domain.statemachine import (
    ORDER,
    Compensation,
    Outcome,
    rollback_plan_for,
)
from bg_coolify_migrate.errors import (
    DnsGateBlocked,
    MigrationError,
    RebuildDriftBlocked,
    RollbackError,
)
from bg_coolify_migrate.journal.store import Journal

log = structlog.get_logger(__name__)


def _states_from_journal(journal: Journal, known: set[str]) -> list[str]:
    """Completed states from a journal, tolerating unknown names.

    A journal written by a different version may name a state we no longer have.
    Skipping it is correct: we cannot compensate a step we do not understand, and
    guessing would be worse than declining. The unknown name is logged so the
    operator can see what we ignored rather than discovering it by its absence.
    """
    out: list[str] = []
    for name in journal.completed_states():
        if name in known:
            out.append(name)
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
    reached: Any
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

    Steps and compensations are injected rather than hardcoded, so the executor
    itself is testable exhaustively — including the chaos cases — without a
    Coolify instance, an SSH server, or a Docker daemon.
    """

    def __init__(
        self,
        *,
        journal: Journal,
        context: Any,
        steps: Mapping[Any, StepFn],
        compensations: Mapping[Any, CompensationFn],
        on_state: Callable[[Any], Awaitable[None]] | None = None,
        order: Sequence[Any] | None = None,
        compensation_map: Mapping[Any, tuple[Compensation, ...]] | None = None,
    ) -> None:
        self.journal = journal
        self.context = context
        self.steps = {str(k): v for k, v in steps.items()}
        self.compensations = {str(k): v for k, v in compensations.items()}
        self.on_state = on_state
        self._order: list[Any] = list(order) if order is not None else list(ORDER)
        raw_map = compensation_map if compensation_map is not None else F1_COMPENSATION
        self._compensation_map = {str(k): v for k, v in raw_map.items()}
        self._keys = [str(s) for s in self._order]
        self._terminal = self._keys[-1]
        self._completed: list[str] = []

    async def run(self, *, start_from: Any = None) -> RunResult:
        """Walk the state machine from ``start_from``.

        Args:
            start_from: Where to begin; defaults to the first state. ``resume``
                relies on the journal instead — completed states are skipped.
        """
        self._completed = _states_from_journal(self.journal, set(self._keys))
        begin = str(start_from) if start_from is not None else self._keys[0]
        self.journal.append("started", state=begin)
        begin_idx = self._keys.index(begin)

        for idx, state in enumerate(self._order):
            key = self._keys[idx]
            if idx < begin_idx:
                continue
            if key == self._terminal:
                break
            if key in self._completed:
                log.debug("saga.skip", state=key, reason="already completed")
                continue

            step = self.steps.get(key)
            if step is None:
                self._completed.append(key)
                continue

            if self.on_state is not None:
                await self.on_state(state)

            self.journal.append("step_started", state=key)
            log.info("saga.step", state=key)

            try:
                undo_info = await step(self.context)
            except (DnsGateBlocked, RebuildDriftBlocked) as exc:
                # A gate is a deliberate, resumable stop — NOT a failure. Nothing
                # is rolled back: the target is created and its data verified,
                # and the operator resumes after fixing the world.
                self.journal.append("blocked", state=key, detail={"reason": str(exc)[:500]})
                log.warning("saga.blocked", state=key)
                return RunResult(outcome=Outcome.BLOCKED, reached=state, error=exc)
            except MigrationError as exc:
                self.journal.append("step_failed", state=key, detail={"error": str(exc)[:500]})
                log.error("saga.step_failed", state=key, error=str(exc)[:200])
                return await self._rollback(state, exc)
            except Exception as exc:  # an unexpected error still needs compensation
                wrapped = MigrationError(f"unexpected error in {key}: {exc}")
                self.journal.append("step_failed", state=key, detail={"error": str(exc)[:500]})
                log.exception("saga.step_crashed", state=key)
                return await self._rollback(state, wrapped)

            self.journal.append("step_completed", state=key, detail=undo_info or {})
            self._completed.append(key)

        self.journal.append("finished")
        log.info("saga.finished")
        return RunResult(outcome=Outcome.SUCCEEDED, reached=self._order[-1])

    async def rollback(self) -> RunResult:
        """Roll back a previously-failed or blocked run."""
        self._completed = _states_from_journal(self.journal, set(self._keys))
        return await self._rollback(self._order[0], None)

    async def _rollback(self, failed_at: Any, error: MigrationError | None) -> RunResult:
        plan = rollback_plan_for(
            self._completed,
            order=self._keys,
            compensation_map=self._compensation_map,
        )
        if not plan:
            self.journal.append("rolled_back", detail={"steps": 0})
            return RunResult(
                outcome=Outcome.FAILED if error else Outcome.ROLLED_BACK,
                reached=failed_at,
                error=error,
            )

        self.journal.append("rollback_started", detail={"steps": len(plan)})
        log.warning("saga.rollback", steps=len(plan), failed_at=str(failed_at))

        ran: list[Compensation] = []
        failed: list[tuple[Compensation, str]] = []

        for step in plan:
            fn = self.compensations.get(str(step.compensation))
            if fn is None:
                log.debug("saga.compensation.missing", compensation=step.compensation.value)
                continue
            undo_info = self.journal.undo_info(step.because_of)
            try:
                await fn(self.context, undo_info)
            except Exception as exc:  # keep going; the rest still matters
                # A failure to delete the target must not also prevent restarting
                # the source. Record and continue; report honestly at the end.
                failed.append((step.compensation, str(exc)[:300]))
                self.journal.append(
                    "rollback_step",
                    state=step.because_of,
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
                state=step.because_of,
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
