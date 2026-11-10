"""Bind mounts on the servers: what a host path holds, who mounts it, the writes.

IO shell; every decision lives in ``domain/binds.py``. Paths always reach the
shell as quoted ARGUMENTS of a fixed script, never spliced into one: a bind
source comes from a compose file someone else wrote.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence

import structlog

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.errors import TransferError
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: One state per argument, in order (domain/binds.STATES). Read-only. A
#: directory ``ls`` cannot read counts as holding data, never as empty — the
#: same rule as LocalFileVolume's REMOTE_FILE_STATE_SCRIPT (v4.7.2:53-73).
STATE_SCRIPT = r"""for p in "$@"; do
if [ -L "$p" ]; then s=symlink
elif [ -f "$p" ]; then if [ -s "$p" ]; then s=file; else s=empty-file; fi
elif [ -d "$p" ]; then s=directory
  if e=$(ls -A "$p" 2>/dev/null); then [ -n "$e" ] || s=empty-directory; fi
elif [ -e "$p" ]; then s=other
else s=missing; fi
printf '%s\n' "$s"
done"""

#: Every container's identity and mounts, one JSON object per line.
_INSPECT_FORMAT = (
    '{"name":{{json .Name}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}'
)


async def path_states(host: RemoteHost, paths: Sequence[str]) -> dict[str, str]:
    """``path -> state`` for each path, in one command.

    Raises:
        TransferError: When the host does not answer one state per path — a
            guess here would be a write into, or a delete of, the unknown.
    """
    unique = list(dict.fromkeys(paths))
    if not unique:
        return {}
    command = (
        "sh -c " + shlex.quote(STATE_SCRIPT) + " sh " + " ".join(shlex.quote(p) for p in unique)
    )
    result = await host.run(command)
    states = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not result.ok or len(states) != len(unique) or not set(states) <= binds.STATES:
        raise TransferError(
            f"could not read the state of {len(unique)} path(s) on {host.target.host}",
            hint=(result.stderr or "").strip()[:300] or None,
        )
    return dict(zip(unique, states, strict=True))


def parse_host_binds(stdout: str) -> list[binds.HostBind]:
    """Every bind mount in ``docker inspect`` output of :data:`_INSPECT_FORMAT`. PURE."""
    out: list[binds.HostBind] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").lstrip("/")
        raw_labels = entry.get("labels")
        labels = (
            {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, dict) else {}
        )
        for mount in entry.get("mounts") or []:
            if isinstance(mount, dict) and mount.get("Type") == "bind":
                out.append(
                    binds.HostBind(
                        container=name,
                        source=str(mount.get("Source") or ""),
                        rw=bool(mount.get("RW", True)),
                        labels=labels,
                    )
                )
    return out


async def host_binds(host: RemoteHost) -> list[binds.HostBind]:
    """Every bind mount of every container on the server, running or not.

    Raises:
        TransferError: When the daemon will not answer: "nobody else mounts
            it" must be a fact, not a silence.
    """
    result = await host.run(
        "docker ps -aq --no-trunc | xargs -r docker inspect --format "
        + shlex.quote(_INSPECT_FORMAT)
    )
    if not result.ok:
        raise TransferError(
            f"could not list the containers' mounts on {host.target.host}",
            hint=(result.stderr or "").strip()[:300] or None,
        )
    return parse_host_binds(result.stdout)


async def prepare_target(host: RemoteHost, path: str, *, kind: str, prior: str) -> None:
    """Make ``path`` ready to receive the copy: its directory (or parent) exists.

    A file replacing Coolify's empty default directory removes that directory
    first, with ``rmdir`` — which refuses anything that is not empty.
    """
    quoted = shlex.quote(path)
    if kind == binds.KIND_DIR:
        command = f"mkdir -p -- {quoted}"
    else:
        parent = shlex.quote(path.rsplit("/", 1)[0] or "/")
        command = f"mkdir -p -- {parent}"
        if prior == binds.STATE_EMPTY_DIR:
            command += f" && rmdir -- {quoted}"
    (await host.run(command)).check()


async def remove_created(
    host: RemoteHost, target: binds.BindTarget, *, removable: set[str]
) -> None:
    """Delete what COPY created for one target, deepest first.

    Only paths in ``removable`` (already judged by :func:`domain.binds.removable`)
    are touched: the target itself with ``rm -rf``, each parent it had to create
    with ``rmdir``, which leaves a directory someone else has since used.
    """
    if target.path in target.created and target.path in removable:
        (await host.run(f"rm -rf -- {shlex.quote(target.path)}")).check()
    for parent in reversed([p for p in target.created if p != target.path]):
        if parent in removable:
            await host.run(f"rmdir -- {shlex.quote(parent)} 2>/dev/null")
