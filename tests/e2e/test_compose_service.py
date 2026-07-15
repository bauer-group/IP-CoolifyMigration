"""A docker-compose stack, migrated for real — the 90% case.

Databases are the simple shape: one resource, one (or two) engine-managed
volumes. The estate is mostly compose stacks, and those bring the paths the
database tests never touch:

* Service volumes are ``{service_uuid}_{slug}`` — an UNDERSCORE, where
  application volumes use a hyphen. Confusing the two is coolify-mover's silent
  data-loss bug.
* A compose service can declare anonymous volumes, which appear in no API and
  only in ``docker inspect`` — and only while the container exists, which after
  Coolify's stop it does not. This is exactly what the pre-stop mount capture
  exists to rescue.

The stack here is a single Postgres service with a named data volume. We seed it,
migrate it, and read the rows back from server-b's daemon — the same ground truth
as the engine suite, but through the /services code path rather than /databases.
"""

from __future__ import annotations

import base64
import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine.planner import build_plan
from bg_coolify_migrate.settings.base import Settings

from .conftest import db_exec, ssh_to, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

SEED = [("Grüße GmbH", 129900), ("Ötztal AG", 4200)]

#: A single Postgres service with a named volume. The password is fixed so the
#: migrated copy — which Coolify recreates from the same compose — comes up with
#: credentials that match the copied data directory.
COMPOSE = """\
services:
  store:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: shop
      POSTGRES_PASSWORD: secret
      POSTGRES_DB: shopdb
    volumes:
      - shopdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U shop -d shopdb"]
      interval: 3s
      timeout: 3s
      retries: 20
volumes:
  shopdata:
"""

FINGERPRINT = (
    "psql -U shop -d shopdb -tA -c "
    "\"SELECT count(*)||':'||sum(cents)||':'||md5(string_agg(customer,'|' ORDER BY id)) "
    'FROM orders"'
)


async def _store_container(host: Any, service_uuid: str, *, timeout: float = 180) -> str:
    """Wait for the compose `store` container and return its name.

    Coolify names it `store-{service_uuid}` — the compose service key, then the
    service uuid (confirmed against a real deploy, not guessed). The wait is the
    point: a service deploy is async, so the container does not exist the instant
    the POST returns. The first cut looked once, immediately, and found nothing.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await host.run(
            f"docker ps --format '{{{{.Names}}}}' | grep {service_uuid} | grep store"
        )
        names = result.stdout.strip().splitlines()
        if names:
            return names[0]
        await asyncio.sleep(4)
    raise AssertionError(f"no store container for service {service_uuid} within {timeout}s")


@pytest_asyncio.fixture
async def compose_stack(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A deployed, seeded Postgres-in-compose service with a known fingerprint."""
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"compose-{suffix}"
    service_name = f"store-{suffix}"

    project = await api.post("/projects", {"name": project_name, "description": "bgcm compose"})
    project_uuid = str(project["uuid"])

    try:
        created = await api.post(
            "/services",
            {
                "name": service_name,
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "docker_compose_raw": base64.b64encode(COMPOSE.encode()).decode(),
                "instant_deploy": True,
            },
        )
        service_uuid = str(created["uuid"])

        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            container = await _store_container(source, service_uuid)
            await wait_until_healthy(source, container)
            values = ",".join(f"('{c}',{n})" for c, n in SEED)
            await db_exec(
                source,
                container,
                "psql -U shop -d shopdb -tA -v ON_ERROR_STOP=1",
                stdin="CREATE TABLE orders(id serial primary key, customer text, cents int);"
                f"INSERT INTO orders(customer,cents) VALUES {values};",
            )
            fingerprint = await db_exec(source, container, FINGERPRINT)

        assert fingerprint.startswith("2:134100:"), f"seed did not take: {fingerprint!r}"
        yield {
            "project": project_name,
            "project_uuid": project_uuid,
            "service_name": service_name,
            "service_uuid": service_uuid,
            "fingerprint": fingerprint,
        }
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            for svc in await api.get("/services") or []:
                if isinstance(svc, dict) and suffix in str(svc.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource("services", str(svc["uuid"]), delete_volumes=True)
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def test_compose_service_data_survives_migration(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], compose_stack: dict[str, Any]
) -> None:
    """server-a -> server-b through the /services path, and the rows arrive."""
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=compose_stack["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    volumes = [item for r in plan.resources for item in r.manifest.items]
    assert volumes, "planner found no volumes in the compose service"

    from bg_coolify_migrate.engine.runner import run_migration

    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.SUCCEEDED, f"compose migration did not succeed: {result}"

    target_uuid = await _target_service(api, compose_stack["service_name"], str(rig["server_b"]["uuid"]))
    assert target_uuid, "no service with the original name landed on server-b"

    async with ssh_to(api, str(rig["server_b"]["uuid"]), settings) as target_host:
        container = await _store_container(target_host, target_uuid)
        await wait_until_healthy(target_host, container)
        arrived = await db_exec(target_host, container, FINGERPRINT)

    assert arrived == compose_stack["fingerprint"], (
        f"compose data did not survive the move\n"
        f"  source: {compose_stack['fingerprint']}\n  target: {arrived}"
    )


async def _target_service(api: CoolifyClient, name: str, target_server: str) -> str | None:
    from bg_coolify_migrate.engine.planner import server_uuid_of

    for svc in await api.get("/services") or []:
        if not isinstance(svc, dict) or svc.get("name") != name:
            continue
        full = await api.get_resource("services", str(svc["uuid"]))
        if server_uuid_of(full) == target_server:
            return str(svc["uuid"])
    return None
