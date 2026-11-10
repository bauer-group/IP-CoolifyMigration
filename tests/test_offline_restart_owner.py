"""Who restarts a stack the CLI stopped before the run: the JOURNAL says.

An interactive ``run`` stops an offline-only stack up front, on the operator's
yes (``cli._offer_offline``). The migration owns the source's restart exactly
when it may have stopped the source itself — ``statemachine.source_was_stopped``
on its journal: QUIESCE completed, or journalled its stop request. A QUIESCE
that refuses BEFORE that request (a failed disarm PATCH, a deployment history
Coolify does not give, a source deployment still queued) reached QUIESCE,
stopped nothing and restarts nothing. Deciding from the state the run reached
handed the stack to a rollback that never restarted it: the source stayed
down, and the report said it was running again.

The first class runs the real QUIESCE step, the real compensations and the real
saga under the real ``cli._execute_plans``; only Coolify's HTTP API (respx) and
the SSH hosts (FakeHost) are faked.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from rich.console import Console

from bg_coolify_migrate import cli
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import ORDER, STOP_REQUESTED, Outcome, State
from bg_coolify_migrate.engine import deploy_triggers as engine_dt
from bg_coolify_migrate.engine import runner, steps
from bg_coolify_migrate.engine.compensations import build_compensations, source_data_refusal
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import RunResult, Saga
from bg_coolify_migrate.errors import QuiesceError, RollbackError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SONAR = ("applications", "app1", "sonar")


def _plan(*uuids: str, environment: str = "production") -> MigrationPlan:
    resources = tuple(
        ResourcePlan(
            snapshot=ResourceSnapshot(
                uuid=uuid, name="sonar" if uuid == "app1" else uuid,
                collection="applications", kind=ResourceKind.APP_DOCKERIMAGE,
            ),
            strategy=Strategy.COPY_DATA,
        )
        for uuid in uuids or ("app1",)
    )
    return MigrationPlan(
        project="p",
        environment=environment,
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=resources,
        coolify_version="4.7.2",
    )


def _host() -> FakeHost:
    return (
        FakeHost()
        .on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        .on(r"authorized_keys\.bak", exit_status=0)
        .on(r"rm -rf /root/\.coolify-migrate/", exit_status=0)
        .quiet_docker()
    )


class _Api:
    """The CLI's own api object: what it stops and restarts."""

    def __init__(self, *, broken: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.broken = broken

    async def restart(self, collection: str, uuid: str) -> None:
        self.calls.append(("restart", collection, uuid))
        if self.broken:
            raise RuntimeError("503 Service Unavailable\nmore detail")


def _capture(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    out = io.StringIO()
    console = Console(file=out, width=300, no_color=True, highlight=False, soft_wrap=True)
    monkeypatch.setattr(cli, "get_console", lambda **_kw: console)
    return out


async def _accept(*_a: object, **_kw: object) -> None:
    return None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path, deploy_timeout=10.0)  # type: ignore[call-arg]


class TestRealQuiesceRefusal:
    """The confirmed case, end to end: QUIESCE refuses before its stop request."""

    @pytest.mark.parametrize("failure", ["disarm_patch", "deployments_history", "pending_queue"])
    async def test_the_cli_starts_what_it_stopped_and_says_so(
        self,
        respx_mock: respx.Router,
        monkeypatch: pytest.MonkeyPatch,
        settings: Settings,
        failure: str,
    ) -> None:
        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr(engine_dt.asyncio, "sleep", instant)
        respx_mock.get(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "app1",
                    "name": "sonar",
                    "fqdn": "",
                    "settings": {
                        "is_auto_deploy_enabled": True,
                        "is_preview_deployments_enabled": False,
                    },
                },
            )
        )
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(503)
            if failure == "disarm_patch"
            else httpx.Response(200, json={"uuid": "app1"})
        )
        queued = [
            {
                "deployment_uuid": "d1",
                "status": "queued",
                "deployment_url": "/project/p/environment/e/application/app1/deployment/d1",
            }
        ]
        respx_mock.get(f"{BASE}/deployments").mock(
            return_value=httpx.Response(200, json=queued if failure == "pending_queue" else [])
        )
        respx_mock.get(f"{BASE}/deployments/applications/app1").mock(
            return_value=httpx.Response(500)
            if failure == "deployments_history"
            else httpx.Response(200, json={"count": 0, "deployments": []})
        )
        saga_restart = respx_mock.post(f"{BASE}/applications/app1/restart").mock(
            return_value=httpx.Response(200, json={"deployment_uuid": "x"})
        )
        respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )
        respx_mock.get(f"{BASE}/applications/appT").mock(return_value=httpx.Response(404))

        async def no_mounts(_ctx: Any) -> dict[str, list[str]]:
            return {}  # the CLI stopped the stack: nothing left to capture

        async def no_pins(_ctx: Any) -> list[str]:
            return []

        monkeypatch.setattr(steps, "_capture_mounts", no_mounts)
        monkeypatch.setattr(steps, "_pin_source", no_pins)

        async def ok(_ctx: Any) -> dict[str, Any]:
            return {}

        async def create(c: Any) -> dict[str, Any]:
            c.target_uuids["app1"] = "appT"
            return {"target_uuids": {"app1": "appT"}}

        step_map: dict[Any, Any] = {s: ok for s in ORDER[:-1]}
        step_map[State.CREATE_TARGET] = create
        step_map[State.QUIESCE] = steps.step_quiesce
        api = CoolifyClient(HOST, "tok", max_retries=0)
        seen: dict[str, Any] = {}

        async def run(_api: Any, run_settings: Settings, plan: MigrationPlan, **kw: Any) -> Any:
            journal = Journal.create(run_settings.resolved_state_dir(), kw["migration_id"])
            ctx = MigrationContext(
                api=api,
                settings=settings,
                plan=plan,
                journal=journal,
                migration_id=kw["migration_id"],
                source_host=_host(),  # type: ignore[arg-type]
                target_host=_host(),  # type: ignore[arg-type]
            )
            saga = Saga(
                journal=journal,
                context=ctx,
                steps=step_map,
                compensations=build_compensations(),
                guard=source_data_refusal,
            )
            result = await saga.run()
            result.notices.extend(ctx.notices)
            seen["result"], seen["journal"] = result, journal
            return result

        monkeypatch.setattr(cli, "_accept_host_key", _accept)
        monkeypatch.setattr(runner, "run_migration", run)
        out = _capture(monkeypatch)
        stopped = [SONAR]
        cli_api = _Api()
        try:
            code = await cli._execute_plans(
                cli_api, settings, [_plan()], stopped,
                accept_drift=True, accept_dns=True, accept_hosts=frozenset(),
                delete_previews=False, trust_host_key=False,
            )
        finally:
            await api.aclose()

        result: RunResult = seen["result"]
        # The refusal came before the stop request, so the rollback owed no restart...
        assert result.outcome is Outcome.ROLLED_BACK and result.reached == State.QUIESCE
        assert seen["journal"].undo_info(State.QUIESCE.value).get(STOP_REQUESTED) is None
        assert saga_restart.call_count == 0
        # ...and the CLI started what it had stopped, before the report said so.
        assert cli_api.calls == [("restart", "applications", "app1")]
        assert stopped == []
        assert code == 5
        report = out.getvalue()
        assert "stopped_before_run: sonar: asked Coolify to start it again" in report


def _run_writing(
    events: list[tuple[str, str | None, dict[str, Any]]],
    *,
    outcome: Outcome = Outcome.ROLLED_BACK,
    reached: State = State.QUIESCE,
    raises: BaseException | None = None,
) -> Any:
    """A fake run_migration that journals ``events`` where the real one would."""

    async def run(_api: Any, run_settings: Settings, plan: MigrationPlan, **kw: Any) -> RunResult:
        journal = Journal.create(run_settings.resolved_state_dir(), kw["migration_id"])
        for event, state, detail in events:
            journal.append(event, state=state, detail=detail)
        if raises is not None:
            raise raises
        error = None if outcome is Outcome.SUCCEEDED else QuiesceError("refused")
        return RunResult(outcome=outcome, reached=reached, error=error)

    return run


#: QUIESCE refused before it asked Coolify to stop anything.
REFUSED_BEFORE_STOP = [
    ("started", "init", {}),
    ("step_started", State.QUIESCE.value, {}),
    ("step_failed", State.QUIESCE.value, {"error": "x"}),
]
#: QUIESCE failed AFTER its stop request: the rollback owes, and runs, the restart.
FAILED_AFTER_STOP = [
    ("started", "init", {}),
    ("step_started", State.QUIESCE.value, {}),
    ("step_started", State.QUIESCE.value, {STOP_REQUESTED: True}),
    ("step_failed", State.QUIESCE.value, {"error": "x"}),
]
QUIESCED = [
    ("started", "init", {}),
    ("step_started", State.QUIESCE.value, {}),
    ("step_completed", State.QUIESCE.value, {}),
]


class TestTheJournalDecides:
    @pytest.mark.parametrize(
        ("events", "reached", "cli_restarts"),
        [
            ([], State.PREFLIGHT, True),  # the saga never began: no journal at all
            (REFUSED_BEFORE_STOP, State.QUIESCE, True),  # REACHED quiesce, stopped nothing
            (FAILED_AFTER_STOP, State.QUIESCE, False),  # its rollback restarts it
            (QUIESCED, State.COPY, False),
            (QUIESCED, State.DONE, False),  # a finished migration keeps it stopped
        ],
    )
    async def test_ownership_follows_the_stop_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Settings,
        events: list[tuple[str, str | None, dict[str, Any]]],
        reached: State,
        cli_restarts: bool,
    ) -> None:
        outcome = Outcome.SUCCEEDED if reached is State.DONE else Outcome.ROLLED_BACK
        monkeypatch.setattr(cli, "_accept_host_key", _accept)
        monkeypatch.setattr(
            runner, "run_migration", _run_writing(events, outcome=outcome, reached=reached)
        )
        out = _capture(monkeypatch)
        api, stopped = _Api(), [SONAR]
        await cli._execute_plans(
            api, settings, [_plan()], stopped,
            accept_drift=True, accept_dns=True, accept_hosts=frozenset(),
            delete_previews=False, trust_host_key=False,
        )
        assert stopped == []  # restarted, or the run's own: nothing left for the finally
        assert (api.calls == [("restart", "applications", "app1")]) is cli_restarts
        assert ("stopped_before_run: sonar" in out.getvalue()) is cli_restarts

    async def test_a_run_that_raises_after_its_stop_keeps_the_source_its_own(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """A refused rollback raises out of the run. The source it stopped - maybe
        finalized, maybe without its data - must not be restarted by the CLI's
        finally, which is exactly what deciding after a return would have done."""
        monkeypatch.setattr(cli, "_accept_host_key", _accept)
        refused = RollbackError("rollback refused: the source lost its data")
        monkeypatch.setattr(
            runner, "run_migration", _run_writing(QUIESCED, raises=refused)
        )
        api, stopped = _Api(), [SONAR]
        with pytest.raises(RollbackError):
            try:
                await cli._execute_plans(
                    api, settings, [_plan()], stopped,
                    accept_drift=True, accept_dns=True, accept_hosts=frozenset(),
                    delete_previews=False, trust_host_key=False,
                )
            finally:
                await cli._restart_stopped(api, stopped)  # what cli._run's finally does
        assert api.calls == []

    async def test_a_run_that_raises_before_any_stop_is_started_again(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        monkeypatch.setattr(cli, "_accept_host_key", _accept)
        monkeypatch.setattr(
            runner, "run_migration", _run_writing([], raises=OSError("ssh: connection lost"))
        )
        _capture(monkeypatch)
        api, stopped = _Api(), [SONAR]
        with pytest.raises(OSError):
            try:
                await cli._execute_plans(
                    api, settings, [_plan()], stopped,
                    accept_drift=True, accept_dns=True, accept_hosts=frozenset(),
                    delete_previews=False, trust_host_key=False,
                )
            finally:
                await cli._restart_stopped(api, stopped)
        assert api.calls == [("restart", "applications", "app1")]

    async def test_an_unreadable_journal_leaves_the_stack_down_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """Cannot tell is not "the run never stopped it": restarting a source the
        run stopped is the move that cannot be taken back."""

        def unreadable(_state_dir: Path, _mid: str) -> bool:
            raise OSError("disk gone")

        monkeypatch.setattr(cli, "_accept_host_key", _accept)
        monkeypatch.setattr(runner, "run_migration", _run_writing(REFUSED_BEFORE_STOP))
        monkeypatch.setattr(runner, "run_stopped_source", unreadable)
        out = _capture(monkeypatch)
        api, stopped = _Api(), [SONAR]
        await cli._execute_plans(
            api, settings, [_plan()], stopped,
            accept_drift=True, accept_dns=True, accept_hosts=frozenset(),
            delete_previews=False, trust_host_key=False,
        )
        assert api.calls == [] and stopped == []
        assert "not starting sonar again" in out.getvalue()
        assert "disk gone" in out.getvalue()

    async def test_a_failed_restart_says_plainly_that_it_is_down(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        monkeypatch.setattr(cli, "_accept_host_key", _accept)
        monkeypatch.setattr(runner, "run_migration", _run_writing(REFUSED_BEFORE_STOP))
        out = _capture(monkeypatch)
        api, stopped = _Api(broken=True), [SONAR]
        await cli._execute_plans(
            api, settings, [_plan()], stopped,
            accept_drift=True, accept_dns=True, accept_hosts=frozenset(),
            delete_previews=False, trust_host_key=False,
        )
        assert stopped == []  # asked once, never again by the finally
        report = out.getvalue()
        assert (
            "stopped_before_run: sonar: DOWN - could not start it again "
            "(503 Service Unavailable); start it in Coolify"
        ) in report

    async def test_each_environment_starts_only_its_own_stack(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """A whole-project run stops at the first failure; the environments that
        never ran are the outer finally's (``cli._run``), not this run's."""
        monkeypatch.setattr(cli, "_accept_host_key", _accept)
        monkeypatch.setattr(runner, "run_migration", _run_writing(REFUSED_BEFORE_STOP))
        _capture(monkeypatch)
        later = ("applications", "app2", "app2")
        api, stopped = _Api(), [SONAR, later]
        await cli._execute_plans(
            api, settings, [_plan("app1"), _plan("app2", environment="staging")], stopped,
            accept_drift=True, accept_dns=True, accept_hosts=frozenset(),
            delete_previews=False, trust_host_key=False,
        )
        assert api.calls == [("restart", "applications", "app1")]
        assert stopped == [later]


class TestRunStoppedSource:
    def test_no_journal_stopped_nothing(self, tmp_path: Path) -> None:
        assert runner.run_stopped_source(tmp_path, "m1") is False

    def test_the_stop_request_is_what_counts(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "m1")
        journal.append("step_started", state=State.QUIESCE.value)
        assert runner.run_stopped_source(tmp_path, "m1") is False
        journal.append("step_started", state=State.QUIESCE.value, detail={STOP_REQUESTED: True})
        assert runner.run_stopped_source(tmp_path, "m1") is True
