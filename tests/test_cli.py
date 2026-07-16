"""CLI tests.

Exit codes are a documented contract that callers script against, so they are
asserted explicitly rather than just "did it fail".
"""

from __future__ import annotations

import asyncio
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
    """`list` answers "what can I migrate, and from where" in one recursive pass:
    server -> project -> environment -> resource, everything, with uuids."""

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
                200, json={"applications": [{"uuid": "a1", "name": "web", "server_uuid": "s1"}]}
            )
        )

    def test_needs_credentials_like_the_others(self) -> None:
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 2
        assert "COOLIFY_URL" in result.stderr

    @respx.mock
    def test_lists_everything_recursively(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        self._mock_instance()
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        # server -> project -> environment -> resource, all in one pass, with uuids.
        for token in ("prod-1", "shop", "p1", "production", "web", "a1", "application"):
            assert token in result.stdout

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
                "project": "shop",
                "project_uuid": "p1",
                "environment": "production",
                "name": "web",
                "uuid": "a1",
                "kind": "application",
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

    @respx.mock
    def test_project_scope_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        result = runner.invoke(app, ["list", "shop", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == [
            {
                "server": "prod-1",
                "server_uuid": "s1",
                "project": "shop",
                "project_uuid": "p1",
                "environment": "production",
                "name": "web",
                "uuid": "a1",
                "kind": "application",
            }
        ]


class TestUuidSelection:
    """`plan`/`run` accept a uuid at the project and resource levels (the ones `list`
    prints uuids for); the environment is always a name - Coolify has no env uuid."""

    def _settings(self, monkeypatch: pytest.MonkeyPatch) -> object:
        from bg_coolify_migrate.settings.base import Settings

        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        return Settings()

    @respx.mock
    async def test_project_uuid_resolves_to_its_name_and_environments(self, api: object) -> None:
        from bg_coolify_migrate.cli import Selection, _resolve_jobs

        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "prj-9f2a", "name": "bauer-group"}])
        )
        respx.get(f"{BASE}/projects/prj-9f2a").mock(
            return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
        )
        name, jobs = await _resolve_jobs(api, Selection("prj-9f2a", None, None))
        assert name == "bauer-group"  # resolved from the uuid
        assert jobs == [("prj-9f2a", "production", None)]  # (project, environment, resource)

    @respx.mock
    async def test_resource_uuid_is_matched_not_treated_as_a_name(
        self, api: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A real resource uuid gets PAST the name/uuid filter and only fails later,
        # at source-server resolution - proving the uuid matched the resource.
        from bg_coolify_migrate.cli import _build

        settings = self._settings(monkeypatch)
        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx.get(f"{BASE}/projects/p1/production").mock(
            return_value=httpx.Response(
                200, json={"applications": [{"uuid": "rsc-1a", "name": "web"}]}
            )
        )
        respx.get(f"{BASE}/applications/rsc-1a").mock(
            return_value=httpx.Response(200, json={"uuid": "rsc-1a"})  # no server relation
        )
        with pytest.raises(MigrationError, match="did not report a server"):
            await _build(api, settings, "shop", "production", "target", "rsc-1a")

    @respx.mock
    async def test_unknown_resource_uuid_is_rejected_clearly(
        self, api: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.cli import _build

        settings = self._settings(monkeypatch)
        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx.get(f"{BASE}/projects/p1/production").mock(
            return_value=httpx.Response(
                200, json={"applications": [{"uuid": "rsc-1a", "name": "web"}]}
            )
        )
        with pytest.raises(MigrationError, match="no resource named 'nope'"):
            await _build(api, settings, "shop", "production", "target", "nope")

    @respx.mock
    async def test_empty_environment_raises_the_skippable_type(
        self, api: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.cli import _build
        from bg_coolify_migrate.errors import EmptyEnvironment

        settings = self._settings(monkeypatch)
        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx.get(f"{BASE}/projects/p1/production").mock(
            return_value=httpx.Response(200, json={"applications": [], "services": []})
        )
        with pytest.raises(EmptyEnvironment):
            await _build(api, settings, "shop", "production", "target", None)


class TestPlanSurfacesRealErrors:
    """Regression: a real failure on a single-scope plan (host key not trusted, no
    server) must surface - never be buried under "nothing to plan"."""

    @respx.mock
    def test_real_error_is_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bg_coolify_migrate.cli as cli_mod
        from bg_coolify_migrate.errors import PreflightError

        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        # assert_can_read_sensitive passes: a security key with a private_key.
        respx.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"uuid": "k1", "private_key": "-----BEGIN"}])
        )

        async def _selection(*_a: object, **_k: object) -> object:
            return Selection("shop", "production", "web"), "target"

        async def _jobs(*_a: object, **_k: object) -> object:
            return "shop", [("shop", "production", "web")]

        async def _boom(*_a: object, **_k: object) -> object:
            raise PreflightError(
                "host key for root@0046-20:22 is not known and was not accepted"
            )

        monkeypatch.setattr(cli_mod, "_resolve_selection", _selection)
        monkeypatch.setattr(cli_mod, "_resolve_jobs", _jobs)
        monkeypatch.setattr(cli_mod, "_build", _boom)

        result = runner.invoke(app, ["plan", "shop/production/web", "--to", "target"])
        assert result.exit_code == 2
        assert "host key" in result.stderr  # the real error, not "nothing to plan"
        assert "nothing to plan" not in result.stderr


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


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    from bg_coolify_migrate.api.client import CoolifyClient

    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


def _plan(project: str = "shop", environment: str = "production", blocked: bool = False):  # type: ignore[no-untyped-def]
    from bg_coolify_migrate.domain.kinds import ResourceKind
    from bg_coolify_migrate.domain.plan import (
        MigrationPlan,
        ResourcePlan,
        ResourceSnapshot,
        ServerRef,
        Strategy,
    )

    snapshot = ResourceSnapshot(
        uuid="a1",
        name="web",
        collection="databases",
        kind=ResourceKind.DATABASE,
        has_previews=blocked,  # a hard blocking reason -> is_blocked is True
    )
    return MigrationPlan(
        project=project,
        environment=environment,
        source_server=ServerRef(uuid="s1", name="old", ip="1.1.1.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="2.2.2.2"),
        resources=(ResourcePlan(snapshot=snapshot, strategy=Strategy.RECREATE_ONLY),),
    )


class TestSelectionResolution:
    async def test_no_selector_non_interactive_demands_one(
        self, api: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.cli import _resolve_selection

        monkeypatch.setattr("bg_coolify_migrate.cli.is_interactive", lambda: False)
        with pytest.raises(MigrationError, match="provide a selector"):
            await _resolve_selection(api, None, None, None)

    async def test_selector_without_target_non_interactive_demands_to(
        self, api: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.cli import _resolve_selection

        monkeypatch.setattr("bg_coolify_migrate.cli.is_interactive", lambda: False)
        with pytest.raises(MigrationError, match="--to is required"):
            await _resolve_selection(api, "shop", None, None)

    async def test_selector_with_target_parses_without_touching_the_api(self, api: object) -> None:
        from bg_coolify_migrate.cli import _resolve_selection

        selection, target = await _resolve_selection(api, "shop/production/web", "srv", None)
        assert selection == Selection("shop", "production", "web")
        assert target == "srv"


class TestPicker:
    @respx.mock
    async def test_walks_project_environment_resource_target(
        self, api: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.cli import _pick
        from bg_coolify_migrate.ui import wizard

        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx.get(f"{BASE}/projects/p1").mock(
            return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
        )
        respx.get(f"{BASE}/projects/p1/production").mock(
            return_value=httpx.Response(200, json={"applications": [{"uuid": "a1", "name": "web"}]})
        )
        respx.get(f"{BASE}/servers").mock(
            return_value=httpx.Response(
                200, json=[{"uuid": "s2", "name": "target", "ip": "1.1.1.1"}]
            )
        )
        monkeypatch.setattr(wizard, "choose_project", lambda projects: "shop")
        monkeypatch.setattr(wizard, "choose_scope_environment", lambda envs: "production")
        monkeypatch.setattr(wizard, "choose_scope_resource", lambda resources: "web")
        monkeypatch.setattr(wizard, "choose_server", lambda servers, message: "s2")

        selection, target = await _pick(api, None)
        assert selection == Selection("shop", "production", "web")
        assert target == "s2"

    @respx.mock
    async def test_whole_project_skips_resource_and_keeps_given_target(
        self, api: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.cli import _pick
        from bg_coolify_migrate.ui import wizard

        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx.get(f"{BASE}/projects/p1").mock(
            return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
        )
        monkeypatch.setattr(wizard, "choose_project", lambda projects: "shop")
        monkeypatch.setattr(wizard, "choose_scope_environment", lambda envs: None)  # whole project

        selection, target = await _pick(api, "given-target")
        assert selection == Selection("shop", None, None)
        assert target == "given-target"

    @respx.mock
    async def test_prompt_that_nests_asyncio_run_does_not_crash(
        self, api: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: questionary's .ask() calls asyncio.run() internally, and _pick
        # runs inside our own asyncio.run(), which used to raise "asyncio.run() cannot
        # be called from a running event loop". Off-loading to a worker thread (which
        # has no running loop) fixes it.
        from bg_coolify_migrate.cli import _pick
        from bg_coolify_migrate.ui import wizard

        respx.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx.get(f"{BASE}/projects/p1").mock(
            return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
        )

        async def _noop() -> None:
            return None

        def _nested_project(_projects: object) -> str:
            asyncio.run(_noop())  # what questionary does under the hood
            return "shop"

        def _nested_environment(_envs: object) -> None:
            asyncio.run(_noop())
            return None

        monkeypatch.setattr(wizard, "choose_project", _nested_project)
        monkeypatch.setattr(wizard, "choose_scope_environment", _nested_environment)

        selection, target = await _pick(api, "given-target")
        assert selection == Selection("shop", None, None)
        assert target == "given-target"


class TestConfirmPlans:
    def test_single_scope_delegates_to_the_plan_wizard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bg_coolify_migrate.cli import _confirm_plans
        from bg_coolify_migrate.ui import wizard

        monkeypatch.setattr(wizard, "confirm_plan", lambda plan: True)
        monkeypatch.setattr(wizard, "confirm_destructive", lambda plan: True)
        assert _confirm_plans([_plan()]) is True

    def test_single_scope_declined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bg_coolify_migrate.cli import _confirm_plans
        from bg_coolify_migrate.ui import wizard

        monkeypatch.setattr(wizard, "confirm_plan", lambda plan: False)
        assert _confirm_plans([_plan()]) is False

    def test_whole_project_confirmed_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import questionary

        from bg_coolify_migrate.cli import _confirm_plans
        from bg_coolify_migrate.ui import wizard

        class _Answer:
            def ask(self) -> bool:
                return True

        monkeypatch.setattr(questionary, "confirm", lambda *a, **k: _Answer())
        monkeypatch.setattr(wizard, "confirm_destructive", lambda plan: True)
        assert _confirm_plans([_plan("a"), _plan("b")]) is True

    def test_whole_project_refuses_when_an_environment_is_blocked(self) -> None:
        from bg_coolify_migrate.cli import _confirm_plans

        assert _confirm_plans([_plan("a"), _plan("b", blocked=True)]) is False
