"""Where the migration pins and unpins volumes against Coolify's Docker cleanup.

A server with "Delete unused volumes" on runs `docker volume prune -af`, which
deletes every volume no container references. QUIESCE leaves the source's
volumes exactly that way (Coolify's stop removes the containers), and COPY fills
target volumes nothing mounts yet. Each test here pins down one moment of the
pin's life: made before the exposure starts, released only once Coolify's own
containers reference the data again — or kept, loudly, when they do not.
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
from bg_coolify_migrate.discovery.docker import Container
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.manifest import Decision, DockerMount, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.naming import VolumeEndpoint, VolumePair
from bg_coolify_migrate.domain.plan import ResourcePlan, Strategy
from bg_coolify_migrate.domain.statemachine import Compensation, FinalizePolicy, Outcome, State
from bg_coolify_migrate.engine import compensations, runner, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import RunResult, Saga
from bg_coolify_migrate.errors import PreflightError, QuiesceError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import SshError, SshTarget
from bg_coolify_migrate.ui import run_report
from tests.fake_daemon import FakeDaemon
from tests.test_engine_steps import BASE, HOST, _manifest, _plan, _snapshot

MID = "m1"
VOL = "postgres-data-db1"
TVOL = "postgres-data-db2"
COOLIFY = {
    "coolify.managed": "true",
    "coolify.projectName": "shop",
    "coolify.environmentName": "production",
    "coolify.resourceName": "postgres",
}


def _labels(side: str, migration: str = MID) -> dict[str, str]:
    return {pins.LABEL_ROLE: pins.ROLE_PIN, pins.LABEL_MIGRATION: migration, pins.LABEL_SIDE: side}


def _source(*volumes: str) -> FakeDaemon:
    daemon = FakeDaemon(volumes=volumes)
    daemon.on(r"^date \+%s$", stdout="1000")
    daemon.on(r"rm -rf /root/\.coolify-migrate/", exit_status=0)
    return daemon


def _target(*volumes: str) -> FakeDaemon:
    daemon = FakeDaemon(volumes=volumes, target=SshTarget(host="10.0.0.2"))
    daemon.on(r"authorized_keys\.bak", exit_status=0)
    return daemon


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
    yield context
    await api.aclose()


def _src(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.source_host, FakeDaemon)
    return ctx.source_host


def _tgt(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.target_host, FakeDaemon)
    return ctx.target_host


def _no_sleep_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(_seconds: float) -> None:
        raise AssertionError("waited, but nothing could have changed")

    monkeypatch.setattr(pins.asyncio, "sleep", forbidden)


# ── QUIESCE ──────────────────────────────────────────────────────────────────


class TestQuiesce:
    async def test_source_is_pinned_before_the_stop_is_requested(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        source = _src(ctx)
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        source.on(r"^docker events", stdout="")
        pinned_at_stop: list[bool] = []

        def stop(_request: httpx.Request) -> httpx.Response:
            pinned_at_stop.append(bool(source.created))
            del source.containers["postgres-db1"]  # docker stop && docker rm -f
            return httpx.Response(200, json={})

        respx_mock.post(f"{BASE}/databases/db1/stop").mock(side_effect=stop)

        result = await steps.step_quiesce(ctx)

        assert pinned_at_stop == [True]
        assert len(result["source_pins"]) == 1
        pin = source.containers[result["source_pins"][0]]
        assert pin.volumes == [VOL] and pin.state == "created"
        assert ctx.journal.undo_info("quiesce")["source_pins"] == result["source_pins"]

    async def test_pin_failure_stops_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        source = _src(ctx)
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        source.create_failures = [(1, "Cannot connect to the Docker daemon")]
        stop = respx_mock.post(f"{BASE}/databases/db1/stop").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(QuiesceError, match="could not pin") as exc:
            await steps.step_quiesce(ctx)
        assert not stop.called
        assert "Nothing was stopped" in (exc.value.hint or "")
        assert "postgres-db1" in source.containers

    async def test_failure_after_the_stop_names_the_pin_in_the_hint(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        source = _src(ctx)
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        source.on(r"^docker events", stdout="postgres-db1 137")  # SIGKILLed data holder

        def stop(_request: httpx.Request) -> httpx.Response:
            del source.containers["postgres-db1"]
            return httpx.Response(200, json={})

        respx_mock.post(f"{BASE}/databases/db1/stop").mock(side_effect=stop)

        with pytest.raises(QuiesceError, match="SIGKILLed") as exc:
            await steps.step_quiesce(ctx)
        pin = next(iter(source.containers))
        hint = exc.value.hint or ""
        assert pin in hint and "removes the pin" in hint
        # The saga's rollback runs next and restarts the source: sending the
        # operator into a second rollback would restart a serving source again.
        assert "coolify-migrate rollback" not in hint
        # The step itself leaves the pin: the source is down until the restart.
        assert source.containers[pin].volumes == [VOL]

    async def test_an_already_stopped_resource_pins_its_planned_volumes(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        source = _src(ctx)  # no containers at all
        source.on(r"^docker events", stdout="")
        respx_mock.post(f"{BASE}/databases/db1/stop").mock(
            return_value=httpx.Response(400, json={"message": "Database is already stopped."})
        )

        result = await steps.step_quiesce(ctx)
        assert source.containers[result["source_pins"][0]].volumes == [VOL]


def test_preview_mounts_are_not_pinned() -> None:
    """Coolify >= 4.4 throws when a preview's volume is in use at its delete."""
    resource = ResourcePlan(snapshot=_snapshot(), strategy=Strategy.COPY_DATA, manifest=_manifest())
    containers = [
        Container(id="b1", name="base", state="running", labels={}),
        Container(id="p1", name="pr", state="running", labels={"coolify.pullRequestId": "7"}),
    ]
    mounts = [
        DockerMount(container="b1", type="volume", name="base-cache", destination="/cache"),
        DockerMount(container="p1", type="volume", name="base-cache-pr-7", destination="/cache"),
        DockerMount(container="b1", type="bind", source="/srv/x", destination="/x"),
    ]
    assert steps._source_volumes_to_pin(resource, containers, mounts) == ["base-cache", VOL]


# ── DISCOVER / COPY ──────────────────────────────────────────────────────────


async def test_discover_pins_a_volume_only_it_sees(
    ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared volume no container mounted: QUIESCE could not have pinned it."""
    source = _src(ctx)
    source.volumes.add("db1-extra")
    source.add("quiesce-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
    manifest = VolumeManifest(
        items=(
            *_manifest().items,
            VolumeItem(
                mount_class=MountClass.NAMED,
                decision=Decision.MIGRATE,
                reason="declared persistent storage whose volume exists",
                source_name="db1-extra",
                source_path="/var/lib/docker/volumes/db1-extra/_data",
                mount_path="/extra",
                bytes=1,
            ),
        )
    )
    ctx.target_uuids = {"db1": "db2"}
    ctx.pre_stop_mounts = {"db1": []}
    object.__setattr__(ctx.settings, "target_storage_timeout", 0.0)

    async def fake_manifest(host, *, mounts, api_storages, uuid, measure):  # type: ignore[no-untyped-def]
        return manifest

    async def fake_read(api, *, collection, uuid):  # type: ignore[no-untyped-def]
        return [
            VolumeEndpoint(TVOL, "/var/lib/postgresql/data"),
            VolumeEndpoint("db2-extra", "/extra"),
        ]

    async def no_storages(*_a: Any) -> None:
        return None

    monkeypatch.setattr(steps, "build_manifest", fake_manifest)
    monkeypatch.setattr(steps, "_storages_or_none", no_storages)
    monkeypatch.setattr("bg_coolify_migrate.api.resources.read_volume_endpoints", fake_read)

    result = await steps.step_discover(ctx)

    added = [n for n in result["source_pins"] if n != "quiesce-pin"]
    assert len(added) == 1 and source.containers[added[0]].volumes == ["db1-extra"]


async def test_target_volumes_are_created_then_pinned_before_rsync(
    ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = _src(ctx), _tgt(ctx)
    source.on(r"mkdir -p /root/\.coolify-migrate/", exit_status=0)
    target.on(r"mkdir -p ~/\.ssh", exit_status=0)
    target.on(r">> ~/\.ssh/authorized_keys", exit_status=0)
    ctx.volume_pairs = {
        "db1": [VolumePair(VolumeEndpoint(VOL, "/data"), VolumeEndpoint(TVOL, "/data"))]
    }
    pinned_at_copy: list[list[str]] = []

    async def fake_copy(_ctx: MigrationContext, source_path: str, target_path: str) -> None:
        pinned_at_copy.append([v for c in target.containers.values() for v in c.volumes])

    monkeypatch.setattr(steps, "_copy_one", fake_copy)

    result = await steps.step_copy(ctx)

    assert pinned_at_copy == [[TVOL]]
    order = [
        c.split()[1] if c.startswith("docker volume") else "pin"
        for c in target.commands
        if c.startswith("docker volume create") or "docker create" in c
    ]
    assert order == ["volume", "pin"]
    assert ctx.journal.undo_info("copy")["target_pins"] == result["target_pins"]


# ── FINALIZE ─────────────────────────────────────────────────────────────────


def _pinned_world(ctx: MigrationContext, *, target_mounted: bool = True) -> None:
    """A run at FINALIZE: both sides pinned, the target's container up."""
    _src(ctx).add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
    target = _tgt(ctx)
    target.add("tgt-pin", state="created", labels=_labels(pins.TARGET), volumes=(TVOL,))
    if target_mounted:
        target.add("postgres-db2", labels=COOLIFY, volumes=(TVOL,))


class TestFinalize:
    async def test_finalize_unpins_the_target(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(finalize_policy=FinalizePolicy.KEEP)
        _pinned_world(ctx)

        result = await steps.step_finalize(ctx)

        assert "tgt-pin" not in _tgt(ctx).containers
        assert set(result["pins_released"]) == {"tgt-pin", "src-pin"}
        assert result["pins_left"] == [] and ctx.notices == []

    async def test_delete_unpins_the_source_before_deleting_it(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Coolify's `docker volume rm -f` would refuse the pinned volume and
        # swallow the refusal: a "deleted" source that keeps its data forever.
        ctx.plan = _plan(finalize_policy=FinalizePolicy.DELETE)
        _pinned_world(ctx)
        pinned_at_delete: list[bool] = []

        def delete(_request: httpx.Request) -> httpx.Response:
            pinned_at_delete.append("src-pin" in _src(ctx).containers)
            return httpx.Response(200, json={})

        respx_mock.delete(f"{BASE}/databases/db1").mock(side_effect=delete)

        await steps.step_finalize(ctx)
        assert pinned_at_delete == [False]

    async def test_delete_source_release_failure_raises_before_any_delete(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(finalize_policy=FinalizePolicy.DELETE)
        _pinned_world(ctx)
        _src(ctx).rm_failures = [(1, "Cannot connect to the Docker daemon")]
        delete = respx_mock.delete(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(SshError):
            await steps.step_finalize(ctx)
        assert not delete.called

    async def test_rename_unpins_the_source_after_the_rename(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A failed rename rolls back with the source still pinned.
        ctx.plan = _plan(finalize_policy=FinalizePolicy.RENAME)
        _pinned_world(ctx)
        pinned_at_rename: list[bool] = []

        def rename(_request: httpx.Request) -> httpx.Response:
            pinned_at_rename.append("src-pin" in _src(ctx).containers)
            return httpx.Response(200, json={})

        respx_mock.patch(f"{BASE}/databases/db1").mock(side_effect=rename)

        await steps.step_finalize(ctx)
        assert pinned_at_rename and all(pinned_at_rename)
        assert "src-pin" not in _src(ctx).containers

    async def test_rename_then_source_release_failure_still_succeeds(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Raising here would roll back a healthy target and leave the source
        # renamed: RESTORE_SOURCE_NAME is owed only on a completed FINALIZE.
        ctx.plan = _plan(finalize_policy=FinalizePolicy.RENAME)
        _pinned_world(ctx)
        _src(ctx).rm_failures = [(1, "Cannot connect to the Docker daemon")]
        respx_mock.patch(f"{BASE}/databases/db1").mock(return_value=httpx.Response(200, json={}))

        result = await steps.step_finalize(ctx)
        assert any("renamed postgres" in a for a in result["actions"])
        assert any(pins.LABEL_MIGRATION in n for n in ctx.notices)

    async def test_finalize_target_release_failure_does_not_roll_back(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(finalize_policy=FinalizePolicy.KEEP)
        _pinned_world(ctx)
        _tgt(ctx).on(r"label=bg-coolify-migrate\.side=target", exit_status=1, stderr="no daemon")

        result = await steps.step_finalize(ctx)
        assert result["policy"] == "keep"
        assert ctx.notices and ctx.plan.target_server.name in ctx.notices[0]

    async def test_finalize_keeps_target_pin_for_unmounted_volume(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # HEALTHCHECK proves a container serves, not which volumes it mounts. A
        # copied volume nothing mounts is, under DELETE, the only copy left.
        ctx.plan = _plan(finalize_policy=FinalizePolicy.KEEP)
        _pinned_world(ctx, target_mounted=False)

        result = await steps.step_finalize(ctx)
        assert result["pins_left"] == ["tgt-pin"]
        assert "tgt-pin" in _tgt(ctx).containers
        assert TVOL in ctx.notices[0]


# ── PREFLIGHT ────────────────────────────────────────────────────────────────


class TestPreflightProbe:
    async def test_a_stopped_pin_is_not_a_hidden_container(self, ctx: MigrationContext) -> None:
        source = _src(ctx)
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        source.add(
            "other-pin", state="created", labels=_labels(pins.SOURCE, "other"), volumes=(VOL,)
        )

        await steps._assert_containers_are_discoverable(ctx)  # no raise

    async def test_a_running_container_with_the_pin_label_still_blocks(
        self, ctx: MigrationContext
    ) -> None:
        # Invariant 9: nothing running near the data may be waved through.
        source = _src(ctx)
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        source.add(
            "impostor", state="running", labels=_labels(pins.SOURCE, "other"), volumes=(VOL,)
        )

        with pytest.raises(PreflightError, match="impostor"):
            await steps._assert_containers_are_discoverable(ctx)


# ── compensations ────────────────────────────────────────────────────────────


class TestTargetCompensations:
    async def test_undo_copy_unpins_before_dropping_volumes(self, ctx: MigrationContext) -> None:
        # Docker refuses to remove a referenced volume, even with -f.
        target = _tgt(ctx)
        target.add("tgt-pin", state="created", labels=_labels(pins.TARGET), volumes=(TVOL,))

        await compensations.undo_copy(ctx, {"volumes_copied": [TVOL]})

        assert TVOL not in target.volumes and not target.containers
        rm = next(i for i, c in enumerate(target.commands) if c.startswith("docker rm "))
        drop = next(i for i, c in enumerate(target.commands) if c.startswith("docker volume rm"))
        assert rm < drop

    async def test_undo_create_target_unpins_the_target_even_when_copy_never_completed(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A COPY that failed part-way owes nothing of its own: the pins and the
        # transfer key would outlive the rollback.
        target, source = _tgt(ctx), _src(ctx)
        target.add("tgt-pin", state="created", labels=_labels(pins.TARGET), volumes=(TVOL,))
        pinned_at_delete: list[bool] = []

        def delete(_request: httpx.Request) -> httpx.Response:
            pinned_at_delete.append("tgt-pin" in target.containers)
            return httpx.Response(200, json={})

        respx_mock.delete(f"{BASE}/databases/db2").mock(side_effect=delete)

        await compensations.undo_create_target(ctx, {"target_uuids": {"db1": "db2"}})

        assert pinned_at_delete == [False]
        assert any("authorized_keys.bak" in c for c in target.commands)
        assert any("rm -rf /root/.coolify-migrate/m1" in c for c in source.commands)

    async def test_a_failed_unpin_is_reported_but_the_delete_still_runs(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        target = _tgt(ctx)
        target.add("tgt-pin", state="created", labels=_labels(pins.TARGET), volumes=(TVOL,))
        target.rm_failures = [(1, "Cannot connect to the Docker daemon")]
        delete = respx_mock.delete(f"{BASE}/databases/db2").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(RuntimeError, match="target pins"):
            await compensations.undo_create_target(ctx, {"target_uuids": {"db1": "db2"}})
        assert delete.called

    async def test_a_failed_key_revoke_is_reported_but_the_delete_still_runs(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_host = FakeDaemon()  # type: ignore[assignment]  # cannot answer the revoke
        delete = respx_mock.delete(f"{BASE}/databases/db2").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(RuntimeError, match="transfer key"):
            await compensations.undo_create_target(ctx, {"target_uuids": {"db1": "db2"}})
        assert delete.called


def _quiesced(ctx: MigrationContext) -> None:
    ctx.journal.append("step_completed", state="create_target", detail={"target_uuids": {}})
    ctx.journal.append("step_started", state="quiesce", detail={"pre_stop_mounts": {"db1": []}})
    ctx.journal.append("step_completed", state="quiesce", detail={})


def _restart(ctx: MigrationContext, *, ok: bool) -> None:
    ctx.journal.append("rollback_started", detail={"steps": 3})
    ctx.journal.append(
        "rollback_step",
        state="quiesce",
        detail={"compensation": Compensation.RESTART_SOURCE.value, "ok": ok},
    )


class TestReleaseSourcePins:
    async def test_waits_for_the_restarted_source_then_unpins(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The capture was journalled EMPTY - a resumed QUIESCE does that - and the
        # pin still waits for its volume: the requirement comes from the pin.
        source = _src(ctx)
        source.add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
        _quiesced(ctx)
        _restart(ctx, ok=True)
        waits: list[float] = []

        async def remount(seconds: float) -> None:
            waits.append(seconds)
            source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))

        monkeypatch.setattr(pins.asyncio, "sleep", remount)

        await compensations.undo_release_source_pins(ctx, {})
        assert waits and "src-pin" not in source.containers
        assert ctx.notices == []

    async def test_zero_wait_when_quiesce_never_completed(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No stop on record - QUIESCE neither completed nor journalled its
        # stop_requested marker - so no restart ran: one look, and a pin whose
        # volume nothing mounts stays, without spending deploy_timeout on it.
        source = _src(ctx)
        source.add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
        ctx.journal.append("step_completed", state="create_target", detail={})
        _no_sleep_allowed(monkeypatch)

        await compensations.undo_release_source_pins(ctx, {})

        assert "src-pin" in source.containers
        assert "src-pin" in ctx.notices[0] and f"rollback {MID}" in ctx.notices[0]
        kept = [
            r.detail
            for r in ctx.journal.read()
            if r.event == "rollback_step"
            and r.detail.get("compensation") == Compensation.RELEASE_SOURCE_PINS.value
        ]
        assert kept == [
            {
                "compensation": "release_source_pins",
                "pins_kept": ["src-pin"],
                "unmounted": [VOL],
            }
        ]

    async def test_waits_after_a_quiesce_that_failed_after_its_stop_request(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # QUIESCE never completed, but its stop_requested marker makes the
        # restart owed (statemachine.source_was_stopped) - so the unpin waits
        # for that restart's remount exactly as after a completed QUIESCE.
        source = _src(ctx)
        source.add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
        ctx.journal.append("step_completed", state="create_target", detail={})
        ctx.journal.append("step_started", state="quiesce", detail={"stop_requested": True})
        ctx.journal.append("step_failed", state="quiesce", detail={})
        _restart(ctx, ok=True)
        waits: list[float] = []

        async def remount(seconds: float) -> None:
            waits.append(seconds)
            source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))

        monkeypatch.setattr(pins.asyncio, "sleep", remount)

        await compensations.undo_release_source_pins(ctx, {})
        assert waits and "src-pin" not in source.containers

    async def test_zero_wait_when_the_restart_failed(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _src(ctx).add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
        _quiesced(ctx)
        _restart(ctx, ok=False)
        _no_sleep_allowed(monkeypatch)

        await compensations.undo_release_source_pins(ctx, {})
        assert "src-pin" in _src(ctx).containers

    async def test_an_earlier_rollbacks_restart_does_not_count(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _src(ctx).add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
        _quiesced(ctx)
        _restart(ctx, ok=True)
        ctx.journal.append("rollback_started", detail={"steps": 1})  # a later rollback
        _no_sleep_allowed(monkeypatch)

        await compensations.undo_release_source_pins(ctx, {})
        assert "src-pin" in _src(ctx).containers

    async def test_a_source_that_still_serves_is_unpinned_at_once(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # QUIESCE failed before its stop landed: the volumes never went idle.
        source = _src(ctx)
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        source.add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
        _no_sleep_allowed(monkeypatch)

        await compensations.undo_release_source_pins(ctx, {})
        assert "src-pin" not in source.containers

    async def test_a_failed_restart_says_the_pins_stay(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.post(f"{BASE}/databases/db1/restart").mock(return_value=httpx.Response(500))
        with pytest.raises(RuntimeError, match=r"could not restart.*pins .* stay"):
            await compensations.undo_quiesce(ctx, {})

    async def test_no_pins_is_a_noop(self, ctx: MigrationContext) -> None:
        await compensations.undo_release_source_pins(ctx, {})
        assert [c for c in _src(ctx).commands if c.startswith("docker rm")] == []


async def test_rollback_unpins_source_after_restart_and_delete(
    ctx: MigrationContext, respx_mock: respx.Router
) -> None:
    """The unpin comes after everything: the restart and the target delete.

    It waits for the restarted source to remount its volumes; nothing else may
    wait behind that (the domain restore, for a resource with domains, runs
    before the restart - tests/test_rollback_domains_engine.py).
    """
    source = _src(ctx)
    source.add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))
    ctx.journal.append(
        "step_completed", state="create_target", detail={"target_uuids": {"db1": "db2"}}
    )
    ctx.journal.append("step_completed", state="quiesce", detail={})
    order: list[str] = []

    def restart(_request: httpx.Request) -> httpx.Response:
        order.append("restart")
        source.add("postgres-db1", labels=COOLIFY, volumes=(VOL,))
        return httpx.Response(200, json={})

    def record(name: str) -> Any:
        def side_effect(_request: httpx.Request) -> httpx.Response:
            order.append(name)
            return httpx.Response(200, json={})

        return side_effect

    respx_mock.post(f"{BASE}/databases/db1/restart").mock(side_effect=restart)
    respx_mock.delete(f"{BASE}/databases/db2").mock(side_effect=record("delete"))
    source.on_command = lambda c: order.append("unpin") if c.startswith("docker rm ") else None

    result = await Saga(
        journal=ctx.journal,
        context=ctx,
        steps={},
        compensations=compensations.build_compensations(),
    ).rollback()

    assert result.outcome is Outcome.ROLLED_BACK
    assert order == ["restart", "delete", "unpin"]


async def test_a_kept_pin_reaches_the_report_and_the_outcome_stays_rolled_back(
    tmp_path: Path, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rollback <id>` on a run whose source is down: the pin stays, and says so."""
    journal = Journal.create(tmp_path, MID)
    journal.append("step_completed", state="create_target", detail={"target_uuids": {}})
    source = _source(VOL)
    source.add("src-pin", state="created", labels=_labels(pins.SOURCE), volumes=(VOL,))

    @asynccontextmanager
    async def hosts(*_a: Any, **_kw: Any) -> AsyncIterator[tuple[FakeDaemon, FakeDaemon]]:
        yield source, _target()

    monkeypatch.setattr(runner, "open_hosts", hosts)
    async with CoolifyClient(HOST, "tok", max_retries=0) as api:
        result = await runner.rollback_migration(
            api, Settings(_env_file=None, state_dir=tmp_path), _plan(), MID
        )

    assert result.outcome is Outcome.ROLLED_BACK
    assert len(result.notices) == 1 and "src-pin" in result.notices[0]
    assert "notice: " in run_report.plain_result(result, migration_id=MID)


def test_the_run_report_lists_what_was_left_in_place() -> None:
    from rich.console import Console

    from bg_coolify_migrate.ui.console import THEME

    result = RunResult(
        outcome=Outcome.ROLLED_BACK, reached=State.VERIFY, notices=["src-pin still pins v1"]
    )
    console = Console(record=True, width=200, theme=THEME)
    console.print(run_report.outcome_panel(result, migration_id=MID))
    text = console.export_text()
    assert "Left in place:" in text and "src-pin still pins v1" in text
