"""The deployment-history witness at its edges: gone, unread, and still running.

* A source application Coolify no longer has answers its own 404 — FINALIZE
  deleted it, or the operator did after a block. That is no unread history: a
  part-way FINALIZE(delete) must be finishable by ``resume``, and a resume after
  the operator deleted a revived source must not roll the cutover back. A
  proxy's 404 in front of a restarting Coolify is not that answer.
* An unread history once START asked for a target BLOCKS: a Coolify API blip at
  FINALIZE must not roll a cutover back. A revival found in the same check is
  raised first, with what could not be read beside it.
* A deployment a block showed still queued or running is never accounted: once
  it has ended it is reported once more, wherever it sorts.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from rich.console import Console

from bg_coolify_migrate.api.client import CoolifyClient
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
from bg_coolify_migrate.domain.plan import (
    FinalizePolicy,
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import Outcome, State
from bg_coolify_migrate.engine import containment
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import (
    CoolifyApiError,
    QuiesceError,
    RollbackError,
    SourceRevived,
    SourceRevivedAfterStart,
    SourceUncheckedAfterStart,
)
from bg_coolify_migrate.ui.console import THEME
from bg_coolify_migrate.ui.run_report import outcome_panel
from tests.fake_daemon import FakeDaemon
from tests.test_containment_history import HISTORY, _app_plan, _quiesce, _rows, _started
from tests.test_containment_history import ctx as ctx  # the fixture
from tests.test_engine_steps import BASE, HOST
from tests.test_rollback_guards import _at_finalize, _saga

API_HISTORY = f"{BASE}/deployments/applications/app3"
GONE = httpx.Response(404, json={"message": "Application not found"})
PROXY_404 = httpx.Response(404, text="404 page not found")
WEB = c.Watched(name="web", uuid="app1", labels={})


def _d(uuid: str, status: str = "finished") -> Deployment:
    return Deployment(uuid=uuid, status=status)


def _two_apps() -> MigrationPlan:
    """postgres, web (app1) and api (app3)."""
    api = ResourcePlan(
        snapshot=ResourceSnapshot(
            uuid="app3", name="api", collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
        ),
        strategy=Strategy.RECREATE_ONLY,
        manifest=VolumeManifest(),
    )
    base = _app_plan()
    return base.model_copy(update={"resources": (*base.resources, api)})


def _record_stop(ctx: MigrationContext, record: dict[str, list[str]]) -> None:
    ctx.journal.append(
        "step_started",
        state=State.QUIESCE.value,
        detail={STOP_CLOCK: 1, STOPPED_CLOCK: 2, SOURCE_DEPLOYMENTS: record},
    )


# ── the client: whose 404 it is ─────────────────────────────────────────────


class TestWhoseNotFound:
    async def test_coolify_saying_it_has_no_such_application(
        self, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=GONE)
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            assert await api.application_deployments("app1") is None

    @pytest.mark.parametrize(
        "answer", [PROXY_404, httpx.Response(404, json={"message": "Not Found."})]
    )
    async def test_any_other_not_found_raises(
        self, respx_mock: respx.Router, answer: httpx.Response
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=answer)
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            with pytest.raises(CoolifyApiError) as excinfo:
                await api.application_deployments("app1")
        assert excinfo.value.status_code == 404


# ── the rules: a block's record and what a resume then reports ──────────────


class TestTheBlockRecord:
    def test_what_had_not_ended_is_held_apart(self) -> None:
        history = {"app1": [_d("d4", "queued"), _d("d3"), _d("d5", "")]}
        record = c.block_record({SOURCE_DEPLOYMENTS: {"app1": ["d1"]}}, history)
        assert record == {
            REVIVAL_SEEN_DEPLOYMENTS: {"app1": ["d3"]},
            REVIVAL_PENDING_DEPLOYMENTS: {"app1": ["d4", "d5"]},
        }

    def test_a_pending_row_read_ended_is_accounted_and_one_not_listed_stays(self) -> None:
        detail = {
            SOURCE_DEPLOYMENTS: {"app1": ["d1"]},
            REVIVAL_SEEN_DEPLOYMENTS: {"app1": ["d2"]},
            REVIVAL_PENDING_DEPLOYMENTS: {"app1": ["d3", "d4"], "app9": ["x1"]},
        }
        record = c.block_record(detail, {"app1": [_d("d5", "in_progress"), _d("d3", "failed")]})
        assert record[REVIVAL_SEEN_DEPLOYMENTS] == {"app1": ["d2", "d3"]}
        assert record[REVIVAL_PENDING_DEPLOYMENTS] == {"app1": ["d4", "d5"], "app9": ["x1"]}

    def test_a_row_that_had_ended_is_never_pending_again(self) -> None:
        # An unread status for a row the stop or a block saw ended is a read that
        # failed: reported at this check, but not held as pending for good.
        detail = {SOURCE_DEPLOYMENTS: {"app1": ["d1"]}, REVIVAL_SEEN_DEPLOYMENTS: {"app1": ["d2"]}}
        record = c.block_record(detail, {"app1": [_d("d2", ""), _d("d1", "")]})
        assert record[REVIVAL_PENDING_DEPLOYMENTS] == {"app1": []}

    def test_pending_at_block_reads_what_was_journalled(self) -> None:
        assert c.pending_at_block({}) == {}
        assert c.pending_at_block({REVIVAL_PENDING_DEPLOYMENTS: "junk"}) == {}
        assert c.pending_at_block({REVIVAL_PENDING_DEPLOYMENTS: {"app1": ["d3", 4]}}) == {
            "app1": frozenset({"d3"})
        }


class TestPendingAtABlock:
    def _judge(self, rows: list[Deployment], known: set[str], pending: set[str]) -> list[str]:
        breaches = c.deployment_breaches(
            [WEB],
            {"app1": frozenset(known)},
            {"app1": rows},
            pending={"app1": frozenset(pending)},
        )
        return [b.describe() for b in breaches]

    def test_once_ended_it_counts_below_a_newer_row_the_block_saw_ended(self) -> None:
        # The block saw d4 finished and d3 still running. d3 then ran and ended;
        # it sorts below d4, which is accounted.
        (line,) = self._judge([_d("d4"), _d("d3", "cancelled-by-user")], {"d4"}, {"d3"})
        assert line.startswith(
            "web: deployment d3 had not ended when this run last blocked, "
            "and is cancelled-by-user now"
        )

    def test_while_pending_it_counts_as_pending(self) -> None:
        (line,) = self._judge([_d("d4"), _d("d3", "in_progress")], {"d4"}, {"d3"})
        assert "d3 is in_progress" in line

    def test_without_the_record_a_row_below_a_known_one_existed_at_the_stop(self) -> None:
        assert self._judge([_d("d4"), _d("d3", "failed")], {"d4"}, set()) == []

    def test_once_accounted_it_is_not_reported_again(self) -> None:
        assert self._judge([_d("d4"), _d("d3")], {"d3", "d4"}, {"d3"}) == []


class TestTheWording:
    def test_unread_histories_name_each_application_and_what_coolify_said(self) -> None:
        unread = [("web", "GET /x failed"), ("api", "timed out")]
        assert c.unread_history_message(unread, "FINALIZE") == (
            "could not read the deployment history of web, api to check that nothing "
            "deployed the stopped source (at FINALIZE)"
        )
        hint = c.unread_history_hint(unread)
        assert "Coolify said of web: GET /x failed" in hint
        assert "Coolify said of api: timed out" in hint
        (line,) = (b.describe() for b in c.unread_breaches(unread[:1]))
        assert line == "web: its deployment history could not be read either (GET /x failed)"

    @pytest.mark.parametrize(
        ("policy", "fate"),
        [("delete", "deletes the source"), ("keep", "leaves the source stopped"), ("x", "applies")],
    )
    def test_the_block_says_resume_once_coolify_answers(self, policy: str, fate: str) -> None:
        hint = c.unchecked_after_start_hint(
            policy=policy, migration_id="m9", unread=[("web", "502")]
        )
        assert "Once Coolify answers again: coolify-migrate resume m9" in hint
        assert fate in hint and "coolify-migrate rollback m9" in hint
        assert "Coolify said of web: 502" in hint
        assert "does not roll back" in hint


# ── the checkpoint ──────────────────────────────────────────────────────────


class TestAnApplicationCoolifyNoLongerHas:
    async def test_has_no_history_to_compare(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get(HISTORY).mock(return_value=GONE)
        await containment.assert_contained(ctx, checkpoint="the end of VERIFY")
        assert route.call_count == 1

    async def test_its_containers_still_count(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=GONE)
        source: FakeDaemon = ctx.source_host  # type: ignore[assignment]
        source.add("app1-120000000000", labels={"coolify.managed": "true"})
        with pytest.raises(SourceRevived, match="app1-120000000000 is running"):
            await containment.assert_contained(ctx, checkpoint="the end of VERIFY")

    async def test_one_finalize_deleted_is_not_asked(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get(HISTORY).mock(return_value=PROXY_404)
        ctx.journal.append("step_started", state=State.FINALIZE.value, detail={"deleted": ["app1"]})
        await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert not route.called

    async def test_the_stop_still_refuses_one(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Before the stop there is nothing to migrate it from.
        respx_mock.get(HISTORY).mock(return_value=GONE)
        with pytest.raises(QuiesceError, match="deployment history of web") as excinfo:
            await containment.deployments_before_stop(ctx)
        assert "no such application any more" in (excinfo.value.hint or "")


class TestAnUnreadHistoryAfterStart:
    async def test_blocks_records_nothing_and_the_resume_checks_again(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _started(ctx)
        route = respx_mock.get(HISTORY).mock(return_value=PROXY_404)
        before = _quiesce(ctx)
        with pytest.raises(SourceUncheckedAfterStart) as excinfo:
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert isinstance(excinfo.value, SourceRevivedAfterStart), "the executor blocks on it"
        assert "could not read the deployment history of web" in str(excinfo.value)
        assert "Once Coolify answers again" in (excinfo.value.hint or "")
        assert _quiesce(ctx) == before, "nothing was shown, so nothing is forgiven"
        assert REVIVAL_SEEN_CLOCK not in _quiesce(ctx)
        route.mock(return_value=_rows(("d2", "finished"), ("d1", "finished")))
        await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)

    async def test_a_revival_found_beside_it_is_raised_first(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _started(ctx)
        ctx.plan = _two_apps()
        _record_stop(ctx, {"app1": ["d1", "d2"], "app3": ["e1"]})
        respx_mock.get(HISTORY).mock(return_value=_rows(("d3", "failed"), ("d2", "finished")))
        respx_mock.get(API_HISTORY).mock(return_value=httpx.Response(502))
        with pytest.raises(SourceRevivedAfterStart) as excinfo:
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert not isinstance(excinfo.value, SourceUncheckedAfterStart)
        message = str(excinfo.value)
        assert "web: deployment d3 came after the stop" in message
        assert "api: its deployment history could not be read either" in message
        # What was read is recorded; api is read again by the resume.
        assert _quiesce(ctx)[REVIVAL_SEEN_DEPLOYMENTS] == {"app1": ["d2", "d3"]}

    async def test_a_running_source_container_is_never_hidden_by_it(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _started(ctx)
        respx_mock.get(HISTORY).mock(return_value=httpx.Response(503))
        source: FakeDaemon = ctx.source_host  # type: ignore[assignment]
        source.add("app1-120000000000", labels={"coolify.managed": "true"})
        with pytest.raises(SourceRevivedAfterStart) as excinfo:
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert not isinstance(excinfo.value, SourceUncheckedAfterStart)
        assert "app1-120000000000 is running" in str(excinfo.value)
        assert _quiesce(ctx)[REVIVAL_SEEN_CLOCK] > 0

    async def test_before_start_a_revival_beside_it_still_rolls_back(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=httpx.Response(503))
        source: FakeDaemon = ctx.source_host  # type: ignore[assignment]
        source.add("app1-120000000000", labels={"coolify.managed": "true"})
        with pytest.raises(SourceRevived) as excinfo:
            await containment.assert_contained(ctx, checkpoint="the DNS gate")
        assert not isinstance(excinfo.value, SourceRevivedAfterStart)
        assert "web: its deployment history could not be read either" in str(excinfo.value)


# ── the whole run: FINALIZE, resumed ────────────────────────────────────────


def _finalize_plan(*, app_first: bool = True) -> MigrationPlan:
    base = _app_plan()
    db, app = base.resources[0], base.resources[-1]
    return base.model_copy(
        update={
            "finalize_policy": FinalizePolicy.DELETE,
            "resources": (app, db) if app_first else (app,),
        }
    )


class TestAPartWayDeleteWithAnApplication:
    async def test_resume_skips_the_deleted_application_and_finishes(
        self, tmp_path: Path, respx_mock: respx.Router
    ) -> None:
        # FINALIZE deleted web, then the database's DELETE failed. The rollback is
        # refused (the target holds the only copy of web); resume must finish.
        history = respx_mock.get(HISTORY).mock(side_effect=[_rows(("d1", "finished")), GONE])
        app = respx_mock.delete(url__regex=rf"{BASE}/applications/app1.*").mock(
            return_value=httpx.Response(200, json={})
        )
        db = respx_mock.delete(url__regex=rf"{BASE}/databases/db1.*").mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json={})]
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            ctx = _at_finalize(tmp_path, api, _finalize_plan())
            _record_stop(ctx, {"app1": ["d1"]})
            with pytest.raises(RollbackError, match="rollback refused"):
                await _saga(ctx).run()
            assert ctx.journal.undo_info(State.FINALIZE.value)["deleted"] == ["app1"]

            result = await _saga(ctx).run()

        assert result.outcome is Outcome.SUCCEEDED
        assert app.call_count == 1 and db.call_count == 2
        assert history.call_count == 1, "the deleted application is not asked again"


class TestABlockThenTheSourceDeletedByHand:
    async def test_the_resume_finishes_instead_of_rolling_back(
        self, tmp_path: Path, respx_mock: respx.Router
    ) -> None:
        # Blocked at FINALIZE over a deploy of web; the operator deleted web in
        # Coolify instead of stopping it, then resumed. No compensation may run:
        # the target has served users since START.
        respx_mock.get(HISTORY).mock(
            side_effect=[_rows(("d3", "failed"), ("d1", "finished")), GONE]
        )
        respx_mock.route().mock(return_value=httpx.Response(404, json={"message": "not found"}))
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            ctx = _at_finalize(tmp_path, api, _finalize_plan(app_first=False))
            _record_stop(ctx, {"app1": ["d1"]})
            first = await _saga(ctx).run()
            second = await _saga(ctx).run()

        assert first.outcome is Outcome.BLOCKED
        assert isinstance(first.error, SourceRevivedAfterStart)
        assert second.outcome is Outcome.SUCCEEDED
        assert second.compensations_run == []


class TestAnApiBlipAtFinalize:
    async def test_blocks_the_run_instead_of_rolling_the_cutover_back(
        self, tmp_path: Path, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(HISTORY).mock(return_value=httpx.Response(503))
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            ctx = _at_finalize(tmp_path, api, _finalize_plan(app_first=False))
            _record_stop(ctx, {"app1": ["d1"]})
            result = await _saga(ctx).run()

        assert result.outcome is Outcome.BLOCKED
        assert isinstance(result.error, SourceUncheckedAfterStart)
        assert result.compensations_run == []
        assert "policy" not in ctx.journal.undo_info(State.FINALIZE.value), "nothing touched"

        console = Console(width=200, force_terminal=False, no_color=True, theme=THEME)
        with console.capture() as capture:
            console.print(outcome_panel(result, migration_id="g6"))
        out = capture.get()
        assert "the source could not be checked" in out
        assert "could not be\nchecked" in out or "could not be checked" in out
        assert "the source was started again" not in out
        assert "Resume once Coolify answers" in out
