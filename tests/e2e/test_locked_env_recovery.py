"""Locked (is_shown_once) variables, carried for real — still locked.

Coolify returns a locked value through no API read. The tool reads it back from
the file Coolify wrote on the source server and sets it on the target with
is_shown_once kept. A unit suite can prove the decoder; only a real instance can
prove the three things that matter here:

* the carried SERVICE_PASSWORD is the one the copied data was initialised with —
  the target's Postgres accepts it over TCP (scram), against the COPIED data;
* the carried value is the FILE's, not the container's: ``$HOME`` arrives
  literally, where the container environment would have expanded it;
* no secret reaches plan.json, the journal or the log output.

Plus Redis, whose locked REDIS_PASSWORD comes from the API's internal_db_url and
needs no file at all, and the two refusals: a file that does not exist, and a
file that no longer matches the resource (a variable changed without a restart).
"""

from __future__ import annotations

import asyncio
import base64
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
from bg_coolify_migrate.transfer.ssh import RemoteHost

from .conftest import db_exec, ssh_to, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

#: Every escaping hazard at once: quotes, a backslash, a ' #' tail and a $VAR
#: that compose would expand if this ever came from the container.
API_SECRET = 'p@ss "q" $HOME \\x #y'

#: Generated credentials, so the target's create generates DIFFERENT ones — the
#: case that used to be refused, because the copied data would not match.
COMPOSE = """\
services:
  store:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${SERVICE_USER_POSTGRES}
      POSTGRES_PASSWORD: ${SERVICE_PASSWORD_POSTGRES}
      POSTGRES_DB: shopdb
    volumes:
      - shopdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1"]
      interval: 3s
      timeout: 3s
      retries: 20
volumes:
  shopdata:
"""

#: Over TCP, so pg_hba's scram-sha-256 applies: the password must be the one the
#: COPIED data directory was initialised with. chr(58) is ':' without quoting.
TCP_FINGERPRINT = (
    'sh -c \'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" '
    '-d shopdb -tA -c "SELECT count(*)||chr(58)||sum(cents) FROM orders"\''
)
SEED = (
    "sh -c 'psql -U \"$POSTGRES_USER\" -d shopdb -tA -v ON_ERROR_STOP=1'",
    "CREATE TABLE orders(id serial primary key, customer text, cents int);"
    "INSERT INTO orders(customer,cents) VALUES ('Grüße GmbH',129900),('Ötztal AG',4200);",
)


def _env_file(uuid: str) -> str:
    return f"/data/coolify/services/{uuid}/.env"


async def _store_container(host: RemoteHost, service_uuid: str, *, timeout: float = 180) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await host.run(
            f"docker ps --format '{{{{.Names}}}}' | grep {service_uuid} | grep store"
        )
        if names := result.stdout.strip().splitlines():
            return names[0]
        await asyncio.sleep(4)
    raise AssertionError(f"no store container for service {service_uuid} within {timeout}s")


async def _env_line(host: RemoteHost, uuid: str, key: str) -> str | None:
    result = await host.run(f"grep -m1 '^{key}=' {_env_file(uuid)}")
    return result.stdout.rstrip("\n") if result.ok else None


async def _wait_for_line(host: RemoteHost, uuid: str, key: str, *, timeout: float = 120) -> str:
    """POST /restart only QUEUES RestartService; wait for the .env rewrite."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if line := await _env_line(host, uuid, key):
            return line
        await asyncio.sleep(3)
    raise AssertionError(f"{_env_file(uuid)} never gained {key}")


async def _envs_by_key(api: CoolifyClient, collection: str, uuid: str) -> dict[str, dict[str, Any]]:
    return {
        str(e["key"]): e
        for e in await api.get_envs(collection, uuid)
        if isinstance(e, dict) and not e.get("is_preview")
    }


async def _lock(api: CoolifyClient, collection: str, uuid: str, key: str, value: str) -> None:
    await api.patch(
        f"/{collection}/{uuid}/envs", {"key": key, "value": value, "is_shown_once": True}
    )


async def _cleanup(api: CoolifyClient, suffix: str, project_uuid: str) -> None:
    with contextlib.suppress(Exception):
        for collection in ("services", "databases"):
            for item in await api.get(f"/{collection}") or []:
                if isinstance(item, dict) and suffix in str(item.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource(
                            collection, str(item["uuid"]), delete_volumes=True
                        )
    with contextlib.suppress(Exception):
        await api.delete(f"/projects/{project_uuid}")


async def _target(api: CoolifyClient, collection: str, name: str, server: str) -> str | None:
    for item in await api.get(f"/{collection}") or []:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        full = await api.get_resource(collection, str(item["uuid"]))
        if server_uuid_of(full) == server:
            return str(item["uuid"])
    return None


@pytest_asyncio.fixture
async def locked_service(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A deployed, seeded compose Postgres whose password and API_SECRET are locked."""
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"locked-{suffix}"
    service_name = f"store-{suffix}"
    project = await api.post("/projects", {"name": project_name, "description": "bgcm locked"})
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
        uuid = str(created["uuid"])
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            container = await _store_container(source, uuid)
            await wait_until_healthy(source, container)
            await db_exec(source, container, SEED[0], stdin=SEED[1])
            fingerprint = await db_exec(source, container, TCP_FINGERPRINT)

            envs = await _envs_by_key(api, "services", uuid)
            password = str(envs["SERVICE_PASSWORD_POSTGRES"]["value"])
            await _lock(api, "services", uuid, "SERVICE_PASSWORD_POSTGRES", password)
            await api.post(
                f"/services/{uuid}/envs",
                {"key": "API_SECRET", "value": API_SECRET, "is_shown_once": True},
            )
            await api.restart("services", uuid)
            source_line = await _wait_for_line(source, uuid, "API_SECRET")
            container = await _store_container(source, uuid)
            await wait_until_healthy(source, container)

        assert fingerprint == "2:134100", f"seed did not take: {fingerprint!r}"
        yield {
            "project": project_name,
            "name": service_name,
            "uuid": uuid,
            "password": password,
            "fingerprint": fingerprint,
            "api_secret_line": source_line,
        }
    finally:
        await _cleanup(api, suffix, project_uuid)


async def test_locked_service_credentials_are_carried_still_locked(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    locked_service: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets = (locked_service["password"], API_SECRET)
    source_envs = await _envs_by_key(api, "services", locked_service["uuid"])
    for key in ("SERVICE_PASSWORD_POSTGRES", "API_SECRET"):
        assert "value" not in source_envs[key], f"{key} is not hidden — the test proves nothing"

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=locked_service["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )
    resource = plan.resources[0]
    assert not plan.is_blocked, resource.blocking_reasons
    assert {"API_SECRET", "SERVICE_PASSWORD_POSTGRES"} <= set(resource.recover_env_keys)
    assert any("will be read from" in w and "API_SECRET" in w for w in resource.warnings)
    dumped = plan.model_dump_json()
    assert not any(s in dumped for s in secrets)

    # accept_drift: floating minor tags are not what this test is about.
    result = await run_migration(api, settings, plan, accept_drift=True)
    assert result.outcome is Outcome.SUCCEEDED, f"migration did not succeed: {result}"

    target_uuid = await _target(
        api, "services", locked_service["name"], str(rig["server_b"]["uuid"])
    )
    assert target_uuid, "no service with the original name landed on server-b"
    target_envs = await _envs_by_key(api, "services", target_uuid)
    for key in ("SERVICE_PASSWORD_POSTGRES", "API_SECRET"):
        assert target_envs[key].get("is_shown_once") is True, f"{key} arrived unlocked"
        assert "value" not in target_envs[key]

    async with ssh_to(api, str(rig["server_b"]["uuid"]), settings) as target_host:
        container = await _store_container(target_host, target_uuid)
        await wait_until_healthy(target_host, container)
        # scram over TCP against the COPIED data: only the source's password works.
        arrived = await db_exec(target_host, container, TCP_FINGERPRINT)
        target_line = await _env_line(target_host, target_uuid, "API_SECRET")

    assert arrived == locked_service["fingerprint"]
    assert target_line == locked_service["api_secret_line"], "the .env line changed in transit"
    assert "$HOME" in (target_line or ""), "a container-sourced value would have expanded $HOME"

    state_dir = settings.resolved_state_dir()
    persisted = "".join(p.read_text(encoding="utf-8") for p in state_dir.iterdir() if p.is_file())
    captured = capsys.readouterr()
    for secret in secrets:
        assert secret not in persisted, "a locked value reached the state dir"
        assert secret not in captured.out + captured.err, "a locked value reached the output"


@pytest_asyncio.fixture
async def locked_redis(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"lredis-{suffix}"
    project = await api.post("/projects", {"name": project_name, "description": "bgcm locked"})
    project_uuid = str(project["uuid"])
    try:
        name = f"redis-{suffix}"
        created = await api.post(
            "/databases/redis",
            {
                "name": name,
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "instant_deploy": True,
                "redis_password": "rootpw",
            },
        )
        uuid = str(created["uuid"])
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            await wait_until_healthy(source, uuid)
            await db_exec(source, uuid, 'redis-cli -a rootpw SET greeting "Grüße GmbH"')
            await db_exec(source, uuid, "redis-cli -a rootpw SAVE")
            await _lock(api, "databases", uuid, "REDIS_PASSWORD", "rootpw")
            # The API URL must be enough on its own: take the file away.
            (await source.run(f"rm -f /data/coolify/databases/{uuid}/docker-compose.yml")).check()
        yield {"project": project_name, "name": name, "uuid": uuid}
    finally:
        await _cleanup(api, suffix, project_uuid)


async def test_a_locked_redis_password_comes_from_the_api(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], locked_redis: dict[str, Any]
) -> None:
    envs = await _envs_by_key(api, "databases", locked_redis["uuid"])
    assert "value" not in envs["REDIS_PASSWORD"], "REDIS_PASSWORD is not hidden"

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=locked_redis["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )
    assert not plan.is_blocked, plan.resources[0].blocking_reasons
    assert plan.resources[0].recover_env_keys == ("REDIS_PASSWORD",)
    assert "rootpw" not in plan.model_dump_json()

    # accept_drift: floating minor tags are not what this test is about.
    result = await run_migration(api, settings, plan, accept_drift=True)
    assert result.outcome is Outcome.SUCCEEDED, f"redis migration did not succeed: {result}"

    target_uuid = await _target(
        api, "databases", locked_redis["name"], str(rig["server_b"]["uuid"])
    )
    assert target_uuid, "no database landed on server-b"
    target_envs = await _envs_by_key(api, "databases", target_uuid)
    assert target_envs["REDIS_PASSWORD"].get("is_shown_once") is True

    async with ssh_to(api, str(rig["server_b"]["uuid"]), settings) as target_host:
        await wait_until_healthy(target_host, target_uuid)
        arrived = await db_exec(
            target_host, target_uuid, "redis-cli -a rootpw --no-raw GET greeting"
        )
    assert "Gr" in arrived, f"the source password does not open the copied data: {arrived!r}"


@pytest_asyncio.fixture
async def undeployed_service(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A service whose .env does not exist on the server, with a locked password."""
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"lnone-{suffix}"
    project = await api.post("/projects", {"name": project_name, "description": "bgcm locked"})
    project_uuid = str(project["uuid"])
    try:
        created = await api.post(
            "/services",
            {
                "name": f"store-{suffix}",
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "docker_compose_raw": base64.b64encode(COMPOSE.encode()).decode(),
                "instant_deploy": False,
            },
        )
        uuid = str(created["uuid"])
        envs = await _envs_by_key(api, "services", uuid)
        await _lock(
            api,
            "services",
            uuid,
            "SERVICE_PASSWORD_POSTGRES",
            str(envs["SERVICE_PASSWORD_POSTGRES"]["value"]),
        )
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            (await source.run(f"rm -f {_env_file(uuid)}")).check()
        yield {"project": project_name, "uuid": uuid}
    finally:
        await _cleanup(api, suffix, project_uuid)


async def test_no_file_refuses_a_regenerated_secret(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], undeployed_service: dict[str, Any]
) -> None:
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=undeployed_service["project"],
            environment="production",
            target_server="e2e-server-b",
        )
    reasons = "\n".join(plan.resources[0].refusals)
    assert "SERVICE_PASSWORD_POSTGRES" in reasons
    assert _env_file(undeployed_service["uuid"]) in reasons


async def test_a_stale_file_refuses_a_regenerated_secret(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], locked_service: dict[str, Any]
) -> None:
    """An unlocked variable added without a restart: the file no longer describes
    the resource, so nothing in it — the locked password included — is trusted."""
    uuid = locked_service["uuid"]
    await api.post(f"/services/{uuid}/envs", {"key": "MODE", "value": "two"})
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=locked_service["project"],
            environment="production",
            target_server="e2e-server-b",
        )
    reasons = "\n".join(plan.resources[0].refusals)
    assert "SERVICE_PASSWORD_POSTGRES" in reasons
    assert "does not match the resource's current configuration" in reasons
