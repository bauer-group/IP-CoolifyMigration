"""Volume pins: keep Coolify's Docker cleanup off the data a migration leaves idle.

IO shell with pure builders, like ``transfer/rsync.py``: the command and the
predicates are total functions; the async functions only run them.

The hazard
----------
With a server's "Delete unused volumes" on, Coolify's Docker cleanup runs
``docker volume prune -af`` — no filter, at every supported release
(v4.1.2:app/Actions/Server/CleanupDocker.php:61-63, v4.5.0:166-168). That
deletes every volume NO container references. And a migration makes volumes
unreferenced by construction:

* the SOURCE's, from QUIESCE on — Coolify's stop is ``docker stop`` then
  ``docker rm -f`` (v4.5.0:app/Actions/Database/StopDatabase.php:70), so the
  containers that referenced them are gone, until a rollback restarts it or for
  good under KEEP/RENAME;
* the TARGET's, from COPY until its containers exist — we create them with
  ``docker volume create`` and fill them while nothing mounts them.

The scheduled cleanup fires at the server's cron, or whenever disk usage crosses
its threshold (v4.5.0:app/Jobs/DockerCleanupJob.php:115,136,156), and the API's
run endpoint (4.3.0+) takes ``delete_unused_volumes`` as a per-request override
(v4.5.0:app/Http/Controllers/Api/ServerDockerCleanupController.php:280-287) — so
protection must not depend on the setting, and the setting is not ours to flip.

The pin
-------
A container that is CREATED AND NEVER STARTED, mounting each volume:

* ``docker volume prune`` skips every volume a container references, running
  or not — a created container is a reference;
* it carries no ``coolify.*`` label, and Coolify's container cleanup only ever
  removes ``coolify.managed=true`` containers (v4.1.2:CleanupDocker.php:51,
  v4.5.0:211-216); the helper reaper lists only RUNNING containers
  (v4.5.0:app/Jobs/CleanupHelperContainersJob.php:37);
* its image is an empty tar we import ourselves — an all-zero stream IS an empty
  tar — so it needs no registry, no network and no particular OS, and Coolify's
  image prune ``docker rmi`` (no ``-f``) refuses an image a container uses
  (v4.5.0:CleanupDocker.php:267,283). ``--pull never``: if the cleanup removed
  the freshly imported image before the create, fail fast and re-import rather
  than resolve the name on Docker Hub;
* every mount is ``readonly,volume-nocopy`` and the container never runs, so no
  byte is written and nothing is chowned (invariant 1).

Pins are found by label — role, migration id, side — the way ``keys.revoke``
finds its authorized_keys line by comment marker, so a compensation can remove
them after a crash between the ``docker create`` and the journal write, and can
never touch another migration's. Removed with ``docker rm`` — never ``-v``, which
would delete anonymous volumes, never ``-f``, which a container that never runs
does not need.

Releasing one
-------------
Only a RUNNING container is evidence that a volume will stay referenced, and
only one that keeps running across a settle window. A container seen once can
vanish right after: Coolify's rolling update removes a new container that fails
its health check, and a queued stop lands whenever its worker gets to it. A
stopped Coolify container is no evidence at all — before 4.4.4 the cleanup's
own ``docker container prune`` removes stopped resource containers just before
it prunes the volumes (v4.4.3:CleanupDocker.php:164,173).

A volume nothing mounted before the migration is pinned apart, labelled
``idle``: no restart will ever mount it, so waiting for one would keep the pin
forever, and releasing it is no more exposure than before the migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

import structlog

from bg_coolify_migrate.discovery import docker
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: Imported on first use from an empty tar. Never pulled: see ``--pull never``.
PIN_IMAGE = "bg-coolify-migrate/pin:1"

LABEL_ROLE = "bg-coolify-migrate.role"
ROLE_PIN = "pin"
LABEL_MIGRATION = "bg-coolify-migrate.migration"
LABEL_SIDE = "bg-coolify-migrate.side"
#: Present, as ``idle``, only on a pin for volumes nothing mounted before the
#: migration. A pin without it waits for a remount — older pins included.
LABEL_HOLD = "bg-coolify-migrate.hold"
HOLD_IDLE = "idle"

SOURCE = "source"
TARGET = "target"

#: The only container state that counts as a reference worth releasing for.
_REFERENCING = "running"

#: What a pin's name may embed. Docker's own rule for container names.
_MIGRATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
#: Docker's rule for a volume name. Also what keeps the comma-separated
#: ``--mount`` spec unambiguous: a name with a comma could smuggle in an option.
_VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]+")
#: The name Docker gives an anonymous volume.
_ANONYMOUS = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class Pin:
    """One pin container and the volumes it holds."""

    name: str
    volumes: frozenset[str]
    idle: bool = False
    """Holds volumes nothing mounted before the migration: released without a wait."""


@dataclass(frozen=True, slots=True)
class Release:
    """What :func:`release_when_referenced` did."""

    released: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    unmounted: tuple[str, ...] = ()
    """The pinned volumes nothing else mounted, which is why ``kept`` was kept."""


# ── pure ─────────────────────────────────────────────────────────────────────


def _check_side(side: str) -> None:
    if side not in (SOURCE, TARGET):
        raise ValueError(f"pin side must be {SOURCE!r} or {TARGET!r}, not {side!r}")


def pin_name(migration_id: str, side: str, volumes: Iterable[str]) -> str:
    """A deterministic, docker-safe container name. PURE.

    Deterministic so a step re-run after a crash asks for the same name, and
    keyed on the volume set so a later pin for volumes the first did not cover
    gets a name of its own.
    """
    if not _MIGRATION_ID.fullmatch(migration_id):
        raise ValueError(f"migration id {migration_id!r} cannot be part of a container name")
    _check_side(side)
    digest = hashlib.sha256("\n".join(sorted(set(volumes))).encode()).hexdigest()[:8]
    return f"bg-coolify-migrate-pin-{migration_id}-{side}-{digest}"


def create_command(
    *, name: str, migration_id: str, side: str, volumes: Iterable[str], idle: bool = False
) -> str:
    """One SSH round trip: import the empty image if absent, then create the pin. PURE.

    Never ``docker run`` and never ``docker start``: the ``/pin`` command only
    satisfies ``docker create`` and does not exist in the image, so the
    container could not start even if asked to. ``idle`` labels a pin for
    volumes nothing mounted before the migration (:data:`LABEL_HOLD`).
    """
    if not _MIGRATION_ID.fullmatch(migration_id):
        raise ValueError(f"migration id {migration_id!r} cannot be part of a container name")
    _check_side(side)
    wanted = sorted(set(volumes))
    if not wanted:
        raise ValueError("a pin needs at least one volume")
    for volume in wanted:
        if not _VOLUME_NAME.fullmatch(volume):
            raise ValueError(f"{volume!r} is not a docker volume name")

    image = shlex.quote(PIN_IMAGE)
    labels = " ".join(
        f"--label {shlex.quote(f'{key}={value}')}"
        for key, value in (
            (LABEL_ROLE, ROLE_PIN),
            (LABEL_MIGRATION, migration_id),
            (LABEL_SIDE, side),
            *([(LABEL_HOLD, HOLD_IDLE)] if idle else []),
        )
    )
    mounts = " ".join(
        "--mount "
        + shlex.quote(f"type=volume,source={volume},target=/pin/{volume},readonly,volume-nocopy")
        for volume in wanted
    )
    return (
        f"(docker image inspect {image} >/dev/null 2>&1 || "
        f"head -c 10240 /dev/zero | docker import - {image} >/dev/null) && "
        f"docker create --pull never --name {shlex.quote(name)} {labels} --network none "
        f"{mounts} {image} /pin"
    )


def is_pin(
    container: docker.Container, *, migration_id: str | None = None, side: str | None = None
) -> bool:
    """Is this a pin — ours, when ``migration_id``/``side`` are given? PURE.

    A RUNNING container is never a pin, whatever its labels say: a pin never
    runs, so one that does is something else wearing our label, and every gate
    that skips pins (PREFLIGHT's hidden-container probe, the reference check)
    must keep seeing it.
    """
    if container.labels.get(LABEL_ROLE) != ROLE_PIN or not container.is_stopped:
        return False
    if migration_id is not None and container.labels.get(LABEL_MIGRATION) != migration_id:
        return False
    return side is None or container.labels.get(LABEL_SIDE) == side


def is_anonymous(volume: str) -> bool:
    """Docker's random 64-hex name. A recreated container never re-mounts one. PURE."""
    return bool(_ANONYMOUS.fullmatch(volume))


def awaited(held: Iterable[Pin]) -> set[str]:
    """The pinned volumes a release must first see mounted by someone else. PURE.

    Every pinned volume except the anonymous ones and those of an idle pin: a
    container Coolify recreates gets a NEW anonymous volume, so waiting for the
    old one would always time out, and an idle pin's volumes were mounted by
    nothing before the migration, so nothing will mount them after it. Taken
    from the pins themselves rather than from QUIESCE's journalled capture,
    which a resumed QUIESCE overwrites with an empty one.
    """
    return {v for pin in held if not pin.idle for v in pin.volumes if not is_anonymous(v)}


def is_reference(container: docker.Container) -> bool:
    """Does this container keep a volume referenced for good? PURE.

    Only a running non-pin. A pin is no reference — it may be released at any
    time — and neither is a stopped container: Coolify removes one at its next
    stop or rolling update, and before 4.4.4 its cleanup prunes stopped resource
    containers right before the volumes (v4.4.3:CleanupDocker.php:164,173).
    """
    return container.state == _REFERENCING and not is_pin(container)


def _labels(migration_id: str, side: str) -> dict[str, str]:
    return {LABEL_ROLE: ROLE_PIN, LABEL_MIGRATION: migration_id, LABEL_SIDE: side}


# ── IO ───────────────────────────────────────────────────────────────────────


async def _our_pins(host: RemoteHost, *, migration_id: str, side: str) -> list[docker.Container]:
    # The label filter narrows; is_pin decides. A filter that silently did not
    # apply must never cost a Coolify container.
    return [
        c
        for c in await docker.list_containers(host, label_filters=_labels(migration_id, side))
        if is_pin(c, migration_id=migration_id, side=side)
    ]


async def list_pins(host: RemoteHost, *, migration_id: str, side: str) -> list[Pin]:
    """This migration's pins on one side, with the volumes each holds."""
    out: list[Pin] = []
    for container in await _our_pins(host, migration_id=migration_id, side=side):
        mounts = await docker.inspect_mounts(host, container.id or container.name)
        out.append(
            Pin(
                name=container.name,
                volumes=frozenset(m.name for m in mounts if m.type == "volume" and m.name),
                idle=container.labels.get(LABEL_HOLD) == HOLD_IDLE,
            )
        )
    return out


async def ensure(
    host: RemoteHost,
    *,
    migration_id: str,
    side: str,
    volumes: Iterable[str],
    idle: Iterable[str] = (),
) -> list[str]:
    """Pin every one of ``volumes`` and ``idle`` that exists. Returns all our pins' names.

    ``idle``: volumes nothing mounted before the migration. They get a pin of
    their own, labelled so a release does not wait for a remount that never
    comes (:func:`awaited`). A volume in both is not idle.

    Idempotent and additive: volumes an existing pin already holds are skipped,
    and the rest get ONE new pin per kind. Never replaces a pin, so there is no
    moment in which a volume is unpinned.

    A volume absent from ``docker volume ls`` is skipped with a warning, never
    mounted: ``--mount`` of a missing volume CREATES it, and a pin must never
    create a volume.
    """
    mounted = {v for v in volumes if v}
    wanted = mounted | {v for v in idle if v}
    if not wanted:
        return []

    present = {v.name for v in await docker.list_volumes(host)}
    absent = sorted(wanted - present)
    if absent:
        log.warning("pins.volume_absent", side=side, volumes=absent)

    held = await list_pins(host, migration_id=migration_id, side=side)
    covered = {v for pin in held for v in pin.volumes}
    todo = (wanted & present) - covered
    names = [pin.name for pin in held]
    for group, is_idle in ((todo & mounted, False), (todo - mounted, True)):
        if group:
            names.append(await _create(host, migration_id, side, sorted(group), idle=is_idle))
    return names


async def _create(
    host: RemoteHost, migration_id: str, side: str, volumes: list[str], *, idle: bool
) -> str:
    name = pin_name(migration_id, side, volumes)
    command = create_command(
        name=name, migration_id=migration_id, side=side, volumes=volumes, idle=idle
    )
    result = await host.run(command)
    if not result.ok and "no such image" in (result.stderr + result.stdout).lower():
        # Coolify's image prune removed the freshly imported, still-unused image
        # between our import and our create. Re-import and create once more.
        log.info("pins.image_pruned_retrying", side=side)
        result = await host.run(command)
    result.check()

    log.info("pins.created", side=side, pin=name, volumes=volumes, idle=idle)
    return name


async def release(
    host: RemoteHost, *, migration_id: str, side: str, only: Iterable[str] | None = None
) -> list[str]:
    """Remove this migration's pins on one side. Returns their names; [] did nothing.

    ``only`` restricts it to the pins of those names. ``docker rm`` alone: no
    ``-v`` (removing a pin must never remove a volume) and no ``-f`` (a pin
    never runs). A pin already gone counts as removed.
    """
    names = None if only is None else set(only)
    removed: list[str] = []
    for container in await _our_pins(host, migration_id=migration_id, side=side):
        if names is not None and container.name not in names:
            continue
        result = await host.run(f"docker rm {shlex.quote(container.name)}")
        if not result.ok and "no such container" not in result.stderr.lower():
            result.check()
        removed.append(container.name)
    if removed:
        log.info("pins.released", side=side, pins=removed)
    return removed


async def unreferenced(host: RemoteHost, volumes: Iterable[str]) -> set[str]:
    """The volumes no RUNNING container other than a pin mounts (:func:`is_reference`).

    Any pin counts as no reference, ours or another migration's: the question is
    whether the volume survives once OUR pin is gone, and another migration may
    release its own at any time. A probe that fails counts as unreferenced —
    the error falls toward keeping the pin, never toward losing the data.
    """
    out: set[str] = set()
    for volume in sorted(set(volumes)):
        users = await docker.containers_mounting(host, volumes=[volume])
        if not any(is_reference(c) for c in users):
            out.add(volume)
    return out


async def release_when_referenced(
    host: RemoteHost,
    *,
    migration_id: str,
    side: str,
    timeout: float,
    settle: float = 0.0,
    interval: float = 5.0,
) -> Release:
    """Release our pins once running containers keep every awaited volume mounted.

    Idle pins go at once (:func:`awaited`). The rest only when every awaited
    volume has been mounted by a running container in each look across
    ``settle`` seconds — a container seen once is no evidence it stays — the
    first such look coming within ``timeout``. ``timeout=0, settle=0`` asks
    once. The pins are kept, with a warning, when the evidence never holds: an
    unmounted volume without its pin is exactly what the cleanup deletes.
    """
    held = await list_pins(host, migration_id=migration_id, side=side)
    if not held:
        return Release()

    idle_names = [pin.name for pin in held if pin.idle]
    idle = (
        await release(host, migration_id=migration_id, side=side, only=idle_names)
        if idle_names
        else []
    )
    waiting = [pin for pin in held if not pin.idle]
    need = awaited(waiting)
    waited = 0.0
    steady_since: float | None = None
    while waiting:
        missing = await unreferenced(host, need) if need else set()
        if missing:
            steady_since = None
        elif steady_since is None:
            steady_since = waited
        # Nothing awaited (anonymous volumes only): nothing to watch settle.
        if steady_since is not None and (not need or waited - steady_since >= settle):
            names = [pin.name for pin in waiting]
            return Release(
                released=(
                    *idle,
                    *await release(host, migration_id=migration_id, side=side, only=names),
                )
            )
        if steady_since is None and waited >= timeout:
            kept = tuple(sorted(pin.name for pin in waiting))
            log.warning(
                "pins.kept",
                side=side,
                pins=list(kept),
                unmounted=sorted(missing),
                hint=f"docker rm {' '.join(kept)} once these volumes are mounted again",
            )
            return Release(released=tuple(idle), kept=kept, unmounted=tuple(sorted(missing)))
        await asyncio.sleep(interval)
        waited += interval
    return Release(released=tuple(idle))


async def release_idle(host: RemoteHost, *, migration_id: str, side: str) -> Release:
    """Release only the idle pins; keep every pin awaiting a remount, without a look.

    For when no look could be evidence: a stop may still be queued, and
    nothing restarted the stack — a container seen running now may be the one
    that stop removes next. ``unmounted`` names every volume the kept pins
    await.
    """
    held = await list_pins(host, migration_id=migration_id, side=side)
    idle_names = [pin.name for pin in held if pin.idle]
    released = (
        await release(host, migration_id=migration_id, side=side, only=idle_names)
        if idle_names
        else []
    )
    waiting = [pin for pin in held if not pin.idle]
    return Release(
        released=tuple(released),
        kept=tuple(sorted(pin.name for pin in waiting)),
        unmounted=tuple(sorted(awaited(waiting))),
    )
