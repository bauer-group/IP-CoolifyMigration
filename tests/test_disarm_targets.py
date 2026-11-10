"""A rollback after FINALIZE switches the targets' auto-deploy off before it stops them.

FINALIZE arms every application target with its source's auto-deploy - usually
on. A rollback of a SUCCEEDED keep or rename run then stops the target, drops
its volumes and waits up to the deploy timeout for the source's restart before
it deletes the target: armed, a push in that window redeploys it, Compose
recreates its volumes empty, and users DNS still sends there reach an empty
instance. A target the rollback KEEPS stayed armed for good. DISARM_TARGETS,
owed to FINALIZE and the first step of such a rollback, switches off what
``arm_targets`` journalled before each PATCH.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import deploy_triggers as dt
from bg_coolify_migrate.domain.deploy_triggers import Triggers
from bg_coolify_migrate.domain.statemachine import (
    ORDER,
    STOP_REQUESTED,
    Compensation,
    FinalizePolicy,
    Outcome,
    State,
    owed_states,
    rollback_plan,
    states_through,
)
from bg_coolify_migrate.engine import compensations
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
    _db,
    _host,
    _journal_originals,
    _no_tags,
    _plan,
)

OFF = {"is_auto_deploy_enabled": False}


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

    monkeypatch.setattr(compensations.asyncio, "sleep", instant)


def _arm_record(ctx: MigrationContext, *uuids: str) -> None:
    ctx.journal.append("step_started", state="finalize", detail={dt.ARM_REQUESTED: list(uuids)})


# ── the decision ─────────────────────────────────────────────────────────────


class TestDisarmBody:
    def test_an_armed_target_goes_off(self) -> None:
        assert dt.target_disarm_body(Triggers(True, False)) == OFF

    def test_one_already_off_needs_nothing(self) -> None:
        # A replay, or a source that had auto-deploy off: FINALIZE armed it off.
        assert dt.target_disarm_body(Triggers(False, False)) == {}

    def test_an_unreadable_one_goes_off_anyway(self) -> None:
        # 4.1.x returns no settings; the update array takes the field at v4.1.2:2368.
        assert dt.target_disarm_body(Triggers(None, None)) == OFF

    def test_previews_are_never_touched(self) -> None:
        # FINALIZE never armed them, so they are not the rollback's to take back.
        assert dt.target_disarm_body(Triggers(True, True)) == OFF
        assert dt.target_disarm_body(Triggers(False, True)) == {}


class TestJournalledUuids:
    def test_tolerant_and_each_once_in_order(self) -> None:
        assert dt.deserialise_uuids(None) == []
        assert dt.deserialise_uuids("appT") == []
        assert dt.deserialise_uuids(["appT", 3, "", "appT", "app2T"]) == ["appT", "app2T"]

    def test_the_journal_accepts_it(self, tmp_path: Path) -> None:
        # Uuids only: no forbidden key, no secret shape.
        journal = Journal.create(tmp_path, "m1")
        journal.append("step_started", state="finalize", detail={dt.ARM_REQUESTED: ["appT"]})
        assert dt.deserialise_uuids(journal.undo_info("finalize")[dt.ARM_REQUESTED]) == ["appT"]


class TestRollbackPlan:
    def _comps(self, completed: list[State]) -> list[Compensation]:
        return [s.compensation for s in rollback_plan(completed)]

    def test_owed_to_finalize(self) -> None:
        plan = rollback_plan(list(states_through(State.FINALIZE)))
        owed = [s for s in plan if s.compensation is Compensation.DISARM_TARGETS]
        assert [s.because_of for s in owed] == [State.FINALIZE.value]

    def test_first_of_all_before_the_rename_and_the_stop(self) -> None:
        comps = self._comps(list(states_through(State.FINALIZE)))
        assert comps[0] is Compensation.DISARM_TARGETS
        assert comps.index(Compensation.DISARM_TARGETS) < comps.index(
            Compensation.RESTORE_SOURCE_NAME
        )
        assert comps.index(Compensation.DISARM_TARGETS) < comps.index(Compensation.STOP_TARGET)
        assert comps.index(Compensation.DISARM_TARGETS) < comps.index(
            Compensation.DELETE_TARGET_RESOURCE
        )

    @pytest.mark.parametrize("state", ORDER[: ORDER.index(State.FINALIZE)])
    def test_never_owed_before_finalize(self, state: State) -> None:
        # Until FINALIZE every target was created disarmed and stays so.
        assert Compensation.DISARM_TARGETS not in self._comps(list(states_through(state)))

    def test_a_finalize_that_crashed_after_arming_owes_it(self) -> None:
        # The explicit `rollback` owes the state a crashed run was inside.
        completed = [s.value for s in states_through(State.HEALTHCHECK)]
        owed = owed_states(completed, in_flight=State.FINALIZE.value)
        assert self._comps([State(s) for s in owed])[0] is Compensation.DISARM_TARGETS


# ── FINALIZE journals each target before it arms it ─────────────────────────


class TestArmJournal:
    async def test_each_target_is_journalled_before_its_patch(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=Triggers(True, False))
        target = App("appT", auto=False)
        seen: list[list[str]] = []

        def patch(request: httpx.Request) -> httpx.Response:
            seen.append(engine.arm_requested(ctx))
            return target.patch(request)

        respx_mock.get(f"{BASE}/applications/appT").mock(side_effect=target.get)
        respx_mock.patch(f"{BASE}/applications/appT").mock(side_effect=patch)
        _no_tags(respx_mock, "applications", "app1")

        assert await engine.arm_targets(ctx) == ["appT"]

        assert seen == [["appT"]]
        assert target.patches == [{"is_auto_deploy_enabled": True}]

    async def test_every_application_target_and_no_database(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_app(), _db(), _app("app2", "api"))
        ctx.target_uuids.update({"app1": "appT", "db1": "db2", "app2": "app2T"})
        _journal_originals(ctx, app1=Triggers(True, False), app2=Triggers(False, False))
        App("appT", auto=False).mount(respx_mock)
        App("app2T", auto=False).mount(respx_mock)
        for collection, uuid in (("applications", "app1"), ("databases", "db1")):
            _no_tags(respx_mock, collection, uuid)
        _no_tags(respx_mock, "applications", "app2")

        await engine.arm_targets(ctx)

        # A database has no auto-deploy; one armed OFF is journalled all the same
        # (the operator may switch it on after the cutover).
        assert engine.arm_requested(ctx) == ["appT", "app2T"]

    async def test_a_resumed_finalize_journals_no_target_twice(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=Triggers(True, False))
        _arm_record(ctx, "appT")
        App("appT", auto=True).mount(respx_mock)
        _no_tags(respx_mock, "applications", "app1")

        await engine.arm_targets(ctx)

        records = [r for r in ctx.journal.read() if dt.ARM_REQUESTED in r.detail]
        assert len(records) == 1 and engine.arm_requested(ctx) == ["appT"]

    async def test_a_record_that_cannot_be_written_arms_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=Triggers(True, False))
        target = App("appT", auto=False).mount(respx_mock)
        _no_tags(respx_mock, "applications", "app1")
        append = ctx.journal.append

        def refuse(event: str, **kw: Any) -> Any:
            if dt.ARM_REQUESTED in (kw.get("detail") or {}):
                raise OSError("disk full")
            return append(event, **kw)

        monkeypatch.setattr(ctx.journal, "append", refuse)

        # Never a failed FINALIZE (the cutover is done): a notice, and no PATCH
        # nothing could take back.
        assert await engine.arm_targets(ctx) == []

        assert target.patches == []
        [notice] = ctx.notices
        assert "switch 'Auto Deploy' on" in notice


# ── the compensation ─────────────────────────────────────────────────────────


class TestDisarmTargets:
    async def test_an_armed_target_is_switched_off_and_a_replay_sends_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        target = App("appT", auto=True).mount(respx_mock)

        assert await engine.disarm_targets(ctx, {dt.ARM_REQUESTED: ["appT"]}) == ["appT"]
        assert await engine.disarm_targets(ctx, {dt.ARM_REQUESTED: ["appT"]}) == []

        assert target.patches == [OFF]

    async def test_a_target_armed_off_is_left_alone(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        target = App("appT", auto=False).mount(respx_mock)
        assert await engine.disarm_targets(ctx, {dt.ARM_REQUESTED: ["appT"]}) == []
        assert target.patches == []

    async def test_an_unreadable_41_target_is_switched_off(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = ctx.plan.model_copy(update={"coolify_version": "4.1.2"})
        target = App("appT", auto=None).mount(respx_mock)
        await engine.disarm_targets(ctx, {dt.ARM_REQUESTED: ["appT"]})
        assert target.patches == [OFF]

    async def test_a_target_that_is_gone_is_done(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # An earlier rollback of this journal deleted it.
        respx_mock.get(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(404, json={"message": "Application not found."})
        )
        patch = respx_mock.patch(f"{BASE}/applications/appT")
        assert await engine.disarm_targets(ctx, {dt.ARM_REQUESTED: ["appT"]}) == []
        assert not patch.called

    async def test_a_failure_is_loud_and_the_others_still_run(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_app(), _app("app2", "api"))
        ctx.target_uuids.update({"app1": "appT", "app2": "app2T"})
        respx_mock.get(f"{BASE}/applications/appT").mock(return_value=httpx.Response(500))
        two = App("app2T", auto=True).mount(respx_mock)

        with pytest.raises(
            RuntimeError, match=r"^auto-deploy NOT switched off for: the target of shop \(appT"
        ) as err:
            await engine.disarm_targets(ctx, {dt.ARM_REQUESTED: ["appT", "app2T"]})

        assert "switch 'Auto Deploy' off" in str(err.value)
        assert two.patches == [OFF]

    async def test_nothing_journalled_is_a_noop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        assert await engine.disarm_targets(ctx, {}) == []
        assert not respx_mock.calls

    async def test_the_compensation_is_registered(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        handler = build_compensations()[Compensation.DISARM_TARGETS]
        assert handler is compensations.undo_disarm_targets
        _arm_record(ctx, "appT")
        target = App("appT", auto=True).mount(respx_mock)

        await handler(ctx, ctx.journal.undo_info("finalize"))

        assert target.patches == [OFF]


# ── the saga, against the API ────────────────────────────────────────────────


class TestRollbackAfterASucceededRun:
    """The first thing a rollback after FINALIZE does is take the targets' trigger away."""

    def _journal(self, ctx: MigrationContext, policy: FinalizePolicy) -> None:
        ctx.target_uuids["app1"] = "appT"
        journal = ctx.journal
        journal.append(
            "step_completed", state="create_target", detail={"target_uuids": {"app1": "appT"}}
        )
        _journal_originals(ctx, app1=Triggers(True, False))
        journal.append("step_started", state="quiesce", detail={STOP_REQUESTED: True})
        journal.append("step_completed", state="quiesce", detail={})
        journal.append("step_completed", state="start_target", detail={"started": ["appT"]})
        journal.append("step_started", state="finalize", detail={"policy": policy.value})
        _arm_record(ctx, "appT")
        journal.append(
            "step_completed",
            state="finalize",
            detail={"policy": policy.value, "target_armed": ["appT"]},
        )

    def _routes(self, respx_mock: respx.Router) -> tuple[App, App, list[str]]:
        order: list[str] = []
        source = App("app1", auto=False)
        target = App("appT", auto=True)

        def source_patch(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.read())
            order.append("rename source" if "name" in body else "source switches")
            return source.patch(request)

        def target_patch(request: httpx.Request) -> httpx.Response:
            order.append("disarm target")
            return target.patch(request)

        def mark(label: str) -> Any:
            def handler(_request: httpx.Request) -> httpx.Response:
                order.append(label)
                return httpx.Response(200, json={})

            return handler

        respx_mock.get(f"{BASE}/applications/app1").mock(side_effect=source.get)
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=source_patch)
        respx_mock.get(f"{BASE}/applications/appT").mock(side_effect=target.get)
        respx_mock.patch(f"{BASE}/applications/appT").mock(side_effect=target_patch)
        respx_mock.get(f"{BASE}/deployments/applications/appT").mock(
            return_value=httpx.Response(200, json={"count": 0, "deployments": []})
        )
        respx_mock.post(f"{BASE}/applications/appT/stop").mock(side_effect=mark("stop target"))
        respx_mock.post(f"{BASE}/applications/app1/restart").mock(side_effect=mark("restart"))
        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=mark("delete target"))
        return source, target, order

    @pytest.mark.parametrize(
        ("policy", "renamed"),
        [(FinalizePolicy.KEEP, []), (FinalizePolicy.RENAME, ["rename source"])],
    )
    async def test_the_target_is_disarmed_before_the_rename_and_the_stop(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        policy: FinalizePolicy,
        renamed: list[str],
    ) -> None:
        ctx.plan = _plan(policy=policy)
        self._journal(ctx, policy)
        source, target, order = self._routes(respx_mock)

        saga = Saga(journal=ctx.journal, context=ctx, steps={}, compensations=build_compensations())
        result = await saga.rollback()

        assert result.outcome is Outcome.ROLLED_BACK
        assert order == [
            "disarm target",
            *renamed,
            "stop target",
            "source switches",
            "restart",
            "delete target",
        ]
        assert result.compensations_run[0] is Compensation.DISARM_TARGETS
        assert target.settings is not None and target.settings["is_auto_deploy_enabled"] is False
        assert source.settings is not None and source.settings["is_auto_deploy_enabled"] is True
