"""rsync command construction and execution.

The command builder is PURE and exhaustively tested; execution is a thin shell.

Flag rationale — every one of these is a bug in coolify-mover, which uses
``rsync -avz --progress`` and nothing else:

``-a``      archive: recurse, symlinks, perms, times, group, owner, devices.
``-H``      **hardlinks.** Without it a hardlinked tree is exploded into
            duplicate files: link identity lost, size inflated.
``-A``      ACLs. Dropped silently otherwise.
``-X``      xattrs — SELinux labels, file capabilities. Dropped silently otherwise.
``-S``      sparse files stay sparse. A sparse DB file can otherwise inflate to
            its apparent size and fill the target's disk.
``--numeric-ids``
            **The critical one.** Docker volume files are owned by *container*
            UIDs — postgres/mysql/redis are 999, clickhouse is 101. Without this,
            rsync maps ownership BY NAME through the remote host's passwd
            database; if uid 999 is some unrelated user there (or absent), the
            files land with wrong ownership and the database will not start.
``--delete``
            Makes a re-run idempotent. Without it a retry merges into whatever a
            failed attempt left behind.
``--partial``
            Keeps partial files so a resumed transfer does not restart from zero.
``--info=progress2``
            Machine-readable aggregate progress for the Rich dashboard.

And one flag we must **never** add: anything that chowns. Coolify's own
``VolumeCloneJob`` does ``chown -R 1000:1000 /target`` after copying, which is
precisely why its volume cloning got disabled — it corrupts every database volume
it touches.

``-z`` is deliberately off by default: server-to-server links are usually fast
and volume data is usually already compressed, so it burns CPU for nothing. It is
available for genuinely slow WAN links.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from bg_coolify_migrate.errors import TransferError
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: The non-negotiable core. See the module docstring for why each one is here.
BASE_FLAGS: tuple[str, ...] = (
    "-a",
    "-H",
    "-A",
    "-X",
    "-S",
    "--numeric-ids",
    "--delete",
    "--partial",
)

#: Emitted by `--info=progress2`, e.g.
#: "  1,234,567  45%   12.34MB/s    0:00:12  (xfr#12, to-chk=100/200)"
_PROGRESS_RE = re.compile(
    r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\S+)(?:\s+\(xfr#(\d+),\s+to-chk=(\d+)/(\d+)\))?"
)


@dataclass(frozen=True, slots=True)
class Progress:
    """One progress sample parsed from rsync's output."""

    bytes_done: int
    percent: int
    rate: str
    eta: str
    files_done: int | None = None
    files_left: int | None = None
    files_total: int | None = None


@dataclass(frozen=True, slots=True)
class RsyncSpec:
    """Everything needed to build one rsync command line."""

    source_path: str
    """Absolute path on the SOURCE. A trailing slash is added automatically."""
    target_path: str
    """Absolute path on the TARGET."""
    target_host: str
    target_user: str = "root"
    target_port: int = 22
    identity_file: str | None = None
    """Path to a private key ON THE SOURCE."""
    paths: Sequence[str] = (".",)
    """Relative paths to include, from :mod:`.partition`. ``('.',)`` = whole tree."""
    compress: bool = False
    dry_run: bool = False
    checksum: bool = False
    """Force a full checksum comparison. Used for the verification pass, where
    the expected result is 'no output at all'."""
    itemize: bool = False
    known_hosts_file: str | None = None
    bandwidth_limit_kbps: int | None = None


def build_ssh_option(spec: RsyncSpec) -> str:
    """The ``-e`` argument.

    Host key checking is NEVER disabled. If we have no known_hosts to offer we
    use `accept-new`, which still refuses a *changed* key — unlike
    `StrictHostKeyChecking=no`, which accepts anything, forever, and is what both
    predecessor tools use.
    """
    parts = ["ssh", "-p", str(spec.target_port)]
    if spec.identity_file:
        parts += ["-i", spec.identity_file, "-o", "IdentitiesOnly=yes"]
    if spec.known_hosts_file:
        parts += [
            "-o",
            f"UserKnownHostsFile={spec.known_hosts_file}",
            "-o",
            "StrictHostKeyChecking=yes",
        ]
    else:
        parts += ["-o", "StrictHostKeyChecking=accept-new"]
    parts += ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=5"]
    return " ".join(parts)


def build_command(spec: RsyncSpec) -> str:
    """Build the rsync command to run **on the source host**.

    PURE. Returns a single shell-safe string.

    The trailing slash on the source is load-bearing: ``rsync /a /b`` creates
    ``/b/a``, whereas ``rsync /a/ /b`` copies a's *contents* into b. We always
    want the latter — we are mirroring a volume's contents, not nesting it.
    """
    flags = list(BASE_FLAGS)
    if spec.compress:
        flags.append("-z")
    if spec.dry_run:
        flags.append("--dry-run")
    if spec.checksum:
        flags.append("--checksum")
    if spec.itemize:
        flags.append("--itemize-changes")
    else:
        flags.append("--info=progress2")
    if spec.bandwidth_limit_kbps:
        flags.append(f"--bwlimit={spec.bandwidth_limit_kbps}")

    source = spec.source_path.rstrip("/") + "/"
    target = spec.target_path.rstrip("/") + "/"

    parts = ["rsync", *flags]

    # Restrict to the planned chunk. `--files-from` reads relative paths and
    # implies --relative, so directories keep their structure under the target.
    if tuple(spec.paths) != (".",):
        listing = "\n".join(spec.paths)
        parts += ["--files-from=-", "--relative"]
        cmd = (
            f"printf '%s\\n' {shlex.quote(listing)} | "
            + " ".join(shlex.quote(p) if " " in p else p for p in parts)
            + f" -e {shlex.quote(build_ssh_option(spec))}"
            + f" {shlex.quote(source)}"
            + f" {shlex.quote(f'{spec.target_user}@{spec.target_host}:{target}')}"
        )
        return cmd

    parts += ["-e", build_ssh_option(spec)]
    parts += [source, f"{spec.target_user}@{spec.target_host}:{target}"]
    return " ".join(shlex.quote(p) for p in parts)


def parse_progress(line: str) -> Progress | None:
    """Parse one ``--info=progress2`` line. Returns ``None`` for anything else."""
    match = _PROGRESS_RE.match(line)
    if not match:
        return None
    return Progress(
        bytes_done=int(match.group(1).replace(",", "")),
        percent=int(match.group(2)),
        rate=match.group(3),
        eta=match.group(4),
        files_done=int(match.group(5)) if match.group(5) else None,
        files_left=int(match.group(6)) if match.group(6) else None,
        files_total=int(match.group(7)) if match.group(7) else None,
    )


async def preflight(host: RemoteHost, *, label: str) -> None:
    """Assert rsync exists on a host.

    Raises:
        TransferError: If rsync is missing. We check both ends before stopping
            anything, because discovering it after the source is down converts a
            preflight failure into an outage.
    """
    if not await host.which("rsync"):
        raise TransferError(
            f"rsync is not installed on the {label} server ({host.target.host})",
            hint="Install it: apt-get install -y rsync  /  apk add rsync",
        )


async def run(
    host: RemoteHost,
    spec: RsyncSpec,
    *,
    timeout: float | None = None,
) -> str:
    """Run rsync on ``host``. Returns stdout.

    Raises:
        TransferError: On a non-zero exit, with rsync's own diagnostics.
    """
    command = build_command(spec)
    log.info(
        "rsync.start",
        source=spec.source_path,
        target=f"{spec.target_host}:{spec.target_path}",
        paths=len(spec.paths),
        dry_run=spec.dry_run,
    )
    result = await host.run(command, timeout=timeout)
    if not result.ok:
        raise TransferError(
            f"rsync failed (exit {result.exit_status}) copying {spec.source_path}",
            hint=(result.stderr or result.stdout).strip()[:600] or None,
        )
    return result.stdout


async def verify_identical(
    host: RemoteHost,
    spec: RsyncSpec,
    *,
    timeout: float | None = None,
) -> list[str]:
    """A checksum dry-run that must produce no output.

    ``--checksum --dry-run --itemize-changes`` re-reads both sides and lists
    anything that still differs. An empty result is a positive proof that the
    two trees match by content — not merely by size and mtime, which is all a
    normal rsync guarantees.

    Returns:
        The itemised differences. **Empty means verified.**
    """
    verify_spec = RsyncSpec(
        source_path=spec.source_path,
        target_path=spec.target_path,
        target_host=spec.target_host,
        target_user=spec.target_user,
        target_port=spec.target_port,
        identity_file=spec.identity_file,
        known_hosts_file=spec.known_hosts_file,
        paths=(".",),
        dry_run=True,
        checksum=True,
        itemize=True,
    )
    output = await run(host, verify_spec, timeout=timeout)
    return [
        line
        for line in output.splitlines()
        if line.strip() and not line.startswith(("sending ", "sent ", "total size"))
    ]
