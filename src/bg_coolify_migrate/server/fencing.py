"""Fencing the old instance — F2's biggest real-world hazard.

After a successful instance migration **both Coolifys are live**, and they are
identical twins:

* the same FQDNs, racing each other for ACME renewals;
* **the same SSH keys**, so both can drive the same managed fleet;
* the same scheduler, both running backups, health checks and auto-deploys.

Two Coolify brains managing one fleet is not theoretical. Both will notice a
container is down and both will start it. Both will renew the same certificate.
Both will run the same scheduled backup to the same S3 bucket.

Geczy's script is silent on all of this: it finishes by offering to restart
Docker on the source. We stop the source and disable its scheduler as a named,
compensable step.

We do NOT delete the source, ever. It stays intact but inert, which is exactly
what makes "rollback" mean "start it again".
"""

from __future__ import annotations

import structlog

from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: Coolify's own containers. Stopping these silences the instance without
#: touching the workloads it manages.
COOLIFY_CONTAINERS = ("coolify", "coolify-realtime", "coolify-db", "coolify-redis")

#: Marker file recording that we fenced this host, so `unfence` is not a guess.
FENCE_MARKER = "/data/coolify/.fenced-by-bg-coolify-migrate"


async def fence(host: RemoteHost, *, target_host: str) -> dict[str, list[str]]:
    """Stop the source's Coolify so it cannot drive the fleet.

    Stops only Coolify's own containers, not the workloads. The workloads are
    already stopped (F2 stopped Docker entirely), and leaving them alone means
    an unfence restores the box exactly.

    Returns what was stopped, for the journal.
    """
    stopped: list[str] = []
    for container in COOLIFY_CONTAINERS:
        result = await host.run(f"docker stop {container} 2>/dev/null")
        if result.ok:
            stopped.append(container)

    # Prevent the daemon from bringing them back on the next boot.
    for container in COOLIFY_CONTAINERS:
        await host.run(f"docker update --restart=no {container} 2>/dev/null")

    await host.run(
        f"printf '%s\\n' 'fenced: migrated to {target_host}' > {FENCE_MARKER} 2>/dev/null"
    )

    log.info("fencing.fenced", host=host.target.host, stopped=stopped, target=target_host)
    return {"stopped": stopped}


async def unfence(host: RemoteHost) -> None:
    """Undo a fence: restore the restart policy and start Coolify again.

    The compensation for :func:`fence`. Only ever run on a host we fenced — the
    marker file proves it.
    """
    for container in COOLIFY_CONTAINERS:
        await host.run(f"docker update --restart=unless-stopped {container} 2>/dev/null")
        await host.run(f"docker start {container} 2>/dev/null")
    await host.run(f"rm -f {FENCE_MARKER} 2>/dev/null")
    log.info("fencing.unfenced", host=host.target.host)


async def is_fenced(host: RemoteHost) -> bool:
    return await host.path_exists(FENCE_MARKER)


async def stop_docker(host: RemoteHost) -> None:
    """Stop the Docker daemon entirely. F2's quiesce.

    Non-negotiable and verified, unlike Geczy's prompt: tarring a live Postgres
    data directory is one keystroke away there, and
    ``--warning=no-file-changed`` explicitly tolerates the files changing
    underneath it. A torn snapshot of the Coolify database means a Coolify that
    boots into an inconsistent state.
    """
    from bg_coolify_migrate.errors import QuiesceError

    await host.run("systemctl stop docker.socket 2>/dev/null")
    result = await host.run("systemctl stop docker")
    if not result.ok:
        raise QuiesceError(
            f"could not stop Docker on {host.target.host}",
            hint=(result.stderr or "").strip()[:300]
            or "The daemon must be stopped before /var/lib/docker can be copied consistently.",
        )

    check = await host.run("systemctl is-active docker")
    if check.stdout.strip() == "active":
        raise QuiesceError(
            f"Docker is still active on {host.target.host} after being asked to stop",
            hint="Something is restarting it. Copying now would produce a torn snapshot.",
        )
    log.info("fencing.docker_stopped", host=host.target.host)


async def start_docker(host: RemoteHost) -> None:
    """Start the Docker daemon. The compensation that ends F2's outage."""
    result = await host.run("systemctl start docker")
    if not result.ok:
        raise RuntimeError(f"could not start Docker on {host.target.host}: {result.stderr[:200]}")
    log.info("fencing.docker_started", host=host.target.host)
