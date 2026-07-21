"""BAUER GROUP Coolify migration toolkit.

Moves a Coolify project — with its data — between servers of one Coolify
instance (F1), and relocates a whole Coolify instance to a new host (F2).

Why this exists
---------------
Coolify can clone a resource to another server but deliberately will not move
the data: ``VolumeCloneJob`` and ``CloneMe``'s ``cloneVolumeData`` flag exist,
but upstream PR #4777 shipped them disabled. The maintainer's stated blockers —
permission damage, job-queue spam at 50+ resources, no progress tracking, and
large-volume failures — are all consequences of running inside Coolify's Laravel
queue. An external orchestrator has none of those constraints.

Design invariants (see AGENTS.md; do not weaken without an explicit decision)
----------------------------------------------------------------------------
* Application-unaware: a cleanly stopped stack makes a volume just bytes. No
  per-engine logic, no engine allowlist.
* Byte-exact: ``rsync -aHAXS --numeric-ids``. Never ``chown`` — Coolify's own
  clone hardcodes ``chown -R 1000:1000`` and that is precisely what corrupts
  postgres/mysql/redis (uid 999) and clickhouse (uid 101) volumes.
* REST API only. No SQL writes against ``coolify-db``.
* The source is never destroyed until an explicit, confirmed finalize step —
  which is what makes rollback cheap.

The public surface is imported lazily (PEP 562) so that ``import
bg_coolify_migrate`` to read ``__version__`` does not drag in asyncssh, httpx
and the whole Rich UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "2.6.1"

# Name -> submodule, resolved on first attribute access.
_LAZY: dict[str, str] = {
    "MigrationError": "bg_coolify_migrate.errors",
    "PreflightError": "bg_coolify_migrate.errors",
    "QuiesceError": "bg_coolify_migrate.errors",
    "TransferError": "bg_coolify_migrate.errors",
    "VerificationError": "bg_coolify_migrate.errors",
    "DnsGateBlocked": "bg_coolify_migrate.errors",
    "RebuildDriftBlocked": "bg_coolify_migrate.errors",
    "CoolifyApiError": "bg_coolify_migrate.errors",
    "InsufficientTokenScope": "bg_coolify_migrate.errors",
}

if TYPE_CHECKING:
    from bg_coolify_migrate.errors import CoolifyApiError as CoolifyApiError
    from bg_coolify_migrate.errors import DnsGateBlocked as DnsGateBlocked
    from bg_coolify_migrate.errors import InsufficientTokenScope as InsufficientTokenScope
    from bg_coolify_migrate.errors import MigrationError as MigrationError
    from bg_coolify_migrate.errors import PreflightError as PreflightError
    from bg_coolify_migrate.errors import QuiesceError as QuiesceError
    from bg_coolify_migrate.errors import RebuildDriftBlocked as RebuildDriftBlocked
    from bg_coolify_migrate.errors import TransferError as TransferError
    from bg_coolify_migrate.errors import VerificationError as VerificationError


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted([*_LAZY, "__version__"])


__all__ = [
    "CoolifyApiError",
    "DnsGateBlocked",
    "InsufficientTokenScope",
    "MigrationError",
    "PreflightError",
    "QuiesceError",
    "RebuildDriftBlocked",
    "TransferError",
    "VerificationError",
    "__version__",
]
