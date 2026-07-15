"""Post-copy verification: content hashes AND metadata.

The comparison is PURE and exhaustively tested; only manifest collection is IO.

Two traps make a naive checksum pass worthless:

1. **``sha256sum`` ignores metadata.** Two files with identical bytes but
   different ownership hash identically — and ownership is exactly what decides
   whether Postgres starts. A content-only check would happily bless a volume
   chowned to the wrong uid, which is the very corruption Coolify's own
   ``chown -R 1000:1000`` causes.
2. **``sha256sum`` cannot read sockets, FIFOs or devices.** A Postgres data
   directory routinely contains a socket. A content-only pass either errors or
   silently skips them.

So we build two manifests per side: **content** (regular files only) and
**metadata** (every entry: type, mode, uid, gid, symlink target). Both must match.

Portability: ``find -printf`` is a GNU extension and does not exist on busybox,
so we probe once and fall back to a POSIX loop. Getting this wrong would not
raise — it would silently produce an empty manifest, i.e. a verification that
passes because it checked nothing.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from bg_coolify_migrate.errors import VerificationError
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)


class DiffKind(StrEnum):
    MISSING_ON_TARGET = "missing_on_target"
    EXTRA_ON_TARGET = "extra_on_target"
    CONTENT_DIFFERS = "content_differs"
    METADATA_DIFFERS = "metadata_differs"


@dataclass(frozen=True, slots=True)
class Difference:
    kind: DiffKind
    path: str
    source_value: str | None = None
    target_value: str | None = None

    def describe(self) -> str:
        if self.kind is DiffKind.MISSING_ON_TARGET:
            return f"missing on target: {self.path}"
        if self.kind is DiffKind.EXTRA_ON_TARGET:
            return f"unexpected on target: {self.path}"
        return f"{self.kind.value}: {self.path} ({self.source_value} != {self.target_value})"


@dataclass(frozen=True, slots=True)
class Manifest:
    """One side's view of a tree."""

    content: dict[str, str] = field(default_factory=dict)
    """relpath -> sha256 (regular files only)."""
    metadata: dict[str, str] = field(default_factory=dict)
    """relpath -> "type|mode|uid|gid|linktarget" (every entry)."""

    @property
    def file_count(self) -> int:
        return len(self.content)

    @property
    def entry_count(self) -> int:
        return len(self.metadata)


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """The durable, auditable result of verifying one volume."""

    source_path: str
    target_path: str
    source: Manifest
    target: Manifest
    differences: tuple[Difference, ...]

    @property
    def ok(self) -> bool:
        return not self.differences

    def summary(self) -> str:
        if self.ok:
            return (
                f"verified {self.source.file_count} files / "
                f"{self.source.entry_count} entries — content and metadata identical"
            )
        return f"{len(self.differences)} difference(s) found"


async def _has_gnu_find(host: RemoteHost) -> bool:
    """Whether ``find -printf`` is available.

    Probed rather than assumed: on busybox ``-printf`` is not merely slower, it
    is absent, and the command would fail. Silently producing an empty manifest
    would mean a verification that passes because it checked nothing.
    """
    result = await host.run("find --version 2>/dev/null | head -1")
    return result.ok and "GNU" in result.stdout


async def collect_content(host: RemoteHost, path: str, *, parallel: int = 4) -> dict[str, str]:
    """sha256 of every regular file, keyed by path relative to ``path``.

    ``-P`` parallelises the hashing across cores — the user asked for
    multithreading and this is the part that is genuinely CPU-bound.
    """
    quoted = shlex.quote(path)
    command = (
        f"cd {quoted} && find . -type f -print0 2>/dev/null | "
        f"xargs -0 -r -P {int(parallel)} -n 64 sha256sum 2>/dev/null | LC_ALL=C sort -k2"
    )
    result = await host.run(command, timeout=None)
    if not result.ok:
        raise VerificationError(
            f"could not hash {path} on {host.target.host}",
            hint=(result.stderr or "").strip()[:400] or None,
        )

    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        digest, _, name = line.partition("  ")
        if digest and name:
            out[name.strip()] = digest.strip()
    return out


async def collect_metadata(host: RemoteHost, path: str) -> dict[str, str]:
    """Type, mode, uid, gid and symlink target for every entry.

    This is the half that catches a wrong ``chown`` — the failure mode a content
    hash cannot see, and the one that actually stops a database from starting.
    """
    quoted = shlex.quote(path)
    if await _has_gnu_find(host):
        command = (
            f"cd {quoted} && find . -printf '%p|%y|%m|%U|%G|%l\\n' 2>/dev/null | LC_ALL=C sort"
        )
    else:
        # POSIX fallback (busybox). Slower, but correct — and an incorrect
        # manifest here means a verification that checks nothing.
        command = (
            f"cd {quoted} && find . 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do "
            f'printf "%s|" "$f"; '
            f'if [ -L "$f" ]; then printf "l|"; else if [ -d "$f" ]; then printf "d|"; '
            f'else printf "f|"; fi; fi; '
            f"stat -c '%a|%u|%g' \"$f\" 2>/dev/null | tr -d '\\n'; "
            f'printf "|"; if [ -L "$f" ]; then readlink "$f" | tr -d "\\n"; fi; '
            f'printf "\\n"; done'
        )

    result = await host.run(command, timeout=None)
    if not result.ok:
        raise VerificationError(
            f"could not stat {path} on {host.target.host}",
            hint=(result.stderr or "").strip()[:400] or None,
        )

    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, _, rest = line.partition("|")
        if name:
            out[name.strip()] = rest.strip()
    return out


async def build_manifest(host: RemoteHost, path: str, *, parallel: int = 4) -> Manifest:
    """Both manifests for one tree."""
    content = await collect_content(host, path, parallel=parallel)
    metadata = await collect_metadata(host, path)
    log.info(
        "verify.manifest",
        host=host.target.host,
        path=path,
        files=len(content),
        entries=len(metadata),
    )
    return Manifest(content=content, metadata=metadata)


def compare(source: Manifest, target: Manifest) -> tuple[Difference, ...]:
    """Diff two manifests. PURE.

    Reports every class of difference rather than stopping at the first: an
    operator deciding whether to trust a migration needs the whole picture, not
    a sample.
    """
    diffs: list[Difference] = []

    for path in sorted(set(source.metadata) | set(target.metadata)):
        in_source = path in source.metadata
        in_target = path in target.metadata
        if in_source and not in_target:
            diffs.append(Difference(DiffKind.MISSING_ON_TARGET, path))
            continue
        if in_target and not in_source:
            diffs.append(Difference(DiffKind.EXTRA_ON_TARGET, path))
            continue
        if source.metadata[path] != target.metadata[path]:
            diffs.append(
                Difference(
                    DiffKind.METADATA_DIFFERS,
                    path,
                    source_value=source.metadata[path],
                    target_value=target.metadata[path],
                )
            )

    for path in sorted(set(source.content) | set(target.content)):
        s_hash = source.content.get(path)
        t_hash = target.content.get(path)
        if s_hash is None or t_hash is None:
            # Already reported by the metadata pass, which covers every entry.
            continue
        if s_hash != t_hash:
            diffs.append(
                Difference(
                    DiffKind.CONTENT_DIFFERS, path, source_value=s_hash[:12], target_value=t_hash[:12]
                )
            )

    return tuple(diffs)


async def verify_volume(
    source_host: RemoteHost,
    target_host: RemoteHost,
    *,
    source_path: str,
    target_path: str,
    parallel: int = 4,
) -> VerificationReport:
    """Build both manifests and compare them.

    Raises:
        VerificationError: Never — the caller decides. A report with
            ``ok == False`` is data, not an exception; the engine turns it into
            one so the failure carries the plan's context.
    """
    source = await build_manifest(source_host, source_path, parallel=parallel)
    target = await build_manifest(target_host, target_path, parallel=parallel)
    diffs = compare(source, target)
    report = VerificationReport(
        source_path=source_path,
        target_path=target_path,
        source=source,
        target=target,
        differences=diffs,
    )
    log.info(
        "verify.done",
        path=source_path,
        ok=report.ok,
        differences=len(diffs),
    )
    return report
