"""After FINALIZE(keep/rename) a rollback first proves the source still holds its BIND data.

The volume guard (test_rollback_source_data.py) looked at ``docker volume ls``
only, and for a bind-only plan at nothing on the source server. The one check
of a bind's source sat inside DROP_TARGET_VOLUMES, which keeps the target's
copy and fails — but the saga goes on after a failed compensation, so
RESTART_SOURCE brought the source up on an empty, recreated directory and
DELETE_TARGET_RESOURCE then let Coolify ``rm -rf`` the target's directory,
where a relative bind's copy lives (Service::deleteConfigurations
v4.7.2:226-233): the only copy of the data, gone, under a "rollback".

So the saga's guard looks before ANY compensation: every copied bind's source
must still be what was copied — present, of the same kind, and not empty where
DISCOVER saw data.
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
from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import BindPair
from bg_coolify_migrate.domain.statemachine import Compensation, Outcome, State
from bg_coolify_migrate.engine import compensations, runner, steps
from bg_coolify_migrate.engine.context import (
    MigrationContext,
    deserialise_bind_pairs,
    serialise_bind_pairs,
)
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import RollbackError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import SshTarget
from tests.test_binds_engine import SRC, TGT, FsHost, _bind, _service
from tests.test_engine_steps import BASE, HOST, _plan

DATA = f"{SRC}/data"
COPY = f"{TGT}/data"
MID = "m1"


def _plan_with_bind() -> Any:
    return _plan(resources=(_service(_bind(DATA)),))


def _hosts() -> tuple[FsHost, FsHost]:
    source = FsHost({f"{DATA}/db": binds.STATE_FILE})
    target = FsHost({f"{COPY}/db": binds.STATE_FILE}, target=SshTarget(host="10.0.0.2"))
    return source, target


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    source, target = _hosts()
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan_with_bind(),
        journal=Journal.create(tmp_path, MID),
        migration_id=MID,
        source_host=source,  # type: ignore[arg-type]
        target_host=target,  # type: ignore[arg-type]
    )
    context.target_uuids = {"svc1": "tgt1"}
    yield context
    await api.aclose()


def _finalized(
    journal: Journal, policy: str = "keep", *, empty: bool = False, discover: bool = True
) -> None:
    """A run that reached DONE with one relative directory bind copied."""
    journal.append(
        "step_completed",
        state=State.CREATE_TARGET.value,
        detail={"target_uuids": {"svc1": "tgt1"}},
    )
    journal.append("step_completed", state=State.QUIESCE.value, detail={})
    if discover:
        row: dict[str, Any] = {"source": DATA, "target": COPY, "mount_path": "/data", "kind": "dir"}
        if empty:
            row["empty"] = True
        journal.append(
            "step_completed",
            state=State.DISCOVER.value,
            detail={"volume_pairs": {"svc1": [row]}},
        )
    journal.append(
        "step_started",
        state=State.COPY.value,
        detail={binds.BIND_TARGET: {"path": COPY, "prior": "empty-directory", "created": []}},
    )
    for state in (State.COPY, State.START_TARGET):
        journal.append("step_completed", state=state.value, detail={})
    journal.append("step_completed", state=State.FINALIZE.value, detail={"policy": policy})


def _source(ctx: MigrationContext) -> FsHost:
    assert isinstance(ctx.source_host, FsHost)
    return ctx.source_host


def _recorder() -> tuple[list[Compensation], dict[Any, Any]]:
    ran: list[Compensation] = []

    def make(comp: Compensation) -> Any:
        async def undo(_ctx: Any, _info: dict[str, Any]) -> None:
            ran.append(comp)

        return undo

    return ran, {c: make(c) for c in Compensation}


def _saga(ctx: MigrationContext, comps: dict[Any, Any]) -> Saga:
    return Saga(
        journal=ctx.journal,
        context=ctx,
        steps={},
        compensations=comps,
        guard=compensations.source_data_refusal,
    )


@pytest.fixture
def source_exists(respx_mock: respx.Router) -> respx.Router:
    respx_mock.get(f"{BASE}/services/svc1").mock(
        return_value=httpx.Response(200, json={"uuid": "svc1"})
    )
    return respx_mock


class TestTheGuardLooksAtBinds:
    async def test_before_finalize_it_asks_the_source_server_nothing(
        self, ctx: MigrationContext
    ) -> None:
        ctx.journal.append("step_completed", state=State.QUIESCE.value, detail={})
        assert await compensations.source_data_refusal(ctx) is None
        assert _source(ctx).commands == []

    @pytest.mark.parametrize("policy", ["keep", "rename"])
    async def test_a_bind_source_that_still_holds_its_data_passes(
        self, ctx: MigrationContext, source_exists: respx.Router, policy: str
    ) -> None:
        _finalized(ctx.journal, policy)
        assert await compensations.source_data_refusal(ctx) is None
        # A bind-only plan has no volume to list: the bind is what is looked at.
        assert not any("docker volume" in c for c in _source(ctx).commands)
        assert any(DATA in c for c in _source(ctx).commands)

    @pytest.mark.parametrize(
        ("paths", "expected"),
        [
            ({}, f"bind {DATA} (gone)"),
            ({DATA: binds.STATE_EMPTY_DIR}, f"bind {DATA} (empty now"),
            ({DATA: binds.STATE_FILE}, f"bind {DATA} (now a file, where a dir was copied)"),
            ({DATA: binds.STATE_SYMLINK}, f"bind {DATA} (now a symlink)"),
        ],
    )
    async def test_a_bind_source_that_lost_its_data_refuses(
        self,
        ctx: MigrationContext,
        source_exists: respx.Router,
        paths: dict[str, str],
        expected: str,
    ) -> None:
        _source(ctx).paths = dict(paths)
        _finalized(ctx.journal)
        reason = await compensations.source_data_refusal(ctx)
        assert reason is not None
        assert expected in reason and "only copy" in reason

    async def test_a_source_empty_when_it_was_copied_is_no_loss(
        self, ctx: MigrationContext, source_exists: respx.Router
    ) -> None:
        _source(ctx).paths = {DATA: binds.STATE_EMPTY_DIR}
        _finalized(ctx.journal, empty=True)
        assert await compensations.source_data_refusal(ctx) is None

    async def test_without_discovers_record_the_plans_binds_count(
        self, ctx: MigrationContext, source_exists: respx.Router
    ) -> None:
        _source(ctx).paths = {}
        _finalized(ctx.journal, discover=False)
        reason = await compensations.source_data_refusal(ctx)
        assert reason is not None and f"bind {DATA} (gone)" in reason


class TestTheSagaRefusesBeforeAnyCompensation:
    """The scenario of the review: nothing may run, above all no restart and no delete."""

    async def test_a_removed_bind_source_runs_no_compensation(
        self, ctx: MigrationContext, source_exists: respx.Router
    ) -> None:
        _source(ctx).paths = {}  # removed by hand after the cutover
        _finalized(ctx.journal, "keep")
        ran, comps = _recorder()

        with pytest.raises(RollbackError, match="rollback refused") as exc:
            await _saga(ctx, comps).rollback()

        assert ran == []
        assert Compensation.RESTART_SOURCE not in ran
        assert Compensation.DELETE_TARGET_RESOURCE not in ran
        assert f"bind {DATA} (gone)" in str(exc.value)
        assert not ctx.journal.is_finished

    async def test_a_source_server_that_cannot_answer_runs_no_compensation(
        self, ctx: MigrationContext, source_exists: respx.Router
    ) -> None:
        _source(ctx).on(r"^sh -c ", exit_status=255, stderr="ssh: connection reset")
        _finalized(ctx.journal, "rename")
        ran, comps = _recorder()

        with pytest.raises(RollbackError, match="could not be checked"):
            await _saga(ctx, comps).rollback()

        assert ran == []

    async def test_a_source_that_holds_its_bind_rolls_back(
        self, ctx: MigrationContext, source_exists: respx.Router
    ) -> None:
        _finalized(ctx.journal, "keep")
        ran, comps = _recorder()

        result = await _saga(ctx, comps).rollback()

        assert result.outcome is Outcome.ROLLED_BACK
        assert Compensation.RESTART_SOURCE in ran
        assert Compensation.DELETE_TARGET_RESOURCE in ran


async def test_rollback_command_over_a_removed_bind_touches_nothing(
    tmp_path: Path, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wired end to end, with the real compensations: one GET, no stop, no delete."""
    journal = Journal.create(tmp_path, MID)
    _finalized(journal, "keep")
    source, target = _hosts()
    source.paths = {}

    @asynccontextmanager
    async def hosts(*_a: Any, **_kw: Any) -> AsyncIterator[tuple[FsHost, FsHost]]:
        yield source, target

    monkeypatch.setattr(runner, "open_hosts", hosts)
    respx_mock.get(f"{BASE}/services/svc1").mock(
        return_value=httpx.Response(200, json={"uuid": "svc1"})
    )

    async with CoolifyClient(HOST, "tok", max_retries=0) as api:
        with pytest.raises(RollbackError, match=f"bind {DATA}"):
            await runner.rollback_migration(
                api, Settings(_env_file=None, state_dir=tmp_path), _plan_with_bind(), MID
            )

    assert [c.request.method for c in respx_mock.calls] == ["GET"]
    assert target.state(f"{COPY}/db") == binds.STATE_FILE, "the target's copy was touched"
    assert target.commands == []


class TestSourceLost:
    @pytest.mark.parametrize(
        ("state", "kind", "was_empty", "lost"),
        [
            (binds.STATE_DIR, binds.KIND_DIR, False, False),
            (binds.STATE_FILE, binds.KIND_FILE, False, False),
            (binds.STATE_EMPTY_DIR, binds.KIND_DIR, True, False),
            (binds.STATE_DIR, None, False, False),
            (binds.STATE_MISSING, binds.KIND_DIR, False, True),
            (binds.STATE_EMPTY_DIR, binds.KIND_DIR, False, True),
            (binds.STATE_EMPTY_FILE, binds.KIND_FILE, False, True),
            (binds.STATE_EMPTY_DIR, None, False, True),  # no record: the answer that refuses
            (binds.STATE_FILE, binds.KIND_DIR, False, True),
            (binds.STATE_SYMLINK, binds.KIND_DIR, False, True),
            (binds.STATE_OTHER, None, False, True),
        ],
    )
    def test_cases(self, state: str, kind: str | None, was_empty: bool, lost: bool) -> None:
        problem = binds.source_lost("/srv/x", state, kind=kind, was_empty=was_empty)
        assert (problem is not None) is lost
        if problem:
            assert problem.startswith("bind /srv/x (")

    def test_the_empty_marker_round_trips_and_is_absent_otherwise(self) -> None:
        pairs = [
            BindPair("/srv/a", "/srv/a", "/a", kind=binds.KIND_DIR, empty=True),
            BindPair("/srv/b", "/srv/b", "/b", kind=binds.KIND_FILE),
        ]
        rows = serialise_bind_pairs(pairs)
        assert rows[0]["empty"] is True and "empty" not in rows[1]
        assert deserialise_bind_pairs({"u": rows}) == {"u": pairs}


async def test_discover_records_which_sources_were_empty(ctx: MigrationContext) -> None:
    assert isinstance(ctx.source_host, FsHost)
    ctx.source_host.paths.update({"/srv/a": binds.STATE_EMPTY_DIR, "/srv/b": binds.STATE_FILE})
    recorded: dict[str, list[dict[str, str]]] = {}
    await steps._record_bind_pairs(
        ctx,
        {"svc1": [BindPair("/srv/a", "/srv/a", "/a"), BindPair("/srv/b", "/srv/b", "/b")]},
        recorded,
    )
    assert recorded["svc1"] == [
        {"source": "/srv/a", "target": "/srv/a", "mount_path": "/a", "kind": "dir", "empty": True},
        {"source": "/srv/b", "target": "/srv/b", "mount_path": "/b", "kind": "file"},
    ]
    assert [p.empty for p in ctx.bind_pairs["svc1"]] == [True, False]
