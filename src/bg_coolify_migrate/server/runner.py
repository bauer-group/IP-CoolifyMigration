"""F2 orchestration.

Reuses the same Saga, journal, transfer and verification as F1 — only the state
machine and the steps differ.

Unlike F1, F2 does not talk to the Coolify API during the run: the instance is
stopped for most of it. We use the API only to learn the version and to reach the
source, then work over SSH.
"""

from __future__ import annotations

import structlog

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.engine.executor import RunResult, Saga
from bg_coolify_migrate.engine.runner import make_migration_id, ssh_target_for
from bg_coolify_migrate.errors import PreflightError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.server import inventory as inventory_mod
from bg_coolify_migrate.server.inventory import ServerInventory
from bg_coolify_migrate.server.statemachine import COMPENSATION, ORDER
from bg_coolify_migrate.server.steps import ServerContext, build_compensations, build_steps
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import RemoteHost, SshTarget

log = structlog.get_logger(__name__)


async def _source_ssh(api: CoolifyClient, settings: Settings) -> SshTarget:
    """Reach the Coolify host itself.

    Coolify's own record for the machine it runs on is the server whose ip is
    localhost/127.0.0.1 — it manages itself. If that is absent the instance is
    controlling only remote servers, which F2 does not handle.
    """
    from bg_coolify_migrate.engine.planner import server_ref

    for server in await api.list_servers():
        # Coolify marks its own host explicitly. Sniffing for a loopback ip was a
        # guess that happens to work on a default install and fails on any
        # instance whose self-record uses a real address.
        if CoolifyClient.server_is_coolify_host(server):
            return await ssh_target_for(api, server_ref(server))

    for server in await api.list_servers():
        ip = str(server.get("ip", ""))
        if ip in ("127.0.0.1", "localhost", "host.docker.internal"):
            return await ssh_target_for(api, server_ref(server))

    raise PreflightError(
        "could not find Coolify's own server record",
        hint=(
            "F2 migrates the host Coolify runs ON. No registered server is marked "
            "is_coolify_host, and none has a loopback address, so this instance does "
            "not appear to manage itself."
        ),
    )


async def plan_server_migration(
    settings: Settings, target_host: str, *, force_overwrite: bool = False
) -> ServerInventory:
    """Inventory both ends. Reads only."""
    url, token = settings.require_coolify()

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        version = await api.version()
        source_ssh = await _source_ssh(api, settings)

    target_ssh = SshTarget(host=target_host, user="root")

    async with RemoteHost.connect(
        source_ssh,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=settings.trust_host_key,
        connect_timeout=settings.ssh_timeout,
    ) as source, RemoteHost.connect(
        target_ssh,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=settings.trust_host_key,
        connect_timeout=settings.ssh_timeout,
    ) as target:
        return await inventory_mod.take(
            source,
            target,
            coolify_version=version,
            headroom_factor=settings.disk_headroom_factor,
            force_overwrite=force_overwrite,
        )


async def run_server_migration(
    settings: Settings,
    target_host: str,
    inventory: ServerInventory,
    *,
    force_overwrite: bool = False,
) -> tuple[RunResult, str]:
    """Execute an instance migration. Returns ``(result, migration_id)``."""
    url, token = settings.require_coolify()
    migration_id = make_migration_id("coolify-instance", target_host)
    journal = Journal.create(settings.resolved_state_dir(), migration_id)

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        source_ssh = await _source_ssh(api, settings)

    target_ssh = SshTarget(host=target_host, user="root")

    async with RemoteHost.connect(
        source_ssh,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=settings.trust_host_key,
        connect_timeout=settings.ssh_timeout,
    ) as source, RemoteHost.connect(
        target_ssh,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=settings.trust_host_key,
        connect_timeout=settings.ssh_timeout,
    ) as target:
        ctx = ServerContext(
            settings=settings,
            journal=journal,
            migration_id=migration_id,
            source_host=source,
            target_host=target,
            inventory=inventory,
            force_overwrite=force_overwrite,
        )
        saga = Saga(
            journal=journal,
            context=ctx,
            steps=build_steps(),
            compensations=build_compensations(),
            order=ORDER,
            compensation_map=COMPENSATION,
        )
        result = await saga.run()
        log.info("server_migration.finished", id=migration_id, outcome=result.outcome.value)
        return result, migration_id
