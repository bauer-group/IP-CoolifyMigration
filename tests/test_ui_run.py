"""Tests for run reporting and the progress dashboard.

The outcome messages are load-bearing, not decoration: a gate that reads as a
failure trains operators to look for a `--force` flag that does not exist, and a
rollback that does not say "your source is safe" invites panic.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from bg_coolify_migrate.domain.statemachine import Compensation, Outcome, State
from bg_coolify_migrate.engine.executor import RunResult
from bg_coolify_migrate.errors import DnsGateBlocked, RollbackError, TransferError
from bg_coolify_migrate.server.inventory import ServerInventory
from bg_coolify_migrate.ui import server_report
from bg_coolify_migrate.ui.console import THEME
from bg_coolify_migrate.ui.dashboard import LiveDashboard, PlainReporter, build
from bg_coolify_migrate.ui.run_report import outcome_panel, plain_result


def render(renderable: object) -> str:
    console = Console(width=200, force_terminal=False, no_color=True, theme=THEME)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


class TestOutcomePanel:
    def test_success_offers_rollback(self) -> None:
        result = RunResult(outcome=Outcome.SUCCEEDED, reached=State.DONE)
        out = render(outcome_panel(result, migration_id="m1"))
        assert "Migration complete" in out
        assert "rollback m1" in out

    def test_blocked_is_not_framed_as_a_failure(self) -> None:
        # It is a deliberate, resumable stop. Framing it as a failure is how you
        # get people hunting for --force.
        result = RunResult(
            outcome=Outcome.BLOCKED, reached=State.DNS_GATE, error=DnsGateBlocked("dns")
        )
        out = render(outcome_panel(result, migration_id="m1"))
        assert "nothing is broken" in out
        assert "NOT a failure" in out
        assert "resume m1" in out

    def test_rolled_back_reassures_about_the_data(self) -> None:
        result = RunResult(
            outcome=Outcome.ROLLED_BACK,
            reached=State.COPY,
            error=TransferError("boom"),
            compensations_run=[Compensation.RESTART_SOURCE],
        )
        out = render(outcome_panel(result, migration_id="m1"))
        assert "running again" in out
        assert "never at risk" in out
        assert "restart source" in out

    def test_rollback_failed_says_the_source_survives(self) -> None:
        # The single most important sentence in the worst case.
        result = RunResult(
            outcome=Outcome.ROLLBACK_FAILED,
            reached=State.COPY,
            error=RollbackError("could not undo"),
            compensations_failed=[(Compensation.DELETE_TARGET_RESOURCE, "api down")],
        )
        out = render(outcome_panel(result, migration_id="m1"))
        assert "needs your attention" in out
        assert "HAS NOT BEEN DELETED" in out
        assert "COULD NOT UNDO" in out

    def test_elapsed_is_shown(self) -> None:
        result = RunResult(outcome=Outcome.SUCCEEDED, reached=State.DONE)
        assert "1m 30s" in render(outcome_panel(result, migration_id="m1", elapsed=90))


class TestPlainResult:
    def test_is_greppable(self) -> None:
        result = RunResult(outcome=Outcome.SUCCEEDED, reached=State.DONE)
        out = plain_result(result, migration_id="m1")
        assert "migration: m1" in out
        assert "outcome: succeeded" in out
        assert "exit_code: 0" in out

    def test_includes_the_error_and_compensations(self) -> None:
        result = RunResult(
            outcome=Outcome.ROLLED_BACK,
            reached=State.COPY,
            error=TransferError("rsync exploded"),
            compensations_run=[Compensation.RESTART_SOURCE],
            compensations_failed=[(Compensation.DROP_TARGET_VOLUMES, "in use")],
        )
        out = plain_result(result, migration_id="m1")
        assert "error: rsync exploded" in out
        assert "undone: restart_source" in out
        assert "undo_failed: drop_target_volumes: in use" in out


class TestPlainReporter:
    async def test_reports_each_step(self, capsys: pytest.CaptureFixture[str]) -> None:
        with PlainReporter() as reporter:
            await reporter.on_state(State.PREFLIGHT)
            await reporter.on_state(State.COPY)
        out = capsys.readouterr().out
        assert "step: preflight started" in out
        assert "step: preflight done" in out
        assert "step: copy started" in out

    async def test_closes_the_last_step_on_exit(self, capsys: pytest.CaptureFixture[str]) -> None:
        with PlainReporter() as reporter:
            await reporter.on_state(State.COPY)
        assert "step: copy done" in capsys.readouterr().out


class TestLiveDashboard:
    async def test_renders_every_step(self) -> None:
        board = LiveDashboard(title="shop/production")
        await board.on_state(State.PREFLIGHT)
        out = render(board._render())
        assert "shop/production" in out
        assert "Preflight checks" in out
        assert "Copy data" in out

    async def test_marks_completed_steps(self) -> None:
        board = LiveDashboard()
        await board.on_state(State.INIT)
        await board.on_state(State.PREFLIGHT)
        out = render(board._render())
        assert "+" in out  # INIT completed

    async def test_marks_the_failed_step(self) -> None:
        board = LiveDashboard()
        with pytest.raises(RuntimeError), board:
            await board.on_state(State.COPY)
            raise RuntimeError("boom")
        assert "failed" in render(board._render())


class TestReporterSelection:
    def test_non_tty_gets_the_plain_reporter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Live display in CI emits megabytes of escape codes.
        monkeypatch.setenv("CI", "1")
        assert isinstance(build(), PlainReporter)

    def test_force_plain(self) -> None:
        assert isinstance(build(force_plain=True), PlainReporter)


class TestServerReport:
    def _inventory(self, **kw: object) -> ServerInventory:
        base = {
            "source_host": "10.0.0.1",
            "target_host": "10.0.0.2",
            "coolify_version": "4.0.0",
            "volumes": ("v1", "v2"),
            "coolify_data_bytes": 1024**3,
            "volumes_bytes": 5 * 1024**3,
            "target_free_bytes": 100 * 1024**3,
            "app_key_fingerprint": "sha256:abc123",
        }
        return ServerInventory(**{**base, **kw})  # type: ignore[arg-type]

    def test_panel_shows_the_fingerprint_not_the_key(self) -> None:
        out = render(server_report.inventory_panel(self._inventory()))
        assert "sha256:abc123" in out
        assert "6.0 GB" in out

    def test_panel_flags_a_missing_app_key(self) -> None:
        out = render(server_report.inventory_panel(self._inventory(app_key_fingerprint="")))
        assert "not found" in out

    def test_unattached_volumes_are_surfaced(self) -> None:
        # Geczy silently skips these; we say we are taking them.
        out = render(server_report.inventory_table(self._inventory(unattached_volumes=("orphan",))))
        assert "orphan" in out
        # Rich wraps a title to the table's width, so compare on the collapsed text.
        assert "no container attached" in " ".join(out.split())

    def test_bind_mounts_are_surfaced(self) -> None:
        out = render(server_report.inventory_table(self._inventory(bind_mounts=("/srv/data",))))
        assert "/srv/data" in out

    def test_blocking_panel(self) -> None:
        out = render(
            server_report.blocking_panel(self._inventory(blocking_reasons=("not enough disk",)))
        )
        assert "not enough disk" in out
        assert "nothing has been changed" in out

    def test_plain_inventory_is_greppable(self) -> None:
        out = server_report.plain_inventory(self._inventory())
        assert "source: 10.0.0.1" in out
        assert "app_key: sha256:abc123" in out
        assert "blocked: False" in out
