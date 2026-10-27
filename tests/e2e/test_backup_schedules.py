"""A database's backup schedules, migrated for real.

The unit suite pins the bodies we build and the pre-4.5 shapes the rig cannot
produce. This proves the parts only a real Coolify can: that create_backup
accepts what we send, that a 4.5 row's S3 destinations arrive on the target in
order and by uuid, that the source's history is reported rather than carried,
and that the target does not dump before it is started.

The S3 storages are dummies. Coolify stores one without connecting (validation
is a separate route), and schedule B is yearly, so nothing ever uploads. The
endpoint is a public IP literal: SafeWebhookUrl resolves hostnames, refuses the
documentation ranges (198.51.100.0/24 among them), and the rig may have no DNS.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome, State
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import run_migration
from bg_coolify_migrate.settings.base import Settings

from .conftest import ssh_to

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

PG = "App\\Models\\StandalonePostgresql"

#: Everything a schedule carries that must be equal on both sides.
CARRIED = (
    "frequency",
    "enabled",
    "save_s3",
    "dump_all",
    "databases_to_backup",
    "database_backup_retention_amount_locally",
    "database_backup_retention_days_locally",
    "database_backup_retention_max_storage_locally",
    "database_backup_retention_amount_s3",
    "database_backup_retention_days_s3",
    "database_backup_retention_max_storage_s3",
    "timeout",
    "missing_backup_notification_days",
)


@pytest_asyncio.fixture
async def s3_storages(api: CoolifyClient) -> AsyncIterator[tuple[str, str]]:
    """Two dummy S3 storages. A 422 here FAILS: skipping would hide the only
    real-instance coverage of the 4.5 S3-uuid path on every run."""
    suffix = uuidlib.uuid4().hex[:8]
    made: list[str] = []
    try:
        for label in ("a", "b"):
            created = await api.post(
                "/s3-storages",
                {
                    "name": f"bgcm-{label}-{suffix}",
                    "endpoint": "https://1.1.1.1",
                    "bucket": f"bgcm-{label}-{suffix}",
                    "region": "us-east-1",
                    # Dummies for a storage nothing ever uploads to.
                    "key": "bgcm-e2e-key",
                    "secret": "bgcm-e2e-not-a-secret",
                },
            )
            made.append(str(created["uuid"]))
        yield made[0], made[1]
    finally:
        for uuid in made:
            with contextlib.suppress(Exception):
                await api.delete(f"/s3-storages/{uuid}")


def _own(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("database_type") == PG]


async def _await_success(api: CoolifyClient, db_uuid: str, backup_uuid: str) -> None:
    """Poll until backup_now's dump has succeeded on the source."""
    deadline = asyncio.get_running_loop().time() + 120
    statuses: list[str] = []
    while asyncio.get_running_loop().time() < deadline:
        row = next(r for r in await api.get_backups(db_uuid) if r["uuid"] == backup_uuid)
        statuses = [str(e.get("status")) for e in row["executions"]]
        if "success" in statuses:
            return
        await asyncio.sleep(5)
    raise AssertionError(f"the seeded backup never succeeded (executions: {statuses})")


async def _database_on(api: CoolifyClient, name: str, server_uuid: str) -> str | None:
    for db in await api.get("/databases") or []:
        if not isinstance(db, dict) or db.get("name") != name:
            continue
        full = await api.get_resource("databases", str(db["uuid"]))
        if server_uuid_of(full) == server_uuid:
            return str(db["uuid"])
    return None


async def test_backup_schedules_follow_the_database(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    seeded_pg: dict[str, Any],
    s3_storages: tuple[str, str],
) -> None:
    s3a, s3b = s3_storages
    source_uuid = seeded_pg["db_uuid"]

    # A: hourly, local, run once now so the source has a history to report.
    a = await api.post(
        f"/databases/{source_uuid}/backups",
        {
            "frequency": "0 * * * *",
            "save_s3": False,
            "database_backup_retention_amount_locally": 3,
            "timeout": 1200,
            "backup_now": True,
        },
    )
    # B: yearly, so it never fires here, with two destinations, the second primary.
    await api.create_backup(
        source_uuid,
        {
            "frequency": "0 0 1 1 *",
            "enabled": True,
            "save_s3": True,
            "s3_storage_uuids": [s3b, s3a],
            "s3_storage_uuid": s3b,
            "database_backup_retention_amount_s3": 5,
            "missing_backup_notification_days": 3,
        },
    )
    await _await_success(api, source_uuid, str(a["uuid"]))
    source_before = sorted(_own(await api.get_backups(source_uuid)), key=lambda r: r["frequency"])
    assert len(source_before) == 2

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=seeded_pg["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )
    [resource] = plan.resources
    assert not resource.refusals, resource.refusals
    assert any("/data/coolify/backups/databases/" in w for w in resource.warnings), (
        f"the source's backup history was not reported: {resource.warnings}"
    )
    assert not any("local-only" in w.lower() for w in resource.warnings), (
        f"4.5 discloses the S3 destinations, so nothing may fall back: {resource.warnings}"
    )

    # Recorded, not asserted, inside the hook: it runs inside the saga.
    ran_before_start: list[Any] = []
    server_b = str(rig["server_b"]["uuid"])

    async def before_start(state: State) -> None:
        if state is not State.START_TARGET:
            return
        target = await _database_on(api, seeded_pg["db_name"], server_b)
        for row in _own(await api.get_backups(str(target))):
            ran_before_start.extend(row["executions"])

    result = await run_migration(api, settings, plan, on_state=before_start)
    assert result.outcome is Outcome.SUCCEEDED, f"migration did not succeed: {result}"
    assert not ran_before_start, f"the target dumped before START: {ran_before_start}"

    target_uuid = await _database_on(api, seeded_pg["db_name"], server_b)
    assert target_uuid, "no database with the original name landed on server-b"
    target_rows = sorted(_own(await api.get_backups(target_uuid)), key=lambda r: r["frequency"])

    assert len(target_rows) == 2, f"expected exactly 2 schedules, got {target_rows}"
    assert {r["uuid"] for r in target_rows}.isdisjoint({r["uuid"] for r in source_before})
    for src, tgt in zip(source_before, target_rows, strict=True):
        assert {k: tgt.get(k) for k in CARRIED} == {k: src.get(k) for k in CARRIED}, (
            f"schedule {src['frequency']!r} changed on the way"
        )
    yearly = target_rows[1]
    assert yearly["frequency"] == "0 0 1 1 *"
    assert yearly["s3_storage_uuid"] == s3b
    assert yearly["s3_storage_uuids"] == [s3b, s3a]

    source_after = sorted(_own(await api.get_backups(source_uuid)), key=lambda r: r["frequency"])
    assert [r["uuid"] for r in source_after] == [r["uuid"] for r in source_before]
    for before, after in zip(source_before, source_after, strict=True):
        assert {k: after.get(k) for k in CARRIED} == {k: before.get(k) for k in CARRIED}
