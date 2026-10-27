"""Rollback against a real instance: does the compensation actually undo?

The chaos unit tests prove the saga's rollback LOGIC converges. They cannot prove
that DELETE_TARGET_RESOURCE really removes a resource from Coolify, or that
RESTART_SOURCE really brings the source back — those are API calls to a real
instance, and an API call that looks right in a mock is exactly what this whole
week has been about.

So: deploy and seed a Postgres, inject a fault AFTER the volumes are copied
(patch VERIFY to raise), and assert the world was put back:

  * the migration reports ROLLED_BACK, not SUCCEEDED
  * the target resource is gone from server-b — the copy did not leave a
    half-built database behind
  * the source is still on server-a, running, with its data intact — the backbone
    of the whole safety story is that the source survives until FINALIZE

The fault lands at VERIFY on purpose: late enough that the target exists and its
volumes were copied (so DELETE_TARGET_RESOURCE has real work to do), early enough
that FINALIZE never ran (so the source was never renamed or deleted).
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import run_migration
from bg_coolify_migrate.errors import VerificationError
from bg_coolify_migrate.settings.base import Settings

from .conftest import PG_FINGERPRINT as FINGERPRINT
from .conftest import db_exec, ssh_to, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

#: A schedule on the source: the rollback deletes the target's copy with the
#: target, and must never touch the source's own row.
BACKUP = {"frequency": "0 4 * * *", "enabled": True, "save_s3": False, "timeout": 900}


async def test_rollback_restores_the_world(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    seeded_pg: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fault after COPY must delete the target and leave the source running."""

    async def _boom(_ctx: object) -> dict[str, Any]:
        raise VerificationError("injected fault: pretend verification found a mismatch")

    # build_steps() resolves step_verify from the module namespace at call time,
    # so patching it here reaches the saga run_migration is about to build.
    monkeypatch.setattr(steps, "step_verify", _boom)

    backup_uuid = await api.create_backup(seeded_pg["db_uuid"], BACKUP)
    before = _own_backup(await api.get_backups(seeded_pg["db_uuid"]), backup_uuid)

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

    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.ROLLED_BACK, f"expected rollback, got {result}"

    # 1. The target was really deleted — no half-built database on server-b.
    #    Coolify's delete is async (it dispatches a deletion job), so poll rather
    #    than read once the instant run_migration returns.
    import asyncio

    for _ in range(24):
        target = await _resource_on(api, seeded_pg["db_name"], str(rig["server_b"]["uuid"]))
        if target is None:
            break
        await asyncio.sleep(5)
    assert target is None, "rollback left a target resource on server-b"

    # 2. The source is still there, under its ORIGINAL name — never renamed,
    #    because FINALIZE never ran.
    source = await _resource_on(api, seeded_pg["db_name"], str(rig["server_a"]["uuid"]))
    assert source is not None, "the source resource vanished — rollback destroyed it"

    # 3. And it is running with its data intact. The API confirming a row exists
    #    is not enough; ask the daemon.
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        await wait_until_healthy(source_host, seeded_pg["db_uuid"])
        arrived = await db_exec(source_host, seeded_pg["db_uuid"], FINGERPRINT)
    assert arrived == seeded_pg["fingerprint"], (
        f"source data changed across the rollback\n"
        f"  before: {seeded_pg['fingerprint']}\n  after:  {arrived}"
    )

    # 4. The source's backup schedule is exactly as it was: same row, same values.
    #    The target's copy went with the target (DeleteResourceJob deletes the rows).
    after = _own_backup(await api.get_backups(seeded_pg["db_uuid"]), backup_uuid)
    assert after == before, f"the rollback touched the source's schedule\n  {before}\n  {after}"


def _own_backup(rows: list[dict[str, Any]], backup_uuid: str) -> dict[str, Any]:
    """One schedule's configuration, without what a run changes (history, timestamps)."""
    row = next((r for r in rows if r.get("uuid") == backup_uuid), None)
    assert row is not None, f"backup schedule {backup_uuid} is gone"
    volatile = {"executions", "updated_at", "last_execution_at"}
    return {k: v for k, v in row.items() if k not in volatile}


async def _resource_on(api: CoolifyClient, name: str, server_uuid: str) -> str | None:
    for db in await api.get("/databases") or []:
        if not isinstance(db, dict) or db.get("name") != name:
            continue
        full = await api.get_resource("databases", str(db["uuid"]))
        if server_uuid_of(full) == server_uuid:
            return str(db["uuid"])
    return None
