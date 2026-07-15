"""Fixtures for the e2e rig.

Every fixture here talks to a real Coolify and two real Docker daemons. Nothing
is mocked, which is the point: the unit suite already proves what we believe
about Coolify, and this suite is where those beliefs meet the product.

The rig must be up (see docker-compose.yml). Without rig.json the tests skip
rather than fail — a missing rig is "not run here", not "broken".
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import RemoteHost, SshTarget

RIG_FILE = Path(__file__).parent / "rig.json"

#: Written by the fixtures below; read by the seeding helpers.
POSTGRES_IMAGE = "postgres:16-alpine"
DB_USER = "shop"
DB_PASSWORD = "shop-secret-pw"
DB_NAME = "shopdb"


@pytest.fixture(scope="session")
def rig() -> dict[str, Any]:
    """The rig description written by bootstrap.py."""
    if not RIG_FILE.exists():
        pytest.skip("no rig.json — run tests/e2e/prepare.py, compose up, bootstrap.py")
    data: dict[str, Any] = json.loads(RIG_FILE.read_text(encoding="utf-8"))
    if not (data["server_a"]["reachable"] and data["server_b"]["reachable"]):
        pytest.skip("Coolify cannot reach both rig servers")
    return data


@pytest.fixture(scope="session")
def coolify_url(rig: dict[str, Any]) -> str:
    """Coolify's address *from wherever the tests run*.

    In the runner container that is the service name; rig.json records the
    published address, which only Windows can use.
    """
    return os.environ.get("COOLIFY_URL") or str(rig["url"])


@pytest.fixture(scope="session")
def settings(rig: dict[str, Any], coolify_url: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        coolify_url=coolify_url,
        coolify_token=str(rig["token"]),
        # The rig's host keys are minted fresh on every boot, so pinning them is
        # meaningless here. Everywhere else this stays off: the invariant is
        # "never StrictHostKeyChecking=no", and TOFU into a state-dir file is a
        # different thing from disabling the check.
        trust_host_key=True,
        state_dir=tmp_path_factory.mktemp("bgcm-state"),
    )


@pytest_asyncio.fixture
async def api(settings: Settings) -> AsyncIterator[CoolifyClient]:
    url, token = settings.require_coolify()
    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as client:
        yield client


@asynccontextmanager
async def ssh_to(
    api: CoolifyClient, server_uuid: str, settings: Settings
) -> AsyncIterator[RemoteHost]:
    """Open SSH to a rig server using Coolify's own key, exactly as the tool does.

    A context manager because RemoteHost.connect is one — handing back a live
    connection would leak it on any assertion failure.
    """
    from bg_coolify_migrate.engine.planner import server_ref
    from bg_coolify_migrate.engine.runner import ssh_target_for

    target: SshTarget = await ssh_target_for(api, server_ref(await api.get_server(server_uuid)))
    async with RemoteHost.connect(
        target,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=settings.trust_host_key,
        connect_timeout=settings.ssh_timeout,
    ) as host:
        yield host


async def psql(host: RemoteHost, container: str, sql: str) -> str:
    """Run SQL in a database container on a rig server.

    Nested docker exec: we SSH to the dind server, whose daemon runs the
    database. Two hops, exactly like production.

    The SQL goes in over stdin (`-i`, no `-c`) rather than embedded in the
    command line. The seed strings contain quotes and umlauts, and shell-quoting
    them through two layers is a game you eventually lose — the first attempt did,
    silently, producing an empty result that looked like missing data.

    `-v ON_ERROR_STOP=1` plus `.check()`: psql exits 0 on a failed statement by
    default, so without both a broken seed reports success and the assertion
    fires somewhere far away.
    """
    result = await host.run(
        f"docker exec -i {container} psql -U {DB_USER} -d {DB_NAME} -tA -v ON_ERROR_STOP=1",
        input_text=sql,
        timeout=60,
    )
    result.check()
    return result.stdout.strip()


async def wait_for_container(host: RemoteHost, name: str, *, timeout: float = 180) -> None:
    """Block until a container reports running. Polls the daemon, never an API."""
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        result = await host.run(f"docker inspect -f '{{{{.State.Status}}}}' {name} 2>/dev/null")
        last = result.stdout.strip()
        if last == "running":
            # Running is not ready. Postgres opens its socket a beat later, and
            # a seed that races it fails with a connection error that looks
            # nothing like the timing bug it is.
            probe = await host.run(f"docker exec {name} pg_isready -U {DB_USER} -d {DB_NAME}")
            if probe.ok:
                return
        await asyncio.sleep(3)
    raise AssertionError(f"{name} never became ready (last status: {last or 'absent'})")


async def wait_until_healthy(host: RemoteHost, name: str, *, timeout: float = 240) -> None:
    """Block until Coolify's health check on the container passes.

    Engine-agnostic, unlike wait_for_container's pg_isready. Coolify puts a
    health check on every managed database, so we can wait on the daemon's own
    verdict rather than teaching this helper each engine's readiness probe.

    Generous by default: mysql:8 and mongo:7 spend 40-60s on first-boot
    initialisation, during which their health check legitimately reports
    unhealthy. That is not a failure, it is the image initialising its data
    directory — waiting less would flake against the slow engines only.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        result = await host.run(
            f"docker inspect -f '{{{{.State.Health.Status}}}}' {name} 2>/dev/null"
        )
        last = result.stdout.strip()
        if last == "healthy":
            return
        await asyncio.sleep(4)
    raise AssertionError(f"{name} never became healthy (last: {last or 'absent'})")


async def db_exec(host: RemoteHost, container: str, argv: str, *, stdin: str | None = None) -> str:
    """Run a command inside a database container on a rig server, checked.

    Two docker-exec hops: SSH to the dind server, whose daemon runs the database.
    `-i` and stdin when there is input, so seed strings with quotes and umlauts do
    not have to survive two layers of shell quoting — the postgres path learned
    that the hard way.
    """
    flag = "-i " if stdin is not None else ""
    result = await host.run(
        f"docker exec {flag}{container} {argv}", input_text=stdin, timeout=90
    )
    result.check()
    return result.stdout.strip()
