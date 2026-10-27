"""A rollback after FINALIZE(keep/rename) first proves the source still holds its data.

FINALIZE releases the kept or renamed source's volume pins, and Coolify's stop
had already removed its containers: on a server that prunes unused volumes the
next cleanup deletes them, and an operator may delete the kept source by hand.
A rollback then stops the target, drops its volumes, deletes it, and restarts
the source on fresh EMPTY volumes — the only copy gone, reported ROLLED_BACK.

So once FINALIZE began with keep or rename, the saga's guard looks before any
compensation runs: every source resource in Coolify, every copied source volume
on the source server. Missing anything refuses; present, the volumes are pinned
again so the cleanup cannot take them before the restart re-mounts them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.discovery import pins
from bg_coolify_migrate.domain.statemachine import (
    Compensation,
    Outcome,
    State,
    source_copy_unproven,
)
from bg_coolify_migrate.engine import compensations, runner
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import RollbackError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.fake_daemon import FakeDaemon
from tests.test_engine_steps import BASE, HOST, _plan
from tests.test_pins_engine import MID, VOL, _source, _target

FINALIZE = State.FINALIZE.value
TVOL = "postgres-data-db2"


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
    yield context
    await api.aclose()


def _finalized(journal: Journal, policy: str = "rename", *, discover: bool = True) -> None:
    """A run that reached DONE: every state through FINALIZE completed."""
    for state in (State.CREATE_TARGET, State.QUIESCE):
        journal.append("step_completed", state=state.value, detail={})
    if discover:
        pairs = {"db1": [{"source": VOL, "target": TVOL, "mount_path": "/var/lib/postgresql/data"}]}
        journal.append("step_completed", state=State.DISCOVER.value, detail={"volume_pairs": pairs})
    for state in (State.COPY, State.START_TARGET):
        journal.append("step_completed", state=state.value, detail={})
    journal.append("step_completed", state=FINALIZE, detail={"policy": policy})


def _source_pins(daemon: FakeDaemon) -> list[Any]:
    return [
        c for c in daemon.containers.values() if c.labels.get(pins.LABEL_SIDE) == pins.SOURCE
    ]


class TestWhenTheSourceMustBeProven:
    @pytest.mark.parametrize("policy", ["keep", "rename"])
    def test_after_a_keep_or_rename_finalize(self, policy: str) -> None:
        assert source_copy_unproven({FINALIZE: {"policy": policy}})

    @pytest.mark.parametrize("markers", [{}, {FINALIZE: {}}, {FINALIZE: {"policy": "delete"}}])
    def test_not_before_finalize_nor_after_delete(self, markers: dict[str, Any]) -> None:
        # DELETE is refused outright (statemachine.rollback_refusal).
        assert not source_copy_unproven(markers)


class TestTheGuard:
    async def test_before_finalize_it_asks_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.journal.append("step_completed", state="quiesce", detail={})
        assert await compensations.source_data_refusal(ctx) is None
        assert not respx_mock.calls
        assert _source_pins(ctx.source_host) == []  # type: ignore[arg-type]

    @pytest.mark.parametrize("policy", ["keep", "rename"])
    async def test_a_source_that_kept_its_data_is_pinned_and_passes(
        self, ctx: MigrationContext, respx_mock: respx.Router, policy: str
    ) -> None:
        _finalized(ctx.journal, policy)
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1"})
        )

        assert await compensations.source_data_refusal(ctx) is None

        # Pinned again: nothing may prune it before the restart re-mounts it.
        [pin] = _source_pins(ctx.source_host)  # type: ignore[arg-type]
        assert pin.volumes == [VOL] and pin.state == "created"

    async def test_a_pruned_volume_refuses(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.source_host = _source()  # type: ignore[assignment]  # the cleanup took it
        _finalized(ctx.journal)
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1"})
        )

        reason = await compensations.source_data_refusal(ctx)

        assert reason is not None
        assert f"volume {VOL}" in reason and "only copy" in reason
        assert _source_pins(ctx.source_host) == []  # type: ignore[arg-type]

    async def test_a_source_deleted_by_hand_refuses(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _finalized(ctx.journal, "keep")
        respx_mock.get(f"{BASE}/databases/db1").mock(return_value=httpx.Response(404, json={}))

        reason = await compensations.source_data_refusal(ctx)

        assert reason is not None and "postgres (no longer in Coolify)" in reason

    async def test_a_source_that_cannot_be_read_refuses(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _finalized(ctx.journal)
        respx_mock.get(f"{BASE}/databases/db1").mock(return_value=httpx.Response(500))

        reason = await compensations.source_data_refusal(ctx)

        assert reason is not None and "could not be read" in reason

    async def test_without_discovers_record_the_plans_volumes_count(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.source_host = _source()  # type: ignore[assignment]
        _finalized(ctx.journal, discover=False)
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1"})
        )

        reason = await compensations.source_data_refusal(ctx)

        assert reason is not None and f"volume {VOL}" in reason


def _recorder() -> tuple[list[Compensation], dict[Any, Any]]:
    ran: list[Compensation] = []

    def make(comp: Compensation) -> Any:
        async def undo(_ctx: Any, _info: dict[str, Any]) -> None:
            ran.append(comp)

        return undo

    return ran, {c: make(c) for c in Compensation}


class TestTheSagaAsksFirst:
    async def test_a_refusal_runs_no_compensation(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "s1")
        _finalized(journal)
        ran, comps = _recorder()

        async def gone(_ctx: Any) -> str | None:
            return "the source no longer holds its data"

        saga = Saga(journal=journal, context=None, steps={}, compensations=comps, guard=gone)
        with pytest.raises(RollbackError, match="rollback refused: the source no longer") as exc:
            await saga.rollback()

        assert ran == []
        assert "everything written to it since the cutover" in (exc.value.hint or "")
        assert str(journal.path) in (exc.value.hint or "")
        assert [r.event for r in journal.read()][-1] == "rollback_refused"
        assert not journal.is_finished

    async def test_a_guard_that_cannot_answer_refuses(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "s2")
        _finalized(journal)
        ran, comps = _recorder()

        async def broken(_ctx: Any) -> str | None:
            raise ConnectionError("ssh: connection reset")

        saga = Saga(journal=journal, context=None, steps={}, compensations=comps, guard=broken)
        with pytest.raises(RollbackError, match="could not be checked"):
            await saga.rollback()
        assert ran == []

    async def test_a_passing_guard_lets_it_run(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "s3")
        _finalized(journal)
        ran, comps = _recorder()

        async def fine(_ctx: Any) -> str | None:
            return None

        saga = Saga(journal=journal, context=None, steps={}, compensations=comps, guard=fine)
        result = await saga.rollback()
        assert result.outcome is Outcome.ROLLED_BACK
        assert Compensation.STOP_TARGET in ran and Compensation.RESTART_SOURCE in ran


async def test_rollback_after_done_on_a_pruned_source_touches_nothing(
    tmp_path: Path, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command is wired to the guard: no stop, no drop, no restart, no delete."""
    journal = Journal.create(tmp_path, MID)
    _finalized(journal)
    pruned, target = _source(), _target(TVOL)

    @asynccontextmanager
    async def hosts(*_a: Any, **_kw: Any) -> AsyncIterator[tuple[FakeDaemon, FakeDaemon]]:
        yield pruned, target

    monkeypatch.setattr(runner, "open_hosts", hosts)
    respx_mock.get(f"{BASE}/databases/db1").mock(
        return_value=httpx.Response(200, json={"uuid": "db1"})
    )

    async with CoolifyClient(HOST, "tok", max_retries=0) as api:
        with pytest.raises(RollbackError, match=f"volume {VOL}"):
            await runner.rollback_migration(
                api, Settings(_env_file=None, state_dir=tmp_path), _plan(), MID
            )

    assert [c.request.method for c in respx_mock.calls] == ["GET"]
    assert TVOL in target.volumes, "the target's copy was dropped"


class TestThePrompt:
    def test_a_started_target_says_its_writes_are_discarded(self) -> None:
        from bg_coolify_migrate.cli import _rollback_prompt

        text = _rollback_prompt(MID, _plan(), {"start_target": {"started": ["db2"]}})
        assert "DISCARDED" in text and "since the cutover" in text

    def test_before_the_start_it_does_not(self) -> None:
        from bg_coolify_migrate.cli import _rollback_prompt

        text = _rollback_prompt(MID, _plan(), {"copy": {}})
        assert "DISCARDED" not in text and "delete the target" in text
