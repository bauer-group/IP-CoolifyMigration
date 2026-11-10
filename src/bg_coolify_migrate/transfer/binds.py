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


#: Exit status of :data:`PREPARE_SCRIPT` and :data:`REMOVE_SCRIPT` when a
#: component of the path resolves somewhere else than the path says — a parent
#: (or, for a removal, the path itself) that is a symbolic link now.
EXIT_REDIRECTED = 64

#: ``$1`` path, ``$2`` kind (dir|file), ``$3`` its prior state. Creates the
#: directory (a dir bind) or the parent (a file bind), then PROVES it with
#: ``cd -P``: a parent swapped for a symlink since the last probe would send
#: every byte of the copy somewhere no check looked at. A file replacing
#: Coolify's empty default directory removes that directory with ``rmdir``,
#: which refuses anything that is not empty, relative to the proven parent.
PREPARE_SCRIPT = r"""p=$1; kind=$2; prior=$3
parent=${p%/*}; [ -n "$parent" ] || parent=/
if [ "$kind" = dir ]; then
  mkdir -p -- "$p" || exit 1
  cd -P -- "$p" || exit 1
  [ "$(pwd -P)" = "$p" ] || exit 64
else
  mkdir -p -- "$parent" || exit 1
  cd -P -- "$parent" || exit 1
  [ "$(pwd -P)" = "$parent" ] || exit 64
  if [ "$prior" = empty-directory ]; then
    [ -L "./${p##*/}" ] && exit 64
    rmdir -- "./${p##*/}" || exit 1
  fi
fi"""

#: ``$1`` mode (rm|rmdir), ``$2`` canonical path. Deletes RELATIVE to the parent
#: it first enters with ``cd -P`` and proves canonical: the working directory is
#: then held by inode, so an ancestor swapped for a symlink after that check
#: changes nothing, and one swapped before it fails the check. ``rm -rf`` given
#: a symlink removes the link, never its target; the explicit ``-L`` refusal
#: keeps a swapped path from passing as a deletion. ``--one-file-system``
#: (GNU coreutils) where available, so a mount someone placed inside the
#: copy is not descended into. A parent that no longer exists means nothing
#: is left to delete.
REMOVE_SCRIPT = r"""mode=$1; p=$2
parent=${p%/*}; [ -n "$parent" ] || parent=/
base=${p##*/}
cd -P -- "$parent" 2>/dev/null || exit 0
[ "$(pwd -P)" = "$parent" ] || exit 64
[ -L "./$base" ] && exit 64
if [ "$mode" = rmdir ]; then
  rmdir -- "./$base" 2>/dev/null || true
  exit 0
fi
if rm --help 2>&1 | grep -q -- --one-file-system; then
  rm -rf --one-file-system -- "./$base"
else
  rm -rf -- "./$base"
fi"""


def _script(script: str, *args: str) -> str:
    """A fixed script with its arguments quoted, never spliced into it. PURE."""
    return "sh -c " + shlex.quote(script) + " sh " + " ".join(shlex.quote(a) for a in args)


async def prepare_target(host: RemoteHost, path: str, *, kind: str, prior: str) -> None:
    """Make ``path`` ready to receive the copy: its directory (or parent) exists.

    Raises:
        TransferError: When a parent of ``path`` resolves elsewhere — a symlink
            planted since the last probe — or the directory cannot be made.
    """
    result = await host.run(_script(PREPARE_SCRIPT, path, kind, prior))
    if result.exit_status == EXIT_REDIRECTED:
        raise TransferError(
            f"a parent of the bind target {path} on {host.target.host} resolves somewhere "
            "else (a symbolic link appeared since it was checked); nothing is written there",
            hint="Find out who changed that path on the target server before resuming.",
        )
    result.check()


async def remove_created(
    host: RemoteHost, target: binds.BindTarget, *, removable: set[str]
) -> None:
    """Delete what COPY created for one target, deepest first.

    Only paths in ``removable`` (already judged by :func:`domain.binds.removable`)
    are touched: the target itself with ``rm -rf``, each parent it had to create
    with ``rmdir``, which leaves a directory someone else has since used. Both
    run through :data:`REMOVE_SCRIPT`, relative to a parent proven canonical.

    Raises:
        TransferError: When the target or a parent resolves somewhere else now;
            nothing is deleted, and the rollback keeps the path loudly.
    """
    if target.path in target.created and target.path in removable:
        result = await host.run(_script(REMOVE_SCRIPT, "rm", target.path))
        if result.exit_status == EXIT_REDIRECTED:
            raise TransferError(
                f"{target.path} on {host.target.host}, or one of its parents, is a symbolic "
                "link now; it was not deleted",
                hint="Find out who changed that path on the target server, then delete "
                "the copy by hand if it is still unwanted.",
            )
        result.check()
    for parent in reversed([p for p in target.created if p != target.path]):
        if parent in removable:
            result = await host.run(_script(REMOVE_SCRIPT, "rmdir", parent))
            if result.exit_status == EXIT_REDIRECTED:
                log.warning("binds.parent_redirected", path=parent)
