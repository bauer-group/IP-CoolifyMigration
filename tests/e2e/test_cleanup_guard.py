"""Coolify's own Docker cleanup, fired mid-migration, must delete nothing of ours.

With "Delete unused volumes" on, Coolify's cleanup runs `docker volume prune
-af` (v4.5.0:app/Actions/Server/CleanupDocker.php:166-168), which deletes every
volume no container references. A migration makes exactly such volumes: the
stopped source's (Coolify's stop removes its containers) and the filled
target's (nothing mounts them before START). The pins in discovery/pins.py are
never-started containers that reference them; these tests run Coolify's REAL
cleanup — its own job, its own container and image prune — at the two worst
moments and read the data back from the daemons.

Without the pins both fail: the prune deletes the source and target volumes,
so the migration starts an empty Postgres, or the rollback restarts one.

Each cleanup is preceded by a CANARY: an unreferenced volume we create and
expect gone afterwards. "Execution status: success" alone does not prove that
`docker volume prune -af` was among the commands, and a cleanup that skipped it
would let both tests pass without testing anything.

NOT parallel-safe: the cleanup prunes every unused volume on both rig daemons.
Run this module on its own, or serially with the rest of the suite.
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
from bg_coolify_migrate.discovery import pins
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome, State
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import make_migration_id, run_migration
from bg_coolify_migrate.errors import VerificationError
from bg_coolify_migrate.settings.base import Settings

from .conftest import db_exec, ssh_to, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

FINGERPRINT = (
    "psql -U shop -d shopdb -tA -c "
    "\"SELECT count(*)||':'||sum(cents)||':'||md5(string_agg(customer,'|' ORDER BY id)) "
    'FROM orders"'
)

#: A manual DockerCleanupJob that finds the per-server lock held (QUIESCE's own
#: stop queues a dispatchAfterStop run that takes it) releases itself and
#: retries until overlapLockExpiresAfter() + 2*60 s (v4.5.0:DockerCleanupJob.php:76).
CLEANUP_TIMEOUT = 900.0


@pytest_asyncio.fixture
async def seeded_pg(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    suffix = uuidlib.uuid4().hex[:8]
    project = await api.post("/projects", {"name": f"cg-{suffix}", "description": "bgcm cleanup"})
    project_uuid = str(project["uuid"])
    db_name = f"pg-{suffix}"
    try:
        db = await api.post(
            "/databases/postgresql",
            {
                "name": db_name,
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "image": "postgres:16-alpine",
                "postgres_user": "shop",
                "postgres_password": "secret",
                "postgres_db": "shopdb",
                "instant_deploy": True,
            },
        )
        db_uuid = str(db["uuid"])
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            await wait_until_healthy(source, db_uuid)
            await db_exec(
                source,
                db_uuid,
                "psql -U shop -d shopdb -tA -v ON_ERROR_STOP=1",
                stdin="CREATE TABLE orders(id serial primary key, customer text, cents int);"
                "INSERT INTO orders(customer,cents) VALUES ('Grüße GmbH',129900),('Ötztal AG',4200);",
            )
            fingerprint = await db_exec(source, db_uuid, FINGERPRINT)
        assert fingerprint.startswith("2:134100:")
        yield {
            "project": f"cg-{suffix}",
            "db_name": db_name,
            "db_uuid": db_uuid,
            "fingerprint": fingerprint,
        }
    finally:
        with contextlib.suppress(Exception):
            for row in await api.get("/databases") or []:
                if isinstance(row, dict) and suffix in str(row.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource(
                            "databases", str(row["uuid"]), delete_volumes=True
                        )
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def _coolify_cleanup(
    api: CoolifyClient, settings: Settings, server_uuid: str, *, canary: str
) -> None:
    """Run Coolify's own manual cleanup with the volume prune forced on, and prove it ran.

    POST /servers/{uuid}/docker-cleanup/run takes delete_unused_volumes as a
    per-request override (4.3.0+; v4.5.0:ServerDockerCleanupController.php:280-287)
    and dispatches the same DockerCleanupJob the scheduler does.
    """
    async with ssh_to(api, server_uuid, settings) as host:
        (await host.run(f"docker volume create {canary}")).check()

    before = {
        str(e.get("uuid"))
        for e in await api.get(f"/servers/{server_uuid}/docker-cleanup/executions") or []
        if isinstance(e, dict)
    }
    await api.post(f"/servers/{server_uuid}/docker-cleanup/run", {"delete_unused_volumes": True})

    deadline = asyncio.get_running_loop().time() + CLEANUP_TIMEOUT
    finished: dict[str, Any] | None = None
    while finished is None:
        assert asyncio.get_running_loop().time() < deadline, "Coolify's cleanup never finished"
        await asyncio.sleep(5)
        for execution in await api.get(f"/servers/{server_uuid}/docker-cleanup/executions") or []:
            if (
                isinstance(execution, dict)
                and str(execution.get("uuid")) not in before
                and execution.get("finished_at")
            ):
                finished = execution
    assert finished.get("status") == "success", f"cleanup failed: {finished}"

    async with ssh_to(api, server_uuid, settings) as host:
        gone = not (await host.run(f"docker volume inspect {canary}")).ok
    assert gone, "the canary volume survived: Coolify's cleanup never ran its volume prune"


async def _pins_on(
    api: CoolifyClient, settings: Settings, server_uuid: str, migration_id: str
) -> list[str]:
    async with ssh_to(api, server_uuid, settings) as host:
        result = await host.run(
            f"docker ps -a --filter label={pins.LABEL_MIGRATION}={migration_id} "
            "--format '{{.Names}} {{.State}}'"
        )
    result.check()
    return [line for line in result.stdout.splitlines() if line.strip()]


async def _plan(api: CoolifyClient, settings: Settings, rig: dict[str, Any], project: str) -> Any:
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        return await build_plan(
            api,
            source_host,
            project=project,
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )


async def _database_on(api: CoolifyClient, name: str, server_uuid: str) -> str | None:
    for row in await api.get("/databases") or []:
        if not isinstance(row, dict) or row.get("name") != name:
            continue
        if server_uuid_of(await api.get_resource("databases", str(row["uuid"]))) == server_uuid:
            return str(row["uuid"])
    return None


async def test_coolify_cleanup_between_copy_and_start_deletes_nothing(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], seeded_pg: dict[str, Any]
) -> None:
    """The widest window: source stopped, target filled, nothing mounts either."""
    a, b = str(rig["server_a"]["uuid"]), str(rig["server_b"]["uuid"])

    # The plan reads the setting off GET /servers/{uuid} - proven live here,
    # because a field we only believe is serialised is the 2.5.6 tags outage.
    original = await api.get(f"/servers/{a}/docker-cleanup")
    await api.patch(f"/servers/{a}/docker-cleanup", {"delete_unused_volumes": True})
    try:
        plan = await _plan(api, settings, rig, seeded_pg["project"])
    finally:
        await api.patch(
            f"/servers/{a}/docker-cleanup",
            {"delete_unused_volumes": bool(original.get("delete_unused_volumes"))},
        )
    assert plan.source_server.prunes_unused_volumes
    assert any(
        "Delete unused volumes" in w and plan.source_server.name in w for w in plan.warnings
    ), plan.warnings

    mid = make_migration_id(plan.project, plan.environment)
    seen: dict[str, list[str]] = {}

    async def at_verify(state: State) -> None:
        # on_state fires BEFORE a step: COPY is done, VERIFY has not started.
        if state is not State.VERIFY:
            return
        seen["a"] = await _pins_on(api, settings, a, mid)
        seen["b"] = await _pins_on(api, settings, b, mid)
        await _coolify_cleanup(api, settings, a, canary=f"bgcm-canary-{mid}-a")
        await _coolify_cleanup(api, settings, b, canary=f"bgcm-canary-{mid}-b")

    result = await run_migration(api, settings, plan, migration_id=mid, on_state=at_verify)

    assert seen["a"] and all(line.endswith(" created") for line in seen["a"]), seen
    assert seen["b"] and all(line.endswith(" created") for line in seen["b"]), seen
    assert result.outcome is Outcome.SUCCEEDED, f"migration did not succeed: {result}"

    target_uuid = await _database_on(api, seeded_pg["db_name"], b)
    assert target_uuid, "no database landed on server-b"
    async with ssh_to(api, b, settings) as target_host:
        await wait_until_healthy(target_host, target_uuid)
        arrived = await db_exec(target_host, target_uuid, FINGERPRINT)
    assert arrived == seeded_pg["fingerprint"], (
        f"the cleanup ate the data\n  source: {seeded_pg['fingerprint']}\n  target: {arrived}"
    )

    # FINALIZE handed both sides back to Coolify's steady state.
    assert await _pins_on(api, settings, a, mid) == []
    assert await _pins_on(api, settings, b, mid) == []


async def test_rollback_after_cleanup_restores_the_source(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    seeded_pg: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup while the source is down, then a rollback onto its volume."""
    a, b = str(rig["server_a"]["uuid"]), str(rig["server_b"]["uuid"])
    plan = await _plan(api, settings, rig, seeded_pg["project"])
    mid = make_migration_id(plan.project, plan.environment)
    target_volumes: list[str] = []

    async def cleanup_then_fail(ctx: Any) -> dict[str, Any]:
        target_volumes.extend(p.target.name for pairs in ctx.volume_pairs.values() for p in pairs)
        await _coolify_cleanup(api, settings, a, canary=f"bgcm-canary-{mid}-a")
        await _coolify_cleanup(api, settings, b, canary=f"bgcm-canary-{mid}-b")
        raise VerificationError("injected fault after Coolify's cleanup")

    # build_steps() resolves step_verify from the module at call time.
    monkeypatch.setattr(steps, "step_verify", cleanup_then_fail)

    result = await run_migration(api, settings, plan, migration_id=mid)
    assert result.outcome is Outcome.ROLLED_BACK, f"expected a rollback, got {result}"
    assert result.notices == [], f"a pin was left behind: {result.notices}"

    async with ssh_to(api, a, settings) as source_host:
        await wait_until_healthy(source_host, seeded_pg["db_uuid"])
        intact = await db_exec(source_host, seeded_pg["db_uuid"], FINGERPRINT)
    assert intact == seeded_pg["fingerprint"], "the rollback restarted a pruned source"

    assert await _pins_on(api, settings, a, mid) == []
    assert await _pins_on(api, settings, b, mid) == []
    assert target_volumes, "the plan paired no volume"
    async with ssh_to(api, b, settings) as target_host:
        for _ in range(24):  # Coolify's delete is a queued job
            listed = (await target_host.run("docker volume ls --format '{{.Name}}'")).stdout
            if not set(target_volumes) & set(listed.split()):
                break
            await asyncio.sleep(5)
    assert not set(target_volumes) & set(listed.split()), "the rollback left target volumes"
