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

The stack here is a Postgres service with a named data volume, plus a stateless
sidecar. We seed it, migrate it, and read the rows back from server-b's daemon —
the same ground truth as the engine suite, but through the /services code path
rather than /databases.

The same run carries the service's scheduled tasks (one deploy-and-migrate is
minutes; a second for tasks alone would double the suite). The sidecar is there
for them: with ONE running container ScheduledTaskJob ignores a task's
``container`` entirely, so only a second container makes the heartbeat below
prove that ``store`` resolves to ``store-{target uuid}`` on server-b.
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
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome, State
from bg_coolify_migrate.engine.planner import build_plan
from bg_coolify_migrate.settings.base import Settings

from .conftest import db_exec, db_exec_when_ready, ssh_to, wait_until_healthy

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
  sidecar:
    image: busybox:1.36
    command: ["sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"]
volumes:
  shopdata:
"""

#: Seeded on the source after the deploy. `paused` is the regression that
#: matters most — upstream defaults an absent `enabled` to TRUE — and
#: nightly-vacuum's 600 a non-default timeout that has to survive.
TASKS: list[dict[str, Any]] = [
    {
        "name": "nightly-vacuum",
        "command": "psql -U shop -d shopdb -c 'VACUUM'",
        "frequency": "0 3 * * *",
        "container": "store",
        "timeout": 600,
        "enabled": True,
    },
    {
        "name": "heartbeat",
        "command": "date",
        "frequency": "* * * * *",
        "container": "store",
        "enabled": True,
    },
    {
        "name": "paused",
        "command": "true",
        "frequency": "daily",
        "container": "store",
        "enabled": False,
    },
]


def _task_projection(tasks: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """What must be equal on both sides. A sorted LIST, not a set: a set would hide
    a task doubled by a retried create — exactly the regression to catch."""
    return sorted(
        (
            t.get("name"),
            t.get("command"),
            t.get("frequency"),
            t.get("container"),
            t.get("timeout"),
            t.get("enabled"),
        )
        for t in tasks
    )


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
            await db_exec_when_ready(
                source,
                container,
                "psql -U shop -d shopdb -tA -v ON_ERROR_STOP=1",
                stdin="CREATE TABLE orders(id serial primary key, customer text, cents int);"
                f"INSERT INTO orders(customer,cents) VALUES {values};",
            )
            fingerprint = await db_exec(source, container, FINGERPRINT)

        assert fingerprint.startswith("2:134100:"), f"seed did not take: {fingerprint!r}"

        for task in TASKS:
            await api.create_scheduled_task("services", service_uuid, task)
        tasks = _task_projection(await api.get_scheduled_tasks("services", service_uuid))
        assert len(tasks) == len(TASKS), f"seeding the tasks did not take: {tasks}"

        yield {
            "project": project_name,
            "project_uuid": project_uuid,
            "service_name": service_name,
            "service_uuid": service_uuid,
            "fingerprint": fingerprint,
            "tasks": tasks,
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

    # Recorded, not asserted, inside the hook: it runs inside the saga, and an
    # assertion there would abort the run instead of failing the test.
    ran_before_start: dict[str, Any] = {}

    async def before_start(state: State) -> None:
        if state is not State.START_TARGET:
            return
        target = await _target_service(api, compose_stack["service_name"], server_b)
        for task in await api.get_scheduled_tasks("services", str(target)):
            executions = await api.get(
                f"/services/{target}/scheduled-tasks/{task['uuid']}/executions"
            )
            if executions:
                ran_before_start[str(task["name"])] = executions

    server_b = str(rig["server_b"]["uuid"])
    result = await run_migration(api, settings, plan, on_state=before_start)
    assert result.outcome is Outcome.SUCCEEDED, f"compose migration did not succeed: {result}"

    target_uuid = await _target_service(api, compose_stack["service_name"], server_b)
    assert target_uuid, "no service with the original name landed on server-b"

    async with ssh_to(api, server_b, settings) as target_host:
        container = await _store_container(target_host, target_uuid)
        await wait_until_healthy(target_host, container)
        arrived = await db_exec(target_host, container, FINGERPRINT)

    assert arrived == compose_stack["fingerprint"], (
        f"compose data did not survive the move\n"
        f"  source: {compose_stack['fingerprint']}\n  target: {arrived}"
    )

    # ── scheduled tasks ──────────────────────────────────────────────────────
    # Nothing may run on a target that was never started. A skipped occurrence
    # writes no execution row, so this can pass vacuously but never falsely.
    assert not ran_before_start, f"tasks ran on the target before START: {ran_before_start}"

    target_tasks = await api.get_scheduled_tasks("services", target_uuid)
    assert _task_projection(target_tasks) == compose_stack["tasks"], (
        "the target's scheduled tasks differ from the source's\n"
        f"  source: {compose_stack['tasks']}\n"
        f"  target: {_task_projection(target_tasks)}"
    )
    source_tasks = await api.get_scheduled_tasks("services", compose_stack["service_uuid"])
    assert _task_projection(source_tasks) == compose_stack["tasks"], "the source's tasks changed"

    # The heartbeat fires every minute. With `store` AND `sidecar` running, its
    # first success proves `container: store` resolves to store-{target uuid}.
    # Bounded: it waits on the rig's scheduler and on the status column turning
    # 'running', neither of which this tool controls.
    heartbeat = next(t for t in target_tasks if t["name"] == "heartbeat")
    statuses = await _await_task_run(api, target_uuid, str(heartbeat["uuid"]), timeout=240)
    assert "success" in statuses, (
        f"the carried heartbeat never succeeded on server-b (executions: {statuses}). "
        "'No valid container' means its container did not resolve; no executions at all "
        "means the rig's scheduler or status checks are not running."
    )


async def _await_task_run(
    api: CoolifyClient, service_uuid: str, task_uuid: str, *, timeout: float
) -> list[str]:
    """Poll a task's executions until one succeeds; return every status seen."""
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    statuses: list[str] = []
    while asyncio.get_running_loop().time() < deadline:
        executions = await api.get(
            f"/services/{service_uuid}/scheduled-tasks/{task_uuid}/executions"
        )
        statuses = [str(e.get("status")) for e in executions or [] if isinstance(e, dict)]
        if "success" in statuses:
            break
        await asyncio.sleep(10)
    return statuses


async def _target_service(api: CoolifyClient, name: str, target_server: str) -> str | None:
    from bg_coolify_migrate.engine.planner import server_uuid_of

    for svc in await api.get("/services") or []:
        if not isinstance(svc, dict) or svc.get("name") != name:
            continue
        full = await api.get_resource("services", str(svc["uuid"]))
        if server_uuid_of(full) == target_server:
            return str(svc["uuid"])
    return None
