"""CLI tests.

Exit codes are a documented contract that callers script against, so they are
asserted explicitly rather than just "did it fail".
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from bg_coolify_migrate import __version__
from bg_coolify_migrate.cli import app
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.observability.logging_setup import reset_logging
from bg_coolify_migrate.settings.base import reset_settings_cache

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reset_logging()
    reset_settings_cache()
    # Never read the developer's real .env or state during tests.
    monkeypatch.chdir(tmp_path)
    for var in ("COOLIFY_URL", "COOLIFY_TOKEN", "STATE_DIR"):
        monkeypatch.delenv(var, raising=False)


class TestBasics:
    def test_version(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.stdout

    def test_help_lists_every_command(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in ("doctor", "plan", "run", "resume", "rollback", "status", "server"):
            assert command in result.stdout

    def test_no_args_shows_help(self) -> None:
        # no_args_is_help: an operator typing the bare command wants orientation,
        # not an error.
        result = runner.invoke(app, [])
        assert "Usage" in result.stdout

    def test_server_subcommand_help(self) -> None:
        result = runner.invoke(app, ["server", "--help"])
        assert result.exit_code == 0
        assert "plan" in result.stdout
        assert "run" in result.stdout


class TestDoctorExitCodes:
    def test_missing_credentials_exits_2(self) -> None:
        # PreflightError: nothing was changed.
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 2
        # Errors go to stderr, not stdout - that is the contract.
        assert "COOLIFY_URL" in result.stderr

    def test_hint_tells_you_what_to_set(self) -> None:
        result = runner.invoke(app, ["doctor"])
        assert "read:sensitive" in result.stderr

    @respx.mock
    def test_insufficient_scope_exits_10(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # THE trap: HTTP 200 with the secret keys silently absent.
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "readonly-token")
        respx.get(f"{BASE}/version").mock(
            return_value=httpx.Response(200, json={"version": "4.0.0"})
        )
        respx.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"uuid": "k1", "name": "default"}])
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 10
        assert "CANNOT read sensitive" in result.stdout

    @respx.mock
    def test_healthy_instance_exits_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "root-token")
        respx.get(f"{BASE}/version").mock(
            return_value=httpx.Response(200, json={"version": "4.0.0"})
        )
        respx.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"uuid": "k1", "private_key": "-----BEGIN"}])
        )
        respx.get(f"{BASE}/servers").mock(
            return_value=httpx.Response(
                200,
                json=[{"uuid": "s1", "name": "prod-1", "ip": "10.0.0.1", "is_reachable": True}],
            )
        )
        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "prod-1" in result.stdout
        assert "can read sensitive data" in result.stdout

    @respx.mock
    def test_api_error_exits_9(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "bad")
        respx.get(f"{BASE}/version").mock(return_value=httpx.Response(401))
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 9


class TestStatus:
    def test_empty_state_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "no migrations recorded" in result.stdout

    def test_lists_migrations(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        state = tmp_path / "state"
        Journal.create(state, "mig-001").append("started", state="init")
        monkeypatch.setenv("STATE_DIR", str(state))
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "mig-001" in result.stdout

    def test_shows_one_migration_in_detail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"
        journal = Journal.create(state, "mig-001")
        journal.append("started", state="init")
        journal.append("step_completed", state="create_target")
        monkeypatch.setenv("STATE_DIR", str(state))
        result = runner.invoke(app, ["status", "mig-001"])
        assert result.exit_code == 0
        assert "create_target" in result.stdout

    def test_unknown_migration_exits_14(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
        result = runner.invoke(app, ["status", "nope"])
        assert result.exit_code == 14  # JournalError


class TestUnimplementedCommandsAreHonest:
    """They must fail loudly rather than pretend to have done something.

    A command that prints nothing and exits 0 is indistinguishable from a
    successful migration, which is exactly the class of silent lie this whole
    project exists to avoid.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["plan", "shop"],
            ["run", "shop", "--to", "new-host"],
            ["resume", "mig-001"],
            ["rollback", "mig-001"],
            ["server", "plan", "--to", "new-host"],
            ["server", "run", "--to", "new-host"],
        ],
    )
    def test_exits_nonzero(self, argv: list[str]) -> None:
        result = runner.invoke(app, argv)
        assert result.exit_code != 0
        assert "not yet implemented" in result.stdout
