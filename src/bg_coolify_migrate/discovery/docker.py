"""Docker introspection over SSH.

IO shell; the meaning of what we find lives in ``domain/manifest.py``.

This is where "application-unaware" becomes concrete. We never ask *what* a
container is — we ask the daemon what it mounts and whether it is stopped. A
Postgres, a ClickHouse and a service nobody has ever heard of are all the same
question once they are not running.

Coolify labels every container it manages (``bootstrap/helpers/docker.php``)::

    coolify.managed=true
    coolify.type=application|service|database
    coolify.applicationId / coolify.serviceId / coolify.databaseId
    coolify.projectName / coolify.environmentName   (Str::slug'd)
    coolify.pullRequestId    (0 = base deploy, anything else = a preview)

and queries them itself with ``docker ps -a --filter=label=...``. Note the
**-a**: Geczy's script uses bare ``docker ps``, which is why a stopped
container's volume is silently skipped and never even reported. We match
Coolify's own behaviour.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

import structlog

from bg_coolify_migrate.domain.manifest import DockerMount, DockerVolume
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

LABEL_MANAGED = "coolify.managed"
LABEL_TYPE = "coolify.type"
LABEL_PROJECT = "coolify.projectName"
LABEL_ENVIRONMENT = "coolify.environmentName"
LABEL_PR_ID = "coolify.pullRequestId"


@dataclass(frozen=True, slots=True)
class Container:
    """One container as Docker reports it."""

    id: str
    name: str
    state: str
    """created | restarting | running | removing | paused | exited | dead"""
    labels: dict[str, str]
    exit_code: int | None = None

    @property
    def is_stopped(self) -> bool:
        return self.state in ("exited", "created", "dead")

    @property
    def pull_request_id(self) -> int:
        raw = self.labels.get(LABEL_PR_ID, "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    @property
    def is_preview(self) -> bool:
        """A preview deployment.

        Matters enormously: ``POST /applications/{uuid}/stop`` does NOT stop
        these (``StopApplication::dispatch($app, false, ...)`` filters
        ``pullRequestId=0``), so after a "successful" stop they keep running and
        keep writing.
        """
        return self.pull_request_id != 0

    @property
    def was_killed(self) -> bool:
        """Exited with 137 (SIGKILL) — i.e. the stop timeout was hit.

        A SIGKILLed database has not flushed. Treated as a hard failure, never a
        warning: a torn snapshot is exactly what we exist to prevent.
        """
        return self.exit_code == 137


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse docker's comma-separated ``k=v`` label string.

    Values may themselves contain ``=`` (Traefik rules do), so split once only.
    A label containing a literal comma cannot be represented by docker's own
    format, so there is nothing smarter to do here.
    """
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        out[key.strip()] = value
    return out


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            log.warning("docker.unparseable_line", line=line[:120])
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


async def list_containers(
    host: RemoteHost,
    *,
    label_filters: dict[str, str],
) -> list[Container]:
    """Containers matching ALL label filters, **including stopped ones**.

    Args:
        host: Where to look.
        label_filters: e.g. ``{"coolify.projectName": "shop"}``.
    """
    filters = " ".join(
        f"--filter {shlex.quote(f'label={k}={v}')}" for k, v in sorted(label_filters.items())
    )
    # -a is load-bearing: without it, stopped containers' volumes vanish.
    result = await host.run_checked(f"docker ps -a {filters} --format '{{{{json .}}}}'")

    containers: list[Container] = []
    for entry in _json_lines(result.stdout):
        containers.append(
            Container(
                id=str(entry.get("ID", "")),
                name=str(entry.get("Names", "")),
                state=str(entry.get("State", "")).lower(),
                labels=_parse_labels(str(entry.get("Labels", ""))),
            )
        )
    return containers


async def inspect_state(host: RemoteHost, container: str) -> tuple[str, int | None]:
    """``(state, exit_code)`` for one container.

    ``docker ps`` does not report the exit code, and we need it to distinguish a
    clean stop from a SIGKILL at the stop timeout.
    """
    result = await host.run_checked(
        f"docker inspect --format '{{{{json .State}}}}' {shlex.quote(container)}"
    )
    try:
        state = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return "unknown", None
    return str(state.get("Status", "unknown")).lower(), state.get("ExitCode")


async def inspect_mounts(host: RemoteHost, container: str) -> list[DockerMount]:
    """Every mount of one container — the authoritative answer.

    Includes anonymous volumes and bind mounts, which the Coolify API cannot
    show us. coolify-mover reads only Coolify's DB records and therefore never
    sees these; its rsync only ever touches
    ``/var/lib/docker/volumes/{name}/_data``, so a bind-mounted resource has its
    row copied and its data silently left behind.
    """
    result = await host.run_checked(
        f"docker inspect --format '{{{{json .Mounts}}}}' {shlex.quote(container)}"
    )
    try:
        entries = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        log.warning("docker.mounts.unparseable", container=container)
        return []

    mounts: list[DockerMount] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mounts.append(
            DockerMount(
                container=container,
                type=str(entry.get("Type", "")),
                name=entry.get("Name") or None,
                source=str(entry.get("Source", "")),
                destination=str(entry.get("Destination", "")),
                rw=bool(entry.get("RW", True)),
            )
        )
    return mounts


async def list_volumes(host: RemoteHost, *, name_filter: str | None = None) -> list[DockerVolume]:
    """All docker volumes. Used only to surface orphans."""
    flt = f"--filter {shlex.quote(f'name={name_filter}')}" if name_filter else ""
    result = await host.run_checked(f"docker volume ls {flt} --format '{{{{json .}}}}'")
    volumes: list[DockerVolume] = []
    for entry in _json_lines(result.stdout):
        volumes.append(
            DockerVolume(
                name=str(entry.get("Name", "")),
                driver=str(entry.get("Driver", "local")),
                labels=_parse_labels(str(entry.get("Labels", "") or "")),
            )
        )
    return volumes


async def path_size(host: RemoteHost, path: str) -> tuple[int, int]:
    """``(bytes, file_count)`` for a path.

    Sizes are needed for a *proportional* disk check on the target. Geczy checks
    against a fixed 1 GB floor and never compares to the total it just computed —
    which is how a 100 GB instance sails through preflight and dies mid-transfer.
    """
    quoted = shlex.quote(path)
    result = await host.run(f"du -sk {quoted} 2>/dev/null | awk '{{print $1}}'")
    size_kb = int(result.stdout.strip() or 0) if result.ok else 0
    count = await host.run(f"find {quoted} -type f 2>/dev/null | wc -l")
    files = int(count.stdout.strip() or 0) if count.ok else 0
    return size_kb * 1024, files


async def volume_exists(host: RemoteHost, name: str) -> bool:
    return (await host.run(f"docker volume inspect {shlex.quote(name)} >/dev/null 2>&1")).ok


async def create_volume(host: RemoteHost, name: str) -> None:
    await host.run_checked(f"docker volume create {shlex.quote(name)}")


async def remove_volume(host: RemoteHost, name: str) -> None:
    """Remove a volume. Used as a compensating action, so it must be tolerant."""
    result = await host.run(f"docker volume rm {shlex.quote(name)}")
    if not result.ok:
        log.warning("docker.volume.rm_failed", volume=name, stderr=result.stderr[:200])


async def image_of(host: RemoteHost, container: str) -> str | None:
    """The image a container runs, e.g. ``{uuid}:{commit_sha}``.

    The ONLY trustworthy record of the deployed commit: Coolify composes the tag
    as ``str($this->commit)->substr(0, 128)``, so the tag IS the commit by
    construction. ``applications.git_commit_sha`` is not updated by a normal
    deploy, and ``SOURCE_COMMIT`` is user-overridable and falls back to the
    literal string ``'unknown'``.
    """
    result = await host.run(
        f"docker inspect --format '{{{{.Config.Image}}}}' {shlex.quote(container)}"
    )
    return result.stdout.strip() or None if result.ok else None


async def container_labels(host: RemoteHost, container: str) -> dict[str, str]:
    """All labels of one container.

    Used to recover settings the API cannot read: ``is_force_https_enabled``,
    ``is_gzip_enabled`` and ``is_stripprefix_enabled`` all manifest as Traefik
    labels, so Docker can prove what the API will not tell us.
    """
    result = await host.run(
        f"docker inspect --format '{{{{json .Config.Labels}}}}' {shlex.quote(container)}"
    )
    if not result.ok:
        return {}
    try:
        parsed = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
