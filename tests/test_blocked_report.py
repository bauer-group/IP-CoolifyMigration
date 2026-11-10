"""What a BLOCKED run's report may claim — true for the gate that stopped it.

It said "your source is untouched and still stopped" for every block, without
looking: also for the drift gate at PREFLIGHT, which stops before anything is
created or stopped.
"""

from __future__ import annotations

from rich.console import Console

from bg_coolify_migrate.domain.statemachine import Outcome, State
from bg_coolify_migrate.engine.executor import RunResult
from bg_coolify_migrate.errors import RebuildDriftBlocked
from bg_coolify_migrate.ui.console import THEME
from bg_coolify_migrate.ui.run_report import outcome_panel


def _render(result: RunResult) -> str:
    console = Console(width=200, force_terminal=False, no_color=True, theme=THEME)
    with console.capture() as capture:
        console.print(outcome_panel(result, migration_id="m1"))
    return capture.get()


class TestBlockedReport:
    def test_the_drift_gate_has_changed_nothing(self) -> None:
        result = RunResult(
            outcome=Outcome.BLOCKED, reached=State.PREFLIGHT, error=RebuildDriftBlocked("drift")
        )
        out = _render(result)
        assert "NOT a failure" in out
        assert "Nothing has been created or stopped yet" in out
        assert "still stopped" not in out and "verified" not in out

    def test_the_dns_gate_says_what_it_checked(self) -> None:
        out = _render(RunResult(outcome=Outcome.BLOCKED, reached=State.DNS_GATE))
        assert "data is verified" in out
        assert "this gate checked that nothing had started it" in out
        assert "found when you resume" in out
        assert "untouched" not in out

    def test_another_gate_claims_nothing(self) -> None:
        out = _render(RunResult(outcome=Outcome.BLOCKED, reached=State.COPY))
        assert "Nothing has been undone" in out
        assert "verified" not in out
