"""Parallel transfer planning.

PURE module: no IO.

The user asked for multithreaded copying, and the naive implementation —
"split the tree into N buckets and run N rsyncs" — has a correctness trap that is
silent and expensive:

**``rsync -H`` only detects hardlinks WITHIN a single invocation.** Split a tree
across N rsyncs and a hardlink whose partners land in different chunks is written
as two independent full copies. Link identity is lost and the volume can grow
enormously — a backup volume using hardlinked snapshots (rsnapshot, Borg caches,
Time-Machine-style trees) can go from 50 GB to hundreds.

So: if the tree contains any hardlinked file, we transfer it as **one** chunk.
Slower, correct. Parallelism is an optimisation; correctness is not.

For everything else we bin-pack top-level entries by size using LPT (longest
processing time first), which is a 4/3-approximation of optimal makespan and is
more than good enough when the input is "12 directories of wildly different
sizes".
"""

from __future__ import annotations

from dataclasses import dataclass

#: Above this, one entry is worth its own rsync even if it unbalances the plan.
#: Below it, per-process overhead dominates and batching wins.
_LARGE_ENTRY_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PathEntry:
    """One top-level entry inside a volume."""

    relpath: str
    """Relative to the volume root, e.g. ``base/`` or ``pg_wal/``."""
    bytes: int


@dataclass(frozen=True, slots=True)
class Chunk:
    """One rsync invocation's worth of work."""

    paths: tuple[str, ...]
    bytes: int

    @property
    def is_whole_tree(self) -> bool:
        """True if this chunk is the entire volume (the hardlink-safe case)."""
        return self.paths == (".",)


@dataclass(frozen=True, slots=True)
class TransferPlan:
    """How one volume will be copied."""

    chunks: tuple[Chunk, ...]
    reason: str
    """Why this shape — surfaced in the UI so a single-chunk plan does not look
    like a bug to someone expecting parallelism."""

    @property
    def parallelism(self) -> int:
        return len(self.chunks)

    @property
    def is_split(self) -> bool:
        """True when the tree is copied as ``--files-from`` chunks.

        A split transfer never names the volume ROOT, so its metadata must be
        synced in a separate non-recursive pass (see
        :attr:`bg_coolify_migrate.transfer.rsync.RsyncSpec.dirs_only`). The
        whole-tree plan carries the root already.
        """
        return not (len(self.chunks) == 1 and self.chunks[0].is_whole_tree)

    @property
    def total_bytes(self) -> int:
        return sum(c.bytes for c in self.chunks)


def whole_tree(total_bytes: int, reason: str) -> TransferPlan:
    """A single-rsync plan covering the entire volume."""
    return TransferPlan(chunks=(Chunk(paths=(".",), bytes=total_bytes),), reason=reason)


def plan_transfer(
    entries: list[PathEntry],
    *,
    max_parallel: int = 4,
    has_hardlinks: bool = False,
    total_bytes: int | None = None,
) -> TransferPlan:
    """Plan how to copy one volume.

    Args:
        entries: Top-level entries with their sizes.
        max_parallel: Upper bound on concurrent rsync processes for this volume.
        has_hardlinks: Whether ``find -links +1`` found anything. If so the tree
            is transferred whole, because ``-H`` cannot span invocations.
        total_bytes: Size of the whole tree; defaults to the sum of ``entries``.

    Returns:
        A plan whose chunks are disjoint and jointly cover the tree.
    """
    total = total_bytes if total_bytes is not None else sum(e.bytes for e in entries)

    if has_hardlinks:
        return whole_tree(
            total,
            "hardlinks present: rsync -H only detects them within one invocation, so "
            "splitting would explode them into duplicate files",
        )

    if not entries:
        return whole_tree(total, "empty or unlistable tree")

    if max_parallel <= 1:
        return whole_tree(total, "parallelism disabled")

    if len(entries) == 1:
        return whole_tree(
            total,
            f"single top-level entry ({entries[0].relpath}): nothing to split across",
        )

    # LPT: place the largest remaining entry into the currently-emptiest bin.
    bins: list[list[PathEntry]] = [[] for _ in range(min(max_parallel, len(entries)))]
    sizes = [0] * len(bins)
    for entry in sorted(entries, key=lambda e: e.bytes, reverse=True):
        idx = sizes.index(min(sizes))
        bins[idx].append(entry)
        sizes[idx] += entry.bytes

    chunks = tuple(
        Chunk(paths=tuple(sorted(e.relpath for e in group)), bytes=size)
        for group, size in zip(bins, sizes, strict=True)
        if group
    )

    if len(chunks) == 1:
        return whole_tree(total, "all entries packed into a single chunk")

    return TransferPlan(
        chunks=chunks,
        reason=f"{len(chunks)} parallel streams over {len(entries)} top-level entries",
    )


def suggest_parallelism(*, entry_count: int, total_bytes: int, cap: int = 8) -> int:
    """A sensible default for concurrent rsyncs.

    Deliberately conservative. The bottleneck for a server-to-server copy is
    almost always the network or the target's disk, and past a handful of streams
    more processes just add seeks and context switches. We also never exceed the
    number of entries — an empty rsync is pure overhead.
    """
    if entry_count <= 1:
        return 1
    if total_bytes < _LARGE_ENTRY_BYTES:
        return 1
    return max(1, min(cap, entry_count, 4))
