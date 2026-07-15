"""The test this whole rig exists for: move a real database's data, for real.

A Postgres with known rows is deployed on server-a. The tool migrates it to
server-b. Then we ask server-b's database for the rows back.

Because server-a and server-b run separate Docker daemons, there is no way to
pass this by accident. If the copy did nothing, server-b's volume is empty and
Postgres either refuses to start or starts empty; either way the fingerprint
does not match. That is the property the whole rig is built to buy.

The fingerprint is count + sum + md5 over the customer names, and those names
carry umlauts and an eszett on purpose: it catches a byte-level encoding mangle
that a row count never would.
"""

from __future__ import annotations

import contextlib
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

from .conftest import (
    DB_NAME,
    DB_PASSWORD,
    DB_USER,
    POSTGRES_IMAGE,
    psql,
    ssh_to,
    wait_for_container,
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

#: Rows whose text would not survive a botched encoding round-trip.
SEED_ROWS = [("Grüße GmbH", 129900), ("Ötztal AG", 4200), ("Straße & Co", 77)]

#: count:sum(cents):md5(names) — pinned so a silently-empty seed cannot pass.
EXPECTED_PREFIX = "3:134177:"

FINGERPRINT_SQL = (
    "SELECT count(*)||':'||sum(cents)||':'||md5(string_agg(customer,'|' ORDER BY id)) FROM orders"
)


async def _deploy_and_seed(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    *,
    project_uuid: str,
    db_name: str,
) -> dict[str, Any]:
    """Deploy a Postgres on server-a and put known rows in it."""
    created = await api.post(
        "/databases/postgresql",
        {
            "name": db_name,
            "project_uuid": project_uuid,
            "environment_name": "production",
            "server_uuid": rig["server_a"]["uuid"],
            # Pinning the tag is not tidiness: the model hook picks the volume's
            # mount path from the major version (18 moved it), so an unpinned tag
            # could silently relocate the very data we are about to copy.
            "image": POSTGRES_IMAGE,
            "postgres_user": DB_USER,
            "postgres_password": DB_PASSWORD,
            "postgres_db": DB_NAME,
            "instant_deploy": True,
        },
    )
    db_uuid = str(created["uuid"])

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
        await wait_for_container(source, db_uuid)
        values = ",".join(f"('{customer}',{cents})" for customer, cents in SEED_ROWS)
        await psql(
            source,
            db_uuid,
            "CREATE TABLE orders(id serial primary key, customer text, cents int);"
            f"INSERT INTO orders(customer,cents) VALUES {values};",
        )
        fingerprint = await psql(source, db_uuid, FINGERPRINT_SQL)

    assert fingerprint.startswith(EXPECTED_PREFIX), f"seed did not take: {fingerprint!r}"
    return {"db_uuid": db_uuid, "db_name": db_name, "fingerprint": fingerprint}


@pytest_asyncio.fixture
async def stack(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A deployed, seeded Postgres on server-a with a known fingerprint.

    Named uniquely per run: a leftover from a crashed run must not be able to
    make this one pass against stale data — precisely the failure mode this
    suite exists to catch.
    """
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"e2e-{suffix}"

    project = await api.post("/projects", {"name": project_name, "description": "bgcm e2e"})
    project_uuid = str(project["uuid"])

    try:
        seeded = await _deploy_and_seed(
            api, settings, rig, project_uuid=project_uuid, db_name=f"pg-{suffix}"
        )
        yield {"project": project_name, "project_uuid": project_uuid, **seeded}
    finally:
        # try/finally around the whole body rather than just after the yield:
        # pytest runs no teardown for a fixture that raises before yielding, so
        # a setup failure would otherwise strand the project on the instance.
        await _teardown(api, project_uuid, suffix)


async def _teardown(api: CoolifyClient, project_uuid: str, suffix: str) -> None:
    """Remove the run's databases and project.

    Deleting the project alone leaves every container running on the dind
    servers — observed, not assumed. Unique per-run names mean orphans cannot
    make a later run pass, but they do fill the disk and muddy any manual look at
    the rig, so delete the resources explicitly, as a user would.

    Every step is best-effort: cleanup must never eclipse the assertion that
    just fired.
    """
    with contextlib.suppress(Exception):
        for db in await api.get("/databases") or []:
            if isinstance(db, dict) and suffix in str(db.get("name", "")):
                with contextlib.suppress(Exception):
                    await api.delete_resource("databases", str(db["uuid"]), delete_volumes=True)
    with contextlib.suppress(Exception):
        await api.delete(f"/projects/{project_uuid}")


async def _plan_to_b(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], project: str
) -> Any:
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
    """UUID of the database called `name` living on `server_uuid`, if any."""
    for db in await api.get("/databases") or []:
        if not isinstance(db, dict) or db.get("name") != name:
            continue
        full = await api.get_resource("databases", str(db["uuid"]))
        if server_uuid_of(full) == server_uuid:
            return str(db["uuid"])
    return None


async def test_migrates_postgres_data_between_servers(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], stack: dict[str, Any]
) -> None:
    """server-a -> server-b, and the rows arrive."""
    plan = await _plan_to_b(api, settings, rig, stack["project"])

    # A plan with no volumes would migrate happily and move nothing, so assert
    # the planner paired the volume before trusting anything downstream.
    found = [item.source_name for resource in plan.resources for item in resource.manifest.items]
    assert found, "planner found no volumes to move"
    assert f"postgres-data-{stack['db_uuid']}" in found, (
        f"expected Coolify's volume name; got {found}"
    )

    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.SUCCEEDED, f"migration did not succeed: {result}"

    # ── the actual question ──────────────────────────────────────────────────
    target_uuid = await _database_on(api, stack["db_name"], str(rig["server_b"]["uuid"]))
    assert target_uuid, "no database with the original name landed on server-b"

    async with ssh_to(api, str(rig["server_b"]["uuid"]), settings) as target_host:
        await wait_for_container(target_host, target_uuid)
        arrived = await psql(target_host, target_uuid, FINGERPRINT_SQL)

    assert arrived == stack["fingerprint"], (
        f"data did not survive the move\n  source: {stack['fingerprint']}\n  target: {arrived}"
    )


async def test_source_survives_the_migration(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], stack: dict[str, Any]
) -> None:
    """RENAME keeps the source intact — the backbone of the rollback story.

    Asserted against the daemon, not the API: the API would only confirm a row
    still exists, and a row is not data.
    """
    plan = await _plan_to_b(api, settings, rig, stack["project"])
    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.SUCCEEDED

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        volume = f"postgres-data-{stack['db_uuid']}"
        still_there = await source_host.run(f"docker volume inspect {volume}")
        assert still_there.ok, f"{volume} was destroyed on the source; rollback would be impossible"

    names = [
        str(db["name"])
        for db in (await api.get("/databases") or [])
        if isinstance(db, dict) and str(db.get("name", "")).startswith(stack["db_name"])
    ]
    assert any(name != stack["db_name"] for name in names), (
        f"expected a renamed source alongside the target; saw {names}"
    )
