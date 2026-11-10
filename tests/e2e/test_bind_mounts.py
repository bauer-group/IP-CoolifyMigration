"""Bind mounts, migrated for real: an absolute host path and a service's ./data.

Until this suite existed, the plan listed a bind mount as "migrated" while COPY
and VERIFY iterated volume pairs only, so not one byte of it moved — and a
service's relative ``./data:/data`` was skipped outright as "regenerated",
though Coolify only ever ``mkdir -p``s that directory on a new resource. Both
looked like success: the target started, with empty directories where the data
belonged.

The stack is one busybox service with both shapes. Seed files are written
THROUGH the container (so they land where Coolify bound them), the stack is
migrated server-a -> server-b, and the files are read back THROUGH the target's
container — the only place that proves the target mounts what was copied.

The rig runs Coolify 4.7.2, where a plain application's host-path storage can
no longer be created through the API (fidelity.storage_refusals), so the
absolute bind comes from a compose file, as it does in the estate.

A third bind is a file Coolify writes from ``content:``. Coolify keeps the
service's stored compose WITHOUT that content (parsers.php v4.7.2:3701-3718)
and the target is created from it, so the target's row is a contentless
directory: the plan mirrors the file instead of skipping it as "regenerated".
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
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.manifest import Decision
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import run_migration
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import RemoteHost

from .conftest import ssh_to

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

#: Umlauts and a space, so a byte-level mangle or a split path cannot pass.
SEED = "Grüße aus der Bindung\n"
#: The content of the file Coolify writes from its storage row.
CONTENT = "greeting=hello from the row"


def _compose(host_dir: str) -> str:
    return f"""\
services:
  app:
    image: busybox:1.36
    command: ["sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"]
    volumes:
      - {host_dir}:/files
      - ./data:/data
      - type: bind
        source: ./app.conf
        target: /etc/app.conf
        content: |
          {CONTENT}
"""


async def _app_container(host: RemoteHost, service_uuid: str, *, timeout: float = 180) -> str:
    """The running `app-{service_uuid}` container's name (a deploy is async)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await host.run(
            f"docker ps --format '{{{{.Names}}}}' | grep {service_uuid} | grep app"
        )
        names = result.stdout.strip().splitlines()
        if names:
            return names[0]
        await asyncio.sleep(4)
    raise AssertionError(f"no app container for service {service_uuid} within {timeout}s")


async def _read(host: RemoteHost, container: str, path: str) -> str:
    result = await host.run(f"docker exec {container} cat {path}")
    result.check()
    return result.stdout


@pytest_asyncio.fixture
async def bind_stack(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A deployed busybox service with seed files in both of its bind mounts."""
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"binds-{suffix}"
    service_name = f"binds-{suffix}"
    host_dir = f"/srv/bgcm-e2e-{suffix}/files"

    project = await api.post("/projects", {"name": project_name, "description": "bgcm binds"})
    project_uuid = str(project["uuid"])
    try:
        created = await api.post(
            "/services",
            {
                "name": service_name,
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "docker_compose_raw": base64.b64encode(_compose(host_dir).encode()).decode(),
                "instant_deploy": True,
            },
        )
        service_uuid = str(created["uuid"])

        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            container = await _app_container(source, service_uuid)
            for path in ("/files/a.txt", "/data/b.txt"):
                write = await source.run(
                    f"docker exec -i {container} sh -c 'cat > {path}'", input_text=SEED
                )
                write.check()
            # A nested directory too: the copy is a tree, not one file.
            (await source.run(f"docker exec {container} mkdir -p /data/deep/er")).check()
            (await source.run(f"docker exec {container} sh -c 'echo x > /data/deep/er/c'")).check()

        yield {
            "project": project_name,
            "service_name": service_name,
            "service_uuid": service_uuid,
            "host_dir": host_dir,
        }
    finally:
        with contextlib.suppress(Exception):
            for svc in await api.get("/services") or []:
                if isinstance(svc, dict) and suffix in str(svc.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource("services", str(svc["uuid"]), delete_volumes=True)
        for server in ("server_a", "server_b"):
            with contextlib.suppress(Exception):
                async with ssh_to(api, str(rig[server]["uuid"]), settings) as host:
                    # The absolute host path is outside every resource: Coolify's
                    # delete leaves it (LocalFileVolume v4.7.2:269-271).
                    await host.run(f"rm -rf /srv/bgcm-e2e-{suffix}")
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def test_bind_mount_data_survives_migration(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], bind_stack: dict[str, Any]
) -> None:
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=bind_stack["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    # The plan says what it will do with each bind, truthfully.
    items = [i for r in plan.resources for i in r.manifest.items]
    by_mount = {i.mount_path: i for i in items if i.mount_class is MountClass.BIND}
    assert by_mount["/files"].decision is Decision.MIGRATE, by_mount["/files"].reason
    assert by_mount["/files"].source_path == bind_stack["host_dir"]
    assert by_mount["/data"].decision is Decision.MIGRATE, by_mount["/data"].reason
    assert by_mount["/data"].source_path.endswith(f"{bind_stack['service_uuid']}/data")
    # Not "regenerated": the target's row will hold no content to write it from.
    conf = by_mount["/etc/app.conf"]
    assert conf.decision is Decision.MIGRATE and not conf.content_file, conf.reason
    assert not plan.is_blocked, [r.blocking_reasons for r in plan.resources]

    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.SUCCEEDED, f"bind migration did not succeed: {result}"

    server_b = str(rig["server_b"]["uuid"])
    target_uuid = None
    for svc in await api.get("/services") or []:
        if isinstance(svc, dict) and svc.get("name") == bind_stack["service_name"]:
            full = await api.get_resource("services", str(svc["uuid"]))
            if server_uuid_of(full) == server_b:
                target_uuid = str(svc["uuid"])
    assert target_uuid, "no service with the original name landed on server-b"

    async with ssh_to(api, server_b, settings) as target_host:
        container = await _app_container(target_host, target_uuid)
        # Through the TARGET's container: what it mounts is what was copied.
        assert await _read(target_host, container, "/files/a.txt") == SEED
        assert await _read(target_host, container, "/data/b.txt") == SEED
        assert (await _read(target_host, container, "/data/deep/er/c")).strip() == "x"
        # A file, not the empty directory Docker makes for a missing bind source.
        assert (await _read(target_host, container, "/etc/app.conf")).strip() == CONTENT
        # The relative bind landed in the TARGET's own directory - read back
        # from its storage row, never the source path with the uuid swapped.
        own = await target_host.run(f"cat /data/coolify/services/{target_uuid}/data/b.txt")
        assert own.ok and own.stdout == SEED
        stray = await target_host.run(
            f"test -e /data/coolify/services/{bind_stack['service_uuid']}"
        )
        assert not stray.ok, "a directory named for the SOURCE uuid appeared on the target"
