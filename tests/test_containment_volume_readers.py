"""A read-only reader of a source volume is no revival.

Coolify's scheduled volume backup (4.3.0+) runs ``docker run --rm --name
volume-backup-<execution uuid> -v <volume>:/volume:ro`` whatever the resource's
status: its scheduler has no 'running' skip (VolumeBackupJob v4.7.2:101-123,
ScheduledJobManager v4.7.2:531-600). It carries no Coolify label and no resource
uuid, so only the migrated-volume probe finds it, and every checkpoint from COPY
to FINALIZE used to roll a run back over it. A container found ONLY that way
counts when it mounts a migrated volume writable; every labelled or uuid-carrying
container counts as before, and so does one whose mounts cannot be told.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import containment as c
from bg_coolify_migrate.domain.containment import STOP_CLOCK
from bg_coolify_migrate.domain.statemachine import STOP_REQUESTED, State
from bg_coolify_migrate.engine import containment
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import SourceRevived
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import SshError
from tests.fake_daemon import FakeDaemon
from tests.test_engine_steps import HOST, _plan
from tests.test_pins_engine import COOLIFY, MID, VOL, _source, _target

STACK = {"coolify.projectName": "shop", "coolify.environmentName": "production"}
TRIPLE = {**STACK, "coolify.resourceName": "postgres"}
SRC = c.Watched(name="postgres", uuid="db1aaaaaaaaaaaaaaaaaaaaa", labels=TRIPLE)
STOP = 1_800_000_000
BACKUP = "volume-backup-7f3c2a"  # Coolify names it after the execution, not the resource


def _mounts(*entries: tuple[str, bool]) -> str:
    return json.dumps(
        [
            {"Type": "volume", "Name": name, "Destination": "/volume", "RW": rw}
            for name, rw in entries
        ]
    )


class TestMountsWritable:
    def test_a_read_only_mount_of_the_volume(self) -> None:
        assert c.mounts_writable(_mounts((VOL, False)), [VOL]) is False

    def test_a_writable_mount_of_the_volume(self) -> None:
        assert c.mounts_writable(_mounts((VOL, True)), [VOL]) is True

    def test_writable_on_any_migrated_volume_is_a_writer(self) -> None:
        mounts = _mounts((VOL, False), ("other-data", True), ("redis-data-r1", True))
        assert c.mounts_writable(mounts, [VOL, "redis-data-r1"]) is True

    def test_only_the_migrated_volumes_matter(self) -> None:
        # Writing elsewhere cannot make the copy of VOL stale.
        assert c.mounts_writable(_mounts((VOL, False), ("scratch", True)), [VOL]) is False

    def test_rw_left_out_is_dockers_default_read_write(self) -> None:
        assert c.mounts_writable(json.dumps([{"Name": VOL}]), [VOL]) is True

    @pytest.mark.parametrize(
        "raw",
        ["", "not json", "{}", json.dumps([{"Name": "other", "RW": False}]), json.dumps(["x"])],
    )
    def test_what_cannot_be_told_is_none(self, raw: str) -> None:
        # The caller counts None as a writer: never silently a reader.
        assert c.mounts_writable(raw, [VOL]) is None


class TestVolumeOnly:
    def test_no_stack_label_and_no_uuid(self) -> None:
        assert c.volume_only([SRC], STACK, name=BACKUP, image="coollabsio/coolify-helper:1", labels={})

    def test_the_stack_labels_make_it_the_stacks(self) -> None:
        assert not c.volume_only([SRC], STACK, name="worker", image="x", labels=dict(STACK))

    def test_a_uuid_makes_it_the_resources(self) -> None:
        assert not c.volume_only([SRC], STACK, name=f"postgres-{SRC.uuid}", image="x", labels={})


class TestSourceBreaches:
    def _judge(self, *seen: c.Seen) -> list[c.Breach]:
        return c.source_breaches([SRC], seen, c.EventLog(oldest=STOP - 1), since=STOP, stack=STACK)

    def test_a_running_read_only_reader_is_no_breach(self) -> None:
        assert self._judge(c.Seen(name=BACKUP, state="running", labels={}, writes=False)) == []

    def test_a_running_writer_is_one(self) -> None:
        (breach,) = self._judge(c.Seen(name="debug", state="running", labels={}, writes=True))
        assert breach.describe() == "the stack: debug is running"

    def test_mounts_that_could_not_be_told_count_as_a_writer(self) -> None:
        assert self._judge(c.Seen(name="debug", state="running", labels={}, writes=None))

    def test_a_writer_that_ran_after_the_stop_and_stopped(self) -> None:
        seen = c.Seen(name="debug", state="exited", labels={}, started_at=STOP + 5, writes=True)
        assert "after the stop" in self._judge(seen)[0].why

    def test_a_labelled_container_counts_even_read_only(self) -> None:
        # Only a container the volume probe alone found is judged by its mounts.
        seen = c.Seen(name="postgres-db1", state="running", labels=TRIPLE, writes=False)
        assert self._judge(seen)[0].resource == "postgres"

    def test_a_uuid_carrier_counts_even_read_only(self) -> None:
        seen = c.Seen(name=f"postgres-{SRC.uuid}", state="running", labels={}, writes=False)
        assert self._judge(seen)


# ── the checkpoint, against a daemon model ──────────────────────────────────


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
        target_host=_target(),  # type: ignore[arg-type]
    )
    context.target_uuids["db1"] = "db2"
    context.journal.append(
        "step_started", state=State.QUIESCE.value, detail={STOP_REQUESTED: True, STOP_CLOCK: STOP}
    )
    yield context
    await api.aclose()


def _src(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.source_host, FakeDaemon)
    return ctx.source_host


class TestTheCheckpoint:
    async def test_a_scheduled_volume_backup_passes(self, ctx: MigrationContext) -> None:
        _src(ctx).add(BACKUP, labels={}, volumes=(VOL,), read_only=True)
        await containment.assert_contained(ctx, checkpoint="the end of COPY", target=False)
        assert any("{{json .Mounts}}" in cmd and BACKUP in cmd for cmd in _src(ctx).commands)

    async def test_an_unlabelled_writer_still_rolls_back(self, ctx: MigrationContext) -> None:
        _src(ctx).add("debug-shell", labels={}, volumes=(VOL,))
        with pytest.raises(SourceRevived, match="the stack: debug-shell is running"):
            await containment.assert_contained(ctx, checkpoint="COPY", target=False)

    async def test_a_labelled_container_is_never_asked_for_its_mounts(
        self, ctx: MigrationContext
    ) -> None:
        _src(ctx).add("postgres-db1", labels=COOLIFY, volumes=(VOL,), read_only=True)
        with pytest.raises(SourceRevived, match="postgres: postgres-db1 is running"):
            await containment.assert_contained(ctx, checkpoint="the DNS gate", target=False)
        assert not any("{{json .Mounts}}" in cmd for cmd in _src(ctx).commands)

    async def test_a_reader_gone_before_its_mounts_were_read_counts(
        self, ctx: MigrationContext
    ) -> None:
        # Listed running, removed (--rm) before the inspect: nothing says it only read.
        _src(ctx).add(BACKUP, labels={}, volumes=(VOL,), read_only=True)
        _src(ctx).on(r"json \.Mounts", stderr=f"Error: No such object: {BACKUP}", exit_status=1)
        with pytest.raises(SourceRevived, match=f"the stack: {BACKUP} is running"):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)

    async def test_a_daemon_that_will_not_say_fails_the_check(self, ctx: MigrationContext) -> None:
        _src(ctx).add(BACKUP, labels={}, volumes=(VOL,), read_only=True)
        _src(ctx).on(r"json \.Mounts", stderr="Cannot connect to the Docker daemon", exit_status=1)
        with pytest.raises(SshError, match="Cannot connect"):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
