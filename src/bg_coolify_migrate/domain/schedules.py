"""Schedules that must keep firing after the move, decided at plan time.

PURE module: no IO.

Coolify hangs schedules off a resource that are part of no resource GET and no
create body: scheduled tasks (applications and services) and backup schedules
(standalone databases). Every migration before this module recreated the stack
without them — the nightly cleanup cron and the database's dumps silently stopped
on the target, and nothing said so.

They are carried at CREATE_TARGET by ``api/schedules.py``. What makes copying
them with the source's ``enabled`` safe before START is upstream's own guard, not
ours. A target created with instant_deploy=false reads 'exited' (the column
default) until START_TARGET, and:

* the scheduler skips any task whose resource's status does not contain
  'running' (ScheduledJobManager v4.1.2:394-405, v4.5.0:670-681). That skip is
  THE guard — the UI's "execute now" and ``artisan`` RunScheduledJobsManually
  dispatch the job without it, but neither is anything this tool calls;
* DatabaseBackupJob returns, writing nothing, for a database whose status does
  not start with 'running' (v4.1.2:122-131, v4.5.0:147-151).

This module decides what the copy cannot reproduce. Refusals are creates that
WILL 422; warnings are behaviour the operator has to judge (AGENTS.md). Every
rule is silent on a stock setup, so a warning here is never furniture.

No line produced here ever contains a task's ``command`` or a backup execution's
``message``: Coolify itself treats the first as sensitive from 4.4 and redacts
the second only from 4.5, and both carry credentials often enough. Names,
frequencies and paths only (invariant 5).
"""

from __future__ import annotations

import posixpath
from collections import Counter
from collections.abc import Iterable
from typing import Any

from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.kinds import DatabaseEngine
from bg_coolify_migrate.domain.versions import Version

#: The UI has always capped a task timeout at 60..36000 (Add.php v4.1.2:44), so
#: only a task created through the API before 4.4 can hold more.
_TASK_TIMEOUT_MAX = 36000


def _name(task: dict[str, Any]) -> str:
    return str(task.get("name") or "?")


def server_timezone(server: dict[str, Any] | None) -> str | None:
    """The clock a server's schedules fire on, or ``None`` when the GET does not say.

    ScheduledJobManager evaluates every cron in the server's
    ``settings.server_timezone`` and falls back to ``config('app.timezone')`` —
    'UTC' — for an empty value (v4.1.2:541-546, v4.5.0:832-837). GET
    /servers/{uuid} serialises it under the eager-loaded ``settings``. A server
    record WITHOUT the key is "we do not know", never "UTC": comparing an unknown
    against a known value would invent a difference.
    """
    if not isinstance(server, dict):
        return None
    settings = server.get("settings")
    if not isinstance(settings, dict) or "server_timezone" not in settings:
        return None
    return str(settings.get("server_timezone") or "UTC")


# ── scheduled tasks ──────────────────────────────────────────────────────────


def scheduled_task_refusals(tasks: list[dict[str, Any]], *, version: Version | None) -> list[str]:
    """Tasks whose create on the target WILL 422."""
    if not supports("task_timeout_max", version):
        return []
    too_long = [
        f"{_name(t)!r} ({timeout}s)"
        for t in tasks
        if isinstance(timeout := t.get("timeout"), int)
        and not isinstance(timeout, bool)
        and timeout > _TASK_TIMEOUT_MAX
    ]
    if not too_long:
        return []
    return [
        f"scheduled task(s) {', '.join(too_long)} run with a timeout above "
        f"{_TASK_TIMEOUT_MAX}s. Coolify >= 4.4 refuses that on create — only a task "
        "created through the API before 4.4 can hold it — so the target's copy would "
        f"be rejected outright. Lower it to {_TASK_TIMEOUT_MAX}s or less on the source "
        "(the UI's own limit), then migrate."
    ]


def scheduled_task_warnings(
    tasks: list[dict[str, Any]],
    *,
    source_uuid: str,
    source_server: dict[str, Any] | None,
    target_server: dict[str, Any] | None,
    version: Version | None,
) -> list[str]:
    """Tasks that are carried but will not behave on the target as on the source."""
    out: list[str] = []

    if supports("task_container_exact_name", version) and source_uuid:
        # A container field holding the FULL name embeds the source uuid, and the
        # target's containers are named after its own. Carried verbatim and
        # reported, never rewritten: deriving a name by string-replacing a uuid is
        # the bug invariant 10 forbids. Before 4.5 such a value matched only by
        # luck (a single container ignores the field), which the target
        # reproduces, so it stays silent there.
        named = sorted(
            _name(t)
            for t in tasks
            if isinstance(container := t.get("container"), str) and source_uuid in container
        )
        if named:
            out.append(
                f"scheduled task(s) {named} name their container by its full name, which "
                "contains the source's uuid; the target's containers are named after the "
                "target's. With more than one container running, the task fails with "
                "'No valid container was found'. Set the container to the bare compose "
                "service name (e.g. 'web') on the source, or on the target afterwards."
            )

    source_tz, target_tz = server_timezone(source_server), server_timezone(target_server)
    armed = sorted(_name(t) for t in tasks if t.get("enabled") is not False)
    if armed and source_tz and target_tz and source_tz != target_tz:
        out.append(
            f"scheduled task(s) {armed} fire on the server's clock: {source_tz} on the "
            f"source server, {target_tz} on the target. The same frequency runs at a "
            "different wall-clock time after the move — align the target server's "
            "timezone, or adjust the frequencies on the target."
        )
    return out


def task_container_mismatches(tasks: list[dict[str, Any]], available: set[str]) -> list[str]:
    """Tasks whose ``container`` names nothing the TARGET runs. PURE.

    A service target is created from the instance's CURRENT template catalog and a
    dockercompose app from git HEAD — not from the source's compose — so a renamed
    compose service leaves the task pointing at nothing. Empty when at most one
    container exists: ScheduledTaskJob then ignores the field (v4.5.0:190,
    v4.1.2:149).
    """
    if len(available) <= 1:
        return []
    return sorted(
        _name(t)
        for t in tasks
        if isinstance(container := t.get("container"), str)
        and container
        and container not in available
    )


def copy_count_mismatches(sent: Iterable[str], found: Iterable[str]) -> dict[str, tuple[int, int]]:
    """``{uuid: (created, present)}`` wherever the two counts differ. PURE.

    Keyed by the uuids the creates returned and the target holds, never by a
    name: the API trims the names it is sent, so a source name with a stray
    space would read as one task missing and another doubled.

    The client retries a POST on 429/502/503/504, and a proxy error that arrives
    after Coolify committed the row turns one create into two. Nothing upstream
    deduplicates (the tables have no uniqueness constraint), so the only proof of
    "exactly once" is to count what the target holds afterwards — kept even where
    a create is retried on 429 alone, because it is the proof, not the cause.
    """
    want, got = Counter(sent), Counter(found)
    return {n: (want[n], got[n]) for n in sorted(set(want) | set(got)) if want[n] != got[n]}


# ── database backup schedules ────────────────────────────────────────────────

#: The morph class each engine's schedules are stored under. There is no morph
#: map (none in app/ at v4.1.2 or v4.5.0), so ``database_type`` is the FQCN
#: create writes with getMorphClass() (DatabasesController v4.1.2:835, v4.5.0:1188).
_BACKUP_OWNER: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRESQL: "App\\Models\\StandalonePostgresql",
    DatabaseEngine.MYSQL: "App\\Models\\StandaloneMysql",
    DatabaseEngine.MARIADB: "App\\Models\\StandaloneMariadb",
    DatabaseEngine.MONGODB: "App\\Models\\StandaloneMongodb",
    DatabaseEngine.REDIS: "App\\Models\\StandaloneRedis",
    DatabaseEngine.KEYDB: "App\\Models\\StandaloneKeydb",
    DatabaseEngine.DRAGONFLY: "App\\Models\\StandaloneDragonfly",
    DatabaseEngine.CLICKHOUSE: "App\\Models\\StandaloneClickhouse",
}
#: Other owners that share the numeric-id space, so their rows can appear in a
#: database's GET. Dropped silently: they belong to a different database.
_OTHER_BACKUP_OWNERS = frozenset({"App\\Models\\ServiceDatabase", "App\\Models\\StandaloneSqlite"})

#: isBackupSolutionAvailable() is false for these from v4.2.0 (create 422s,
#: DatabasesController v4.2.0:853, v4.5.0:1116); on 4.1.x the job threw
#: 'Unsupported database type'. A schedule on one never produced a backup.
_NO_BACKUP_ENGINES = frozenset(
    {DatabaseEngine.REDIS, DatabaseEngine.KEYDB, DatabaseEngine.DRAGONFLY}
)

#: Where DatabaseBackupJob puts local dumps: {team-slug}-{team_id}/{db-slug}-{uuid}/
#: below this (v4.5.0:288-290), so source and target never share a directory.
_BACKUP_ROOT = "/data/coolify/backups/databases/"


def own_backup_schedules(
    rows: list[dict[str, Any]], engine: DatabaseEngine | None
) -> list[dict[str, Any]]:
    """The rows that really belong to this database. PURE.

    GET /databases/{uuid}/backups filters on ``database_id`` only, never on
    ``database_type`` (v4.1.2:167, v4.5.0:447), and every engine has its own id
    sequence — so a MySQL with the same hidden numeric id as this Postgres hands
    its schedules to this Postgres's GET. Copying them would create the MySQL's
    schedules on the Postgres target.
    """
    if engine is None:
        return []
    owner = _BACKUP_OWNER[engine]
    return [r for r in rows if r.get("database_type") == owner]


def is_backupless(engine: DatabaseEngine | None) -> bool:
    """Whether Coolify has no backup for this engine, so schedules are never carried."""
    return engine in _NO_BACKUP_ENGINES


def backup_s3_destinations(row: dict[str, Any]) -> tuple[str, ...] | None:
    """Where a schedule uploads, primary first — or ``None`` when undisclosed. PURE.

    Only v4.5.0 names a schedule's S3 storages by uuid (``s3_storage_uuids``,
    always present on 4.5 rows; v4.5.0:200-211). Before that a row carries the
    hidden numeric ``s3_storage_id`` alone, and no API maps it to a uuid:
    /s3-storages hides ``id`` (from 4.3.0, absent before) and hands a root token
    the key and secret, which this tool will not read.
    """
    if "s3_storage_uuids" not in row:
        return None
    raw = row.get("s3_storage_uuids") or []
    uuids = [str(u) for u in raw if u] if isinstance(raw, list) else []
    primary = row.get("s3_storage_uuid")
    if primary and str(primary) in uuids:
        uuids.insert(0, uuids.pop(uuids.index(str(primary))))
    return tuple(dict.fromkeys(uuids))


def _frequencies(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r.get("frequency") or "?") for r in rows]


def backup_warnings(
    rows: list[dict[str, Any]],
    *,
    engine: DatabaseEngine | None,
    source_timezone: str | None,
    target_timezone: str | None,
) -> list[str]:
    """What a database's backup schedules will not do on the target as on the source.

    ``rows`` is the raw GET. Schedules are named by their frequency — a backup
    schedule has no name — and never by anything an execution recorded.
    """
    out: list[str] = []
    known = set(_BACKUP_OWNER.values()) | _OTHER_BACKUP_OWNERS
    if unknown := sorted({str(r.get("database_type")) for r in rows} - known):
        count = sum(1 for r in rows if str(r.get("database_type")) in unknown)
        out.append(
            f"{count} backup schedule(s) of an unrecognised database type {unknown} are not "
            "carried. Check the source database's Backups tab and recreate any that "
            "belong to it on the target."
        )

    own = own_backup_schedules(rows, engine)
    if is_backupless(engine):
        if own:
            out.append(
                f"{len(own)} backup schedule(s) are not carried: Coolify has no backup for "
                f"{engine}, its API refuses one on create from 4.2.0, and the schedule never "
                "produced a backup on the source either."
            )
        return out

    s3 = [r for r in own if r.get("save_s3")]
    if undisclosed := _frequencies([r for r in s3 if backup_s3_destinations(r) is None]):
        out.append(
            f"backup schedule(s) {undisclosed} upload to S3, but this Coolify (< 4.5) names "
            "a schedule's S3 storage only by a hidden numeric id that no API maps to a "
            "uuid. The target's copy is created LOCAL-ONLY: select the S3 storage in the "
            "target database's Backups tab, or off-site backups stop after the move."
        )
    if dangling := _frequencies([r for r in s3 if backup_s3_destinations(r) == ()]):
        out.append(
            f"backup schedule(s) {dangling} are set to upload to S3, but their S3 storage "
            "no longer exists. The target's copy is created local-only."
        )
    if no_local := _frequencies([r for r in own if r.get("disable_local_backup") is True]):
        out.append(
            f"backup schedule(s) {no_local} keep no local dumps on the source "
            "(disable_local_backup), a setting no API can set. The target keeps local "
            "dumps on the target server's disk, under the schedule's local retention; "
            "switch it off in the target's Backups tab if disk matters."
        )

    armed = _frequencies([r for r in own if r.get("enabled") is not False])
    if armed and source_timezone and target_timezone and source_timezone != target_timezone:
        out.append(
            f"backup schedule(s) {armed} fire on the server's clock: {source_timezone} on "
            f"the source server, {target_timezone} on the target. The same frequency "
            "dumps at a different wall-clock time after the move."
        )

    runs = [e for r in own for e in r.get("executions") or [] if isinstance(e, dict)]
    if runs:
        out.append(
            f"the source has {len(runs)} backup run(s) on record. They are not migrated, "
            "and the target starts a fresh history. The dumps stay where they are, on "
            f"the source server under {_dump_directory(runs)} (and in S3 if uploaded). "
            "Once the source stops running, no retention prunes them and no finalize "
            "policy removes them."
        )
    return out


def _dump_directory(runs: list[dict[str, Any]]) -> str:
    """The directory of the newest recorded dump, else the root all dumps live under."""
    dated = sorted(
        (str(e.get("created_at") or ""), str(e["filename"]))
        for e in runs
        if isinstance(e.get("filename"), str) and e["filename"].startswith("/")
    )
    if not dated:
        return _BACKUP_ROOT
    return posixpath.dirname(dated[-1][1]) + "/"


#: ServiceDatabase::isBackupSolutionAvailable, identical v4.1.2:185-193 and
#: v4.5.0:192-200: the image before ':' names one of these (databaseType() maps
#: timescale and pgvector to postgresql), or a custom_type is set.
_SERVICE_BACKUP_IMAGES = (
    "mysql",
    "postgres",
    "postgis",
    "mariadb",
    "mongo",
    "timescale",
    "pgvector",
)


def service_backup_warnings(service: dict[str, Any]) -> list[str]:
    """A service's databases that can hold backup schedules nobody can read.

    Unconditional on purpose: the /databases/{uuid}/backups routes resolve only
    standalone databases, and GET /services/{uuid} loads its databases without
    schedules (v4.1.2:736, v4.5.0:880), so no API says whether one exists.
    """
    capable = sorted(
        str(db.get("name") or "?")
        for db in service.get("databases") or []
        if isinstance(db, dict) and _service_database_backs_up(db)
    )
    if not capable:
        return []
    return [
        f"service database(s) {capable} can have backup schedules, and no Coolify API "
        "exposes a service database's schedules on any release, so they are neither "
        "read nor recreated. Check each one's Backups tab on the source and recreate "
        "its schedules on the target."
    ]


def _service_database_backs_up(db: dict[str, Any]) -> bool:
    if db.get("custom_type"):
        return True
    image = str(db.get("image") or "").split(":", 1)[0]
    return any(marker in image for marker in _SERVICE_BACKUP_IMAGES)


# ── volume backups ───────────────────────────────────────────────────────────


def may_hold_volume_backups(version: Version | None) -> bool:
    """Whether this instance can hold volume-backup schedules nobody can read.

    The ``volume_backups`` feature (4.3.0+): such a schedule has PUT, run and
    DELETE routes, but no GET on any release, so no plan can see it.
    """
    return supports("volume_backups", version)


def volume_backup_notes(resources: Iterable[str], *, deletes_source: bool) -> list[str]:
    """What the move does to the volume backups of ``resources``. PURE.

    ``resources`` are the plan's resources with a volume, on an instance that
    :func:`may_hold_volume_backups`. Unconditional per plan, like
    :func:`service_backup_warnings`: no API says whether a schedule exists.
    """
    names = sorted(set(resources))
    if not names:
        return []
    out = [
        f"volume(s) of {names} can have their own backup schedules (Coolify 4.3+, a "
        "volume's Backups setting). No Coolify API reads them, so they are neither seen "
        "nor carried. Check each volume's Backups on the source and set them up again "
        "on the target."
    ]
    if deletes_source:
        out.append(volume_backup_delete_caveat(names))
    return out


def volume_backup_delete_caveat(resources: Iterable[str]) -> str:
    """The cost of ``--finalize delete`` to those volume backups. PURE.

    Deleting a resource runs DeleteScheduledVolumeBackup for every schedule on
    its storages, with local and S3 archive deletion on (DeleteResourceJob,
    v4.3.0:49,123-137 .. v4.5.0:125,188-209) — unlike a database's dumps, which a
    delete leaves where they are.
    """
    return (
        f"--finalize delete also makes Coolify delete every archive the volume backups "
        f"of {sorted(set(resources))} made, on the source server and in S3. The target "
        "starts without them. Keep a copy of any archive you need before finalizing, "
        "or finalize with rename."
    )
