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
rollback would delete what they wrote. A check that cannot be answered — the
event log, a deployment history — never passes: it raises ``QuiesceError``. An
unread deployment history once START asked blocks instead
(``SourceUncheckedAfterStart``), for the revival's reason — a Coolify API that
does not answer for a while must not roll a cutover back — and a breach found in
the same check is raised first, with the unread histories beside it.

An application Coolify says it no longer has (its own 404: the row is
soft-deleted) has no history left to compare, and nothing queues a deployment
of it any more: every lookup behind ``/deploy``, the webhooks and the dashboard
skips trashed rows; only the cancel route reads them, so that a deployment
already in flight during the delete stays cancellable (DeployController
v4.7.2:239-240) — and such a one may still start a container. Its containers
carry its uuid and are still looked for. An application FINALIZE journalled as
deleted is not asked at all: the delete is this run's own.

Also here, because they guard the same claim: the deployment record QUIESCE
journals before its stop (:func:`deployments_before_stop`), and PREFLIGHT's
refusal of two server records that are one daemon (:func:`assert_separate_daemons`).
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any, NoReturn

import structlog

from bg_coolify_migrate.discovery import docker, pins, quiesce
from bg_coolify_migrate.domain import containment
from bg_coolify_migrate.domain.containment import Breach, Seen, Watched
from bg_coolify_migrate.domain.deployments import Deployment
from bg_coolify_migrate.domain.statemachine import START_REQUESTED, STARTED_EARLY, State
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.planner import observed_labels, resource_labels
from bg_coolify_migrate.errors import (
    MigrationError,
    PreflightError,
    QuiesceError,
    SourceDeploymentPending,
    SourceRevived,
    SourceRevivedAfterStart,
    SourceUncheckedAfterStart,
    TargetStartedEarly,
)
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: The daemon's engine ID (``containment.parse_daemon_id``).
DAEMON_ID_COMMAND = "docker info --format '{{.ID}}'"

#: Each source application's deployment history, by uuid, as one check read it.
History = dict[str, list[Deployment]]

#: ``(application name, why)`` for each history a check could not read.
Unread = list[tuple[str, str]]

#: What a 404 from Coolify itself means at the stop: no history to record.
_GONE = "Coolify has no such application any more (404)"


async def assert_contained(
    ctx: MigrationContext, *, checkpoint: str, source: bool = True, target: bool = True
) -> None:
    """Refuse to go on if anything started the stopped source or an unstarted target.

    ``checkpoint`` names where in the run this asks, for the message. The target
    side skips every target START already asked for (journalled), so a resumed
    START does not take its own start for someone else's.

    Raises:
        SourceRevived: A source container runs, or started after the stop gate
            proved it down, or a source application has a deployment the stop
            did not see ended — before START asked for any target: the run
            rolls back.
        SourceRevivedAfterStart: The same, once START has asked for a target:
            the run blocks (:func:`_block_after_start`).
        TargetStartedEarly: A target container runs, or ever started, before
            START asked for it. Journalled first under START's state, so the
            rollback owes STOP_TARGET for it (``statemachine.target_may_run``).
        SourceUncheckedAfterStart: Nothing of the above, but a source
            application's deployment history could not be read, once START has
            asked for a target: the run blocks.
        QuiesceError: The event log could not be read, or a deployment history
            could not be read before START asked for any target.
    """
    found_source: list[Breach] = []
    history: History | None = None
    unread: Unread = []
    if source:
        found_source, history, unread = await _source_breaches(ctx, checkpoint=checkpoint)
    found_target, early = await _target_breaches(ctx) if target else ([], [])
    if early:
        journal_started_early(ctx, early)
    # What was found goes first, whatever could not be read beside it: an unread
    # history must not turn a revival found after START into a rollback.
    also = containment.unread_breaches(unread)
    if found_source:
        message = containment.breach_message(
            "the source was started again after QUIESCE stopped it",
            checkpoint,
            [*found_source, *found_target, *also],
        )
        if _start_asked(ctx):
            await _block_after_start(ctx, message, history)
        raise SourceRevived(message, hint=containment.source_hint())
    if found_target:
        raise TargetStartedEarly(
            containment.breach_message(
                "a target was started before START asked for it",
                checkpoint,
                [*found_target, *also],
            ),
            hint=containment.TARGET_HINT,
        )
    if unread:
        message = containment.unread_history_message(unread, checkpoint)
        if _start_asked(ctx):
            raise SourceUncheckedAfterStart(
                message,
                hint=containment.unchecked_after_start_hint(
                    policy=ctx.plan.finalize_policy.value,
                    migration_id=ctx.migration_id,
                    unread=unread,
                ),
            )
        raise QuiesceError(message, hint=containment.unread_history_hint(unread))
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


async def _block_after_start(
    ctx: MigrationContext, message: str, history: History | None
) -> NoReturn:
    """Stop the run, roll nothing back, and say what the operator decides.

    The source's clock is journalled first (``containment.REVIVAL_SEEN_CLOCK``):
    a resume judges from it, so the starts this message names do not block it
    again once the operator has stopped the source, and anything that starts
    after it still does. A clock that cannot be read is no reason to give up the
    block — a rollback is exactly what must not happen here — so the resume then
    judges from the old instant and blocks again until the source's daemon has
    forgotten those starts; the operator can roll back by hand.

    The deployments this check read go in the same record, for the same reason,
    clock or no clock (``containment.block_record``): those that had ended are
    accounted from now on; one still queued or running is reported while it is,
    and once more after it ended, because it ran after this check. A history
    this check could not read records nothing — the resume reads it again.
    """
    detail: dict[str, Any] = {}
    if history is not None:
        detail.update(
            containment.block_record(ctx.journal.undo_info(State.QUIESCE.value), history)
        )
    try:
        detail[containment.REVIVAL_SEEN_CLOCK] = await quiesce.now_on(ctx.source_host)
    except Exception as exc:  # the block itself must not become a rollback
        log.warning("containment.revival_clock_unread", error=str(exc)[:200])
    if detail:
        ctx.journal.append("containment", state=State.QUIESCE.value, detail=detail)
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


def _source_applications(ctx: MigrationContext) -> list[Watched]:
    applications = {
        r.snapshot.uuid for r in ctx.plan.resources if r.snapshot.collection == "applications"
    }
    return [w for w in _watched_sources(ctx) if w.uuid in applications]


async def _source_breaches(
    ctx: MigrationContext, *, checkpoint: str
) -> tuple[list[Breach], History | None, Unread]:
    """The source side: the plan's label filter, then the probes no rename can hide from.

    The uuid probe and the migrated-volume probe are PREFLIGHT's
    (``steps._label_independent_containers``): a project, environment or
    resource renamed after PLAN and then deployed carries the NEW names, which
    the plan's filter cannot see. Pins — ours and any other migration's — are
    created and never started, and are left out. A container only the volume
    probe found has its mounts read: a read-only reader is no revival
    (``containment.source_breaches``).

    Then every source application's deployment history, against the record
    QUIESCE journalled at the stop (``containment.deployment_breaches``).
    Returned with the breaches — ``None`` for a journal without that record —
    so a block can journal what it showed, and with the histories that could
    not be read, which never pass (:func:`assert_contained` decides how).
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
            empty=events is not None and events.oldest is None,
            hint="the daemon keeps its last 256 events, and none across a restart: a source "
            "container that started and was removed again before then cannot be ruled out; "
            "one still there is found, and an application's deployment history still counts",
        )
    breaches = containment.source_breaches(watched, seen, events, since=since, stack=stack)
    applications = _source_applications(ctx)
    detail = ctx.journal.undo_info(State.QUIESCE.value)
    accounted = containment.accounted_deployments(detail)
    if accounted is None:
        if applications:
            log.warning(
                "containment.no_deployment_record",
                checkpoint=checkpoint,
                hint="the journal predates the stop's deployment record: an application's "
                "deployment history is not compared",
            )
        return breaches, None, []
    deleted = _deleted_by_finalize(ctx)
    history: History = {}
    unread: Unread = []
    for application in applications:
        if application.uuid not in accounted:
            continue
        if application.uuid in deleted:
            log.debug("containment.history_skipped", application=application.name)
            continue
        rows, why = await _read_history(ctx, application)
        if rows is not None:
            history[application.uuid] = rows
        elif why is not None:
            unread.append((application.name, why))
        else:
            log.warning(
                "containment.source_application_gone",
                checkpoint=checkpoint,
                application=application.name,
                hint="Coolify no longer has it (deleted in Coolify): no history to compare; "
                "its containers are still looked for",
            )
    breaches.extend(
        containment.deployment_breaches(
            applications, accounted, history, pending=containment.pending_at_block(detail)
        )
    )
    return breaches, history, unread


def _deleted_by_finalize(ctx: MigrationContext) -> set[str]:
    """The source uuids FINALIZE(delete) journalled as deleted (``steps._delete_source``)."""
    raw = ctx.journal.undo_info(State.FINALIZE.value).get("deleted")
    return {u for u in raw if isinstance(u, str)} if isinstance(raw, list) else set()


async def _read_history(
    ctx: MigrationContext, application: Watched
) -> tuple[list[Deployment] | None, str | None]:
    """One source application's newest deployment rows, uuid and status only.

    ``(rows, None)`` when read; ``(None, None)`` when Coolify says it has no such
    application (``client.application_deployments``); ``(None, why)`` when it
    could not be read — which never means that nothing deployed the source, the
    rule of the event log (``quiesce.start_events``).
    """
    try:
        rows = await ctx.api.application_deployments(application.uuid)
    except MigrationError as exc:
        return None, (str(exc).splitlines() or [type(exc).__name__])[0][:200]
    return rows, None


async def _deployment_history(ctx: MigrationContext, application: Watched) -> list[Deployment]:
    """QUIESCE: one source application's history, or the stop is refused.

    Before the stop a source Coolify no longer has cannot be recorded either:
    its 404 refuses like any unread history.
    """
    rows, why = await _read_history(ctx, application)
    if rows is not None:
        return rows
    unread = [(application.name, why or _GONE)]
    raise QuiesceError(
        containment.unread_history_message(unread), hint=containment.unread_history_hint(unread)
    )


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


async def deployments_before_stop(ctx: MigrationContext) -> dict[str, list[str]]:
    """QUIESCE, just before the stop: the deployments each source application had ENDED.

    Journalled beside the stop clock (``containment.SOURCE_DEPLOYMENTS``); every
    later source checkpoint reports a row this does not hold. Read after
    ``deploy_triggers.await_source_deployments`` waited them out, so every row
    queued from here on is someone else's. Read-only, and BEFORE anything is
    stopped, while the source still serves: a history that cannot be read
    refuses the stop (:func:`_deployment_history`), and so does a deployment
    queued since that wait — Coolify's stop would not cancel it, and it would
    start the source again after it.

    Raises:
        QuiesceError: A history could not be read.
        SourceDeploymentPending: A source deployment is queued or running.
    """
    record: dict[str, list[str]] = {}
    pending: list[str] = []
    for application in _source_applications(ctx):
        rows = await _deployment_history(ctx, application)
        pending.extend(
            f"{application.name}: deployment {d.uuid} is {d.status or 'of unknown status'}"
            for d in containment.unsettled(rows)
        )
        record[application.uuid] = containment.ended_deployments(rows)
    if pending:
        raise SourceDeploymentPending(
            "a source deployment has not ended, so the stop is refused:\n" + "\n".join(pending),
            hint=(
                "It was queued after this run waited for the source's deployments to end. "
                "Coolify's stop cancels no deployment, and its queue starts what is waiting: "
                "the source would come back up after the stop, beside a copy that never sees "
                "its writes. Let it finish, or cancel it in Coolify, then run the migration "
                "again - this tool never cancels one. Nothing was stopped: the source keeps "
                "serving, its auto-deploy is switched back on and the created target is deleted."
            ),
        )
    return record


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
