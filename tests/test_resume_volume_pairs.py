"""A resumed run must copy and verify what DISCOVER paired.

Before this, ``resume_migration`` rehydrated the target uuids and the pre-stop
mounts but never the volume pairs. DISCOVER is skipped on resume as completed,
so a resume after a crash inside COPY or VERIFY iterated an empty
``ctx.volume_pairs``: COPY copied nothing, VERIFY verified nothing, START ran on
empty target volumes, and a DELETE finalize then removed the only intact copy.
Every step involved read the empty dict as "a stateless stack".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.naming import VolumeEndpoint, VolumePair
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import Outcome, State
from bg_coolify_migrate.engine import runner, steps
from bg_coolify_migrate.engine.context import (
    MigrationContext,
    deserialise_volume_pairs,
    is_volume_row,
)
from bg_coolify_migrate.engine.executor import RunResult
from bg_coolify_migrate.errors import TransferError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"

RECORDED = {
    "db1": [
        {"source": "postgres-data-db1", "target": "postgres-data-db2", "mount_path": "/var/lib/pg"},
        # A bind mount, journalled beside the pairs and mirrored path-to-path.
        {"source": "/srv/uploads", "target": "/srv/uploads", "mount_path": "/uploads"},
    ]
}


def _plan() -> MigrationPlan:
    manifest = VolumeManifest(
        items=(
            VolumeItem(
                mount_class=MountClass.NAMED,
                decision=Decision.MIGRATE,
                reason="named volume",
                source_name="postgres-data-db1",
                source_path="/var/lib/docker/volumes/postgres-data-db1/_data",
                mount_path="/var/lib/pg",
                bytes=1024,
            ),
        )
    )
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=(
            ResourcePlan(
                snapshot=ResourceSnapshot(
                    uuid="db1",
                    name="postgres",
                    collection="databases",
                    kind=ResourceKind.DATABASE,
                    engine=DatabaseEngine.POSTGRESQL,
                    image="postgres:16",
                ),
                strategy=Strategy.COPY_DATA,
                manifest=manifest,
            ),
        ),
    )


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=FakeHost(),  # type: ignore[arg-type]
        target_host=FakeHost(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


class TestDeserialiseVolumePairs:
    def test_rebuilds_the_volume_pairs_and_skips_bind_mounts(self) -> None:
        pairs = deserialise_volume_pairs(RECORDED)
        assert pairs == {
            "db1": [
                VolumePair(
                    source=VolumeEndpoint("postgres-data-db1", "/var/lib/pg"),
                    target=VolumeEndpoint("postgres-data-db2", "/var/lib/pg"),
                )
            ]
        }
        # The paths a COPY would mirror come from the names, as DISCOVER made them.
        assert pairs["db1"][0].target_path == "/var/lib/docker/volumes/postgres-data-db2/_data"

    def test_absent_is_empty(self) -> None:
        assert deserialise_volume_pairs(None) == {}

    def test_a_resource_with_only_bind_mounts_has_no_pairs(self) -> None:
        only_bind = {"app1": [{"source": "/srv/x", "target": "/srv/x", "mount_path": "/x"}]}
        assert deserialise_volume_pairs(only_bind) == {"app1": []}

    def test_a_non_list_entry_raises_rather_than_reading_as_no_volumes(self) -> None:
        with pytest.raises(ValueError, match="not a list"):
            deserialise_volume_pairs({"db1": "postgres-data-db1"})

    def test_a_row_without_a_target_raises(self) -> None:
        with pytest.raises(ValueError, match="without a target"):
            deserialise_volume_pairs({"db1": [{"source": "postgres-data-db1"}]})

    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            ({"source": "postgres-data-db1", "target": "x"}, True),
            ({"source": "/srv/uploads", "target": "/srv/uploads"}, False),
            ({"source": None, "target": "x"}, False),
            ({"source": "", "target": "x"}, False),
            ("postgres-data-db1", False),
        ],
    )
    def test_is_volume_row(self, row: object, expected: bool) -> None:
        assert is_volume_row(row) is expected


async def test_resume_rehydrates_volume_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix itself: a resumed context carries DISCOVER's pairs into COPY."""
    journal = Journal.create(tmp_path, "m1")
    journal.append("step_completed", state="create_target", detail={"target_uuids": {"db1": "db2"}})
    journal.append("step_started", state="quiesce", detail={"pre_stop_mounts": {"db1": []}})
    journal.append("step_completed", state="quiesce", detail={})
    journal.append("step_completed", state="discover", detail={"volume_pairs": RECORDED})
    journal.append("step_started", state="copy")  # the crash happened in here

    @asynccontextmanager
    async def fake_hosts(*_a: Any, **_kw: Any) -> AsyncIterator[tuple[FakeHost, FakeHost]]:
        yield FakeHost(), FakeHost()

    seen: dict[str, MigrationContext] = {}

    async def fake_execute(ctx: MigrationContext, **_kw: Any) -> RunResult:
        seen["ctx"] = ctx
        return RunResult(outcome=Outcome.SUCCEEDED, reached=State.DONE)

    monkeypatch.setattr(runner, "open_hosts", fake_hosts)
    monkeypatch.setattr(runner, "_execute", fake_execute)

    async with CoolifyClient(HOST, "tok", max_retries=0) as api:
        await runner.resume_migration(
            api, Settings(_env_file=None, state_dir=tmp_path), _plan(), "m1"
        )

    ctx = seen["ctx"]
    assert [p.target.name for p in ctx.volume_pairs["db1"]] == ["postgres-data-db2"]
    assert ctx.target_uuids == {"db1": "db2"}
    assert ctx.pre_stop_mounts == {"db1": []}


class TestPairsNotLoadedGuard:
    """COPY and VERIFY refuse to do nothing when the journal says otherwise."""

    def _discover_completed(self, ctx: MigrationContext, recorded: object) -> None:
        ctx.journal.append("step_completed", state="discover", detail={"volume_pairs": recorded})

    async def test_copy_refuses_when_discover_paired_volumes_this_run_lacks(
        self, ctx: MigrationContext
    ) -> None:
        self._discover_completed(ctx, RECORDED)
        with pytest.raises(TransferError, match="postgres") as exc:
            await steps.step_copy(ctx)
        assert "copy" in str(exc.value)
        # Refused before anything touched a server: no key installed.
        assert ctx.ephemeral_key is None
        assert ctx.target_host.commands == []  # type: ignore[attr-defined]

    async def test_verify_refuses_too(self, ctx: MigrationContext) -> None:
        self._discover_completed(ctx, RECORDED)
        with pytest.raises(TransferError, match="verify"):
            await steps.step_verify(ctx)

    async def test_a_bind_mount_only_resource_is_not_a_lost_pair(
        self, ctx: MigrationContext
    ) -> None:
        self._discover_completed(
            ctx, {"db1": [{"source": "/srv/x", "target": "/srv/x", "mount_path": "/x"}]}
        )
        assert (await steps.step_copy(ctx))["volumes_copied"] == []

    async def test_loaded_pairs_pass_the_guard(self, ctx: MigrationContext) -> None:
        self._discover_completed(ctx, RECORDED)
        ctx.volume_pairs = deserialise_volume_pairs(RECORDED)
        steps._assert_pairs_loaded(ctx, step="copy")  # no raise

    def test_pairs_not_loaded_is_pure_and_tolerates_garbage(self) -> None:
        assert steps._pairs_not_loaded(None, {}) == []
        assert steps._pairs_not_loaded({"db1": "nope"}, {}) == []
        assert steps._pairs_not_loaded(RECORDED, {}) == ["db1"]
