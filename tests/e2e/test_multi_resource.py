"""A project with more than one resource, migrated atomically.

Every other e2e test migrates a single resource. Real projects hold several — an
app, its database, a cache — and the plan is supposed to move the whole
environment as one unit. This proves the n>1 case: two databases in one project,
both seeded with distinct data, both expected on server-b afterwards.

If the saga migrated only the first resource, or paired one resource's volume to
another's target, the second fingerprint would fail. Distinct data per resource
is what makes that detectable.
"""

from __future__ import annotations

import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import run_migration
from bg_coolify_migrate.settings.base import Settings

from .conftest import db_exec, ssh_to, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

PG_FINGERPRINT = (
    "psql -U shop -d shopdb -tA -c "
    "\"SELECT count(*)||':'||sum(cents)||':'||md5(string_agg(customer,'|' ORDER BY id)) "
    'FROM orders"'
)
REDIS_FINGERPRINT = "redis-cli -a rootpw --no-raw MGET greeting amount"


@pytest_asyncio.fixture
async def two_resource_project(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A project holding a seeded Postgres and a seeded Redis on server-a."""
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"multi-{suffix}"
    pg_name, redis_name = f"pg-{suffix}", f"redis-{suffix}"

    project = await api.post("/projects", {"name": project_name, "description": "bgcm multi"})
    project_uuid = str(project["uuid"])

    try:
        pg = await api.post(
            "/databases/postgresql",
            {
                "name": pg_name,
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
        redis = await api.post(
            "/databases/redis",
            {
                "name": redis_name,
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "redis_password": "rootpw",
                "instant_deploy": True,
            },
        )
        pg_uuid, redis_uuid = str(pg["uuid"]), str(redis["uuid"])

        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            await wait_until_healthy(source, pg_uuid)
            await wait_until_healthy(source, redis_uuid)
            await db_exec(
                source,
                pg_uuid,
                "psql -U shop -d shopdb -tA -v ON_ERROR_STOP=1",
                stdin="CREATE TABLE orders(id serial primary key, customer text, cents int);"
                "INSERT INTO orders(customer,cents) VALUES ('Grüße GmbH',129900),('Ötztal AG',4200);",
            )
            await db_exec(source, redis_uuid, 'redis-cli -a rootpw MSET greeting "Käse" amount 4711')
            await db_exec(source, redis_uuid, "redis-cli -a rootpw SAVE")
            pg_fp = await db_exec(source, pg_uuid, PG_FINGERPRINT)
            redis_fp = await db_exec(source, redis_uuid, REDIS_FINGERPRINT)

        assert pg_fp.startswith("2:134100:"), f"pg seed failed: {pg_fp!r}"
        assert "amount" in redis_fp or "4711" in redis_fp, f"redis seed failed: {redis_fp!r}"
        yield {
            "project": project_name,
            "project_uuid": project_uuid,
            "pg_name": pg_name,
            "redis_name": redis_name,
            "pg_fp": pg_fp,
            "redis_fp": redis_fp,
        }
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            for db in await api.get("/databases") or []:
                if isinstance(db, dict) and suffix in str(db.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource(
                            "databases", str(db["uuid"]), delete_volumes=True
                        )
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def test_both_resources_migrate_atomically(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], two_resource_project: dict[str, Any]
) -> None:
    """One plan, two resources, both arrive on server-b with their own data."""
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=two_resource_project["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    # The plan must cover BOTH resources, or the migration silently moves one.
    assert len(plan.resources) == 2, (
        f"expected 2 resources in the plan, got {[r.snapshot.name for r in plan.resources]}"
    )

    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.SUCCEEDED, f"multi-resource migration failed: {result}"

    target = str(rig["server_b"]["uuid"])
    pg_target = await _target_db(api, two_resource_project["pg_name"], target)
    redis_target = await _target_db(api, two_resource_project["redis_name"], target)
    assert pg_target and redis_target, "not both resources landed on server-b"

    async with ssh_to(api, target, settings) as target_host:
        await wait_until_healthy(target_host, pg_target)
        await wait_until_healthy(target_host, redis_target)
        pg_arrived = await db_exec(target_host, pg_target, PG_FINGERPRINT)
        redis_arrived = await db_exec(target_host, redis_target, REDIS_FINGERPRINT)

    assert pg_arrived == two_resource_project["pg_fp"], (
        f"postgres data lost\n  source: {two_resource_project['pg_fp']}\n  target: {pg_arrived}"
    )
    assert redis_arrived == two_resource_project["redis_fp"], (
        f"redis data lost\n  source: {two_resource_project['redis_fp']}\n  target: {redis_arrived}"
    )


async def _target_db(api: CoolifyClient, name: str, target_server: str) -> str | None:
    for db in await api.get("/databases") or []:
        if not isinstance(db, dict) or db.get("name") != name:
            continue
        full = await api.get_resource("databases", str(db["uuid"]))
        if server_uuid_of(full) == target_server:
            return str(db["uuid"])
    return None
