"""The volume manifest and its reconciliation algebra.

PURE module: no IO. The callers in ``discovery/`` gather raw facts; this module
decides what they mean.

**Why reconciliation, rather than trusting one source?** Each available source is
individually incomplete, and each of our two predecessors picked exactly one and
lost data as a result:

===========================  ==========================================================
Source                       What it alone misses
===========================  ==========================================================
Coolify API ``/storages``    Anonymous volumes and anything the compose parser did not
                             turn into a row. (``coolify-mover`` uses only this.)
``docker ps`` + ``inspect``  Nothing — *if* you remember ``-a``. Geczy used bare
                             ``docker ps``, so every stopped container's volume was
                             silently skipped. Coolify's own code uses ``-a``.
``docker volume ls``         Mount paths and ownership; it only proves existence.
                             But it is the only way to see orphans.
===========================  ==========================================================

So: ``docker inspect`` is the **truth** (it is what the kernel will actually
mount), the API is the **intent** (it is what Coolify will recreate), and
``volume ls`` is the **residue**. We take the union, classify every item, and
refuse rather than guess when they disagree in a way that could lose bytes.

This is what makes the tool application-unaware: we never ask "is this Postgres?"
— we ask "what does this container mount, and is it stopped?".
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from bg_coolify_migrate.domain.compose import PASSTHROUGH_HOST_PATHS, MountClass
from bg_coolify_migrate.domain.naming import COOLIFY_BASE_DIR, volume_data_path


class DiscoverySource(StrEnum):
    """Where a manifest item was observed. Kept per-item for auditability."""

    COOLIFY_API = "coolify_api"
    DOCKER_INSPECT = "docker_inspect"
    DOCKER_VOLUME_LS = "docker_volume_ls"


class Decision(StrEnum):
    """What the migration will do with an item."""

    MIGRATE = "migrate"
    SKIP = "skip"
    REFUSE = "refuse"
    """Blocks the migration. Used where proceeding would silently lose data and
    no safe default exists — anonymous volumes being the canonical case."""


class DockerMount(BaseModel):
    """One entry of ``docker inspect --format '{{json .Mounts}}'``."""

    model_config = ConfigDict(frozen=True)

    container: str
    type: str
    """volume | bind | tmpfs | npipe"""
    name: str | None = None
    source: str = ""
    """Host path. For named volumes docker reports /var/lib/docker/volumes/<n>/_data."""
    destination: str
    rw: bool = True


class ApiStorage(BaseModel):
    """One entry from ``GET /v1/{kind}/{uuid}/storages``."""

    model_config = ConfigDict(frozen=True)

    kind: str
    """persistent | file"""
    name: str | None = None
    mount_path: str
    host_path: str | None = None
    is_directory: bool | None = None
    content_is_placeholder: bool = False
    """True when the API returned ``[binary file]`` or ``[file too large to
    display]`` instead of real content (the 5 MiB cap). Such a file cannot be
    recreated through the API and must be rsynced instead."""


class DockerVolume(BaseModel):
    """One entry of ``docker volume ls``."""

    model_config = ConfigDict(frozen=True)

    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    driver: str = "local"


class VolumeItem(BaseModel):
    """One unit of data to move (or consciously not move).

    The durable contract between discovery -> transfer -> verification ->
    rollback. Serialised into the journal, so field names are stable.
    """

    model_config = ConfigDict(frozen=True)

    mount_class: MountClass
    decision: Decision
    reason: str
    """Human-readable justification. Always populated, including for MIGRATE, so
    a report can explain every line without the reader re-deriving the logic."""

    source_name: str | None = None
    target_name: str | None = None
    source_path: str
    target_path: str | None = None
    mount_path: str
    container: str | None = None

    bytes: int | None = None
    file_count: int | None = None
    discovered_from: frozenset[DiscoverySource] = Field(default_factory=frozenset)

    @property
    def moves_data(self) -> bool:
        return self.decision is Decision.MIGRATE


class VolumeManifest(BaseModel):
    """Everything the migration knows about the bytes it must move."""

    model_config = ConfigDict(frozen=True)

    items: tuple[VolumeItem, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def to_migrate(self) -> tuple[VolumeItem, ...]:
        return tuple(i for i in self.items if i.decision is Decision.MIGRATE)

    @property
    def refused(self) -> tuple[VolumeItem, ...]:
        return tuple(i for i in self.items if i.decision is Decision.REFUSE)

    @property
    def skipped(self) -> tuple[VolumeItem, ...]:
        return tuple(i for i in self.items if i.decision is Decision.SKIP)

    @property
    def total_bytes(self) -> int:
        return sum(i.bytes or 0 for i in self.to_migrate)

    @property
    def is_blocked(self) -> bool:
        return bool(self.refused)


def _is_coolify_config_dir(host_path: str) -> bool:
    """True for /data/coolify/{applications,services,databases}/<uuid>/... paths.

    Derived state: regenerated on every deploy and embedding the OLD uuid.
    Copying it would plant stale container names and labels on the target.
    """
    return host_path.startswith(f"{COOLIFY_BASE_DIR}/")


def _classify_docker_mount(m: DockerMount) -> tuple[MountClass, Decision, str]:
    if m.type == "tmpfs":
        return MountClass.TMPFS, Decision.SKIP, "tmpfs is RAM-backed and empty by contract"
    if m.type == "bind":
        if m.source in PASSTHROUGH_HOST_PATHS:
            return (
                MountClass.PASSTHROUGH,
                Decision.SKIP,
                f"{m.source} is supplied by the target host, not migrated",
            )
        if _is_coolify_config_dir(m.source):
            return (
                MountClass.BIND,
                Decision.SKIP,
                "Coolify regenerates this config dir on deploy; it embeds the old uuid",
            )
        return MountClass.BIND, Decision.MIGRATE, "bind mount: host data must be mirrored"
    if m.type == "volume":
        if not m.name:
            return (
                MountClass.ANONYMOUS,
                Decision.REFUSE,
                "anonymous volume: docker's random id cannot be reproduced on the target",
            )
        # Docker generates a 64-hex name for anonymous volumes even though the
        # mount reports a Name. Detect and refuse those too.
        if len(m.name) == 64 and all(c in "0123456789abcdef" for c in m.name):
            return (
                MountClass.ANONYMOUS,
                Decision.REFUSE,
                f"anonymous volume {m.name[:12]}...: its id cannot be reproduced on the target",
            )
        return MountClass.NAMED, Decision.MIGRATE, "named volume"
    return (
        MountClass.PASSTHROUGH,
        Decision.SKIP,
        f"unsupported mount type {m.type!r}",
    )


def reconcile(
    *,
    docker_mounts: list[DockerMount],
    api_storages: list[ApiStorage] | None = None,
    docker_volumes: list[DockerVolume] | None = None,
    uuid_prefixes: frozenset[str] = frozenset(),
) -> VolumeManifest:
    """Merge the three discovery sources into one manifest.

    Args:
        docker_mounts: From ``docker inspect`` over ALL containers of the stack,
            found with ``docker ps -a --filter label=coolify.*`` — note ``-a``.
            This is the authority.
        api_storages: From ``GET /v1/{kind}/{uuid}/storages``. Used to detect
            intent that Docker does not show, and file-mounts whose content the
            API cannot round-trip.
        docker_volumes: From ``docker volume ls``. Used only to surface orphans.
        uuid_prefixes: Resource uuids of the stack, used to recognise which
            unattached volumes are plausibly ours.

    Returns:
        A manifest whose every item carries a decision and a reason.
    """
    api_storages = api_storages or []
    docker_volumes = docker_volumes or []

    items: list[VolumeItem] = []
    warnings: list[str] = []
    seen_paths: set[tuple[str, str]] = set()
    migrating_names: set[str] = set()

    # 1. Docker inspect — the truth.
    for m in docker_mounts:
        mount_class, decision, reason = _classify_docker_mount(m)
        source_path = volume_data_path(m.name) if m.type == "volume" and m.name else m.source
        items.append(
            VolumeItem(
                mount_class=mount_class,
                decision=decision,
                reason=reason,
                source_name=m.name,
                source_path=source_path,
                mount_path=m.destination,
                container=m.container,
                discovered_from=frozenset({DiscoverySource.DOCKER_INSPECT}),
            )
        )
        seen_paths.add((m.container, m.destination))
        if decision is Decision.MIGRATE and m.name:
            migrating_names.add(m.name)

    # 2. API storages — intent Docker cannot show us.
    #    A declared storage with no live mount means the compose and Coolify's
    #    parsed view disagree; that is worth surfacing but is not itself data.
    docker_paths = {m.destination for m in docker_mounts}
    for s in api_storages:
        if s.content_is_placeholder:
            warnings.append(
                f"file mount at {s.mount_path!r} exceeds the API's 5 MiB content cap "
                "(or is binary); it will be mirrored with rsync instead of recreated via the API"
            )
        if s.mount_path not in docker_paths:
            warnings.append(
                f"Coolify declares a {s.kind} storage at {s.mount_path!r} that no container "
                "currently mounts; it will not be migrated"
            )

    # 3. docker volume ls — orphans.
    for v in docker_volumes:
        if v.name in migrating_names:
            continue
        belongs = any(v.name.startswith(p) for p in uuid_prefixes) or (
            v.labels.get("coolify.managed") == "true"
        )
        if belongs:
            warnings.append(
                f"volume {v.name!r} exists and looks like it belongs to this stack, but no "
                "container mounts it; it is NOT migrated (delete it, or attach it first)"
            )

    return VolumeManifest(items=tuple(items), warnings=tuple(warnings))
