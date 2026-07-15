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

from bg_coolify_migrate.domain.statemachine import Outcome
from bg_coolify_migrate.engine.executor import RunResult
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

    lines.append(Text(_next_step(result, migration_id), style="muted"))

    if elapsed is not None:
        lines.append(Text(f"\nElapsed: {human_duration(elapsed)}", style="muted"))

    return Panel(
        Group(*lines),
        title=_OUTCOME_TITLE[result.outcome],
        border_style=_OUTCOME_STYLE[result.outcome],
        title_align="left",
    )


def _next_step(result: RunResult, migration_id: str) -> str:
    """What to actually do now. Every path ends with a command or a reassurance."""
    if result.outcome is Outcome.SUCCEEDED:
        return f"Migration id: {migration_id}\nRollback is still possible: coolify-migrate rollback {migration_id}"

    if result.outcome is Outcome.BLOCKED:
        return (
            "This is NOT a failure. The target is created and its data is verified;\n"
            "your source is untouched and still stopped.\n\n"
            f"Fix the condition above, then: coolify-migrate resume {migration_id}\n"
            f"Or abandon it:               coolify-migrate rollback {migration_id}"
        )

    if result.outcome is Outcome.ROLLED_BACK:
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
    return "\n".join(lines)
