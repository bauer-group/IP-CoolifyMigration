"""A one-click template service, migrated for real — on the SOURCE's own compose.

A template target is created from ``type`` (which keeps ``service_type``) and
then given the source's compose by one PATCH (resources.carry_source_compose).
Until then it ran the instance's CURRENT catalog, which the CDN refreshes for
every release: the v4.7.0 sync moved getoutline from postgres 12 to 16 on the
same volume key and mount, so the copy verified and the target's database
refused its data after the stop.

The catalog of a rig is whatever the CDN served it, so the drift cannot be
staged here. What can: a source whose compose differs from its template. The
source below is the ``gotify`` template (one container, a pinned ~30 MB image,
one named data volume, no database) with one environment line added after the
create and one taken out, its row deleted. The target must hold that edited
compose exactly — not the template's — run it, keep ``service_type``, carry the
volume's bytes, and lose the row its own ``type`` create generated for the line
taken out: every row reaches every container through the .env.
"""

from __future__ import annotations

import base64
import contextlib
import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import yaml

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine.planner import build_plan, decode_compose, server_uuid_of
from bg_coolify_migrate.settings.base import Settings

from .conftest import ssh_to, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

TEMPLATE = "gotify"
#: The template's compose service key, its volume key, and where it keeps its data.
PART = "gotify"
VOLUME_KEY = "gotify-data"
#: The line the source's compose has and the template's does not.
EDIT = "BGCM_SOURCE_COMPOSE=kept"
#: The variable the template declares and the source's compose no longer does.
DROPPED = "GOTIFY_PASSSTRENGTH"


def _with_edit(raw: str) -> str:
    """The template's compose with :data:`EDIT` added to its one part, :data:`DROPPED` out."""
    doc = yaml.safe_load(raw)
    part = doc["services"][PART]
    env = part.setdefault("environment", [])
    if isinstance(env, list):
        part["environment"] = [e for e in env if not str(e).startswith(f"{DROPPED}=")]
        part["environment"].append(EDIT)
    else:
        env.pop(DROPPED, None)
        key, value = EDIT.split("=", 1)
        env[key] = value
    return yaml.safe_dump(doc, sort_keys=False)


async def _container(host: Any, service_uuid: str, *, timeout: float = 240) -> str:
    """Wait for ``{part}-{service uuid}`` to exist; return its name."""
    import asyncio

    name = f"{PART}-{service_uuid}"
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await host.run(f"docker ps -a --format '{{{{.Names}}}}' --filter name={name}")
        if name in result.stdout.split():
            return name
        await asyncio.sleep(4)
    raise AssertionError(f"no {name} container within {timeout}s")


async def _volume_dir(host: Any, service_uuid: str) -> str:
    """The data directory of ``{uuid}_{key}``, as the daemon reports it."""
    result = await host.run(
        f"docker volume inspect -f '{{{{.Mountpoint}}}}' {service_uuid}_{VOLUME_KEY}"
    )
    path = result.stdout.strip()
    assert result.ok and path, f"no volume {service_uuid}_{VOLUME_KEY}: {result.stderr[:200]}"
    return path


@pytest_asyncio.fixture
async def template_stack(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A deployed gotify template service whose compose was edited, with a marker."""
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"template-{suffix}"
    service_name = f"notify-{suffix}"
    token = f"bgcm-{uuidlib.uuid4().hex}"

    project = await api.post("/projects", {"name": project_name, "description": "bgcm template"})
    project_uuid = str(project["uuid"])

    try:
        created = await api.post(
            "/services",
            {
                "type": TEMPLATE,
                "name": service_name,
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "instant_deploy": False,
            },
        )
        service_uuid = str(created["uuid"])

        # The source drifts from its template, as a hand-edited or an older
        # catalog's service does. Verbatim from 4.4 on, so read back exactly.
        created_full = await api.get_resource("services", service_uuid)
        template_raw = decode_compose(created_full.get("docker_compose_raw"))
        assert template_raw, "the template service came back without its compose"
        await api.update_resource(
            "services",
            service_uuid,
            {"docker_compose_raw": base64.b64encode(_with_edit(template_raw).encode()).decode()},
        )
        source = await api.get_resource("services", service_uuid)
        compose = decode_compose(source.get("docker_compose_raw"))
        assert compose and EDIT in compose and compose != template_raw, "the edit did not take"
        assert DROPPED not in compose
        assert source.get("service_type") == TEMPLATE
        # The re-parse keeps the row the create wrote for the line taken out;
        # the operator deletes it, so the source runs without it.
        for row in await api.get_envs("services", service_uuid):
            if row.get("key") == DROPPED:
                await api.delete_env("services", service_uuid, str(row["uuid"]))
        assert DROPPED not in {r.get("key") for r in await api.get_envs("services", service_uuid)}

        await api.start("services", service_uuid)
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as host:
            container = await _container(host, service_uuid)
            await wait_until_healthy(host, container)
            data = await _volume_dir(host, service_uuid)
            seeded = await host.run(f"echo {token} > {data}/bgcm-marker && test -s {data}/gotify.db")
            assert seeded.ok, f"seeding the volume did not take: {seeded.stderr[:200]}"

        yield {
            "project": project_name,
            "service_name": service_name,
            "service_uuid": service_uuid,
            "compose": compose,
            "token": token,
        }
    finally:
        with contextlib.suppress(Exception):
            for svc in await api.get("/services") or []:
                if isinstance(svc, dict) and suffix in str(svc.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource("services", str(svc["uuid"]), delete_volumes=True)
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def test_template_target_runs_the_source_compose(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], template_stack: dict[str, Any]
) -> None:
    """server-a -> server-b: the source's compose, its template identity, its bytes."""
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=template_stack["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )
    assert not plan.is_blocked, [r.refusals for r in plan.resources]
    migrating = [i.source_name for r in plan.resources for i in r.manifest.to_migrate]
    assert f"{template_stack['service_uuid']}_{VOLUME_KEY}" in migrating, migrating

    from bg_coolify_migrate.engine.runner import run_migration

    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.SUCCEEDED, f"template migration did not succeed: {result}"

    server_b = str(rig["server_b"]["uuid"])
    target_uuid = await _target_service(api, template_stack["service_name"], server_b)
    assert target_uuid, "no service with the original name landed on server-b"

    target = await api.get_resource("services", target_uuid)
    assert target.get("service_type") == TEMPLATE, "the template identity was lost"
    # Compared as documents: a urls PATCH re-dumps the compose through
    # updateCompose on 4.5+ (services.php v4.7.0:398-403), and the rig decides
    # whether the generated URL is carried. Without one it is byte-identical.
    target_raw = decode_compose(target.get("docker_compose_raw"))
    assert target_raw and yaml.safe_load(target_raw) == yaml.safe_load(template_stack["compose"]), (
        "the target does not run the source's compose"
    )
    # Its `type` create wrote a row for DROPPED from the template; CREATE deleted it.
    target_keys = {r.get("key") for r in await api.get_envs("services", target_uuid)}
    assert DROPPED not in target_keys, "a row only the template has reached the target"

    async with ssh_to(api, server_b, settings) as target_host:
        container = await _container(target_host, target_uuid)
        await wait_until_healthy(target_host, container)
        env = await target_host.run(
            f"docker inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {container}"
        )
        assert EDIT in env.stdout.split(), "the running target lacks the source's edit"
        assert not [e for e in env.stdout.split() if e.startswith(f"{DROPPED}=")], (
            "the running target got the template's variable through its .env"
        )
        data = await _volume_dir(target_host, target_uuid)
        marker = await target_host.run(f"cat {data}/bgcm-marker")

    # The marker, not gotify.db: gotify creates a fresh database at startup.
    assert marker.stdout.strip() == template_stack["token"], "the volume's bytes did not arrive"


async def _target_service(api: CoolifyClient, name: str, target_server: str) -> str | None:
    for svc in await api.get("/services") or []:
        if not isinstance(svc, dict) or svc.get("name") != name:
            continue
        full = await api.get_resource("services", str(svc["uuid"]))
        if server_uuid_of(full) == target_server:
            return str(svc["uuid"])
    return None
