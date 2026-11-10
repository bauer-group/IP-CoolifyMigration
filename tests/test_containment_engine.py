"""The containment checkpoints, against a daemon model.

From QUIESCE to FINALIZE the run holds the source stopped and the target
unstarted; a git push with auto-deploy on, a CI ``/deploy`` or a dashboard
deploy breaks either without a word. Each checkpoint asks both daemons and
refuses, and the run rolls back — the source, never deleted before FINALIZE,
keeps what it wrote. Also here: START refusing a target someone else started,
and STOP_TARGET owed as soon as START asked for anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.discovery import pins
from bg_coolify_migrate.domain.containment import STOP_CLOCK
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import VolumeManifest
from bg_coolify_migrate.domain.naming import VolumeEndpoint, VolumePair
from bg_coolify_migrate.domain.plan import ResourcePlan, ResourceSnapshot, Strategy
from bg_coolify_migrate.domain.statemachine import (
    START_REQUESTED,
    STARTED_EARLY,
    STOP_REQUESTED,
    Compensation,
    Outcome,
    State,
    rollback_refusal,
)
from bg_coolify_migrate.engine import compensations, containment, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import (
    AlreadyRunning,
    CoolifyApiError,
    QuiesceError,
    SourceRevived,
    SourceRevivedAfterStart,
    TargetStartedEarly,
)
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.fake_daemon import FakeDaemon
from tests.test_engine_steps import BASE, HOST, _plan
from tests.test_pins_engine import COOLIFY, MID, VOL, _labels, _source, _target

TVOL = "postgres-data-db2"
MANAGED = {"coolify.managed": "true"}
STOP = 1_800_000_000  # the source's clock at the stop: 2027-01-15 08:00:00 UTC
AFTER = "2027-01-15T08:05:00.000000001Z"
BEFORE = "2027-01-15T07:00:00Z"


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, MID),
        migration_id=MID,
        source_host=_source(VOL),  # type: ignore[arg-type]
        target_host=_target(TVOL),  # type: ignore[arg-type]
    )
    context.target_uuids["db1"] = "db2"
    context.journal.append(
        "step_started", state=State.QUIESCE.value, detail={STOP_REQUESTED: True, STOP_CLOCK: STOP}
    )
    yield context
    await api.aclose()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def instant(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(steps.asyncio, "sleep", instant)
    return slept


def _src(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.source_host, FakeDaemon)
    return ctx.source_host


def _tgt(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.target_host, FakeDaemon)
    return ctx.target_host


def _event(name: str, when: int, **attributes: str) -> str:
    return json.dumps(
        {
            "Type": "container",
            "Action": "start",
            "Actor": {"Attributes": {"name": name, **attributes}},
            "time": when,
        }
    )


def _start_detail(ctx: MigrationContext) -> dict[str, Any]:
    return ctx.journal.undo_info(State.START_TARGET.value)


# ── the checkpoint ───────────────────────────────────────────────────────────


class TestQuietStacks:
    async def test_pass_and_ask_both_daemons(self, ctx: MigrationContext) -> None:
        # A pin of ours and one of another migration mount the source's volume:
        # created, never started, never a writer.
        _src(ctx).add("pin-a", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
        _src(ctx).add(
            "pin-b", state="created", labels=_labels(pins.SOURCE, "m0"), volumes=(VOL,)
        )
        # An exited one-shot from before the stop: it has not run since.
        _src(ctx).add("init-db1", state="exited", labels=COOLIFY).started_at = BEFORE

        await containment.assert_contained(ctx, checkpoint="the DNS gate")

        for daemon in (_src(ctx), _tgt(ctx)):
            assert any("docker events" in c for c in daemon.commands)
        assert any(f"--since {STOP}" in c for c in _src(ctx).commands)
        assert any(f"volume={VOL}" in c for c in _src(ctx).commands), "the volume probe"


class TestASourceRevived:
    async def test_a_running_source_container(self, ctx: MigrationContext) -> None:
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevived) as excinfo:
            await containment.assert_contained(ctx, checkpoint="the DNS gate")
        assert "(found at the DNS gate)" in str(excinfo.value)
        assert "postgres: postgres-db1 is running" in str(excinfo.value)
        assert excinfo.value.exit_code == 5, "the stack did not stay stopped"
        assert isinstance(excinfo.value, QuiesceError)
        assert "auto-deploy" in (excinfo.value.hint or "")

    async def test_one_started_after_the_stop_and_gone_quiet(self, ctx: MigrationContext) -> None:
        _src(ctx).add("postgres-db1", state="exited", labels=COOLIFY).started_at = AFTER
        with pytest.raises(SourceRevived, match="after the stop, and has stopped again"):
            await containment.assert_contained(ctx, checkpoint="START")

    async def test_one_that_came_and_went(self, ctx: MigrationContext) -> None:
        _src(ctx).events = f"oldest {STOP - 10}\n" + _event("postgres-db1-x", STOP + 30)
        with pytest.raises(SourceRevived, match="the daemon's event log"):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)

    async def test_renamed_after_plan_and_deployed(self, ctx: MigrationContext) -> None:
        # The deploy stamped the NEW project name; only the uuid probe sees it.
        renamed = {**COOLIFY, "coolify.projectName": "shop-renamed"}
        _src(ctx).add("postgres-db1", labels=renamed)
        with pytest.raises(SourceRevived, match="postgres: postgres-db1 is running"):
            await containment.assert_contained(ctx, checkpoint="the end of VERIFY")

    async def test_a_writer_found_only_by_the_volume_it_mounts(self, ctx: MigrationContext) -> None:
        _src(ctx).add("debug-shell", labels={}, volumes=(VOL,))
        with pytest.raises(SourceRevived, match="the stack: debug-shell is running"):
            await containment.assert_contained(ctx, checkpoint="COPY", target=False)

    async def test_after_start_it_blocks_instead_of_rolling_back(
        self, ctx: MigrationContext
    ) -> None:
        # tests/test_containment_after_start.py has the rest of this case.
        ctx.journal.append("step_completed", state=State.START_TARGET.value, detail={})
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevivedAfterStart) as excinfo:
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert "does not roll back by itself" in (excinfo.value.hint or "")


class TestWithoutTheStopClock:
    async def test_only_a_running_container_counts(self, tmp_path: Path) -> None:
        # A journal from before the record: no window to judge StartedAt or events by.
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            ctx = MigrationContext(
                api=api,
                settings=Settings(_env_file=None, state_dir=tmp_path),
                plan=_plan(),
                journal=Journal.create(tmp_path, MID),
                migration_id=MID,
                source_host=_source(VOL),  # type: ignore[arg-type]
                target_host=_target(),  # type: ignore[arg-type]
            )
            _src(ctx).add("postgres-db1", state="exited", labels=COOLIFY).started_at = AFTER
            await containment.assert_contained(ctx, checkpoint="START", target=False)
            assert not any("docker events" in c for c in _src(ctx).commands)
            _src(ctx).add("postgres-db1-2", labels=COOLIFY)
            with pytest.raises(SourceRevived):
                await containment.assert_contained(ctx, checkpoint="START", target=False)


class TestAnEventLogThatCannotBeRead:
    async def test_is_never_taken_for_nothing_started(self, ctx: MigrationContext) -> None:
        _tgt(ctx).on(r"docker events", stdout="", stderr="Cannot connect", exit_status=1)
        with pytest.raises(QuiesceError, match="could not read the docker event log") as excinfo:
            await containment.assert_contained(ctx, checkpoint="the start of COPY", source=False)
        assert not isinstance(excinfo.value, TargetStartedEarly)

    async def test_a_window_the_log_no_longer_reaches_is_not_a_breach(
        self, ctx: MigrationContext
    ) -> None:
        # The daemon dropped everything before STOP+100; what still exists is checked.
        _src(ctx).events = f"oldest {STOP + 100}\n"
        await containment.assert_contained(ctx, checkpoint="the DNS gate")


class TestATargetStartedEarly:
    async def test_a_running_target_is_journalled_and_refused(self, ctx: MigrationContext) -> None:
        _tgt(ctx).add("db2", labels={**COOLIFY, "coolify.name": "db2"})
        with pytest.raises(TargetStartedEarly) as excinfo:
            await containment.assert_contained(ctx, checkpoint="the start of COPY", source=False)
        assert "postgres: db2 is running" in str(excinfo.value)
        assert "ACME" in (excinfo.value.hint or "")
        # The rollback owes STOP_TARGET for it although START never ran.
        assert _start_detail(ctx)[STARTED_EARLY] == ["db2"]

    async def test_one_that_ran_and_stopped(self, ctx: MigrationContext) -> None:
        _tgt(ctx).add("db2", state="exited", labels=MANAGED).started_at = BEFORE
        with pytest.raises(TargetStartedEarly, match="has stopped again"):
            await containment.assert_contained(ctx, checkpoint="the end of VERIFY")

    async def test_one_that_came_and_went(self, ctx: MigrationContext) -> None:
        _tgt(ctx).events = "oldest 1\n" + _event("x", 5, **{"com.docker.compose.project": "db2"})
        with pytest.raises(TargetStartedEarly, match="the daemon's event log"):
            await containment.assert_contained(ctx, checkpoint="the DNS gate")

    async def test_its_own_pins_are_not_starts(self, ctx: MigrationContext) -> None:
        _tgt(ctx).add("pin-t", state="created", labels=_labels(pins.TARGET), volumes=(TVOL,))
        await containment.assert_contained(ctx, checkpoint="the end of COPY")

    async def test_a_target_start_asked_for_is_not_watched(self, ctx: MigrationContext) -> None:
        ctx.journal.append(
            "step_started", state=State.START_TARGET.value, detail={START_REQUESTED: ["db2"]}
        )
        _tgt(ctx).add("db2", labels=MANAGED)
        await containment.assert_contained(ctx, checkpoint="START")
        assert not any("docker events" in c for c in _tgt(ctx).commands), "nothing to watch"

    async def test_both_at_once_name_both_and_journal_the_target(
        self, ctx: MigrationContext
    ) -> None:
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        _tgt(ctx).add("db2", labels=MANAGED)
        with pytest.raises(SourceRevived) as excinfo:
            await containment.assert_contained(ctx, checkpoint="the DNS gate")
        assert "postgres-db1 is running" in str(excinfo.value)
        assert "db2 is running" in str(excinfo.value)
        assert _start_detail(ctx)[STARTED_EARLY] == ["db2"]


# ── where the steps ask ──────────────────────────────────────────────────────


class TestTheSteps:
    async def test_copy_without_volumes_still_checks_the_source(
        self, ctx: MigrationContext
    ) -> None:
        # COPY used to return before any check when there was nothing to copy.
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevived, match=r"found at COPY\)"):
            await steps.step_copy(ctx)

    async def test_copy_refuses_a_started_target_before_writing_to_it(
        self, ctx: MigrationContext
    ) -> None:
        ctx.volume_pairs = {
            "db1": [VolumePair(VolumeEndpoint(VOL, "/data"), VolumeEndpoint(TVOL, "/data"))]
        }
        _tgt(ctx).add("db2", labels=MANAGED)
        with pytest.raises(TargetStartedEarly, match="the start of COPY"):
            await steps.step_copy(ctx)
        assert not any("docker volume create" in c for c in _tgt(ctx).commands)
        assert not any("mkdir -p /root/.coolify-migrate" in c for c in _src(ctx).commands)

    async def test_verify_checks_at_its_end(self, ctx: MigrationContext) -> None:
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevived, match="the end of VERIFY"):
            await steps.step_verify(ctx)

    async def test_the_dns_gate_checks_first_on_every_attempt(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevived, match="the DNS gate"):
            await steps.step_dns_gate(ctx)
        assert not respx_mock.calls, "refused before it read a single domain"

    async def test_finalize_checks_before_it_journals_its_policy(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevived, match="FINALIZE"):
            await steps.step_finalize(ctx)
        # Nothing journalled, nothing touched: the rollback runs like at any checkpoint.
        assert "policy" not in ctx.journal.undo_info(State.FINALIZE.value)
        assert rollback_refusal(ctx.journal.details()) is None
        assert not respx_mock.calls
        assert not any("docker rm" in c for c in _src(ctx).commands)


# ── START ────────────────────────────────────────────────────────────────────


def _app_plan() -> Any:
    app = ResourcePlan(
        snapshot=ResourceSnapshot(
            uuid="app1", name="web", collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
        ),
        strategy=Strategy.RECREATE_ONLY,
        manifest=VolumeManifest(),
    )
    return _plan(resources=(*_plan().resources, app))


class TestStart:
    async def test_each_request_is_journalled_before_it_is_made(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        asked: list[Any] = []

        def answer(request: httpx.Request) -> httpx.Response:
            asked.append(list(_start_detail(ctx).get(START_REQUESTED, [])))
            return httpx.Response(200, json={"message": "Database starting request queued."})

        respx_mock.post(f"{BASE}/databases/db2/start").mock(side_effect=answer)
        detail = await steps.step_start_target(ctx)
        assert asked == [["db2"]], "journalled before Coolify was asked"
        assert detail == {"started": ["db2"], "deployments": {}}

    async def test_already_running_is_an_early_start_never_a_success(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.post(f"{BASE}/databases/db2/start").mock(
            return_value=httpx.Response(400, json={"message": "Database is already running."})
        )
        with pytest.raises(TargetStartedEarly, match="reading it as already running"):
            await steps.step_start_target(ctx)
        assert _start_detail(ctx)[STARTED_EARLY] == ["db2"]

    async def test_a_skipped_application_start_is_an_early_start(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _app_plan()
        ctx.target_uuids["app1"] = "tgt1"
        respx_mock.post(f"{BASE}/databases/db2/start").mock(
            return_value=httpx.Response(200, json={"message": "queued"})
        )
        respx_mock.post(f"{BASE}/applications/tgt1/start").mock(
            return_value=httpx.Response(
                200, json={"message": "Deployment already queued for this commit."}
            )
        )
        with pytest.raises(TargetStartedEarly, match="one this run did not ask for") as excinfo:
            await steps.step_start_target(ctx)
        assert "interruption" not in str(excinfo.value)
        detail = _start_detail(ctx)
        assert detail[START_REQUESTED] == ["db2", "tgt1"]
        assert detail[STARTED_EARLY] == ["tgt1"]

    async def test_a_target_found_running_refuses_before_any_start(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _tgt(ctx).add("db2", labels=MANAGED)
        with pytest.raises(TargetStartedEarly, match=r"\(found at START\)"):
            await steps.step_start_target(ctx)
        assert not respx_mock.calls

    async def test_a_resume_keeps_what_its_journal_shows_accepted(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _app_plan()
        ctx.target_uuids["app1"] = "tgt1"
        ctx.journal.append(
            "step_started",
            state=State.START_TARGET.value,
            detail={START_REQUESTED: ["db2", "tgt1"], "deployments": {"app1": "dep1"}},
        )
        _tgt(ctx).add("db2", labels=MANAGED)  # ours, asked for before the interruption
        db = respx_mock.post(f"{BASE}/databases/db2/start").mock(
            return_value=httpx.Response(400, json={"message": "Database is already running."})
        )
        app = respx_mock.post(f"{BASE}/applications/tgt1/start")
        detail = await steps.step_start_target(ctx)
        assert detail == {"started": ["db2", "tgt1"], "deployments": {"app1": "dep1"}}
        assert db.called and not app.called, "asking again would be skipped for dep1"

    async def test_a_resume_without_the_accepted_deployment_says_it_may_be_its_own(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _app_plan()
        ctx.target_uuids["app1"] = "tgt1"
        ctx.journal.append(
            "step_started", state=State.START_TARGET.value, detail={START_REQUESTED: ["tgt1"]}
        )
        respx_mock.post(f"{BASE}/databases/db2/start").mock(
            return_value=httpx.Response(200, json={})
        )
        respx_mock.post(f"{BASE}/applications/tgt1/start").mock(
            return_value=httpx.Response(200, json={"message": "skipped"})
        )
        with pytest.raises(TargetStartedEarly, match="before an interruption"):
            await steps.step_start_target(ctx)


class TestTheClient:
    async def test_already_running_is_its_own_error(self, respx_mock: respx.Router) -> None:
        respx_mock.post(f"{BASE}/services/s1/start").mock(
            return_value=httpx.Response(400, json={"message": "Service is already running."})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            with pytest.raises(AlreadyRunning) as excinfo:
                await api.start("services", "s1")
        assert excinfo.value.status_code == 400

    async def test_any_other_refusal_stays_an_api_error(self, respx_mock: respx.Router) -> None:
        respx_mock.post(f"{BASE}/services/s1/start").mock(
            return_value=httpx.Response(400, json={"message": "UUID is required."})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            with pytest.raises(CoolifyApiError) as excinfo:
                await api.start("services", "s1")
        assert not isinstance(excinfo.value, AlreadyRunning)


# ── the rollback ─────────────────────────────────────────────────────────────


class TestStopTarget:
    async def test_stops_every_target_that_may_run(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _app_plan()
        ctx.target_uuids["app1"] = "tgt1"
        db = respx_mock.post(f"{BASE}/databases/db2/stop").mock(
            return_value=httpx.Response(400, json={"message": "Database is already stopped."})
        )
        app = respx_mock.post(f"{BASE}/applications/tgt1/stop").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_start_target(
            ctx, {START_REQUESTED: ["tgt1"], STARTED_EARLY: ["db2", "tgt1"]}
        )
        assert db.called and app.call_count == 1

    async def test_a_saga_failing_at_copy_on_an_early_target_stops_it_first(
        self, ctx: MigrationContext
    ) -> None:
        ran: list[str] = []

        def record(name: str) -> Any:
            async def compensate(_ctx: Any, _undo: dict[str, Any]) -> None:
                ran.append(name)

            return compensate

        async def copy(_ctx: MigrationContext) -> dict[str, Any]:
            containment.journal_started_early(_ctx, ["db2"])
            raise TargetStartedEarly("started early")

        async def ok(_ctx: MigrationContext) -> dict[str, Any]:
            return {}

        saga = Saga(
            journal=ctx.journal,
            context=ctx,
            steps={
                **{s: ok for s in (State.CREATE_TARGET, State.QUIESCE, State.DISCOVER)},
                State.COPY: copy,
            },
            compensations={c: record(c.value) for c in Compensation},
        )
        result = await saga.run()
        assert result.outcome is Outcome.ROLLED_BACK
        assert ran[0] == Compensation.STOP_TARGET.value
        assert ran.index("stop_target") < ran.index("restart_source")
