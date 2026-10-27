"""The planner's reads for a resource's schedules. Reads only — never mutates.

Kept out of ``planner.build_plan`` so its loop calls one function: the reads are
fail-soft, and the decisions are ``domain/schedules.py``'s.

Fail-soft for the same reason as the planner's env read: an unreadable schedule
list is a worse plan, not a reason to refuse to plan. CREATE reads it again, and
fails loudly there — before the source is stopped.

The schedules themselves are never stored in the plan or the journal, only the
lines derived from them: a task's command can carry credentials (invariant 5).

Backup schedules produce warnings only. The bounds create enforces are the UI's
own, so a stray legacy value is near-impossible and 422s at CREATE with zero
downtime; a negative retention is normalised rather than refused (see
``api.schedules.build_backup_body``).
"""

from __future__ import annotations

from typing import Any

import structlog

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.fields import SCHEDULED_TASK_COLLECTIONS
from bg_coolify_migrate.domain import schedules
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from bg_coolify_migrate.domain.versions import Version

log = structlog.get_logger(__name__)


async def plan_schedules(
    api: CoolifyClient,
    snapshot: ResourceSnapshot,
    *,
    source: dict[str, Any] | None = None,
    source_server: dict[str, Any] | None,
    target_server: dict[str, Any] | None,
    version: Version | None,
) -> tuple[list[str], list[str]]:
    """``(warnings, refusals)`` for the schedules this resource will carry.

    ``source`` is the resource's own GET, already in the planner's hands; a
    service's databases are read off it.
    """
    warnings: list[str] = []
    refusals: list[str] = []

    if snapshot.collection == "databases":
        rows: list[dict[str, Any]] = []
        try:
            rows = await api.get_backups(snapshot.uuid)
        except Exception as exc:  # diagnostics only, never fatal
            log.debug("planner.backups_unavailable", uuid=snapshot.uuid, error=str(exc)[:120])
        warnings.extend(
            schedules.backup_warnings(
                rows,
                engine=snapshot.engine,
                source_timezone=schedules.server_timezone(source_server),
                target_timezone=schedules.server_timezone(target_server),
            )
        )
    elif snapshot.collection == "services" and source:
        warnings.extend(schedules.service_backup_warnings(source))

    if snapshot.collection in SCHEDULED_TASK_COLLECTIONS:
        tasks: list[dict[str, Any]] = []
        try:
            tasks = await api.get_scheduled_tasks(snapshot.collection, snapshot.uuid)
        except Exception as exc:  # diagnostics only, never fatal
            log.debug(
                "planner.scheduled_tasks_unavailable", uuid=snapshot.uuid, error=str(exc)[:120]
            )
        refusals.extend(schedules.scheduled_task_refusals(tasks, version=version))
        warnings.extend(
            schedules.scheduled_task_warnings(
                tasks,
                source_uuid=snapshot.uuid,
                source_server=source_server,
                target_server=target_server,
                version=version,
            )
        )

    return warnings, refusals
