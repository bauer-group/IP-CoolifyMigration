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

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.naming import volume_data_path


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
    fs_path: str | None = None
    """A file storage's host path, as stored (absolute, or relative to the
    resource's directory)."""
    has_content: bool = False
    """A file storage Coolify writes from content kept in the row."""


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


@dataclass(frozen=True, slots=True)
class _BindContext:
    """What a bind's classification needs beyond the mount itself."""

    uuid: str | None
    rows: tuple[binds.StorageRow, ...] | None
    carry: binds.StorageCarry
    base: str
    """Where a relative ``fs_path`` of this resource resolves."""


def _storage_host_path(s: ApiStorage, context: _BindContext) -> str | None:
    """The host path a storage row mounts, or None for a named volume. PURE."""
    if s.kind == "file":
        return binds.resolve_fs_path(s.fs_path, context.base)
    return binds.normalise(s.host_path)


_BIND_CLASSES: dict[binds.BindAction, tuple[MountClass, Decision]] = {
    binds.BindAction.MIGRATE: (MountClass.BIND, Decision.MIGRATE),
    binds.BindAction.SKIP: (MountClass.BIND, Decision.SKIP),
    binds.BindAction.PASSTHROUGH: (MountClass.PASSTHROUGH, Decision.SKIP),
    binds.BindAction.REFUSE: (MountClass.BIND, Decision.REFUSE),
}


def _bind_context(
    api_storages: list[ApiStorage] | None, *, uuid: str | None, kind: ResourceKind | None
) -> _BindContext:
    """The resource's host-path storage rows, resolved where Coolify mounts them. PURE."""
    carry = binds.carry_for(kind)
    base = binds.workdir(kind, uuid or "")
    if api_storages is None:
        return _BindContext(uuid=uuid, rows=None, carry=carry, base=base)
    context = _BindContext(uuid=uuid, rows=(), carry=carry, base=base)
    rows: list[binds.StorageRow] = []
    for s in api_storages:
        mount = binds.normalise(s.mount_path)
        host = _storage_host_path(s, context)
        if mount is None or host is None:
            continue
        is_file = s.kind == "file"
        rows.append(
            binds.StorageRow(
                mount,
                host,
                writes_content=is_file and s.has_content and not s.content_is_placeholder,
                placeholder=is_file and s.content_is_placeholder,
            )
        )
    return _BindContext(uuid=uuid, rows=tuple(rows), carry=carry, base=base)


#: Appended to an anonymous-volume refusal. Coolify removes every container on
#: stop AND before every deploy (``docker rm -f``: StopApplication v4.5.0:50,
#: ApplicationDeploymentJob.stop_running_container ahead of ``compose up``;
#: StartService runs ``up --force-recreate``), so such a volume — typically an
#: image ``VOLUME`` the compose does not mount — comes back EMPTY after every
#: restart. Stopped, the stack has no container mounting it and plans clear.
_OFFLINE_HINT = (
    " - stop the stack in Coolify to migrate it offline (Coolify recreates this "
    "volume empty on every restart anyway; its contents are not carried)"
)


def _classify_docker_mount(
    m: DockerMount, context: _BindContext
) -> tuple[MountClass, Decision, str]:
    if m.type == "tmpfs":
        return MountClass.TMPFS, Decision.SKIP, "tmpfs is RAM-backed and empty by contract"
    if m.type == "bind":
        # domain/binds.py: a bind is an arbitrary host path, so whether it is
        # data, where the target mounts it and whether it may be written are
        # decided there, against the resource's own storage rows.
        action, reason = binds.classify(
            source=m.source,
            destination=m.destination,
            read_only=not m.rw,
            uuid=context.uuid,
            rows=context.rows,
            carry=context.carry,
        )
        mount_class, decision = _BIND_CLASSES[action]
        return mount_class, decision, reason
    if m.type == "volume":
        if not m.name:
            return (
                MountClass.ANONYMOUS,
                Decision.REFUSE,
                "anonymous volume: docker's random id cannot be reproduced on the target"
                + _OFFLINE_HINT,
            )
        # Docker generates a 64-hex name for anonymous volumes even though the
        # mount reports a Name. Detect and refuse those too.
        if len(m.name) == 64 and all(c in "0123456789abcdef" for c in m.name):
            return (
                MountClass.ANONYMOUS,
                Decision.REFUSE,
                f"anonymous volume {m.name[:12]}...: its id cannot be reproduced on the target"
                + _OFFLINE_HINT,
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
    resource_uuid: str | None = None,
    kind: ResourceKind | None = None,
) -> VolumeManifest:
    """Merge the three discovery sources into one manifest.

    Args:
        docker_mounts: From ``docker inspect`` over ALL containers of the stack,
            found with ``docker ps -a --filter label=coolify.*`` — note ``-a``.
            This is the authority.
        api_storages: From ``GET /v1/{kind}/{uuid}/storages``. Used to detect
            intent that Docker does not show, and file-mounts whose content the
            API cannot round-trip. ``None`` when the API could not be read: a
            bind mount is then judged without its storage rows.
        docker_volumes: From ``docker volume ls``. Used only to surface orphans.
        uuid_prefixes: Resource uuids of the stack, used to recognise which
            unattached volumes are plausibly ours.
        resource_uuid: The resource's uuid: which paths under ``/data/coolify``
            are its own directory (domain/binds.py).
        kind: The resource's kind: how its storage rows reach the target.

    Returns:
        A manifest whose every item carries a decision and a reason.
    """
    bind_context = _bind_context(api_storages, uuid=resource_uuid, kind=kind)
    api_storages = api_storages or []
    docker_volumes = docker_volumes or []

    items: list[VolumeItem] = []
    warnings: list[str] = []
    migrating_names: set[str] = set()
    seen_volume_names: set[str] = set()
    seen_bind_sources: set[str] = set()

    # 1. Docker inspect — the truth.
    for m in docker_mounts:
        mount_class, decision, reason = _classify_docker_mount(m, bind_context)
        # One item per payload, not per sighting. The same named volume mounted
        # into a second container (nginx and php-fpm sharing a WordPress web
        # root) — or at a second path — is the same bytes seen again, not a
        # second copy job. Duplicates here cascade: N rows in the plan, an
        # N-fold inflated total that the disk-headroom preflight then demands, and N
        # pairing endpoints for one volume. First sighting wins; its mount_path
        # is a live one, so pairing still works.
        if m.type == "volume" and m.name:
            if m.name in seen_volume_names:
                continue
            seen_volume_names.add(m.name)
        elif m.type == "bind" and decision is not Decision.SKIP:
            # Once per host path: the copy, the refusal and the plan row alike.
            key = binds.normalise(m.source) or m.source
            if key in seen_bind_sources:
                continue
            seen_bind_sources.add(key)
        if m.type == "volume" and m.name:
            source_path = volume_data_path(m.name)
        elif m.type == "bind":
            source_path = binds.normalise(m.source) or m.source
        else:
            source_path = m.source
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
        if decision is Decision.MIGRATE and m.name:
            migrating_names.add(m.name)

    # 2. API storages — intent Docker cannot show us.
    docker_paths = {m.destination for m in docker_mounts}
    volumes_by_name = {v.name: v for v in docker_volumes}
    for s in api_storages:
        # A file the API shows as a placeholder (binary, over the 5 MiB cap) is
        # judged where a container mounts it — step 1 mirrors it with rsync, or
        # refuses it where CREATE cannot recreate it (domain/binds.classify).
        if s.mount_path in docker_paths:
            continue  # a live mount already covers it (step 1 is the truth)

        # The volume is already migrating from a live mount, just at a DIFFERENT
        # path than the API declares. That is a stale row — an older compose
        # revision's mount point that Coolify never re-parsed — not extra data.
        # A second item would copy the same volume twice and, worse, demand a
        # target mount path the target's compose will never declare, which makes
        # the pairing step refuse the whole migration.
        if s.name and s.name in migrating_names:
            warnings.append(
                f"Coolify declares persistent storage {s.name!r} at {s.mount_path!r}, but "
                "containers mount that volume elsewhere; the declared path looks stale and "
                "the volume is migrated once, from its live mount"
            )
            continue

        # No container mounts it. But if Coolify DECLARES a persistent storage and
        # its docker volume actually EXISTS, that is data, not noise — migrate it.
        # This is the safety net for a stack whose containers are gone (a stopped
        # compose app runs `compose down`, keeping only its volumes) or that
        # discovery otherwise missed: the API is the intent, `volume ls` is the
        # residue, and together they are unambiguous. Skipping it silently lost a
        # live database (GlobaLeaks: 14 MB the plan reported as 0 B).
        volume = volumes_by_name.get(s.name) if s.name else None
        if s.kind == "persistent" and volume is not None:
            items.append(
                VolumeItem(
                    mount_class=MountClass.NAMED,
                    decision=Decision.MIGRATE,
                    reason=(
                        "declared persistent storage whose volume exists; migrated even "
                        "though no container currently mounts it"
                    ),
                    source_name=s.name,
                    source_path=volume_data_path(s.name) if s.name else s.mount_path,
                    mount_path=s.mount_path,
                    discovered_from=frozenset(
                        {DiscoverySource.COOLIFY_API, DiscoverySource.DOCKER_VOLUME_LS}
                    ),
                )
            )
            if s.name:
                migrating_names.add(s.name)
            continue

        # The same safety net for a host path. With no container at all (an
        # already-stopped stack), Coolify's row is the only record of a bind,
        # and its directory holds data no restart regenerates. While containers
        # exist, an unmounted row is stale (the compose no longer declares it),
        # and the target's parse would create no row to copy it to.
        host = _storage_host_path(s, bind_context)
        if not docker_mounts and host is not None:
            action, reason = binds.classify(
                source=host,
                destination=s.mount_path,
                read_only=True,  # no container to say otherwise: never copy a system path
                uuid=bind_context.uuid,
                rows=bind_context.rows,
                carry=bind_context.carry,
            )
            mount_class, decision = _BIND_CLASSES[action]
            if decision is not Decision.SKIP:
                if host in seen_bind_sources:
                    continue
                seen_bind_sources.add(host)
            items.append(
                VolumeItem(
                    mount_class=mount_class,
                    decision=decision,
                    reason=f"{reason} (declared by Coolify; no container mounts it now)",
                    source_path=host,
                    mount_path=s.mount_path,
                    discovered_from=frozenset({DiscoverySource.COOLIFY_API}),
                )
            )
            continue

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

    # 4. Nothing to inspect. An already-stopped resource is migratable — the API
    # is the intent and `volume ls` is the residue, and above they agree — but
    # the authority is missing, so say what that costs while the operator can
    # still decide. An anonymous volume or an undeclared bind mount appears in
    # NEITHER remaining source; only a container would have shown it.
    if not docker_mounts and any(i.decision is Decision.MIGRATE for i in items):
        warnings.append(
            "no container was available to inspect, so these mounts come from Coolify's "
            "declared storages and `docker volume ls` alone — an anonymous volume or an "
            "undeclared bind mount appears in neither and cannot be ruled out"
        )

    return VolumeManifest(items=tuple(items), warnings=tuple(warnings))
