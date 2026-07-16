"""F2 step implementations: migrating a whole Coolify instance.

Keeps Geczy's three correct architectural decisions and fixes its operational
ones. See ``docs/server-migration.md`` for the full comparison.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

import structlog

from bg_coolify_migrate.engine import keys
from bg_coolify_migrate.engine.context import EphemeralKey
from bg_coolify_migrate.errors import MigrationError, PreflightError, TransferError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.server import appkey, fencing
from bg_coolify_migrate.server.inventory import (
    COOLIFY_DATA_DIR,
    DOCKER_VOLUMES_DIR,
    ServerInventory,
)
from bg_coolify_migrate.server.statemachine import ServerState
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer import rsync, verify
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: Coolify's installer. Pinned by version, never `| bash` from latest — Geczy
#: installs whatever is current against a database copied from an older instance,
#: which runs an unplanned schema migration on first boot.
INSTALL_URL = "https://cdn.coollabs.io/coolify/install.sh"


@dataclass
class ServerContext:
    """State for one F2 run."""

    settings: Settings
    journal: Journal
    migration_id: str
    source_host: RemoteHost
    target_host: RemoteHost
    inventory: ServerInventory
    force_overwrite: bool = False

    app_key: str = ""
    db_password: str | None = None
    ephemeral_key: EphemeralKey | None = None
    paths_copied: list[str] = field(default_factory=list)


async def step_init(ctx: ServerContext) -> dict[str, Any]:
    return {
        "source": ctx.inventory.source_host,
        "target": ctx.inventory.target_host,
        "coolify_version": ctx.inventory.coolify_version,
        "total_bytes": ctx.inventory.total_bytes,
    }


async def step_preflight(ctx: ServerContext) -> dict[str, Any]:
    if ctx.inventory.is_blocked and not ctx.force_overwrite:
        raise PreflightError(
            "instance migration is blocked:\n"
            + "\n".join(f"  {r}" for r in ctx.inventory.blocking_reasons)
        )

    for host, label in ((ctx.source_host, "source"), (ctx.target_host, "target")):
        await rsync.ensure_installed(host, label=label)

    if not await ctx.target_host.which("systemctl"):
        raise PreflightError(
            "the target has no systemctl",
            hint="Coolify's installer expects a systemd host.",
        )
    return {"checks": "ok"}


async def step_inventory(ctx: ServerContext) -> dict[str, Any]:
    return {
        "volumes": len(ctx.inventory.volumes),
        "unattached_volumes": list(ctx.inventory.unattached_volumes),
        "bind_mounts": list(ctx.inventory.bind_mounts),
        "containers": ctx.inventory.container_count,
    }


async def step_read_app_key(ctx: ServerContext) -> dict[str, Any]:
    """Capture APP_KEY BEFORE anything moves, so we can assert it survived.

    Only the fingerprint is journalled. The key itself never leaves memory.
    """
    ctx.app_key, ctx.db_password = await appkey.read(ctx.source_host)
    return {"app_key_fingerprint": appkey.fingerprint(ctx.app_key)}


async def step_stop_source(ctx: ServerContext) -> dict[str, Any]:
    """Stop Docker entirely. Mandatory and verified.

    Geczy makes this a prompt and then tars the filesystem with
    `--warning=no-file-changed`, which explicitly tolerates a live Postgres
    changing underneath it. Answering "n" is one keystroke from a torn snapshot
    of the Coolify database itself.
    """
    await fencing.stop_docker(ctx.source_host)
    return {"docker_stopped": True}


async def step_transfer(ctx: ServerContext) -> dict[str, Any]:
    """Mirror /data/coolify and /var/lib/docker/volumes with rsync.

    rsync, not `tar | ssh`: resumable, chunkable, and it does not need 2x disk
    for an intermediate archive. Geczy's single stream restarts from zero if it
    drops at 95% of 100 GB.
    """
    ctx.ephemeral_key = await keys.install(
        source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id
    )
    ctx.journal.append(
        "step_started",
        state=ServerState.TRANSFER.value,
        detail={"key_fingerprint": ctx.ephemeral_key.fingerprint},
    )

    paths = [COOLIFY_DATA_DIR, DOCKER_VOLUMES_DIR, *ctx.inventory.bind_mounts]
    for path in paths:
        await ctx.target_host.run_checked(f"mkdir -p {shlex.quote(path)}")
        spec = rsync.RsyncSpec(
            source_path=path,
            target_path=path,
            target_host=ctx.target_host.target.host,
            target_user=ctx.target_host.target.user,
            target_port=ctx.target_host.target.port,
            identity_file=ctx.ephemeral_key.remote_path,
            compress=ctx.settings.transfer_compress,
            bandwidth_limit_kbps=ctx.settings.transfer_bandwidth_kbps,
        )
        await rsync.run(ctx.source_host, spec)
        ctx.paths_copied.append(path)
        log.info("server.transferred", path=path)

    return {
        "paths_copied": list(ctx.paths_copied),
        "key_fingerprint": ctx.ephemeral_key.fingerprint,
    }


async def step_verify(ctx: ServerContext) -> dict[str, Any]:
    """Checksum + metadata, both ends.

    Geczy verifies nothing at all: success means `ssh` returned 0.
    """
    total = 0
    for path in ctx.paths_copied:
        report = await verify.verify_volume(
            ctx.source_host,
            ctx.target_host,
            source_path=path,
            target_path=path,
            parallel=ctx.settings.verify_parallel,
        )
        total += len(report.differences)
        if not report.ok:
            details = "\n".join(f"  {d.describe()}" for d in report.differences[:10])
            raise MigrationError(
                f"{path}: {len(report.differences)} difference(s) after transfer:\n{details}",
                hint="Coolify will NOT be installed on the target. Your source is intact.",
            )
    return {"paths_verified": list(ctx.paths_copied), "differences": total}


async def step_install_coolify(ctx: ServerContext) -> dict[str, Any]:
    """Install Coolify on the target — AFTER the data is in place.

    The ordering is the entire feature. install.sh merges the .env with
    `awk '!seen[$1]++'` (existing values first) and only fills EMPTY or MISSING
    vars, so a .env that is already there keeps its APP_KEY and DB_PASSWORD.

    Pinned to the SOURCE's version. Geczy pipes the latest installer against an
    older database, which triggers an unplanned schema migration on first boot.
    """
    version = ctx.inventory.coolify_version.lstrip("v")
    result = await ctx.target_host.run(
        f"curl -fsSL {INSTALL_URL} -o /tmp/coolify-install.sh && "
        f"chmod +x /tmp/coolify-install.sh && "
        f"env VERSION={shlex.quote(version)} bash /tmp/coolify-install.sh",
        timeout=1800,
    )
    if not result.ok:
        raise TransferError(
            f"Coolify's installer failed on the target (exit {result.exit_status})",
            hint=(result.stderr or result.stdout)[-800:],
        )
    return {"installed_version": version}


async def step_assert_app_key(ctx: ServerContext) -> dict[str, Any]:
    """The invariant. If this fails, the ordering was violated.

    Geczy never checks; it works by luck.
    """
    await appkey.assert_survived(
        ctx.target_host, expected=ctx.app_key, expected_db_password=ctx.db_password
    )
    return {"app_key_fingerprint": appkey.fingerprint(ctx.app_key), "survived": True}


async def step_boot(ctx: ServerContext) -> dict[str, Any]:
    """Wait for Coolify to be READY, then prove decryption actually works.

    A matching APP_KEY is necessary but not sufficient — a truncated volume can
    match the key and still be unreadable. Probing beats assuming.

    "Ready", not "running". The container reports ``running`` a second after it
    starts, but Laravel needs a good while longer to migrate, warm its cache and
    connect to the database — and until it has, ``artisan tinker`` cannot run at
    all. Probing on ``running`` read that not-yet-ready state as "the data is
    corrupt" and rolled back a migration that had in fact succeeded. So we wait
    for the container's health check to pass, and then poll the probe, treating
    "cannot answer yet" as a reason to wait rather than to abort.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + 300
    healthy = False
    while asyncio.get_running_loop().time() < deadline:
        status = await ctx.target_host.run(
            "docker inspect -f '{{.State.Health.Status}}' coolify 2>/dev/null"
        )
        if status.stdout.strip() == "healthy":
            healthy = True
            break
        await asyncio.sleep(5)
    if not healthy:
        raise TransferError(
            "Coolify did not become healthy on the target within 300s",
            hint="Check `docker logs coolify` there. Your source is intact and fenced-free.",
        )

    # Poll the probe: NOT_READY is transient (app still warming up even past the
    # health check); only a probe that RAN and could not decrypt is terminal.
    probe_deadline = asyncio.get_running_loop().time() + 300
    while True:
        result = await appkey.decrypt_probe(ctx.target_host)
        if result is appkey.ProbeResult.OK:
            break
        if result is appkey.ProbeResult.DECRYPT_FAILED:
            raise appkey.AppKeyError(
                "Coolify started but cannot decrypt its own stored values",
                hint=(
                    "APP_KEY matches, so the key is right but the data is not readable "
                    "with it. The coolify-db volume may be incomplete. Do NOT fence the "
                    "source."
                ),
            )
        if asyncio.get_running_loop().time() >= probe_deadline:
            raise TransferError(
                "Coolify became healthy but never answered the decrypt probe",
                hint="`artisan tinker` kept failing on the target. Check `docker logs "
                "coolify`. Your source is intact and fenced-free.",
            )
        await asyncio.sleep(5)

    return {"booted": True}


async def step_reconcile(ctx: ServerContext) -> dict[str, Any]:
    """Compare what came up against what we left behind."""
    from bg_coolify_migrate.discovery import docker as docker_mod

    volumes = await docker_mod.list_volumes(ctx.target_host)
    names = {v.name for v in volumes}
    missing = sorted(set(ctx.inventory.volumes) - names)

    if missing:
        log.warning("server.reconcile.missing_volumes", volumes=missing[:10])

    return {
        "target_volumes": len(names),
        "source_volumes": len(ctx.inventory.volumes),
        "missing": missing,
    }


async def step_fence_source(ctx: ServerContext) -> dict[str, Any]:
    """Stop the old Coolify so two brains do not drive one fleet.

    The source is NOT deleted — it stays intact but inert, which is what makes
    "rollback" mean "start it again".
    """
    result = await fencing.fence(ctx.source_host, target_host=ctx.inventory.target_host)
    await keys.revoke(
        source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id
    )
    return {"fenced": result["stopped"]}


# ── compensations ────────────────────────────────────────────────────────────


async def undo_stop_source(ctx: ServerContext, undo_info: dict[str, Any]) -> None:
    """Start Docker again. The compensation that ends the outage."""
    await fencing.start_docker(ctx.source_host)


async def undo_transfer(ctx: ServerContext, undo_info: dict[str, Any]) -> None:
    """Remove what we copied, so a retry cannot merge into a partial tree.

    Only ever removes paths this run recorded copying, and only when the target
    was empty to begin with — never a directory that was already there.
    """
    if not ctx.inventory.target_is_empty:
        log.warning(
            "server.compensate.wipe_skipped",
            reason="the target was not empty before we started; refusing to remove its data",
        )
        return
    for path in undo_info.get("paths_copied") or []:
        await ctx.target_host.run(f"rm -rf {shlex.quote(str(path))}")
        log.info("server.compensate.wiped", path=path)


async def undo_revoke_key(ctx: ServerContext, undo_info: dict[str, Any]) -> None:
    await keys.revoke(
        source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id
    )


async def undo_fence(ctx: ServerContext, undo_info: dict[str, Any]) -> None:
    await fencing.unfence(ctx.source_host)


def build_steps() -> dict[Any, Any]:
    return {
        ServerState.INIT: step_init,
        ServerState.PREFLIGHT: step_preflight,
        ServerState.INVENTORY: step_inventory,
        ServerState.READ_APP_KEY: step_read_app_key,
        ServerState.STOP_SOURCE: step_stop_source,
        ServerState.TRANSFER: step_transfer,
        ServerState.VERIFY: step_verify,
        ServerState.INSTALL_COOLIFY: step_install_coolify,
        ServerState.ASSERT_APP_KEY: step_assert_app_key,
        ServerState.BOOT: step_boot,
        ServerState.RECONCILE: step_reconcile,
        ServerState.FENCE_SOURCE: step_fence_source,
    }


def build_compensations() -> dict[Any, Any]:
    from bg_coolify_migrate.domain.statemachine import Compensation

    return {
        Compensation.START_SOURCE_DOCKER: undo_stop_source,
        Compensation.WIPE_TARGET_DATA: undo_transfer,
        Compensation.REVOKE_EPHEMERAL_KEY: undo_revoke_key,
        Compensation.UNFENCE_SOURCE: undo_fence,
    }
