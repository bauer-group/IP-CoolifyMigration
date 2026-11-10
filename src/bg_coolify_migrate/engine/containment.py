"""The containment checkpoints: is the source still stopped, the target still unstarted?

IO shell. It asks both daemons and lets ``domain/containment.py`` decide. Called
by the steps from COPY to FINALIZE, because QUIESCE proves the source stopped
only once and nothing in Coolify keeps it so (a git push, a CI ``/deploy``, the
dashboard), and CREATE leaves the target startable by the same pushes and tags.

A breach is never a warning and has no override — the quiesce gate has no
``--force``. The checkpoint raises. Before START the run rolls back, and the
source, never deleted before FINALIZE, keeps whatever it wrote. Once START has
asked for a target, a revived source BLOCKS the run instead
(``SourceRevivedAfterStart``): the target may have served users since, and the
rollback would delete what they wrote.

Also here, because it guards the same claim: PREFLIGHT's refusal of two server
records that are one daemon (:func:`assert_separate_daemons`).
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any, NoReturn

import structlog

from bg_coolify_migrate.discovery import docker, pins, quiesce
from bg_coolify_migrate.domain import containment
from bg_coolify_migrate.domain.containment import Breach, Seen, Watched
from bg_coolify_migrate.domain.statemachine import START_REQUESTED, STARTED_EARLY, State
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.planner import observed_labels, resource_labels
from bg_coolify_migrate.errors import (
    PreflightError,
    SourceRevived,
    SourceRevivedAfterStart,
    TargetStartedEarly,
)
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: The daemon's engine ID (``containment.parse_daemon_id``).
DAEMON_ID_COMMAND = "docker info --format '{{.ID}}'"


async def assert_contained(
    ctx: MigrationContext, *, checkpoint: str, source: bool = True, target: bool = True
) -> None:
    """Refuse to go on if anything started the stopped source or an unstarted target.

    ``checkpoint`` names where in the run this asks, for the message. The target
    side skips every target START already asked for (journalled), so a resumed
    START does not take its own start for someone else's.

    Raises:
        SourceRevived: A source container runs, or started after the stop gate
            proved it down — before START asked for any target: the run rolls
            back.
        SourceRevivedAfterStart: The same, once START has asked for a target:
            the run blocks (:func:`_block_after_start`).
        TargetStartedEarly: A target container runs, or ever started, before
            START asked for it. Journalled first under START's state, so the
            rollback owes STOP_TARGET for it (``statemachine.target_may_run``).
    """
    found_source = await _source_breaches(ctx, checkpoint=checkpoint) if source else []
    found_target, early = await _target_breaches(ctx) if target else ([], [])
    if early:
        journal_started_early(ctx, early)
    if found_source:
        message = containment.breach_message(
            "the source was started again after QUIESCE stopped it",
            checkpoint,
            [*found_source, *found_target],
        )
        if _start_asked(ctx):
            await _block_after_start(ctx, message)
        raise SourceRevived(message, hint=containment.source_hint())
    if found_target:
        raise TargetStartedEarly(
            containment.breach_message(
                "a target was started before START asked for it", checkpoint, found_target
            ),
            hint=containment.TARGET_HINT,
        )
    log.debug("containment.ok", checkpoint=checkpoint, source=source, target=target)


def revival_clock(ctx: MigrationContext) -> int | None:
    """From when a source start counts as a revival (``containment.judged_from``)."""
    return containment.judged_from(ctx.journal.undo_info(State.QUIESCE.value))


def start_requested(ctx: MigrationContext) -> list[str]:
    """Every target uuid START has asked Coolify to start, from the journal."""
    raw = ctx.journal.undo_info(State.START_TARGET.value).get(START_REQUESTED)
    return [u for u in raw if isinstance(u, str)] if isinstance(raw, list) else []


def _start_asked(ctx: MigrationContext) -> bool:
    """Has START asked Coolify to start any target — so one may be serving users?

    ``START_REQUESTED`` is journalled before each request, and a START that
    completed asked for all of them. A target someone ELSE started does not
    count (``STARTED_EARLY``): that happens before the DNS gate let anything
    start, and rolling it back is the containment's whole point.
    """
    return bool(start_requested(ctx)) or (
        State.START_TARGET.value in ctx.journal.completed_states()
    )


async def _block_after_start(ctx: MigrationContext, message: str) -> NoReturn:
    """Stop the run, roll nothing back, and say what the operator decides.

    The source's clock is journalled first (``containment.REVIVAL_SEEN_CLOCK``):
    a resume judges from it, so the starts this message names do not block it
    again once the operator has stopped the source, and anything that starts
    after it still does. A clock that cannot be read is no reason to give up the
    block — a rollback is exactly what must not happen here — so the resume then
    judges from the old instant and blocks again until the source's daemon has
    forgotten those starts; the operator can roll back by hand.
    """
    try:
        seen_at = await quiesce.now_on(ctx.source_host)
    except Exception as exc:  # the block itself must not become a rollback
        log.warning("containment.revival_clock_unread", error=str(exc)[:200])
    else:
        ctx.journal.append(
            "containment",
            state=State.QUIESCE.value,
            detail={containment.REVIVAL_SEEN_CLOCK: seen_at},
        )
    raise SourceRevivedAfterStart(
        message,
        hint=containment.revived_after_start_hint(
            policy=ctx.plan.finalize_policy.value, migration_id=ctx.migration_id
        ),
    )


def journal_started_early(ctx: MigrationContext, uuids: list[str]) -> None:
    """Record targets found started by someone else, so the rollback stops them."""
    recorded = ctx.journal.undo_info(State.START_TARGET.value).get(STARTED_EARLY)
    known = [u for u in recorded if isinstance(u, str)] if isinstance(recorded, list) else []
    ctx.journal.append(
        "containment",
        state=State.START_TARGET.value,
        detail={STARTED_EARLY: sorted({*known, *uuids})},
    )


async def _seen(
    host: RemoteHost,
    container: docker.Container,
    *,
    inspect: bool,
    volumes: Sequence[str] | None = None,
) -> Seen:
    """What the daemon says of one container. ``volumes`` only for one found by
    the volume probe alone: whether it mounts one of them writable."""
    handle = container.id or container.name
    started = None
    if inspect and container.is_stopped:
        started = await quiesce.started_at(host, handle)
    writes = None
    if volumes is not None:
        mounts = await quiesce.mounts_of(host, handle)
        writes = None if mounts is None else containment.mounts_writable(mounts, volumes)
        if writes is False:
            log.debug("containment.read_only_reader", container=container.name)
    return Seen(
        name=container.name,
        state=container.state,
        labels=container.labels,
        image=container.image,
        started_at=started,
        writes=writes,
    )


def _watched_sources(ctx: MigrationContext) -> list[Watched]:
    return [
        Watched(
            name=r.snapshot.name,
            uuid=r.snapshot.uuid,
            labels=resource_labels(
                project=ctx.plan.project, environment=ctx.plan.environment, name=r.snapshot.name
            ),
        )
        for r in ctx.plan.resources
    ]


async def _source_breaches(ctx: MigrationContext, *, checkpoint: str) -> list[Breach]:
    """The source side: the plan's label filter, then the probes no rename can hide from.

    The uuid probe and the migrated-volume probe are PREFLIGHT's
    (``steps._label_independent_containers``): a project, environment or
    resource renamed after PLAN and then deployed carries the NEW names, which
    the plan's filter cannot see. Pins — ours and any other migration's — are
    created and never started, and are left out. A container only the volume
    probe found has its mounts read: a read-only reader is no revival
    (``containment.source_breaches``).
    """
    host = ctx.source_host
    since = revival_clock(ctx)
    stack = observed_labels(ctx.plan)
    watched = _watched_sources(ctx)
    found: dict[str, docker.Container] = {}
    for container in await docker.list_containers(host, label_filters=stack):
        found.setdefault(container.id or container.name, container)
    for container in await docker.managed_containers(host):
        if any(docker.carries_uuid(container, w.uuid) for w in watched):
            found.setdefault(container.id or container.name, container)
    volumes = sorted(
        {i.source_name for r in ctx.plan.resources for i in r.manifest.to_migrate if i.source_name}
        | {p.source.name for pairs in ctx.volume_pairs.values() for p in pairs}
    )
    if volumes:
        for container in await docker.containers_mounting(host, volumes=volumes):
            found.setdefault(container.id or container.name, container)

    inspect = since is not None
    seen: list[Seen] = []
    for c in found.values():
        if pins.is_pin(c):
            continue
        alone = containment.volume_only(
            watched, stack, name=c.name, image=c.image, labels=c.labels
        )
        seen.append(await _seen(host, c, inspect=inspect, volumes=volumes if alone else None))
    events = await quiesce.start_events(host, since=since) if since is not None else None
    if since is None:
        log.warning(
            "containment.no_stop_clock",
            checkpoint=checkpoint,
            hint="the journal predates the stop-clock record: only running containers count",
        )
    elif (complete_from := containment.event_log_gap(events, since)) is not None:
        log.warning(
            "containment.event_log_gap",
            checkpoint=checkpoint,
            server=ctx.plan.source_server.name,
            complete_from=complete_from,
            hint="the daemon keeps its last 256 events: a source container that started and "
            "was removed again before then cannot be ruled out; one still there is found",
        )
    return containment.source_breaches(watched, seen, events, since=since, stack=stack)


async def _target_breaches(ctx: MigrationContext) -> tuple[list[Breach], list[str]]:
    """The target side, and the target uuids it found started.

    By name labels, as HEALTHCHECK looks, and by uuid (``steps._containers_by_uuid``),
    for every target START has not asked for yet.
    """
    host = ctx.target_host
    requested = set(start_requested(ctx))
    watched: list[Watched] = []
    for r in ctx.plan.resources:
        target_uuid = ctx.target_uuids.get(r.snapshot.uuid)
        if not target_uuid or target_uuid in requested:
            continue
        watched.append(
            Watched(
                name=r.snapshot.name,
                uuid=target_uuid,
                labels=resource_labels(
                    project=ctx.plan.project,
                    environment=ctx.plan.environment,
                    name=r.snapshot.name,
                ),
            )
        )
    if not watched:
        return [], []
    found: dict[str, docker.Container] = {}
    for w in watched:
        for container in await docker.list_containers(host, label_filters=dict(w.labels)):
            found.setdefault(container.id or container.name, container)
    for container in await docker.managed_containers(host):
        if any(docker.carries_uuid(container, w.uuid) for w in watched):
            found.setdefault(container.id or container.name, container)
    seen = [await _seen(host, c, inspect=True) for c in found.values() if not pins.is_pin(c)]
    breaches = containment.target_breaches(
        watched, seen, await quiesce.start_events(host, since=0)
    )
    return breaches, sorted({b.uuid for b in breaches if b.uuid})


async def assert_separate_daemons(ctx: MigrationContext) -> None:
    """PREFLIGHT: refuse when the source and target servers are one Docker daemon.

    Read-only: ``docker info`` on each (``containment.shared_daemon_refusal``).
    A daemon that does not answer proves nothing, so the run goes on — with a
    warning, because the plan's uuid comparison is then the only guard.

    Raises:
        PreflightError: Both daemons report the same engine ID.
    """
    ids = [await _daemon_id(host) for host in (ctx.source_host, ctx.target_host)]
    refusal = containment.shared_daemon_refusal(
        ids[0],
        ids[1],
        source=ctx.plan.source_server.name,
        target=ctx.plan.target_server.name,
    )
    if refusal is not None:
        raise PreflightError(refusal, hint=containment.SHARED_DAEMON_HINT)
    if None in ids:
        log.warning(
            "preflight.daemon_id_unread",
            source=ids[0] is not None,
            target=ids[1] is not None,
            hint="could not tell whether the two servers are one Docker daemon",
        )


async def _daemon_id(host: RemoteHost) -> str | None:
    result = await host.run(DAEMON_ID_COMMAND)
    return containment.parse_daemon_id(result.stdout) if result.ok else None


async def record_source_volumes(ctx: MigrationContext) -> dict[str, str]:
    """Journal the CreatedAt of every source volume COPY read, for the rollback guard.

    Called once the end-of-copy checkpoint passed, so these are the volumes the
    copy is a copy of. ``compensations.source_data_refusal`` compares them after
    FINALIZE(keep/rename): a volume Coolify's cleanup pruned and a redeploy
    recreated EMPTY keeps its name, never its CreatedAt.
    """
    volumes = sorted({p.source.name for pairs in ctx.volume_pairs.values() for p in pairs})
    if not volumes:
        return {}
    created = await volumes_created(ctx.source_host, volumes)
    ctx.journal.append(
        "step_started",
        state=State.COPY.value,
        detail={containment.SOURCE_VOLUMES_CREATED: created},
    )
    return created


async def volumes_created(host: RemoteHost, volumes: list[str]) -> dict[str, str]:
    """``{volume: CreatedAt}`` in one ``docker volume inspect``. Raises if it fails."""
    names = " ".join(shlex.quote(v) for v in volumes)
    result = await host.run_checked(
        f"docker volume inspect --format '{{{{.Name}}}} {{{{.CreatedAt}}}}' {names}"
    )
    return containment.parse_volume_created(result.stdout)


def recorded_volumes(undo_info: dict[str, Any]) -> dict[str, str]:
    """COPY's journalled ``{volume: CreatedAt}``; empty for a journal without the record."""
    raw = undo_info.get(containment.SOURCE_VOLUMES_CREATED)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
