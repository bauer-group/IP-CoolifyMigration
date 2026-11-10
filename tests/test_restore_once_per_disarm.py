"""A rollback gives a source its deploy switches back once per disarm, never again.

Several outcomes send the operator back to ``coolify-migrate rollback <id>`` - a
KEPT target, a failed restart, a failed compensation - and a replay runs every
compensation again. RESTORE_SOURCE_DEPLOY_TRIGGERS compared the source with its
originals each time, so a source whose auto-deploy the operator had switched off
in between (to freeze it before a second attempt) was switched back on behind
their back. Like RESTART_SOURCE's once-per-stop rule, each restored source is now
journalled, and a replay leaves it as it finds it - until QUIESCE disarms it
again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import deploy_triggers as dt
from bg_coolify_migrate.domain.deploy_triggers import Triggers
from bg_coolify_migrate.domain.statemachine import STOP_REQUESTED, Compensation, Outcome
from bg_coolify_migrate.engine import deploy_triggers as engine
from bg_coolify_migrate.engine.compensations import build_compensations
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.test_deploy_triggers_engine import (
    BASE,
    HOST,
    App,
    _app,
    _host,
    _journal_originals,
    _plan,
)

DISARM = ("step_started", "quiesce", {dt.JOURNAL_KEY: {"app1": {"auto_deploy": True}}})
ON = {"is_auto_deploy_enabled": True}


def _restored(uuid: str) -> tuple[str, str, dict[str, Any]]:
    return ("rollback_step", "create_target", dt.restored_detail(uuid))


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path, deploy_timeout=10.0),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_host(),  # type: ignore[arg-type]
        target_host=_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(engine.asyncio, "sleep", instant)


# ── the decision ─────────────────────────────────────────────────────────────


class TestRestoredSinceDisarm:
    def test_nothing_restored_yet(self) -> None:
        assert dt.restored_since_disarm([]) == set()
        assert dt.restored_since_disarm([DISARM]) == set()

    def test_a_restore_counts_after_the_disarm(self) -> None:
        assert dt.restored_since_disarm([DISARM, _restored("app1"), _restored("app2")]) == {
            "app1",
            "app2",
        }

    def test_a_later_disarm_starts_over(self) -> None:
        # QUIESCE switched the sources off again: the next rollback owes them again.
        assert dt.restored_since_disarm([DISARM, _restored("app1"), DISARM]) == set()
        assert dt.restored_since_disarm([DISARM, _restored("app1"), DISARM, _restored("app2")]) == {
            "app2"
        }

    def test_only_quiesces_disarm_record_resets(self) -> None:
        stop = ("step_started", "quiesce", {STOP_REQUESTED: True})
        elsewhere = ("step_started", "create_target", {dt.JOURNAL_KEY: {}})
        assert dt.restored_since_disarm([DISARM, _restored("app1"), stop, elsewhere]) == {"app1"}

    def test_only_this_compensations_mark_counts(self) -> None:
        restarted = (
            "rollback_step",
            "quiesce",
            {"compensation": Compensation.RESTART_SOURCE.value, "restarted": "app1"},
        )
        ok = (
            "rollback_step",
            "create_target",
            {"compensation": Compensation.RESTORE_SOURCE_DEPLOY_TRIGGERS.value, "ok": True},
        )
        garbage = (
            "rollback_step",
            "create_target",
            {"compensation": Compensation.RESTORE_SOURCE_DEPLOY_TRIGGERS.value, dt.RESTORED_KEY: 7},
        )
        not_a_step = ("step_started", "create_target", dt.restored_detail("app1"))
        assert dt.restored_since_disarm([restarted, ok, garbage, not_a_step]) == set()

    def test_the_journal_accepts_the_mark_and_keeps_it_out_of_the_undo_info(
        self, tmp_path: Path
    ) -> None:
        journal = Journal.create(tmp_path, "m1")
        journal.append("rollback_step", state="create_target", detail=dt.restored_detail("app1"))
        # Rollback bookkeeping, never a fact about CREATE (Journal.undo_info).
        assert dt.RESTORED_KEY not in journal.undo_info("create_target")
        records = [(r.event, r.state, r.detail) for r in journal.read()]
        assert dt.restored_since_disarm(records) == {"app1"}


# ── the engine ───────────────────────────────────────────────────────────────


class TestRestoreOnce:
    async def test_a_replay_leaves_a_switch_the_operator_turned_off_again(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _journal_originals(ctx, app1=Triggers(True, False))
        app = App("app1", auto=False).mount(respx_mock)

        await engine.restore_sources(ctx)
        assert app.patches == [ON]
        assert app.settings is not None
        app.settings["is_auto_deploy_enabled"] = False  # frozen by the operator
        reads = len(respx_mock.calls)

        await engine.restore_sources(ctx)

        assert app.patches == [ON] and app.settings["is_auto_deploy_enabled"] is False
        assert len(respx_mock.calls) == reads, "a restored source is not even read"

    async def test_the_mark_is_journalled_after_the_restore(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _journal_originals(ctx, app1=Triggers(True, False))
        app = App("app1", auto=False)
        marked_before_patch: list[bool] = []

        def patch(request: httpx.Request) -> httpx.Response:
            records = [(r.event, r.state, r.detail) for r in ctx.journal.read()]
            marked_before_patch.append("app1" in dt.restored_since_disarm(records))
            return app.patch(request)

        respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=app.get)
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=patch)

        await engine.restore_sources(ctx)

        assert marked_before_patch == [False]
        [mark] = [r for r in ctx.journal.read() if dt.RESTORED_KEY in r.detail]
        assert (mark.event, mark.state, mark.detail) == _restored("app1")

    async def test_a_source_already_holding_its_originals_is_marked_too(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Switched back on by hand before the rollback: nothing to send, but done.
        _journal_originals(ctx, app1=Triggers(True, False))
        app = App("app1", auto=True).mount(respx_mock)

        await engine.restore_sources(ctx)
        app.settings = {"is_auto_deploy_enabled": False, "is_preview_deployments_enabled": False}
        await engine.restore_sources(ctx)

        assert app.patches == []

    async def test_a_failed_restore_is_not_marked_and_the_replay_retries_it(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_app(), _app("app2", "api"))
        _journal_originals(ctx, app1=Triggers(True, False), app2=Triggers(True, False))
        read_one = respx_mock.get(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(500)
        )
        two = App("app2", auto=False).mount(respx_mock)

        with pytest.raises(RuntimeError, match="auto-deploy NOT switched back on for: shop"):
            await engine.restore_sources(ctx)

        one = App("app1", auto=False)
        read_one.mock(side_effect=one.get)
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=one.patch)
        await engine.restore_sources(ctx)

        assert one.patches == [ON]
        assert two.patches == [ON], "the one already restored is not sent again"

    async def test_a_new_disarm_makes_the_restore_owed_again(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A rollback interrupted after the restore, then `resume`: QUIESCE runs
        # again and switches the source off again - the next rollback owes it.
        _journal_originals(ctx, app1=Triggers(True, False))
        app = App("app1", auto=False).mount(respx_mock)
        await engine.restore_sources(ctx)
        assert app.settings is not None and app.settings["is_auto_deploy_enabled"] is True

        await engine.disarm_sources(ctx)
        assert app.settings["is_auto_deploy_enabled"] is False
        await engine.restore_sources(ctx)

        assert app.patches == [ON, {"is_auto_deploy_enabled": False}, ON]
        # The first originals survive the second record.
        assert engine.recorded(ctx) == {"app1": Triggers(True, False)}

    async def test_a_resumed_quiesce_with_nothing_to_switch_writes_no_record(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The source is off already (this run's own disarm before a crash): no
        # new disarm happens, so no restore mark may be reset.
        _journal_originals(ctx, app1=Triggers(True, False))
        App("app1", auto=False).mount(respx_mock)

        await engine.disarm_sources(ctx)

        records = [r for r in ctx.journal.read() if dt.JOURNAL_KEY in r.detail]
        assert len(records) == 1


# ── the saga ─────────────────────────────────────────────────────────────────


class TestReplayedRollback:
    async def test_the_second_rollback_neither_rearms_nor_restarts_the_source(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.journal.append(
            "step_completed", state="create_target", detail={"target_uuids": {"app1": "appT"}}
        )
        _journal_originals(ctx, app1=Triggers(True, False))
        ctx.journal.append("step_started", state="quiesce", detail={STOP_REQUESTED: True})
        ctx.journal.append("step_completed", state="quiesce", detail={})
        ctx.target_uuids["app1"] = "appT"
        app = App("app1", auto=False).mount(respx_mock)
        restart = respx_mock.post(f"{BASE}/applications/app1/restart").mock(
            return_value=httpx.Response(200, json={})
        )
        respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )

        def saga() -> Saga:
            return Saga(
                journal=ctx.journal, context=ctx, steps={}, compensations=build_compensations()
            )

        first = await saga().rollback()
        assert first.outcome is Outcome.ROLLED_BACK
        assert app.settings is not None
        app.settings["is_auto_deploy_enabled"] = False  # the operator freezes the source

        second = await saga().rollback()

        assert second.outcome is Outcome.ROLLED_BACK
        assert app.patches == [ON]
        assert app.settings["is_auto_deploy_enabled"] is False
        assert restart.call_count == 1
