"""Carrying a resource's schedules to the target.

IO shell. What carries and what the plan reports is decided in
``domain/schedules.py``; this module reads the source, builds whitelisted bodies
and creates them on the target.

Runs inside CREATE_TARGET, before QUIESCE, and is FATAL — unlike copy_tags. A
lost cleanup cron or a database that silently stopped being backed up is a
regression nobody notices until the day it is needed, while failing here costs
zero downtime: the in-step undo deletes the target, and its schedules go with it
(Application::forceDeleting and DeleteService delete its tasks,
v4.5.0:Application.php:417-426, DeleteService.php:133-135; DeleteResourceJob
deletes a database's schedule rows, v4.1.2:94, v4.5.0:167).

The source's ``enabled`` is carried as it is. Creating disabled and arming at
START was considered and rejected: it is one more write after the source is down,
a journal field and a compensation — protecting nothing, because the target reads
status 'exited' until START and upstream skips it (see domain/schedules.py).

NEVER log a task's ``command`` or a backup execution's ``message``. Both carry
credentials often enough, and the redaction processor is key-based and knows
neither word. Names, frequencies and counts only. No S3 credential is ever read:
storages are shared on the one control plane and named by uuid.
"""

from __future__ import annotations

from typing import Any

import structlog

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.fields import (
    SCHEDULED_TASK_COLLECTIONS,
    SCHEDULED_TASK_REQUIRED,
    fields_for,
    filter_body,
    filter_for,
    missing_required,
)
from bg_coolify_migrate.domain import compose as compose_mod
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.schedules import (
    backup_s3_destinations,
    copy_count_mismatches,
    is_backupless,
    own_backup_schedules,
    task_container_mismatches,
)
from bg_coolify_migrate.domain.versions import Version
from bg_coolify_migrate.errors import CoolifyApiError, InsufficientTokenScope, MigrationError

log = structlog.get_logger(__name__)


# ── scheduled tasks ──────────────────────────────────────────────────────────


def build_scheduled_task_body(
    task: dict[str, Any], *, version: Version | None = None
) -> dict[str, Any]:
    """The create body for one source task. PURE.

    Built field by field, never by filtering the GET dict wholesale: that carries
    uuid and timestamps, and the create route 422s per unknown field. ``enabled``
    False survives (filter_body drops only None) — it has to, because upstream
    defaults an ABSENT enabled to true and a disabled task would come back armed.

    Raises:
        InsufficientTokenScope: The ``command`` key is absent. From 4.4 Coolify
            removes it from every task a request may not read sensitive data
            for, and a task without its command is not a task. Never dropped
            silently.
        MigrationError: The task lacks a name or frequency — it could only 422.
    """
    name = task.get("name")
    if "command" not in task:
        raise InsufficientTokenScope(
            f"scheduled task {name!r}: its command is not readable",
            hint=(
                "From Coolify 4.4 a scheduled task's command is returned only to a "
                "token that can read sensitive data: `root` or `read:sensitive`, held "
                "by an admin or owner of the team (see `coolify-migrate doctor`). "
                "Without it the target would get the task with no command at all."
            ),
        )
    body = filter_body(
        {
            "name": name,
            "command": task.get("command"),
            "frequency": task.get("frequency"),
            # `or None`: ScheduledTaskJob tests it with empty(), and '' would reach
            # the server as null anyway (ConvertEmptyStringsToNull).
            "container": task.get("container") or None,
            "timeout": task.get("timeout"),
            "enabled": task.get("enabled"),
        },
        fields_for("scheduled-tasks", version),
    )
    if missing := missing_required(body, SCHEDULED_TASK_REQUIRED):
        raise MigrationError(
            f"scheduled task {name!r} has no {', '.join(sorted(missing))}",
            hint="Coolify requires it on create. Fix or delete the task on the source, then migrate.",
        )
    return body


async def copy_scheduled_tasks(
    api: CoolifyClient,
    *,
    collection: str,
    source_uuid: str,
    target_uuid: str,
    kind: ResourceKind,
    version: Version | None = None,
) -> int:
    """Recreate the source's scheduled tasks on the target. Returns the count.

    Every body is built BEFORE the first POST, so an unreadable command fails
    while the target still holds none. After the POSTs the target is re-read and
    its tasks counted against the uuids the creates returned (see
    :func:`~bg_coolify_migrate.domain.schedules.copy_count_mismatches`): a doubled
    task would run every cron twice per tick, so it fails CREATE.

    Counted by uuid, never by name. The API trims every string it receives
    (TrimStrings is global middleware, Kernel.php v4.1.2:56, v4.5.0:61), while
    the UI's Add form saves a name untrimmed (Livewire skips that middleware), so
    a source task "nightly " comes back as "nightly" and a name count would call
    it doubled on every run.
    """
    if collection not in SCHEDULED_TASK_COLLECTIONS:
        return 0
    bodies = [
        build_scheduled_task_body(t, version=version)
        for t in await api.get_scheduled_tasks(collection, source_uuid)
    ]
    if not bodies:
        return 0

    created = [await api.create_scheduled_task(collection, target_uuid, body) for body in bodies]

    names = [str(b["name"]) for b in bodies]
    present = await api.get_scheduled_tasks(collection, target_uuid)
    if copy_count_mismatches(created, [str(t.get("uuid")) for t in present]):
        unexpected = sorted(str(t.get("name")) for t in present if t.get("uuid") not in created)
        raise CoolifyApiError(
            "the target's scheduled tasks do not match what was created "
            f"({len(present)} on the target, {len(created)} created"
            + (f"; not created by this run: {unexpected})" if unexpected else ")"),
            hint=(
                "A create that timed out at a proxy (502/504) after Coolify had saved "
                "it was retried and saved twice, or a created task was not reported "
                "back. A doubled task runs every cron twice. The source was never "
                "stopped; the created target will be deleted — re-run the migration."
            ),
        )
    log.info("api.scheduled_tasks.copied", count=len(bodies), names=names, target=target_uuid)

    await _warn_unmatched_containers(
        api, collection=collection, target_uuid=target_uuid, kind=kind, bodies=bodies
    )
    return len(bodies)


async def _warn_unmatched_containers(
    api: CoolifyClient,
    *,
    collection: str,
    target_uuid: str,
    kind: ResourceKind,
    bodies: list[dict[str, Any]],
) -> None:
    """Say so when a task's ``container`` names nothing the target runs.

    A warning, not a refusal, like copy_tags: the source cannot be edited to a
    name that exists only in the new template, so the operator fixes the task on
    the target. Never raises — a failed check costs the check, not the cutover.
    """
    if not any(b.get("container") for b in bodies):
        return
    try:
        available = await _target_container_names(
            api, collection=collection, target_uuid=target_uuid, kind=kind
        )
    except (CoolifyApiError, compose_mod.ComposeError) as exc:
        log.warning(
            "api.scheduled_tasks.container_check_failed", target=target_uuid, error=str(exc)[:200]
        )
        return
    if available is None:
        return
    if missing := task_container_mismatches(bodies, available):
        log.warning(
            "api.scheduled_tasks.container_missing",
            names=missing,
            available=sorted(available),
            target=target_uuid,
            hint=(
                "these tasks name a container the target does not run, so they will "
                "fail with 'No valid container was found'. Set each one's container "
                "to one of the available names on the target."
            ),
        )


async def _target_container_names(
    api: CoolifyClient, *, collection: str, target_uuid: str, kind: ResourceKind
) -> set[str] | None:
    """The names a task's ``container`` can match on the target, or None if unknowable.

    ScheduledTaskJob matches ``{name}-{resource uuid}`` against the running
    containers. For a service the names are its sub-applications' and
    sub-databases' (GET /services/{uuid} eager-loads both, v4.1.2:736,
    v4.5.0:885); for a dockercompose application they are the compose service
    keys, which exist once LoadComposeFile ran — CREATE's compose gate waits for
    exactly that. Any other application runs one container, and the field is moot.
    """
    if collection == "services":
        full = await api.get_resource("services", target_uuid)
        return {
            str(entry["name"])
            for key in ("applications", "databases")
            for entry in full.get(key) or []
            if isinstance(entry, dict) and entry.get("name")
        }
    if kind is ResourceKind.APP_GIT_COMPOSE:
        full = await api.get_resource("applications", target_uuid)
        raw = full.get("docker_compose_raw")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return set(compose_mod.services(compose_mod.parse(raw)))
    return None


# ── database backup schedules ────────────────────────────────────────────────

#: Retention acts only on values > 0, so a negative one — which the UI's
#: BackupEdit stores (v4.1.2:46-58, v4.5.0:61-74) and the API's min:0 refuses —
#: behaves exactly like 0, "keep all" (bootstrap/helpers/databases.php
#: v4.1.2:302-319, v4.5.0:344-369). Sent as 0: the same behaviour, accepted.
_RETENTION_FIELDS = (
    "database_backup_retention_amount_locally",
    "database_backup_retention_days_locally",
    "database_backup_retention_max_storage_locally",
    "database_backup_retention_amount_s3",
    "database_backup_retention_days_s3",
    "database_backup_retention_max_storage_s3",
)


def build_backup_body(row: dict[str, Any], *, version: Version | None = None) -> dict[str, Any]:
    """The create body for one source backup schedule. PURE.

    Values only, through the registry's ``backups`` fields for ``version``; never ``backup_now`` (it dumps at once,
    and needs ``deploy`` from 4.5). The S3 destinations are set from what the row
    DISCLOSES (4.5+), primary first, even with save_s3 off — that keeps the
    storage the operator preselected. A row that uploads to S3 without
    disclosing where (before 4.5, or a storage since deleted) is created with
    save_s3 off: sending it on would 422 the whole schedule, and the plan has
    already said "local-only" out loud.
    """
    body = filter_for(row, "backups", version)
    for field in _RETENTION_FIELDS:
        value = body.get(field)
        if isinstance(value, int | float) and not isinstance(value, bool) and value < 0:
            body[field] = 0
    if destinations := backup_s3_destinations(row):
        body["s3_storage_uuids"] = list(destinations)
        body["s3_storage_uuid"] = destinations[0]
    elif body.get("save_s3"):
        body["save_s3"] = False
    return body


async def copy_backup_schedules(
    api: CoolifyClient,
    *,
    collection: str,
    source_uuid: str,
    target_uuid: str,
    engine: DatabaseEngine | None,
    version: Version | None = None,
) -> int:
    """Recreate a standalone database's backup schedules on the target. Returns the count.

    Only this database's own rows (the GET mixes in other engines' rows with the
    same numeric id), and none for an engine Coolify cannot back up. After the
    POSTs the target is re-read and its own rows counted against the uuids the
    creates returned, so a doubled schedule fails CREATE instead of dumping twice.

    Nothing to undo beyond the target delete: DeleteResourceJob removes the rows.
    Their dump FILES are deliberately left — see compensations.undo_create_target.
    """
    if collection != "databases" or is_backupless(engine):
        return 0
    rows = own_backup_schedules(await api.get_backups(source_uuid), engine)
    if not rows:
        return 0

    bodies = [build_backup_body(r, version=version) for r in rows]
    for row, body in zip(rows, bodies, strict=True):
        if row.get("save_s3") and not body.get("save_s3"):
            # The plan said so when it saw it; a schedule added since has not been
            # announced anywhere else.
            log.warning(
                "api.backups.s3_dropped",
                frequency=row.get("frequency"),
                target=target_uuid,
                hint="its S3 storage is not disclosed by this Coolify; created local-only",
            )
    created = [await api.create_backup(target_uuid, body) for body in bodies]

    present = own_backup_schedules(await api.get_backups(target_uuid), engine)
    if copy_count_mismatches(created, [str(r.get("uuid")) for r in present]):
        raise CoolifyApiError(
            f"the target database holds {len(present)} backup schedule(s) where "
            f"{len(created)} were created",
            hint=(
                "A schedule was saved twice, or one the target did not report back. A "
                "doubled schedule dumps twice per run. The source was never stopped; "
                "the created target will be deleted — re-run the migration."
            ),
        )
    log.info("api.backups.copied", count=len(created), target=target_uuid)
    return len(created)
