"""A rollback after FINALIZE(keep/rename) tells a recreated-empty volume from the one COPY read.

A volume pruned after FINALIZE released its pins, then recreated EMPTY by a
redeploy (ApplicationDeploymentJob v4.7.2:5011-5030, ``external: false``), has
the same name: the name check passed, the rollback dropped the target and
restarted the source on nothing. COPY journals each source volume's CreatedAt
once the end-of-copy checkpoint passed, and the guard compares it — as an
instant, so a daemon's time zone is no new volume.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bg_coolify_migrate.domain import containment as c
from bg_coolify_migrate.domain.containment import SOURCE_VOLUMES_CREATED
from bg_coolify_migrate.domain.naming import VolumeEndpoint, VolumePair
from bg_coolify_migrate.domain.statemachine import State
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.transfer.ssh import SshError
from tests.test_containment_engine import TVOL, _src, _tgt, ctx
from tests.test_engine_steps import BASE
from tests.test_pins_engine import VOL

__all__ = ["ctx"]  # a fixture, imported for pytest


class TestParsing:
    def test_volume_created(self) -> None:
        out = c.parse_volume_created("vol-a 2027-01-15T08:00:00Z\n\nvol-b\nvol-c  x \n")
        assert out == {"vol-a": "2027-01-15T08:00:00Z", "vol-c": "x"}


class TestVolumeRecreated:
    def test_the_same_instant_in_another_zone_is_the_same_volume(self) -> None:
        assert not c.volume_recreated("2027-01-15T08:00:00Z", "2027-01-15T09:00:00+01:00")

    def test_a_new_creation_time_is_a_new_volume(self) -> None:
        assert c.volume_recreated("2027-01-15T08:00:00Z", "2027-01-16T08:00:00Z")

    def test_unparseable_values_compare_as_text(self) -> None:
        assert not c.volume_recreated("weird", " weird ")
        assert c.volume_recreated("weird", "other")


class TestCopyJournalsWhatItCopiedFrom:
    async def test_copy_journals_what_it_copied_from(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source, target = _src(ctx), _tgt(ctx)
        source.volume_created[VOL] = "2027-01-01T00:00:00Z"
        source.on(r"mkdir -p /root/\.coolify-migrate/", exit_status=0)
        target.on(r"mkdir -p ~/\.ssh", exit_status=0)
        target.on(r">> ~/\.ssh/authorized_keys", exit_status=0)
        ctx.volume_pairs = {
            "db1": [VolumePair(VolumeEndpoint(VOL, "/data"), VolumeEndpoint(TVOL, "/data"))]
        }

        async def fake_copy(_ctx: MigrationContext, source_path: str, target_path: str) -> None:
            return None

        monkeypatch.setattr(steps, "_copy_one", fake_copy)
        await steps.step_copy(ctx)
        recorded = ctx.journal.undo_info(State.COPY.value)[SOURCE_VOLUMES_CREATED]
        assert recorded == {VOL: "2027-01-01T00:00:00Z"}


class TestTheRollbackGuard:
    def _finalized(self, ctx: MigrationContext, created: str | None) -> None:
        pairs = {"db1": [{"source": VOL, "target": TVOL, "mount_path": "/data"}]}
        for state in (State.CREATE_TARGET, State.QUIESCE):
            ctx.journal.append("step_completed", state=state.value, detail={})
        ctx.journal.append(
            "step_completed", state=State.DISCOVER.value, detail={"volume_pairs": pairs}
        )
        if created is not None:
            ctx.journal.append(
                "step_started",
                state=State.COPY.value,
                detail={SOURCE_VOLUMES_CREATED: {VOL: created}},
            )
        for state in (State.COPY, State.START_TARGET):
            ctx.journal.append("step_completed", state=state.value, detail={})
        ctx.journal.append("step_completed", state=State.FINALIZE.value, detail={"policy": "keep"})

    async def test_a_volume_recreated_under_its_name_refuses(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Pruned after FINALIZE, recreated EMPTY by a redeploy: same name, new volume.
        self._finalized(ctx, "2027-01-01T00:00:00Z")
        _src(ctx).volume_created[VOL] = "2027-02-01T00:00:00Z"
        respx_mock.get(f"{BASE}/databases/db1").mock(return_value=httpx.Response(200, json={}))
        reason = await compensations.source_data_refusal(ctx)
        assert reason is not None
        assert f"volume {VOL} (created 2027-02-01T00:00:00Z" in reason
        assert "recreated empty" in reason and "only copy" in reason

    async def test_the_volume_the_copy_read_passes(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        self._finalized(ctx, "2027-01-01T01:00:00+01:00")
        _src(ctx).volume_created[VOL] = "2027-01-01T00:00:00Z"  # the same instant
        respx_mock.get(f"{BASE}/databases/db1").mock(return_value=httpx.Response(200, json={}))
        assert await compensations.source_data_refusal(ctx) is None

    async def test_an_older_journal_keeps_the_name_check(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        self._finalized(ctx, None)
        respx_mock.get(f"{BASE}/databases/db1").mock(return_value=httpx.Response(200, json={}))
        assert await compensations.source_data_refusal(ctx) is None
        assert not any("docker volume inspect" in c for c in _src(ctx).commands)

    async def test_a_creation_time_that_cannot_be_read_raises(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The saga turns a guard that cannot answer into a refusal.
        self._finalized(ctx, "2027-01-01T00:00:00Z")
        _src(ctx).on(r"docker volume inspect --format", exit_status=1, stderr="daemon down")
        respx_mock.get(f"{BASE}/databases/db1").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(SshError, match="daemon down"):
            await compensations.source_data_refusal(ctx)
