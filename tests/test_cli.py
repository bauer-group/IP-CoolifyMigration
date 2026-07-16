"""CLI tests.

Exit codes are a documented contract that callers script against, so they are
asserted explicitly rather than just "did it fail".
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from bg_coolify_migrate import __version__
from bg_coolify_migrate.cli import Selection, _parse_selector, app
from bg_coolify_migrate.errors import MigrationError
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

    def test_error_output_tolerates_brackets_in_a_name(self) -> None:
        # A resource named `api [v2]` reaches the error path as `no resource named
        # 'api [v2]'`; the '[v2]' must not itself throw a Rich MarkupError.
        import typer

        from bg_coolify_migrate.cli import _fail
        from bg_coolify_migrate.errors import PreflightError

        with pytest.raises(typer.Exit):
            _fail(PreflightError("no resource named 'api [v2]'"))

    @pytest.mark.parametrize("command", ["resume", "rollback", "server"])
    def test_bare_leaf_command_shows_help_not_a_terse_error(self, command: str) -> None:
        # A bare command used to answer "Missing argument '…'." — orientation for an
        # operator who does not yet know the arguments should be the help. (plan/run
        # are excluded: with no selector on a TTY they open the interactive picker.)
        result = runner.invoke(app, [command])
        assert "Usage" in result.stdout
        assert "Missing argument" not in result.stdout

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


class TestList:
    """`list` answers "what can I migrate, and from where" — the discovery step
    that `doctor` (servers only) and `status` (migrations only) never covered."""

    def _mock_instance(self) -> None:
        respx.get(f"{BASE}/servers").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"uuid": "s1", "name": "prod-1", "ip": "10.0.0.1", "id": 1},
                    {"uuid": "s2", "name": "spare", "ip": "10.0.0.2", "id": 2},
                ],
            )
        )
        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx.get(f"{BASE}/projects/p1").mock(
            return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
        )
        respx.get(f"{BASE}/projects/p1/production").mock(
            return_value=httpx.Response(
                200, json={"applications": [{"uuid": "a1", "server_uuid": "s1"}]}
            )
        )

    def test_needs_credentials_like_the_others(self) -> None:
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 2
        assert "COOLIFY_URL" in result.stderr

    @respx.mock
    def test_lists_projects_with_their_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        self._mock_instance()
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "shop" in result.stdout
        assert "production" in result.stdout
        assert "prod-1" in result.stdout
        # An empty server is still shown — it is a candidate migration target.
        assert "spare" in result.stdout

    @respx.mock
    def test_json_is_machine_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        self._mock_instance()
        result = runner.invoke(app, ["list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload == [
            {
                "server": "prod-1",
                "server_uuid": "s1",
                "server_ip": "10.0.0.1",
                "project": "shop",
                "project_uuid": "p1",
                "environment": "production",
                "resources": 1,
            }
        ]

    @respx.mock
    def test_server_filter_narrows_to_one_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        self._mock_instance()
        result = runner.invoke(app, ["list", "--server", "prod-1"])
        assert result.exit_code == 0
        assert "shop" in result.stdout
        assert "spare" not in result.stdout

    @respx.mock
    def test_unknown_server_filter_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        self._mock_instance()
        result = runner.invoke(app, ["list", "--server", "does-not-exist"])
        assert result.exit_code == 2
        assert "no server named" in result.stderr


class TestSelector:
    """`project[/environment[/resource]]` - the three scopes, cleanly selectable."""

    def test_project_only_is_the_whole_project(self) -> None:
        assert _parse_selector("shop", None) == Selection("shop", None, None)

    def test_project_environment(self) -> None:
        assert _parse_selector("shop/production", None) == Selection("shop", "production", None)

    def test_project_environment_resource(self) -> None:
        assert _parse_selector("shop/production/web", None) == Selection("shop", "production", "web")

    def test_environment_override_fills_a_bare_project(self) -> None:
        assert _parse_selector("shop", "staging") == Selection("shop", "staging", None)

    def test_environment_given_twice_is_rejected(self) -> None:
        with pytest.raises(MigrationError, match="twice"):
            _parse_selector("shop/production", "staging")

    def test_too_many_segments_is_rejected(self) -> None:
        with pytest.raises(MigrationError, match="invalid selector"):
            _parse_selector("a/b/c/d", None)

    def test_empty_segment_is_rejected(self) -> None:
        with pytest.raises(MigrationError, match="invalid selector"):
            _parse_selector("shop//web", None)


class TestListResources:
    """`list <project>` drills into one project's resources, surfacing uuids for an
    unambiguous project/environment/<uuid> selection."""

    @respx.mock
    def test_shows_resource_names_and_uuids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        respx.get(f"{BASE}/servers").mock(
            return_value=httpx.Response(
                200, json=[{"uuid": "s1", "name": "prod-1", "ip": "10.0.0.1", "id": 1}]
            )
        )
        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx.get(f"{BASE}/projects/p1").mock(
            return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
        )
        respx.get(f"{BASE}/projects/p1/production").mock(
            return_value=httpx.Response(
                200, json={"applications": [{"uuid": "a1", "name": "web", "server_uuid": "s1"}]}
            )
        )
        result = runner.invoke(app, ["list", "shop"])
        assert result.exit_code == 0
        assert "web" in result.stdout
        assert "a1" in result.stdout  # the uuid, for selection
        assert "prod-1" in result.stdout


class TestCommandsRequireCredentials:
    """Every mutating command fails closed without a usable token.

    Exit 2 (preflight) means nothing was changed - the guarantee that makes it
    safe to script these.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["plan", "shop", "--to", "new-host"],
            ["run", "shop", "--to", "new-host", "--yes"],
            ["server", "plan", "--to", "new-host"],
            ["server", "run", "--to", "new-host", "--yes"],
        ],
    )
    def test_exits_2_without_credentials(self, argv: list[str]) -> None:
        result = runner.invoke(app, argv)
        assert result.exit_code == 2
        assert "COOLIFY_URL" in result.stderr


class TestResumeAndRollbackNeedAPlan:
    """They refuse rather than re-plan against a world that has since moved.

    A plan is a fact about the decision we made; re-deriving it after the fact
    could disagree with what was actually created, and then the rollback would
    delete the wrong things.
    """

    def test_resume_without_a_plan_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        result = runner.invoke(app, ["resume", "mig-001"])
        assert result.exit_code != 0
        assert "no saved plan" in result.stderr

    def test_rollback_without_a_plan_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        result = runner.invoke(app, ["rollback", "mig-001"])
        assert result.exit_code != 0
        assert "no saved plan" in result.stderr


class TestRunValidation:
    def test_invalid_finalize_policy_is_rejected(self) -> None:
        result = runner.invoke(app, ["run", "shop", "--to", "x", "--finalize", "obliterate"])
        assert result.exit_code == 2
        assert "keep|rename|delete" in result.stderr

    @pytest.mark.parametrize("policy", ["keep", "rename", "delete"])
    def test_valid_policies_get_past_parsing(self, policy: str) -> None:
        # No credentials, so it stops at preflight - but not at the parser.
        result = runner.invoke(app, ["run", "shop", "--to", "x", "--finalize", policy, "--yes"])
        assert "keep|rename|delete" not in result.stderr
