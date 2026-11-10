"""The witnesses a busy or restarted daemon cannot lose.

The daemon replays only its last 256 events (moby daemon/events/events.go:13,27),
and every Docker health probe logs three: on a host whose other stacks probe every
few seconds the event log rarely reaches back to the stop, so a source deploy that
came up and was removed again (a failed redeploy) went unseen. For an
APPLICATION, Coolify's own deployment history is durable and needs no clock:
QUIESCE journals, just before the stop, which deployments each source
application had ENDED, and every source checkpoint reports a row that record
does not hold — queued after the stop, or not ended by it — or one still queued
or running. QUIESCE waits out the source's deployments first, so such a row is
never this tool's own. Before START it rolls back, after START it blocks, as
every revival does; a history that cannot be read never passes.

Also here: a daemon that holds no event at all (restarted since the stop) is a
gap in the event log, not a complete one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.discovery import quiesce
from bg_coolify_migrate.domain import containment as c
from bg_coolify_migrate.domain.containment import (
    REVIVAL_PENDING_DEPLOYMENTS,
    REVIVAL_SEEN_CLOCK,
    REVIVAL_SEEN_DEPLOYMENTS,
    SOURCE_DEPLOYMENTS,
    STOP_CLOCK,
    STOPPED_CLOCK,
)
from bg_coolify_migrate.domain.deployments import Deployment
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import VolumeManifest
from bg_coolify_migrate.domain.plan import MigrationPlan, ResourcePlan, ResourceSnapshot, Strategy
from bg_coolify_migrate.domain.statemachine import STOP_REQUESTED, State
from bg_coolify_migrate.engine import containment, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import (
    QuiesceError,
    SourceDeploymentPending,
    SourceRevived,
    SourceRevivedAfterStart,
)
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.fake_daemon import FakeDaemon
from tests.test_engine_steps import BASE, HOST, _plan
from tests.test_pins_engine import MID, VOL, _target

STOP = 1_800_000_000
SEEN = STOP + 600
WEB = c.Watched(name="web", uuid="app1", labels={})
HISTORY = f"{BASE}/deployments/applications/app1"


def _d(uuid: str, status: str = "finished") -> Deployment:
    return Deployment(uuid=uuid, status=status)


# ── the rules ────────────────────────────────────────────────────────────────


class TestTheRecord:
    def test_the_stop_records_only_what_had_ended(self) -> None:
        rows = [_d("d4", "queued"), _d("d3", "in_progress"), _d("d2", "failed"), _d("d1")]
        assert c.ended_deployments([*rows, _d(""), _d("d1"), _d("d9", "")]) == ["d1", "d2"]

    def test_no_record_is_no_comparison(self) -> None:
        assert c.accounted_deployments({}) is None
        assert c.accounted_deployments({SOURCE_DEPLOYMENTS: "junk"}) is None

    def test_a_block_adds_what_it_showed(self) -> None:
        detail = {
            SOURCE_DEPLOYMENTS: {"app1": ["d1"], "app2": [], "bad": "x"},
            REVIVAL_SEEN_DEPLOYMENTS: {"app1": ["d3", 7], "gone": ["d8"]},
        }
        assert c.accounted_deployments(detail) == {
            "app1": frozenset({"d1", "d3"}),
            "app2": frozenset(),
        }

    def test_each_block_keeps_the_earlier_ones(self) -> None:
        history = {"app1": [_d("d5"), _d("d3"), _d("")]}
        detail = {REVIVAL_SEEN_DEPLOYMENTS: {"app1": ["d4"], "app2": ["d9"]}}
        assert c.block_record(detail, history)[REVIVAL_SEEN_DEPLOYMENTS] == {
            "app1": ["d3", "d4", "d5"],
            "app2": ["d9"],
        }
        assert c.block_record({}, history)[REVIVAL_SEEN_DEPLOYMENTS] == {"app1": ["d3", "d5"]}


class TestDeploymentBreaches:
    def _judge(self, rows: list[Deployment], known: frozenset[str]) -> list[c.Breach]:
        return c.deployment_breaches([WEB], {"app1": known}, {"app1": rows})

    def test_nothing_new_is_nothing(self) -> None:
        assert self._judge([_d("d2"), _d("d1")], frozenset({"d1", "d2"})) == []

    def test_a_row_newer_than_the_record(self) -> None:
        (breach,) = self._judge([_d("d3", "failed"), _d("d2")], frozenset({"d2"}))
        assert breach.describe().startswith(
            "web: deployment d3 came after the stop, or had not ended by it, and is failed"
        )

    def test_one_still_queued_or_running_counts_whatever_the_record(self) -> None:
        # Shown at a block, still running at the resume: it starts the source yet.
        (breach,) = self._judge([_d("d3", "in_progress"), _d("d2")], frozenset({"d2", "d3"}))
        assert "is in_progress" in breach.why
        (unread,) = self._judge([_d("d3", "")], frozenset({"d3"}))
        assert "did not report" in unread.why

    def test_a_row_below_a_recorded_one_existed_at_the_stop(self) -> None:
        # The stop read a page of rows; two created in the same second can sort the
        # other way next time (created_at alone before the id tiebreak), and the
        # one beyond that page surfaces. It is older than d2: no deployment since.
        assert self._judge([_d("d2"), _d("d0")], frozenset({"d2", "d1"})) == []

    def test_an_application_with_no_ended_deployment_reports_every_row(self) -> None:
        assert len(self._judge([_d("d2"), _d("d1")], frozenset())) == 2

    def test_only_what_both_the_record_and_this_read_hold_is_judged(self) -> None:
        other = c.Watched(name="api", uuid="app2", labels={})
        assert c.deployment_breaches([WEB, other], {"app1": frozenset()}, {"app2": [_d("x")]}) == []


class TestAnEmptyEventLog:
    def test_is_a_gap_from_when_it_was_read(self) -> None:
        log = c.parse_event_log(f"until {SEEN}\n")
        assert log == c.EventLog(oldest=None, read_at=SEEN)
        assert c.event_log_gap(log, STOP) == SEEN

    def test_without_the_read_time_the_gap_starts_at_the_stop(self) -> None:
        assert c.event_log_gap(c.EventLog(), STOP) == STOP + 1

    def test_one_that_reaches_back_is_still_complete(self) -> None:
        log = c.parse_event_log(f"until {SEEN}\noldest {STOP - 5}\n")
        assert c.event_log_gap(log, STOP) is None

    def test_the_command_prints_the_read_time_first(self) -> None:
        command = quiesce.start_events_command(since=STOP)
        assert command.startswith('u="$(date +%s)" && echo "until $u" && docker events')


# ── the checkpoints, against a daemon model and the API ─────────────────────


def _app_plan() -> MigrationPlan:
    app = ResourcePlan(
        snapshot=ResourceSnapshot(
            uuid="app1", name="web", collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
        ),
        strategy=Strategy.RECREATE_ONLY,
        manifest=VolumeManifest(),
    )
    return _plan(resources=(*_plan().resources, app))


def _rows(*rows: tuple[str, str]) -> httpx.Response:
    """Coolify's answer; every row carries the whole deployment log."""
    return httpx.Response(
        200,
        json={
            "count": len(rows),
            "deployments": [
                {"deployment_uuid": u, "status": s, "logs": "DB_PASSWORD=hunter2"} for u, s in rows
            ],
        },
    )


def _source() -> FakeDaemon:
    daemon = FakeDaemon(volumes=(VOL,))
    daemon.on(r"^date \+%s$", stdout=str(SEEN))
    return daemon


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_app_plan(),
        journal=Journal.create(tmp_path, MID),
        migration_id=MID,
        source_host=_source(),  # type: ignore[arg-type]
        target_host=_target(),  # type: ignore[arg-type]
    )
    context.target_uuids.update({"db1": "db2", "app1": "tgt1"})
    context.journal.append(
        "step_started",
        state=State.QUIESCE.value,
        detail={
            STOP_REQUESTED: True,
            STOP_CLOCK: STOP - 60,
            SOURCE_DEPLOYMENTS: {"app1": ["d1", "d2"]},
        },
    )
    context.journal.append("step_started", state=State.QUIESCE.value, detail={STOPPED_CLOCK: STOP})
    yield context
    await api.aclose()


def _started(ctx: MigrationContext) -> None:
    ctx.journal.append("step_completed", state=State.START_TARGET.value, detail={})


def _quiesce(ctx: MigrationContext) -> dict[str, Any]:
    return ctx.journal.undo_info(State.QUIESCE.value)


class TestTheCheckpoint:
    async def test_the_same_history_passes(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get(HISTORY).mock(return_value=_rows(("d2", "finished"), ("d1", "failed")))
        await containment.assert_contained(ctx, checkpoint="the end of COPY")
        assert route.call_count == 1

    async def test_a_deploy_that_came_and_went_rolls_back_before_start(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Its container is gone and the event log has forgotten it: the row has not.
        respx_mock.get(HISTORY).mock(return_value=_rows(("d3", "failed"), ("d2", "finished")))
        with pytest.raises(SourceRevived) as excinfo:
            await containment.assert_contained(ctx, checkpoint="the DNS gate")
        assert not isinstance(excinfo.value, SourceRevivedAfterStart)
        assert "web: deployment d3 came after the stop" in str(excinfo.value)
        assert "rolls back now" in (excinfo.value.hint or "")
        assert "hunter2" not in str(excinfo.value), "the client keeps status only"

    async def test_after_start_it_blocks_and_the_resume_passes(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _started(ctx)
        route = respx_mock.get(HISTORY).mock(return_value=_rows(("d3", "finished"), ("d2", "finished")))
        with pytest.raises(SourceRevivedAfterStart, match="web: deployment d3"):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        record = _quiesce(ctx)
        assert record[REVIVAL_SEEN_DEPLOYMENTS] == {"app1": ["d2", "d3"]}
        assert record[REVIVAL_SEEN_CLOCK] == SEEN
        # The operator was shown d3 and stopped the source: the resume goes on.
        await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        # A deploy after the block still counts — and the next block keeps d3. d4
        # had not ended: it is held apart, never accounted.
        route.mock(return_value=_rows(("d4", "queued"), ("d3", "finished")))
        with pytest.raises(SourceRevivedAfterStart, match="web: deployment d4 is queued"):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert _quiesce(ctx)[REVIVAL_SEEN_DEPLOYMENTS] == {"app1": ["d2", "d3"]}
        assert _quiesce(ctx)[REVIVAL_PENDING_DEPLOYMENTS] == {"app1": ["d4"]}

    async def test_a_deploy_still_running_blocks_until_it_ends_and_once_more(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Shown running at a block, it then ran: it may have started the source
        # after that check and been removed again, which no container shows.
        _started(ctx)
        route = respx_mock.get(HISTORY).mock(return_value=_rows(("d3", "in_progress")))
        for _ in range(2):
            with pytest.raises(SourceRevivedAfterStart, match="d3 is in_progress"):
                await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert _quiesce(ctx)[REVIVAL_PENDING_DEPLOYMENTS] == {"app1": ["d3"]}
        route.mock(return_value=_rows(("d3", "cancelled-by-user")))
        with pytest.raises(
            SourceRevivedAfterStart, match="d3 had not ended when this run last blocked"
        ):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert _quiesce(ctx)[REVIVAL_SEEN_DEPLOYMENTS] == {"app1": ["d3"]}
        assert _quiesce(ctx)[REVIVAL_PENDING_DEPLOYMENTS] == {"app1": []}
        # Shown ended now: the next resume goes on.
        await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)

    async def test_the_block_records_the_history_without_a_clock(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _started(ctx)
        ctx.source_host = FakeDaemon(volumes=(VOL,))  # type: ignore[assignment]
        ctx.source_host.on(r"^date \+%s$", stderr="date: cannot run", exit_status=1)  # type: ignore[attr-defined]
        respx_mock.get(HISTORY).mock(return_value=_rows(("d3", "finished")))
        with pytest.raises(SourceRevivedAfterStart):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert _quiesce(ctx)[REVIVAL_SEEN_DEPLOYMENTS] == {"app1": ["d3"]}
        assert REVIVAL_SEEN_CLOCK not in _quiesce(ctx)

    @pytest.mark.parametrize(
        ("answer", "said"),
        [
            (httpx.Response(500, json={"message": "Server Error"}), "500"),
            # A proxy in front of a restarting Coolify: not Coolify saying it is gone.
            (httpx.Response(404, text="404 page not found"), "failed"),
        ],
    )
    async def test_a_history_that_cannot_be_read_never_passes(
        self, ctx: MigrationContext, respx_mock: respx.Router, answer: httpx.Response, said: str
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=answer)
        with pytest.raises(QuiesceError, match="could not read the deployment history of web") as excinfo:
            await containment.assert_contained(ctx, checkpoint="the end of VERIFY")
        assert not isinstance(excinfo.value, SourceRevived)
        assert said in (excinfo.value.hint or "")

    async def test_a_journal_without_the_record_asks_nothing(
        self, tmp_path: Path, respx_mock: respx.Router
    ) -> None:
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            ctx = MigrationContext(
                api=api,
                settings=Settings(_env_file=None, state_dir=tmp_path),
                plan=_app_plan(),
                journal=Journal.create(tmp_path, MID),
                migration_id=MID,
                source_host=_source(),  # type: ignore[arg-type]
                target_host=_target(),  # type: ignore[arg-type]
            )
            ctx.journal.append(
                "step_started", state=State.QUIESCE.value, detail={STOP_CLOCK: STOP}
            )
            await containment.assert_contained(ctx, checkpoint="START", target=False)
        assert not respx_mock.calls

    async def test_the_target_side_reads_no_source_history(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        await containment.assert_contained(ctx, checkpoint="the start of COPY", source=False)
        assert not respx_mock.calls


# ── QUIESCE: the record, just before the stop ───────────────────────────────


class TestTheStopRecord:
    async def test_records_what_had_ended(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=_rows(("d2", "finished"), ("d1", "failed")))
        assert await containment.deployments_before_stop(ctx) == {"app1": ["d1", "d2"]}

    async def test_a_deploy_queued_since_the_wait_refuses_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=_rows(("d3", "queued"), ("d2", "finished")))
        with pytest.raises(SourceDeploymentPending, match="web: deployment d3 is queued") as excinfo:
            await containment.deployments_before_stop(ctx)
        assert "Nothing was stopped" in (excinfo.value.hint or "")

    async def test_an_unreadable_history_refuses_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=httpx.Response(502))
        with pytest.raises(QuiesceError, match="deployment history of web"):
            await containment.deployments_before_stop(ctx)

    async def test_a_plan_without_applications_asks_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan()
        assert await containment.deployments_before_stop(ctx) == {}
        assert not respx_mock.calls

    async def test_quiesce_journals_it_with_the_stop_clock_before_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []

        async def nothing(*_a: Any, **_kw: Any) -> Any:
            return None

        async def no_mounts(_ctx: Any) -> dict[str, list[str]]:
            return {}

        async def no_pins(_ctx: Any) -> list[str]:
            return []

        async def now_on(_host: Any) -> int:
            return STOP

        async def request_stop(_ctx: Any) -> None:
            stop_record = next(
                r.detail for r in ctx.journal.read() if r.detail.get(STOP_CLOCK) == STOP
            )
            order.append(f"stop, recorded {stop_record.get(SOURCE_DEPLOYMENTS)}")

        async def stopped(*_a: Any, **_kw: Any) -> Any:
            return SimpleNamespace(elapsed=0.0)

        async def killed(*_a: Any, **_kw: Any) -> list[tuple[str, int]]:
            return []

        def history(_request: httpx.Request) -> httpx.Response:
            order.append("history")
            return _rows(("d9", "finished"))

        monkeypatch.setattr(steps.deploy_triggers, "disarm_sources", nothing)
        monkeypatch.setattr(steps.deploy_triggers, "await_source_deployments", nothing)
        monkeypatch.setattr(steps, "_capture_mounts", no_mounts)
        monkeypatch.setattr(steps, "_pin_source", no_pins)
        monkeypatch.setattr(steps.quiesce, "now_on", now_on)
        monkeypatch.setattr(steps, "_request_stop", request_stop)
        monkeypatch.setattr(steps.quiesce, "wait_until_stopped", stopped)
        monkeypatch.setattr(steps.quiesce, "killed_since", killed)
        respx_mock.get(HISTORY).mock(side_effect=history)

        await steps.step_quiesce(ctx)

        assert order == ["history", "stop, recorded {'app1': ['d9']}"]
        assert _quiesce(ctx)[SOURCE_DEPLOYMENTS] == {"app1": ["d9"]}
