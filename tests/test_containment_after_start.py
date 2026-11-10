"""A source found revived once START asked for a target BLOCKS — it never rolls back.

By FINALIZE the DNS gate has let the target start and users have written to it
since; the rollback stops it and deletes it with its volumes. Answering a CI
``/deploy`` of the stopped source with that saga traded every post-cutover write
for the few the revived source received, unasked and for good. So after START a
revival is a resumable stop, like the DNS gate: the operator stops the source
and resumes, or rolls back by hand. Before START the rollback stays the answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from rich.console import Console

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import containment as c
from bg_coolify_migrate.domain.containment import REVIVAL_SEEN_CLOCK, STOP_CLOCK, STOPPED_CLOCK
from bg_coolify_migrate.domain.statemachine import START_REQUESTED, STOP_REQUESTED, Outcome, State
from bg_coolify_migrate.engine import compensations, containment, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import SourceRevived, SourceRevivedAfterStart
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.ui.console import THEME
from bg_coolify_migrate.ui.run_report import outcome_panel
from tests.fake_daemon import FakeDaemon
from tests.test_engine_steps import HOST, _plan
from tests.test_pins_engine import COOLIFY, MID, VOL, _target

TVOL = "postgres-data-db2"
STOP = 1_800_000_000
SEEN = STOP + 600  # the source's clock when the block is raised


def _source(clock: int | None = SEEN) -> FakeDaemon:
    daemon = FakeDaemon(volumes=(VOL,))
    if clock is None:
        daemon.on(r"^date \+%s$", stderr="date: cannot run", exit_status=1)
    else:
        daemon.on(r"^date \+%s$", stdout=str(clock))
    return daemon


def _event(name: str, when: int) -> str:
    return json.dumps(
        {
            "Type": "container",
            "Action": "start",
            "Actor": {"Attributes": {"name": name, **COOLIFY}},
            "time": when,
        }
    )


async def _context(tmp_path: Path, api: CoolifyClient, source: FakeDaemon) -> MigrationContext:
    ctx = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, MID),
        migration_id=MID,
        source_host=source,  # type: ignore[arg-type]
        target_host=_target(TVOL),  # type: ignore[arg-type]
    )
    ctx.target_uuids["db1"] = "db2"
    ctx.journal.append(
        "step_started",
        state=State.QUIESCE.value,
        detail={STOP_REQUESTED: True, STOP_CLOCK: STOP - 60, STOPPED_CLOCK: STOP},
    )
    return ctx


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


def _started(ctx: MigrationContext) -> None:
    ctx.journal.append(
        "step_completed", state=State.START_TARGET.value, detail={"started": ["db2"]}
    )


def _src(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.source_host, FakeDaemon)
    return ctx.source_host


def _tgt(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.target_host, FakeDaemon)
    return ctx.target_host


class TestAfterStart:
    async def test_a_revival_blocks_and_says_both_ways_on(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        ctx = await _context(tmp_path, api, _source())
        _started(ctx)
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevivedAfterStart) as excinfo:
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        error = excinfo.value
        assert isinstance(error, SourceRevived) and error.exit_code == 5
        assert "postgres: postgres-db1 is running" in str(error)
        hint = error.hint or ""
        assert f"coolify-migrate resume {MID}" in hint
        assert f"coolify-migrate rollback {MID}" in hint
        assert "renames the source" in hint, "the plan's policy is rename"
        assert "does not roll back by itself" in hint
        assert ctx.journal.undo_info(State.QUIESCE.value)[REVIVAL_SEEN_CLOCK] == SEEN

    async def test_the_resume_passes_once_the_operator_stopped_the_source(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        ctx = await _context(tmp_path, api, _source())
        _started(ctx)
        _src(ctx).add("postgres-db1", labels=COOLIFY).started_at = "2027-01-15T08:01:00Z"
        _src(ctx).events = f"oldest {STOP - 100}\n" + _event("postgres-db1", STOP + 60)
        with pytest.raises(SourceRevivedAfterStart):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        # Coolify's stop: docker stop + docker rm -f. Its start stays in the event log.
        del _src(ctx).containers["postgres-db1"]
        await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)

    async def test_a_start_after_the_block_still_counts(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        ctx = await _context(tmp_path, api, _source())
        _started(ctx)
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevivedAfterStart):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        del _src(ctx).containers["postgres-db1"]
        _src(ctx).events = f"oldest {STOP - 100}\n" + _event("postgres-db1-new", SEEN + 30)
        with pytest.raises(SourceRevivedAfterStart, match="postgres-db1-new"):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)

    async def test_a_clock_that_cannot_be_read_still_blocks(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        ctx = await _context(tmp_path, api, _source(clock=None))
        _started(ctx)
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevivedAfterStart):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        assert REVIVAL_SEEN_CLOCK not in ctx.journal.undo_info(State.QUIESCE.value)

    async def test_a_resumed_start_that_asked_before_blocks_too(
        self, tmp_path: Path, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        ctx = await _context(tmp_path, api, _source())
        ctx.journal.append(
            "step_started", state=State.START_TARGET.value, detail={START_REQUESTED: ["db2"]}
        )
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevivedAfterStart, match=r"found at START\)"):
            await steps.step_start_target(ctx)
        assert not respx_mock.calls, "nothing asked again"


class TestBeforeStart:
    async def test_a_revival_still_rolls_back(self, tmp_path: Path, api: CoolifyClient) -> None:
        ctx = await _context(tmp_path, api, _source())
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevived) as excinfo:
            await containment.assert_contained(ctx, checkpoint="the DNS gate")
        assert not isinstance(excinfo.value, SourceRevivedAfterStart)
        assert "rolls back now" in (excinfo.value.hint or "")
        assert REVIVAL_SEEN_CLOCK not in ctx.journal.undo_info(State.QUIESCE.value)

    async def test_a_target_someone_else_started_is_no_start_of_ours(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        ctx = await _context(tmp_path, api, _source())
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        _tgt(ctx).add("db2", labels={"coolify.managed": "true"})
        with pytest.raises(SourceRevived) as excinfo:
            await containment.assert_contained(ctx, checkpoint="the end of VERIFY")
        assert not isinstance(excinfo.value, SourceRevivedAfterStart)


class TestTheSaga:
    async def test_a_revival_at_finalize_leaves_the_target_and_its_volumes(
        self, tmp_path: Path, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        ctx = await _context(tmp_path, api, _source())
        _src(ctx).add("postgres-db1", labels=COOLIFY)  # a CI /deploy of the source

        async def ok(_ctx: MigrationContext) -> dict[str, Any]:
            return {}

        async def start(_ctx: MigrationContext) -> dict[str, Any]:
            return {"started": ["db2"], "deployments": {}}

        before = (State.CREATE_TARGET, State.QUIESCE, State.DISCOVER, State.COPY)
        saga = Saga(
            journal=ctx.journal,
            context=ctx,
            steps={
                **{s: ok for s in (*before, State.VERIFY, State.DNS_GATE, State.HEALTHCHECK)},
                State.START_TARGET: start,
                State.FINALIZE: steps.step_finalize,
            },
            compensations=compensations.build_compensations(),
        )
        result = await saga.run()

        assert result.outcome is Outcome.BLOCKED
        assert isinstance(result.error, SourceRevivedAfterStart)
        assert result.compensations_run == []
        assert TVOL in _tgt(ctx).volumes, "the target keeps what users wrote to it"
        events = [r.event for r in ctx.journal.read()]
        assert "blocked" in events and "rollback_started" not in events
        assert not respx_mock.calls, "FINALIZE touched nothing"

        console = Console(width=200, force_terminal=False, no_color=True, theme=THEME)
        with console.capture() as capture:
            console.print(outcome_panel(result, migration_id=MID))
        out = capture.get()
        assert "the source was started again" in out
        assert "Your source is NOT stopped" in out
        assert "nothing is broken" not in out


class TestTheWindow:
    def test_judged_from_the_gate_then_the_block(self) -> None:
        assert c.judged_from({}) is None
        assert c.judged_from({STOP_CLOCK: 10}) == 10, "without the gate's clock: stricter"
        assert c.judged_from({STOP_CLOCK: 10, STOPPED_CLOCK: 40}) == 40
        assert c.judged_from({STOP_CLOCK: 10, STOPPED_CLOCK: 40, REVIVAL_SEEN_CLOCK: 90}) == 90
        assert c.judged_from({STOPPED_CLOCK: 40, REVIVAL_SEEN_CLOCK: 5}) == 40
        assert c.judged_from({REVIVAL_SEEN_CLOCK: 90}) == 90
        assert c.judged_from({STOP_CLOCK: True}) is None, "a bool is no clock"

    @pytest.mark.parametrize(
        ("policy", "fate"),
        [
            ("delete", "deletes the source, and those writes with it"),
            ("rename", "renames the source"),
            ("keep", "leaves the source stopped"),
            ("other", "applies the finalize policy"),
        ],
    )
    def test_the_hint_says_what_finalize_then_does(self, policy: str, fate: str) -> None:
        hint = c.revived_after_start_hint(policy=policy, migration_id="m9")
        assert fate in hint
        assert "coolify-migrate resume m9" in hint and "coolify-migrate rollback m9" in hint
        assert "auto-deploy" in hint, "the causes, as before START"
