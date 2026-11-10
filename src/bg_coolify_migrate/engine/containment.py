"""The containment checkpoints: is the source still stopped, the target still unstarted?

IO shell. It asks both daemons and lets ``domain/containment.py`` decide. Called
by the steps from COPY to FINALIZE, because QUIESCE proves the source stopped
only once and nothing in Coolify keeps it so (a git push, a CI ``/deploy``, the
dashboard), and CREATE leaves the target startable by the same pushes and tags.

A breach is never a warning and has no override — the quiesce gate has no
``--force``. The checkpoint raises; the run rolls back, and the source, never
deleted before FINALIZE, keeps whatever it wrote.
"""

from __future__ import annotations

import structlog

from bg_coolify_migrate.discovery import docker, pins, quiesce
from bg_coolify_migrate.domain import containment
from bg_coolify_migrate.domain.containment import Breach, Seen, Watched
from bg_coolify_migrate.domain.statemachine import START_REQUESTED, STARTED_EARLY, State
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.planner import observed_labels, resource_labels
from bg_coolify_migrate.errors import SourceRevived, TargetStartedEarly
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)


async def assert_contained(
    ctx: MigrationContext, *, checkpoint: str, source: bool = True, target: bool = True
) -> None:
    """Refuse to go on if anything started the stopped source or an unstarted target.

    ``checkpoint`` names where in the run this asks, for the message. The target
    side skips every target START already asked for (journalled), so a resumed
    START does not take its own start for someone else's.

    Raises:
        SourceRevived: A source container runs, or started after the stop.
        TargetStartedEarly: A target container runs, or ever started, before
            START asked for it. Journalled first under START's state, so the
            rollback owes STOP_TARGET for it (``statemachine.target_may_run``).
    """
    found_source = await _source_breaches(ctx, checkpoint=checkpoint) if source else []
    found_target, early = await _target_breaches(ctx) if target else ([], [])
    if early:
        journal_started_early(ctx, early)
    if found_source:
        raise SourceRevived(
            containment.breach_message(
                "the source was started again after QUIESCE stopped it",
                checkpoint,
                [*found_source, *found_target],
            ),
            hint=containment.source_hint(target_serving=_target_serving(ctx)),
        )
    if found_target:
        raise TargetStartedEarly(
            containment.breach_message(
                "a target was started before START asked for it", checkpoint, found_target
            ),
            hint=containment.TARGET_HINT,
        )
    log.debug("containment.ok", checkpoint=checkpoint, source=source, target=target)


def stop_clock(ctx: MigrationContext) -> int | None:
    """The source's clock QUIESCE read just before its stop request, from the journal."""
    raw = ctx.journal.undo_info(State.QUIESCE.value).get(containment.STOP_CLOCK)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def start_requested(ctx: MigrationContext) -> list[str]:
    """Every target uuid START has asked Coolify to start, from the journal."""
    raw = ctx.journal.undo_info(State.START_TARGET.value).get(START_REQUESTED)
    return [u for u in raw if isinstance(u, str)] if isinstance(raw, list) else []


def _target_serving(ctx: MigrationContext) -> bool:
    return State.START_TARGET.value in ctx.journal.completed_states()


def journal_started_early(ctx: MigrationContext, uuids: list[str]) -> None:
    """Record targets found started by someone else, so the rollback stops them."""
    recorded = ctx.journal.undo_info(State.START_TARGET.value).get(STARTED_EARLY)
    known = [u for u in recorded if isinstance(u, str)] if isinstance(recorded, list) else []
    ctx.journal.append(
        "containment",
        state=State.START_TARGET.value,
        detail={STARTED_EARLY: sorted({*known, *uuids})},
    )


async def _seen(host: RemoteHost, container: docker.Container, *, inspect: bool) -> Seen:
    started = None
    if inspect and container.is_stopped:
        started = await quiesce.started_at(host, container.id or container.name)
    return Seen(
        name=container.name,
        state=container.state,
        labels=container.labels,
        image=container.image,
        started_at=started,
    )


async def _source_breaches(ctx: MigrationContext, *, checkpoint: str) -> list[Breach]:
    """The source side: the plan's label filter, then the probes no rename can hide from.

    The uuid probe and the migrated-volume probe are PREFLIGHT's
    (``steps._label_independent_containers``): a project, environment or
    resource renamed after PLAN and then deployed carries the NEW names, which
    the plan's filter cannot see. Pins — ours and any other migration's — are
    created and never started, and are left out.
    """
    host = ctx.source_host
    since = stop_clock(ctx)
    stack = observed_labels(ctx.plan)
    watched = [
        Watched(
            name=r.snapshot.name,
            uuid=r.snapshot.uuid,
            labels=resource_labels(
                project=ctx.plan.project, environment=ctx.plan.environment, name=r.snapshot.name
            ),
        )
        for r in ctx.plan.resources
    ]
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
    seen = [
        await _seen(host, c, inspect=inspect) for c in found.values() if not pins.is_pin(c)
    ]
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
