"""Rendering a finished (or stopped) run.

Kept apart from :mod:`.report`, which renders *plans*. A run's report has a
different job: it must tell an operator, in one screen, whether their data is
safe and what to do next.

The three outcomes that are not "success" get equal billing, because two of them
are not failures at all — a gate is a deliberate stop, and pretending otherwise
trains people to reach for `--force` flags that do not exist.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bg_coolify_migrate.domain.statemachine import Outcome, State
from bg_coolify_migrate.engine.executor import RunResult
from bg_coolify_migrate.errors import SourceRevivedAfterStart
from bg_coolify_migrate.ui.console import human_duration

_OUTCOME_STYLE = {
    Outcome.SUCCEEDED: "ok",
    Outcome.BLOCKED: "gate",
    Outcome.FAILED: "err",
    Outcome.ROLLED_BACK: "warn",
    Outcome.NEEDS_RESUME: "warn",
    Outcome.ROLLBACK_FAILED: "err",
}

_OUTCOME_TITLE = {
    Outcome.SUCCEEDED: "Migration complete",
    Outcome.BLOCKED: "Stopped at a gate - nothing is broken",
    Outcome.FAILED: "Migration failed",
    Outcome.ROLLED_BACK: "Migration failed and was rolled back",
    Outcome.NEEDS_RESUME: "Migration interrupted",
    Outcome.ROLLBACK_FAILED: "Rollback incomplete - needs your attention",
}


def outcome_panel(result: RunResult, *, migration_id: str, elapsed: float | None = None) -> Panel:
    lines: list[RenderableType] = []

    if result.error is not None:
        lines.append(Text(str(result.error)))
        lines.append(Text(""))

    if result.compensations_run:
        lines.append(Text("Undone:", style="bold"))
        lines.extend(
            Text(f"  - {c.value.replace('_', ' ')}") for c in result.compensations_run
        )
        lines.append(Text(""))

    if result.compensations_failed:
        lines.append(Text("COULD NOT UNDO:", style="bold err"))
        for compensation, error in result.compensations_failed:
            lines.append(Text(f"  - {compensation.value}: {error}", style="err"))
        lines.append(Text(""))

    if result.notices:
        lines.append(Text("Left in place:", style="bold warn"))
        lines.extend(Text(f"  - {notice}", style="warn") for notice in result.notices)
        lines.append(Text(""))
    if result.notes:
        lines.append(Text("Still yours to do:", style="bold warn"))
        lines.extend(Text(f"  - {note}", style="warn") for note in result.notes)
        lines.append(Text(""))

    lines.append(Text(_next_step(result, migration_id), style="muted"))

    if elapsed is not None:
        lines.append(Text(f"\nElapsed: {human_duration(elapsed)}", style="muted"))

    return Panel(
        Group(*lines),
        title=(
            "Stopped - the source was started again, and needs your decision"
            if isinstance(result.error, SourceRevivedAfterStart)
            else _OUTCOME_TITLE[result.outcome]
        ),
        border_style=_OUTCOME_STYLE[result.outcome],
        title_align="left",
    )


def _next_step(result: RunResult, migration_id: str) -> str:
    """What to actually do now. Every path ends with a command or a reassurance."""
    if result.outcome is Outcome.SUCCEEDED:
        return f"Migration id: {migration_id}\nRollback is still possible: coolify-migrate rollback {migration_id}"

    if result.outcome is Outcome.BLOCKED:
        return (
            f"This is NOT a failure. {_where_blocked(result)}\n\n"
            f"Fix the condition above, then: coolify-migrate resume {migration_id}\n"
            f"Or abandon it:               coolify-migrate rollback {migration_id}"
        )

    if result.outcome is Outcome.ROLLED_BACK:
        # A restart that FAILED is never here: RESTART_SOURCE then fails and the
        # outcome is ROLLBACK_FAILED. A notice is what is left: a restart whose
        # deployment had not finished, a pin kept because the source did not
        # mount its volumes in time - neither proves the source runs.
        if result.notices:
            return (
                "Everything we changed has been undone, but read what was left in place\n"
                "above: it says what may not be back yet.\n"
                "Your data was never at risk: the source is only destroyed at finalize."
            )
        return (
            "Everything we changed has been undone and your source is running again.\n"
            "Your data was never at risk: the source is only destroyed at finalize."
        )

    if result.outcome is Outcome.ROLLBACK_FAILED:
        return (
            "Some compensating actions could not run, so the system is in a state\n"
            "only you can adjudicate.\n\n"
            "Your SOURCE HAS NOT BEEN DELETED - that only happens at an explicit,\n"
            f"confirmed finalize. Inspect: coolify-migrate status {migration_id}"
        )

    return (
        f"Nothing was changed, or it has been undone.\n"
        f"Inspect: coolify-migrate status {migration_id}"
    )


def _where_blocked(result: RunResult) -> str:
    """What a gate's stop has and has not touched — true for the gate that stopped it.

    Two gates block. The drift gate stops at PREFLIGHT, before anything is
    created or stopped. The DNS gate stops after VERIFY with the source stopped,
    and it checks first that nothing restarted the source or started the target
    (``engine/containment.py``) — the claim it may make is that one, as of that
    check: nothing guards either while the run waits. And a source found running
    again once START asked for a target blocks too: the target is up, the
    source is NOT stopped, and the error above says the two ways on.
    """
    if isinstance(result.error, SourceRevivedAfterStart):
        return (
            "Nothing has been undone: the target is started and keeps what it\n"
            "received. Your source is NOT stopped - something started it again (above).\n"
            "Stop it in Coolify and resume to finish, or roll back to give up the target."
        )
    reached = str(result.reached)
    if reached == State.PREFLIGHT.value:
        return (
            "Nothing has been created or stopped yet:\n"
            "your source runs exactly as it did."
        )
    if reached == State.DNS_GATE.value:
        return (
            "The target is created and its data is verified.\n"
            "Your source is stopped: this gate checked that nothing had started it, or the\n"
            "target, since the stop. Nothing watches either while the run waits - a deploy\n"
            "of either meanwhile (a git push with auto-deploy on, a CI deploy, the dashboard)\n"
            "is found when you resume, and the run then rolls back."
        )
    return "Nothing has been undone: the run stopped where it says above."


def verification_table(result_context: object) -> Table | None:
    """Summarise what was proven identical.

    Rendered on success because "it worked" is worth substantiating: this is the
    difference between this tool and its predecessors, which verify nothing and
    call an ssh exit code 0 a success.
    """
    verifications = getattr(result_context, "verifications", None)
    if not verifications:
        return None

    table = Table(title="Verification", title_justify="left")
    table.add_column("Volume", style="path")
    table.add_column("Files", justify="right")
    table.add_column("Entries", justify="right")
    table.add_column("Result")

    for reports in verifications.values():
        for report in reports:
            table.add_row(
                report.target_path,
                str(report.source.file_count),
                str(report.source.entry_count),
                Text("identical", style="ok") if report.ok else Text("DIFFERS", style="err"),
            )
    return table


def plain_result(result: RunResult, *, migration_id: str) -> str:
    """Line-oriented outcome for pipes and CI."""
    lines = [
        f"migration: {migration_id}",
        f"outcome: {result.outcome.value}",
        f"reached: {result.reached.value}",
        f"exit_code: {result.exit_code}",
    ]
    if result.error is not None:
        lines.append(f"error: {str(result.error).splitlines()[0]}")
    for compensation in result.compensations_run:
        lines.append(f"undone: {compensation.value}")
    for compensation, error in result.compensations_failed:
        lines.append(f"undo_failed: {compensation.value}: {error}")
    lines.extend(f"notice: {notice}" for notice in result.notices)
    lines.extend(f"note: {note}" for note in result.notes)
    return "\n".join(lines)
