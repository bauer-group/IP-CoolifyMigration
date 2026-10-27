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

SOURCE = "source"
TARGET = "target"

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


def create_command(*, name: str, migration_id: str, side: str, volumes: Iterable[str]) -> str:
    """One SSH round trip: import the empty image if absent, then create the pin. PURE.

    Never ``docker run`` and never ``docker start``: the ``/pin`` command only
    satisfies ``docker create`` and does not exist in the image, so the
    container could not start even if asked to.
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

    Every pinned volume except the anonymous ones: a container Coolify recreates
    gets a NEW anonymous volume, so waiting for the old one would always time
    out. Taken from the pins themselves rather than from QUIESCE's journalled
    capture, which a resumed QUIESCE overwrites with an empty one.
    """
    return {v for pin in held for v in pin.volumes if not is_anonymous(v)}


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
            )
        )
    return out


async def ensure(
    host: RemoteHost, *, migration_id: str, side: str, volumes: Iterable[str]
) -> list[str]:
    """Pin every one of ``volumes`` that exists. Returns the names of all our pins.

    Idempotent and additive: volumes an existing pin already holds are skipped,
    and the rest get ONE new pin. Never replaces a pin, so there is no moment in
    which a volume is unpinned.

    A volume absent from ``docker volume ls`` is skipped with a warning, never
    mounted: ``--mount`` of a missing volume CREATES it, and a pin must never
    create a volume.
    """
    wanted = {v for v in volumes if v}
    if not wanted:
        return []

    present = {v.name for v in await docker.list_volumes(host)}
    absent = sorted(wanted - present)
    if absent:
        log.warning("pins.volume_absent", side=side, volumes=absent)

    held = await list_pins(host, migration_id=migration_id, side=side)
    covered = {v for pin in held for v in pin.volumes}
    todo = sorted((wanted & present) - covered)
    names = [pin.name for pin in held]
    if not todo:
        return names

    name = pin_name(migration_id, side, todo)
    command = create_command(name=name, migration_id=migration_id, side=side, volumes=todo)
    result = await host.run(command)
    if not result.ok and "no such image" in (result.stderr + result.stdout).lower():
        # Coolify's image prune removed the freshly imported, still-unused image
        # between our import and our create. Re-import and create once more.
        log.info("pins.image_pruned_retrying", side=side)
        result = await host.run(command)
    result.check()

    log.info("pins.created", side=side, pin=name, volumes=todo)
    return [*names, name]


async def release(host: RemoteHost, *, migration_id: str, side: str) -> list[str]:
    """Remove this migration's pins on one side. Returns their names; [] did nothing.

    ``docker rm`` alone: no ``-v`` (removing a pin must never remove a volume)
    and no ``-f`` (a pin never runs). A pin already gone counts as removed.
    """
    removed: list[str] = []
    for container in await _our_pins(host, migration_id=migration_id, side=side):
        result = await host.run(f"docker rm {shlex.quote(container.name)}")
        if not result.ok and "no such container" not in result.stderr.lower():
            result.check()
        removed.append(container.name)
    if removed:
        log.info("pins.released", side=side, pins=removed)
    return removed


async def unreferenced(host: RemoteHost, volumes: Iterable[str]) -> set[str]:
    """The volumes no container other than a pin mounts.

    Any pin counts as no reference, ours or another migration's: the question is
    whether the volume survives once OUR pin is gone, and another migration may
    release its own at any time. A probe that fails counts as unreferenced —
    the error falls toward keeping the pin, never toward losing the data.
    """
    out: set[str] = set()
    for volume in sorted(set(volumes)):
        users = await docker.containers_mounting(host, volumes=[volume])
        if not any(not is_pin(c) for c in users):
            out.add(volume)
    return out


async def release_when_referenced(
    host: RemoteHost,
    *,
    migration_id: str,
    side: str,
    timeout: float,
    interval: float = 5.0,
) -> Release:
    """Release our pins once something else mounts every pinned volume.

    ``timeout=0`` asks once. The pins are kept, with a warning, when a volume
    is still unmounted at the end: an unmounted volume without its pin is
    exactly what the cleanup deletes.
    """
    held = await list_pins(host, migration_id=migration_id, side=side)
    if not held:
        return Release()

    need = awaited(held)
    waited = 0.0
    while True:
        missing = await unreferenced(host, need) if need else set()
        if not missing:
            return Release(
                released=tuple(await release(host, migration_id=migration_id, side=side))
            )
        if waited >= timeout:
            kept = tuple(sorted(pin.name for pin in held))
            log.warning(
                "pins.kept",
                side=side,
                pins=list(kept),
                unmounted=sorted(missing),
                hint=f"docker rm {' '.join(kept)} once these volumes are mounted again",
            )
            return Release(kept=kept, unmounted=tuple(sorted(missing)))
        await asyncio.sleep(interval)
        waited += interval
