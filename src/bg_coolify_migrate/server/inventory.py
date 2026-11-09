"""What an instance migration will move, and what would block it.

The discovery half fixes Geczy's two silent-data-loss bugs:

* It discovers volumes from ``docker ps`` — **running containers only**. A
  stopped container's volume is silently skipped and never even reported.
* It drops bind mounts, because ``docker inspect .Name`` is empty for them and
  its ``[ -n "$volumeName" ]`` guard filters them out.

We enumerate ``docker volume ls`` (everything that exists) AND ``docker ps -a``
(everything that is attached, running or not), and reconcile.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from bg_coolify_migrate.discovery import docker
from bg_coolify_migrate.domain import host_keys
from bg_coolify_migrate.domain.versions import parse_version
from bg_coolify_migrate.errors import MigrationError
from bg_coolify_migrate.server.fencing import FENCE_MARKER
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

COOLIFY_DATA_DIR = "/data/coolify"
DOCKER_VOLUMES_DIR = "/var/lib/docker/volumes"

#: Paths that must move for the instance to survive.
#:
#: /data/coolify carries source/.env (APP_KEY!), ssh/keys, proxy config and every
#: resource's generated compose. Copying it wholesale is Geczy's one genuinely
#: correct architectural decision, and we keep it.
REQUIRED_PATHS = (COOLIFY_DATA_DIR,)


class ServerInventory(BaseModel):
    """Everything an F2 migration needs to know before it starts."""

    model_config = ConfigDict(frozen=True)

    source_host: str
    target_host: str
    coolify_version: str

    volumes: tuple[str, ...] = ()
    unattached_volumes: tuple[str, ...] = ()
    """Exist but no container mounts them. Migrated anyway — Geczy skips these
    silently because it enumerates from `docker ps` without -a."""

    bind_mounts: tuple[str, ...] = ()
    """Host paths mounted into containers. Geczy drops these entirely."""

    container_count: int = 0
    running_count: int = 0

    coolify_data_bytes: int = 0
    volumes_bytes: int = 0

    target_free_bytes: int = 0
    target_is_empty: bool = True
    target_has_docker: bool = False

    app_key_fingerprint: str = ""
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    self_host_key_warning: str = ""
    """Coolify (4.7+) pins its own host's SSH key, and the new host presents
    another (domain/host_keys.py). One of ``warnings`` too; kept apart because its
    remedy is due AFTER the move, so the run repeats it as a notice."""

    @property
    def total_bytes(self) -> int:
        return self.coolify_data_bytes + self.volumes_bytes

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking_reasons)


@dataclass
class _Probe:
    volumes: list[str] = field(default_factory=list)
    attached: set[str] = field(default_factory=set)
    binds: set[str] = field(default_factory=set)


async def _probe_source(host: RemoteHost) -> _Probe:
    probe = _Probe()

    for volume in await docker.list_volumes(host):
        probe.volumes.append(volume.name)

    # -a: a stopped container's volume is still its data. This single flag is
    # the difference between Geczy silently losing it and us moving it.
    containers = await docker.list_containers(host, label_filters={})
    for container in containers:
        for mount in await docker.inspect_mounts(host, container.id or container.name):
            if mount.type == "volume" and mount.name:
                probe.attached.add(mount.name)
            elif mount.type == "bind" and mount.source:
                if mount.source in ("/var/run/docker.sock", "/tmp"):
                    continue
                if mount.source.startswith((COOLIFY_DATA_DIR, DOCKER_VOLUMES_DIR)):
                    continue  # already covered by the wholesale copies
                probe.binds.add(mount.source)

    return probe


async def take(
    source: RemoteHost,
    target: RemoteHost,
    *,
    coolify_version: str,
    headroom_factor: float = 1.2,
    force_overwrite: bool = False,
    self_server: Mapping[str, Any] | None = None,
) -> ServerInventory:
    """Inventory both ends. Reads only.

    ``self_server`` is Coolify's record of the source host, settings included, as
    the API listed it; it only feeds the host-key warning.
    """
    blocking: list[str] = []
    warnings: list[str] = []

    # ── source ──────────────────────────────────────────────────────────────
    if not await source.path_exists(COOLIFY_DATA_DIR):
        blocking.append(f"{COOLIFY_DATA_DIR} does not exist on the source — is this Coolify?")

    probe = await _probe_source(source)
    containers = await docker.list_containers(source, label_filters={})
    running = [c for c in containers if not c.is_stopped]

    unattached = sorted(set(probe.volumes) - probe.attached)
    if unattached:
        warnings.append(
            f"{len(unattached)} volume(s) exist but no container mounts them; they WILL be "
            "migrated (Geczy's script would silently skip them)"
        )
    if probe.binds:
        warnings.append(
            f"{len(probe.binds)} bind mount(s) outside /data/coolify will be migrated: "
            + ", ".join(sorted(probe.binds)[:5])
        )

    coolify_bytes, _ = await docker.path_size(source, COOLIFY_DATA_DIR)
    volumes_bytes, _ = await docker.path_size(source, DOCKER_VOLUMES_DIR)

    from bg_coolify_migrate.server import appkey

    app_key_fp = ""
    try:
        key, _ = await appkey.read(source)
        app_key_fp = appkey.fingerprint(key)
    except Exception as exc:
        blocking.append(str(exc).splitlines()[0])

    self_host_key_warning = None
    if self_server is not None:
        # The instance-wide checking mode is in the same .env, which the target
        # inherits with /data/coolify. Read again rather than widening appkey.read:
        # the text never leaves this frame, and an unreadable file only leaves
        # the mode unknown (the warning then names both outcomes).
        try:
            env_text: str | None = await source.read_file(appkey.COOLIFY_ENV_PATH)
        except MigrationError:
            env_text = None
        self_host_key_warning = host_keys.self_host_key_warning(
            self_server,
            version=parse_version(coolify_version),
            instance_mode=host_keys.instance_checking_mode(env_text),
        )
        if self_host_key_warning:
            warnings.append(self_host_key_warning)

    # ── target ──────────────────────────────────────────────────────────────
    target_has_docker = await target.which("docker")
    if not target_has_docker:
        warnings.append("docker is not installed on the target; install.sh will install it")

    # `tar -Pxf - -C /` MERGES. Pointed at a box already running Coolify, it
    # merges two Postgres data directories into one. Geczy assumes empty and
    # never checks.
    target_is_empty = not await target.path_exists(COOLIFY_DATA_DIR)
    if not target_is_empty:
        message = (
            f"the target already has {COOLIFY_DATA_DIR}. Extracting over it MERGES two "
            "Coolify installations — including two Postgres data directories — rather "
            "than replacing one"
        )
        if force_overwrite:
            warnings.append(message + " (proceeding: --force-overwrite)")
        else:
            blocking.append(message)

    target_free = await target.free_bytes("/")
    total = coolify_bytes + volumes_bytes
    required = int(total * headroom_factor)
    if target_free < required:
        # Proportional. Geczy checks a fixed 1 GB floor and never compares
        # against the total it just computed — hence issue #8, a 100 GB instance.
        blocking.append(
            f"target has {target_free / 1024**3:.1f} GB free but needs "
            f"{required / 1024**3:.1f} GB ({total / 1024**3:.1f} GB x {headroom_factor})"
        )

    if await source.path_exists(FENCE_MARKER):
        warnings.append(
            "the source is already fenced by a previous migration; it was migrated before"
        )

    return ServerInventory(
        source_host=source.target.host,
        target_host=target.target.host,
        coolify_version=coolify_version,
        volumes=tuple(sorted(probe.volumes)),
        unattached_volumes=tuple(unattached),
        bind_mounts=tuple(sorted(probe.binds)),
        container_count=len(containers),
        running_count=len(running),
        coolify_data_bytes=coolify_bytes,
        volumes_bytes=volumes_bytes,
        target_free_bytes=target_free,
        target_is_empty=target_is_empty,
        target_has_docker=target_has_docker,
        app_key_fingerprint=app_key_fp,
        blocking_reasons=tuple(blocking),
        warnings=tuple(warnings),
        self_host_key_warning=self_host_key_warning or "",
    )
