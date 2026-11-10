"""STOP_TARGET brings a target application's deployments to an end before its /stop.

An application's ``/stop`` cancels no deployment (StopApplication, 'high' queue),
so a deployment still queued or building lands after it and serves the claimed
hosts beside the restarted source — and DeleteResourceJob cancels a preview's
deployments only, so the container can outlive the delete. The commonest case:
START asked for one target, a later target's start failed a moment after, and
the rollback ran while the first one's deployment was still in progress.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import containment as c
from bg_coolify_migrate.domain.containment import STOP_CLOCK, STOPPED_CLOCK
from bg_coolify_migrate.domain.deployments import Deployment
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import VolumeManifest
from bg_coolify_migrate.domain.plan import ResourcePlan, ResourceSnapshot, Strategy
from bg_coolify_migrate.domain.statemachine import (
    START_REQUESTED,
    STARTED_EARLY,
    STOP_REQUESTED,
    Compensation,
    Outcome,
    State,
)
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import CoolifyApiError, TargetStartedEarly
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.test_engine_steps import BASE, HOST, _plan
from tests.test_pins_engine import MID, VOL, _source, _target

LIST = f"{BASE}/deployments/applications/tgt1"
EMPTY = httpx.Response(200, json={"count": 0, "deployments": []})


def _rows(*rows: tuple[str, str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "count": len(rows),
            "deployments": [
                {"deployment_uuid": u, "status": s, "logs": '[{"output": "TOKEN=x"}]'}
                for u, s in rows
            ],
        },
    )


def _app_first_plan() -> Any:
    app = ResourcePlan(
        snapshot=ResourceSnapshot(
            uuid="app1", name="web", collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
        ),
        strategy=Strategy.RECREATE_ONLY,
        manifest=VolumeManifest(),
    )
    return _plan(resources=(app, *_plan().resources))


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_app_first_plan(),
        journal=Journal.create(tmp_path, MID),
        migration_id=MID,
        source_host=_source(VOL),  # type: ignore[arg-type]
        target_host=_target("postgres-data-db2"),  # type: ignore[arg-type]
    )
    context.target_uuids.update({"app1": "tgt1", "db1": "db2"})
    context.journal.append(
        "step_started",
        state=State.QUIESCE.value,
        detail={STOP_REQUESTED: True, STOP_CLOCK: 900, STOPPED_CLOCK: 950},
    )
    yield context
    await api.aclose()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def instant(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(compensations.asyncio, "sleep", instant)
    return slept


class Order:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def mark(self, name: str, response: httpx.Response | None = None) -> Any:
        def respond(_request: httpx.Request) -> httpx.Response:
            self.calls.append(name)
            return response if response is not None else httpx.Response(200, json={})

        return respond


class TestAPartialStart:
    async def test_the_earlier_target_s_deployment_ends_before_its_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        order = Order()
        respx_mock.post(f"{BASE}/applications/tgt1/start").mock(
            return_value=httpx.Response(200, json={"deployment_uuid": "dep1"})
        )
        respx_mock.post(f"{BASE}/databases/db2/start").mock(
            return_value=httpx.Response(400, json={"message": "Database is already running."})
        )
        listed = iter([_rows(("dep1", "in_progress")), _rows(("dep1", "cancelled-by-user"))])

        def deployments(_request: httpx.Request) -> httpx.Response:
            order.calls.append("list")
            return next(listed)

        respx_mock.get(LIST).mock(side_effect=deployments)
        respx_mock.post(f"{BASE}/deployments/dep1/cancel").mock(side_effect=order.mark("cancel"))
        respx_mock.post(f"{BASE}/applications/tgt1/stop").mock(side_effect=order.mark("stop app"))
        respx_mock.post(f"{BASE}/databases/db2/stop").mock(side_effect=order.mark("stop db"))
        ran: list[str] = []

        def record(name: str) -> Any:
            async def compensate(_ctx: Any, _undo: dict[str, Any]) -> None:
                ran.append(name)

            return compensate

        async def ok(_ctx: MigrationContext) -> dict[str, Any]:
            return {}

        before = (State.CREATE_TARGET, State.QUIESCE, State.DISCOVER, State.COPY, State.VERIFY)
        saga = Saga(
            journal=ctx.journal,
            context=ctx,
            steps={
                **{s: ok for s in (*before, State.DNS_GATE)},
                State.START_TARGET: steps.step_start_target,
            },
            compensations={
                **{x: record(x.value) for x in Compensation},
                Compensation.STOP_TARGET: compensations.undo_start_target,
            },
        )
        result = await saga.run()

        assert isinstance(result.error, TargetStartedEarly)
        assert result.outcome is Outcome.ROLLED_BACK
        assert order.calls == ["list", "cancel", "list", "stop app", "stop db"]
        assert no_sleep == [], "a cancel is answered once the row reads cancelled"


class TestUndoStartTarget:
    async def test_a_deployment_the_run_never_saw_is_cancelled(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        # START was skipped: someone else's deployment of the target is queued.
        order = Order()
        listed = iter(
            [
                _rows(("theirs", "queued"), ("old", "finished")),
                _rows(("theirs", "cancelled-by-user")),
            ]
        )
        respx_mock.get(LIST).mock(side_effect=lambda _r: next(listed))
        cancel = respx_mock.post(f"{BASE}/deployments/theirs/cancel").mock(
            side_effect=order.mark("cancel")
        )
        respx_mock.post(f"{BASE}/applications/tgt1/stop").mock(side_effect=order.mark("stop"))
        await compensations.undo_start_target(
            ctx, {START_REQUESTED: ["tgt1"], STARTED_EARLY: ["tgt1"]}
        )
        assert order.calls == ["cancel", "stop"]
        assert cancel.call_count == 1

    async def test_one_still_running_at_the_deadline_fails_after_the_stop(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ctx.settings, "deploy_timeout", 10.0)
        respx_mock.get(LIST).mock(return_value=_rows(("dep1", "in_progress")))
        cancel = respx_mock.post(f"{BASE}/deployments/dep1/cancel").mock(
            return_value=httpx.Response(500, json={"message": "Failed to cancel deployment"})
        )
        stop = respx_mock.post(f"{BASE}/applications/tgt1/stop").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(RuntimeError) as excinfo:
            await compensations.undo_start_target(
                ctx, {START_REQUESTED: ["tgt1"], "deployments": {"app1": "dep1"}}
            )
        assert stop.called, "the stop is still sent"
        assert "web: deployment dep1" in str(excinfo.value)
        assert "still queued or running after 10s" in str(excinfo.value)
        assert cancel.call_count >= 2, "a failed cancel is asked again"
        assert sum(no_sleep) >= 10.0

    async def test_an_unreadable_list_is_read_again_and_journalled_ones_read_alone(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        respx_mock.get(LIST).mock(
            side_effect=[httpx.Response(500, json={"message": "Server Error"}), EMPTY]
        )
        one = respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json={"status": "finished"})
        )
        respx_mock.post(f"{BASE}/applications/tgt1/stop").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_start_target(
            ctx, {"started": ["tgt1"], "deployments": {"app1": "dep1"}}
        )
        assert one.call_count == 2, "the list missed it both times"
        assert no_sleep == []

    async def test_a_list_that_stays_unreadable_is_never_taken_for_settled(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ctx.settings, "deploy_timeout", 10.0)
        respx_mock.get(LIST).mock(return_value=httpx.Response(500, json={"message": "Error"}))
        stop = respx_mock.post(f"{BASE}/applications/tgt1/stop").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(RuntimeError, match="web: its deployments could not be read"):
            await compensations.undo_start_target(ctx, {"started": ["tgt1"]})
        assert stop.called

    async def test_databases_and_services_have_no_deployments_to_read(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        stop = respx_mock.post(f"{BASE}/databases/db2/stop").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_start_target(ctx, {"started": ["db2"]})
        assert stop.called
        assert [str(call.request.url) for call in respx_mock.calls] == [
            f"{BASE}/databases/db2/stop"
        ]


class TestTheClient:
    async def test_lists_newest_rows_without_their_logs(self, respx_mock: respx.Router) -> None:
        route = respx_mock.get(LIST).mock(return_value=_rows(("d1", "queued"), ("d0", "finished")))
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            rows = await api.application_deployments("tgt1")
        assert rows == [
            Deployment(uuid="d1", status="queued"),
            Deployment(uuid="d0", status="finished"),
        ]
        assert route.calls.last.request.url.params["take"] == "50"

    async def test_an_absent_application_lists_nothing(self, respx_mock: respx.Router) -> None:
        respx_mock.get(LIST).mock(
            return_value=httpx.Response(404, json={"message": "Application not found"})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            assert await api.application_deployments("tgt1") is None

    @pytest.mark.parametrize(("status", "cancelled"), [(200, True), (400, False), (404, False)])
    async def test_cancel(self, respx_mock: respx.Router, status: int, cancelled: bool) -> None:
        respx_mock.post(f"{BASE}/deployments/d1/cancel").mock(
            return_value=httpx.Response(status, json={"message": "x"})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            assert await api.cancel_deployment("d1") is cancelled

    async def test_a_cancel_that_failed_raises(self, respx_mock: respx.Router) -> None:
        respx_mock.post(f"{BASE}/deployments/d1/cancel").mock(
            return_value=httpx.Response(403, json={"message": "no permission"})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            with pytest.raises(CoolifyApiError):
                await api.cancel_deployment("d1")


class TestUnsettled:
    def test_queued_running_and_unread_count(self) -> None:
        rows = [
            Deployment("a", "queued"),
            Deployment("b", "in_progress"),
            Deployment("c", ""),
            Deployment("d", "finished"),
            Deployment("e", "failed"),
            Deployment("f", "cancelled-by-user"),
            Deployment("g", "some-future-status"),
        ]
        assert [d.uuid for d in c.unsettled(rows)] == ["a", "b", "c"]
