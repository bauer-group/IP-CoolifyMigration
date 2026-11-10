"""`run` refuses a plan blocked only by its policy, or by the move as a whole.

Both kinds of refusal are derived when the plan is READ: a kept-source refusal
(``ResourcePlan.kept_source_refusals``) only under the finalize policy that
``run`` swaps onto the built plan, the same-server refusal
(``MigrationPlan.plan_refusals``) from the plan's two servers. Neither is in a
resource's own ``refusals``. These tests drive the CLI end to end and prove the
refusal lands before anything is touched: exit 2 with ``--yes``, a declined
confirmation interactively, no stop, and the saga never started. The only API
call is the scope check at startup; respx refuses any other.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

import bg_coolify_migrate.cli as cli_mod
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.observability.logging_setup import reset_logging
from bg_coolify_migrate.settings.base import reset_settings_cache

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
KEPT = "routers written into its compose route shop.example.com, which finalize moves"
SOURCE = ServerRef(uuid="s1", name="old", ip="10.0.0.1")
TARGET = ServerRef(uuid="s2", name="new", ip="10.0.0.2")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    reset_logging()
    reset_settings_cache()
    monkeypatch.chdir(tmp_path)
    for var in ("COOLIFY_URL", "COOLIFY_TOKEN", "STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield
    # The CLI configures structlog to cache each logger on first use; left
    # configured, a later test's capture_logs() misses a cached logger.
    reset_logging()


def _anonymous() -> VolumeItem:
    name = "a" * 64
    return VolumeItem(
        mount_class=MountClass.ANONYMOUS,
        decision=Decision.REFUSE,
        reason="anonymous volume of a running container",
        source_name=name,
        source_path=f"/var/lib/docker/volumes/{name}/_data",
        mount_path="/cache",
    )


def _plan(*, kept: bool = False, same_server: bool = False, offline: bool = False) -> MigrationPlan:
    """A plan built like the planner builds it: under the default policy, rename."""
    resource = ResourcePlan(
        snapshot=ResourceSnapshot(
            uuid="a1",
            name="shop",
            collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            server_uuid="s1",
        ),
        strategy=Strategy.COPY_DATA,
        manifest=VolumeManifest(items=(_anonymous(),) if offline else ()),
        kept_source_refusals=(KEPT,) if kept else (),
    )
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=SOURCE,
        target_server=SOURCE if same_server else TARGET,
        resources=(resource,),
    )


class _Run:
    """What one CLI invocation did: its result, the saga calls, the API writes."""

    def __init__(self, result: Any, executed: list[Any], writes: respx.Route) -> None:
        self.result = result
        self.executed = executed
        self.writes = writes

    @property
    def out(self) -> str:
        """stdout with the console's line wrapping undone."""
        return " ".join(self.result.stdout.split())


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    plan: MigrationPlan,
    *argv: str,
    interactive: bool = False,
) -> _Run:
    from bg_coolify_migrate.ui import wizard

    monkeypatch.setenv("COOLIFY_URL", HOST)
    monkeypatch.setenv("COOLIFY_TOKEN", "tok")
    executed: list[Any] = []

    async def _selection(*_a: object, **_k: object) -> object:
        return cli_mod.Selection("shop", "production", None), "new"

    async def _jobs(*_a: object, **_k: object) -> object:
        return "shop", [("shop", "production", None)]

    async def _build(*_a: object, **_k: object) -> MigrationPlan:
        return plan

    async def _execute(
        api: object, settings: object, plans: list[MigrationPlan], *_a: object, **_k: object
    ) -> int:
        executed.append([p.finalize_policy for p in plans])
        return 0

    def _never(*_a: object, **_k: object) -> bool:
        raise AssertionError("must not be asked")

    monkeypatch.setattr(cli_mod, "_resolve_selection", _selection)
    monkeypatch.setattr(cli_mod, "_resolve_jobs", _jobs)
    monkeypatch.setattr(cli_mod, "_build", _build)
    monkeypatch.setattr(cli_mod, "_execute_plans", _execute)
    monkeypatch.setattr(cli_mod, "is_interactive", lambda: interactive)
    # The offer to stop an offline-only stack, and the questions after the plan.
    monkeypatch.setattr(wizard, "confirm_offline", _never)
    monkeypatch.setattr(wizard, "confirm_destructive", _never)
    monkeypatch.setattr(wizard, "_ask", _never)

    # Any other request is unmocked, and respx fails it.
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/security/keys").mock(
            return_value=httpx.Response(200, json=[{"uuid": "k1", "private_key": "-----BEGIN"}])
        )
        writes = router.route(path__regex=r"/(stop|start|restart)$").mock(
            return_value=httpx.Response(200, json={})
        )
        result = CliRunner().invoke(cli_mod.app, ["run", "shop/production", "--to", "new", *argv])
    return _Run(result, executed, writes)


class TestUnattended:
    @pytest.mark.parametrize("policy", ["keep", "rename"])
    def test_a_kept_source_refusal_exits_2_before_anything(
        self, monkeypatch: pytest.MonkeyPatch, policy: str
    ) -> None:
        run = _invoke(monkeypatch, _plan(kept=True), "--yes", "--finalize", policy)
        assert run.result.exit_code == 2, run.result.output
        assert run.executed == [] and not run.writes.called
        assert "routers written into its compose route shop.example.com" in run.out

    def test_the_same_plan_runs_under_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The refusal is the policy's: swapped to delete, the plan runs.
        from bg_coolify_migrate.domain.statemachine import FinalizePolicy

        run = _invoke(monkeypatch, _plan(kept=True), "--yes", "--finalize", "delete")
        assert run.result.exit_code == 0, run.result.output
        assert run.executed == [[FinalizePolicy.DELETE]]

    @pytest.mark.parametrize("policy", ["keep", "delete"])
    def test_a_same_server_plan_exits_2_whatever_the_policy(
        self, monkeypatch: pytest.MonkeyPatch, policy: str
    ) -> None:
        run = _invoke(monkeypatch, _plan(same_server=True), "--yes", "--finalize", policy)
        assert run.result.exit_code == 2, run.result.output
        assert run.executed == [] and not run.writes.called
        assert "is the source server" in run.out


class TestInteractive:
    @pytest.mark.parametrize(
        ("plan", "argv"),
        [
            (_plan(kept=True, offline=True), ("--finalize", "keep")),
            (_plan(same_server=True, offline=True), ("--finalize", "delete")),
        ],
        ids=["kept-source-refusal", "same-server"],
    )
    def test_an_offline_only_stack_is_not_stopped_and_the_plan_is_declined(
        self, monkeypatch: pytest.MonkeyPatch, plan: MigrationPlan, argv: tuple[str, ...]
    ) -> None:
        # Stopping the stack would clear its anonymous volumes, never the
        # refusal: the offer is not made, and confirm_plan declines on its own.
        assert plan.resources[0].offline_only
        run = _invoke(monkeypatch, plan, *argv, interactive=True)
        assert run.result.exception is None, run.result.output
        assert run.result.exit_code == 0
        assert run.executed == [] and not run.writes.called
        assert "This migration cannot proceed" in run.out
        assert "Nothing has been changed" in run.out

    def test_without_the_refusal_the_stack_is_offered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The control: the same offline-only stack under delete gets the offer,
        # which this fixture refuses to answer.
        run = _invoke(
            monkeypatch, _plan(kept=True, offline=True), "--finalize", "delete", interactive=True
        )
        assert isinstance(run.result.exception, AssertionError)
        assert "must not be asked" in str(run.result.exception)
        assert run.executed == [] and not run.writes.called
