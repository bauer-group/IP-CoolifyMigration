"""Revivals are judged from the moment the stop gate proved the source down.

Coolify's stop is no single act: StopApplication stops a compose app's
containers one at a time, QUIESCE stops the plan's resources one after another,
and every Coolify container runs ``restart: unless-stopped``. A container that
exits on its own while it waits its turn is restarted by Docker, then stopped
and removed by Coolify — a ``start`` after the stop REQUEST in a stack the gate
then saw end stopped. Judged from the request, the end-of-COPY checkpoint took it
for a revival and rolled back a healthy migration, on every attempt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.containment import STOP_CLOCK, STOPPED_CLOCK
from bg_coolify_migrate.domain.statemachine import STOP_REQUESTED, State
from bg_coolify_migrate.engine import containment, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import QuiesceError, SourceRevived
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.fake_daemon import FakeDaemon
from tests.test_engine_steps import BASE, HOST, _plan
from tests.test_pins_engine import COOLIFY, MID, VOL, _target

REQUEST = 1_800_000_000  # the source's clock at the stop request
PROVEN = REQUEST + 60  # ... and once the gate saw the stack down


def _event(name: str, when: int) -> str:
    return json.dumps(
        {
            "Type": "container",
            "Action": "start",
            "Actor": {"Attributes": {"name": name, **COOLIFY}},
            "time": when,
        }
    )


def _clock(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000001Z")


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    source = FakeDaemon(volumes=(VOL,))
    source.on(r"^date \+%s$", stdout=str(PROVEN + 300))
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, MID),
        migration_id=MID,
        source_host=source,  # type: ignore[arg-type]
        target_host=_target(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


def _src(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.source_host, FakeDaemon)
    return ctx.source_host


def _quiesced(ctx: MigrationContext, *, proven: bool = True) -> None:
    detail: dict[str, object] = {STOP_REQUESTED: True, STOP_CLOCK: REQUEST}
    if proven:
        detail[STOPPED_CLOCK] = PROVEN
    ctx.journal.append("step_started", state=State.QUIESCE.value, detail=detail)


class TestTheWindow:
    async def test_a_restart_during_the_stop_sequence_is_no_revival(
        self, ctx: MigrationContext
    ) -> None:
        _quiesced(ctx)
        # Restarted by Docker between the stop request and the gate's proof.
        _src(ctx).events = f"oldest {REQUEST - 10}\n" + _event("worker-db1", REQUEST + 30)
        await containment.assert_contained(ctx, checkpoint="the end of COPY")
        assert any(f"--since {PROVEN}" in cmd for cmd in _src(ctx).commands)

    async def test_a_start_after_the_proof_is_one(self, ctx: MigrationContext) -> None:
        _quiesced(ctx)
        _src(ctx).events = f"oldest {REQUEST - 10}\n" + _event("worker-db1", PROVEN + 5)
        with pytest.raises(SourceRevived, match="worker-db1 was started"):
            await containment.assert_contained(ctx, checkpoint="the end of COPY")

    async def test_started_at_is_judged_by_the_same_instant(self, ctx: MigrationContext) -> None:
        _quiesced(ctx)
        early = _src(ctx).add("worker-db1", state="exited", labels=COOLIFY)
        early.started_at = _clock(REQUEST + 30)
        await containment.assert_contained(ctx, checkpoint="the DNS gate")
        early.started_at = _clock(PROVEN + 5)
        with pytest.raises(SourceRevived, match="after the stop, and has stopped again"):
            await containment.assert_contained(ctx, checkpoint="the DNS gate")

    async def test_without_the_gate_s_clock_the_request_s_is_used(
        self, ctx: MigrationContext
    ) -> None:
        # Stricter, never looser: a journal that has only the request's clock.
        _quiesced(ctx, proven=False)
        _src(ctx).events = f"oldest {REQUEST - 10}\n" + _event("worker-db1", REQUEST + 30)
        with pytest.raises(SourceRevived):
            await containment.assert_contained(ctx, checkpoint="the end of COPY")


class TestQuiesceJournalsTheProof:
    async def test_after_the_gate_and_the_kill_check(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        source = FakeDaemon(volumes=(VOL,))
        source.on_sequence(r"^date \+%s$", [{"stdout": str(REQUEST)}, {"stdout": str(PROVEN)}])
        source.on(r"^docker events", stdout="")
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        ctx.source_host = source  # type: ignore[assignment]

        def stop(_request: httpx.Request) -> httpx.Response:
            del source.containers["postgres-db1"]  # docker stop && docker rm -f
            return httpx.Response(200, json={})

        respx_mock.post(f"{BASE}/databases/db1/stop").mock(side_effect=stop)
        await steps.step_quiesce(ctx)

        detail = ctx.journal.undo_info(State.QUIESCE.value)
        assert detail[STOP_CLOCK] == REQUEST, "killed_since's window is unchanged"
        assert detail[STOPPED_CLOCK] == PROVEN
        assert containment.revival_clock(ctx) == PROVEN

    async def test_not_when_a_data_holder_was_killed(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        source = FakeDaemon(volumes=(VOL,))
        source.on_sequence(r"^date \+%s$", [{"stdout": str(REQUEST)}, {"stdout": str(PROVEN)}])
        source.on(r"^docker events", stdout="postgres-db1 137")
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        ctx.source_host = source  # type: ignore[assignment]

        def stop(_request: httpx.Request) -> httpx.Response:
            del source.containers["postgres-db1"]
            return httpx.Response(200, json={})

        respx_mock.post(f"{BASE}/databases/db1/stop").mock(side_effect=stop)
        with pytest.raises(QuiesceError, match="SIGKILLed"):
            await steps.step_quiesce(ctx)
        assert STOPPED_CLOCK not in ctx.journal.undo_info(State.QUIESCE.value)
