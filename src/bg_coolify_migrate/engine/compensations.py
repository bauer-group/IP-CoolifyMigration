"""Compensating actions.

Each reads its undo info from the **journal**, not from the context: after a
crash there is no context. That is the whole reason the journal records what it
records.

Best-effort but loud. A compensation that fails does not stop the others — a
failure to delete the target must not also prevent restarting the source — but it
is recorded and the run reports honestly at the end.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import structlog

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.discovery import docker, pins
from bg_coolify_migrate.domain import binds, domains
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.containment import unsettled, volume_recreated
from bg_coolify_migrate.domain.deployments import (
    Deployment,
    Progress,
    deployment_uuid_of,
    judge,
    restart_blocked_notice,
)
from bg_coolify_migrate.domain.statemachine import (
    START_REQUESTED,
    STARTED_EARLY,
    STOP_REQUESTED,
    Compensation,
    State,
    source_copy_unproven,
    source_was_stopped,
)
from bg_coolify_migrate.engine import containment, deploy_triggers, keys
from bg_coolify_migrate.engine.context import (
    MigrationContext,
    deserialise_bind_pairs,
    deserialise_volume_pairs,
)
from bg_coolify_migrate.errors import CoolifyApiError, MigrationError, TransferError
from bg_coolify_migrate.journal.store import JournalRecord
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer import binds as transfer_binds

log = structlog.get_logger(__name__)


async def undo_create_target(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Delete the resources we created — each only once its source holds its hosts.

    ``delete_volumes=True`` is correct here and only here: these are volumes WE
    created on the target minutes ago. The source's volumes are never touched by
    a rollback.

    Giving the source its domains back is RESTORE_SOURCE_FQDN, a compensation of
    its own, so the planner can run it BEFORE the source is restarted — see
    ``statemachine._PRECEDENCE``. This one only checks the outcome: a target that
    holds custom hosts is deleted only once its source holds every one of them
    again (or never let go — every rollback before FINALIZE). Deleting the last
    holder of a host is what makes Coolify (4.4+) delete a Coolify-managed DNS
    record for good (ManagedDnsRecordCleanup v4.5.0:322-342); a stopped target
    left in place keeps the record alive and harms nothing. Such a target is
    kept, named, and the rollback ends ROLLBACK_FAILED: give the source its
    domains back, then ``rollback <id>`` again deletes it.
    """
    target_uuids: dict[str, str] = undo_info.get("target_uuids") or {}
    restores: Mapping[str, dict[str, Any]] = undo_info.get("parked_domains") or {}
    delete_failures: list[str] = await _release_target_side(ctx)
    kept: list[str] = []
    for source_uuid, target_uuid in target_uuids.items():
        try:
            collection = ctx.collection_of(source_uuid)
        except KeyError:
            log.warning("compensate.unknown_resource", source_uuid=source_uuid)
            continue
        if reason := await _source_lacks_its_hosts(ctx, collection, source_uuid, restores):
            kept.append(f"{_source_name(ctx, source_uuid)}'s target {target_uuid} ({reason})")
            log.error("compensate.target_kept", uuid=target_uuid, reason=reason)
            continue
        try:
            await ctx.api.delete_resource(collection, target_uuid, delete_volumes=True)
            log.info("compensate.target_deleted", uuid=target_uuid)
        except Exception as exc:
            if isinstance(exc, CoolifyApiError) and exc.status_code == 404:
                # Already gone: a re-run rollback, an in-step undo that got this
                # far before, or a retried DELETE whose first attempt landed.
                # Counting it as a failure meant a rollback could never converge.
                log.info("compensate.target_already_gone", uuid=target_uuid)
                continue
            delete_failures.append(target_uuid)
            log.error("compensate.target_delete_failed", uuid=target_uuid, error=str(exc)[:200])
        if collection == "databases":
            # Deliberately NOT deleted with the schedules: a rollback can follow a
            # SUCCEEDED run, and then these dumps are the last copy of everything
            # written since the cutover. An orphaned dump is recoverable.
            log.info(
                "compensate.target_backup_files_kept",
                uuid=target_uuid,
                hint=(
                    "any backup dumps of this target stay under /data/coolify/backups/"
                    f"databases/<team>/<name>-{target_uuid}/ on the target server, and "
                    "under its S3 prefix if uploaded"
                ),
            )

    problems: list[str] = []
    if kept:
        problems.append(
            "target(s) KEPT, holding custom domains their source does not hold: "
            + "; ".join(kept)
            + f" - give the source its domains back, then `coolify-migrate rollback "
            f"{ctx.migration_id}` deletes them (deleting the last holder would let "
            "Coolify delete a DNS record it manages; a source this rollback restarted "
            "is not restarted again)"
        )
    if delete_failures:
        # Surfaced so the rollback reports honestly.
        problems.append("could not delete target(s): " + ", ".join(delete_failures))
    if problems:
        raise RuntimeError("; ".join(problems))


async def _source_lacks_its_hosts(
    ctx: MigrationContext,
    collection: str,
    source_uuid: str,
    restores: Mapping[str, dict[str, Any]],
) -> str | None:
    """Why a target may NOT be deleted yet, or ``None`` when it may.

    Only a source whose journalled restore body names custom hosts is read: a
    target of anything else holds no host its source could lose. A source that
    will not answer counts as not holding them — deleting on a guess is the one
    move that cannot be taken back.
    """
    body = restores.get(source_uuid)
    if not body or collection not in ("applications", "services"):
        return None
    source_wildcard = ctx.plan.source_server.wildcard_domain or None
    if not any(
        domains.is_custom(h, source_wildcard=source_wildcard) for h in domains.body_hosts(body)
    ):
        return None
    try:
        current = await ctx.api.get_resource(collection, source_uuid)
    except Exception as exc:
        return f"its source could not be read: {_why(exc)}"
    missing = domains.missing_custom_hosts(body, current, source_wildcard=source_wildcard)
    if missing:
        return "its source does not hold " + ", ".join(missing)
    return None


async def undo_restore_source_deploy_triggers(
    ctx: MigrationContext, undo_info: dict[str, Any]
) -> None:
    """Switch back on the source deploy switches QUIESCE switched off.

    Owed to CREATE_TARGET, which has always completed by the time QUIESCE runs —
    the reasoning of RESTORE_SOURCE_FQDN and RELEASE_SOURCE_PINS — so it is owed
    even when QUIESCE refuses before the stop or fails after it. It reads
    QUIESCE's journal itself (``undo_info`` is CREATE's) and is a no-op when that
    holds no originals. ``statemachine._PRECEDENCE`` runs it after the domain
    restore and before RESTART_SOURCE. A rollback after a SUCCEEDED keep or rename
    run owes it too: FINALIZE left that source disarmed on purpose. Once per
    disarm, like RESTART_SOURCE once per stop (``deploy_triggers.restore_sources``):
    ``rollback <id>`` replays it, and a replay must not switch back on what the
    operator switched off since.
    """
    await deploy_triggers.restore_sources(ctx)


async def undo_disarm_targets(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Switch off again the target auto-deploy FINALIZE switched on.

    Owed to FINALIZE, so ``undo_info`` is FINALIZE's merged journal detail — the
    targets it journalled before arming each (``deploy_triggers.ARM_REQUESTED``).
    First in every rollback after FINALIZE (``statemachine._COMPENSATION``):
    STOP_TARGET, DROP_TARGET_VOLUMES and RESTART_SOURCE's wait all come before
    the target's delete, and a push to an armed target redeploys it in between —
    or for good, when DELETE_TARGET_RESOURCE keeps it. Idempotent
    (``deploy_triggers.disarm_targets``), since ``rollback <id>`` replays it.
    """
    await deploy_triggers.disarm_targets(ctx, undo_info)


async def undo_restore_source_fqdn(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Give the source back the domains FINALIZE released.

    Owed to CREATE_TARGET, so ``undo_info`` is CREATE's merged journal records —
    the restore bodies (journal key ``parked_domains``, a name kept from the days
    CREATE parked them) and the targets made. Both survive a crash. Before
    FINALIZE the source never lost a host, so this compares and writes nothing.
    """
    await restore_source_domains(
        ctx,
        undo_info.get("parked_domains") or {},
        targets=undo_info.get("target_uuids") or {},
    )


async def restore_source_domains(
    ctx: MigrationContext,
    parked: Mapping[str, dict[str, Any]],
    *,
    targets: Mapping[str, str],
    holders: frozenset[str] | None = None,
) -> None:
    """Give every source the domains of its journalled restore body. Loud on failure.

    Each source is read first and PATCHed only where it differs
    (``restore_application_domains`` / ``restore_service_urls``), so a rollback
    before FINALIZE writes nothing. Every resource is attempted, and the failures
    are raised together at the end: one source that will not take its domains
    back must not stop the next one from getting its own. The error names each
    source and the exact domains to set back FIRST, because the executor keeps
    only the first 300 characters.

    ``holders`` — the uuids a 409 may be overridden for — defaults to
    :func:`own_domain_holders`, read at the first 409 (a restore that finds
    nothing to send reads nothing more). Pass it when the targets are about to
    be deleted first (:func:`undo_partial_create`).
    """
    if not parked:
        return
    known: list[frozenset[str]] = [holders] if holders is not None else []

    async def own() -> frozenset[str]:
        if not known:
            known.append(await own_domain_holders(ctx, targets))
        return known[0]

    failures: list[str] = []
    for source_uuid, body in parked.items():
        try:
            collection = ctx.collection_of(source_uuid)
            if await _restore_one(ctx, collection, source_uuid, body, own=own):
                log.info("compensate.source_domains_restored", uuid=source_uuid)
        except Exception as exc:
            log.error(
                "compensate.source_domains_restore_failed",
                uuid=source_uuid,
                error=str(exc)[:200],
            )
            failures.append(
                f"{_source_name(ctx, source_uuid)} -> {domains.restore_summary(body)} ({_why(exc)})"
            )
    if failures:
        raise RuntimeError(
            "domains NOT restored: "
            + "; ".join(failures)
            + " - set them back on the source in Coolify and restart it; until then it "
            "answers only on its blank or generated hosts"
        )


async def _release_target_side(ctx: MigrationContext) -> list[str]:
    """Unpin the target's volumes and revoke the transfer key, before the delete.

    Both are owed by COPY, whose own compensations run only if COPY completed.
    A COPY that fails part-way leaves its pins and its authorised key with
    nothing to undo them - and a pinned volume survives the delete below, since
    Coolify's ``docker volume rm -f`` cannot remove a referenced volume and
    swallows the refusal (v4.5.0:app/Models/StandalonePostgresql.php:180). Both
    are found by markers carrying the migration id, so this is exact, and a
    no-op when there is nothing to undo.

    Best-effort: a failure is reported, never allowed to stop the delete.
    """
    failures: list[str] = []
    try:
        await pins.release(ctx.target_host, migration_id=ctx.migration_id, side=pins.TARGET)
    except Exception as exc:
        failures.append(f"target pins: {str(exc)[:200]}")
        log.error("compensate.target_pins_release_failed", error=str(exc)[:200])
    try:
        await keys.revoke(
            source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id
        )
    except Exception as exc:
        failures.append(f"transfer key: {str(exc)[:200]}")
        log.error("compensate.key_revoke_failed", error=str(exc)[:200])
    return failures


class _HeldElsewhere(RuntimeError):
    """A 409 names a holder this migration did not create, so it is not forced."""


async def _restore_one(
    ctx: MigrationContext,
    collection: str,
    source_uuid: str,
    body: dict[str, Any],
    *,
    own: Callable[[], Awaitable[frozenset[str]]],
) -> bool:
    """Restore one source, overriding a conflict only with our OWN resources.

    Returns whether a PATCH was sent. The PATCH saves before it answers (fill +
    save, ApplicationsController v4.5.0:3766-3780), so a ``/restart`` that
    follows reads the restored row.

    A 409 whose holders are all this migration's own (:func:`domains.
    only_own_holders`) is retried ONCE with ``force_domain_override`` — in the
    update ``$allowedFields`` at every tag from v4.1.2 (applications :2368,
    services :962) to v4.5.0 (:3363, :1250). After FINALIZE the target still
    exists and holds the custom domain: the restore runs before its delete, so
    it always meets it. Any other holder is the operator's to resolve.
    """
    send: Callable[[dict[str, Any]], Awaitable[bool]]
    if collection == "services":
        send = functools.partial(api_resources.restore_service_urls, ctx.api, source_uuid)
    elif collection == "applications":
        send = functools.partial(api_resources.restore_application_domains, ctx.api, source_uuid)
    else:
        return False
    try:
        return await send(body)
    except CoolifyApiError as exc:
        if exc.status_code != 409:
            raise
        holders = domains.conflict_holders(exc.body)
        if not domains.only_own_holders(exc.body, await own()):
            raise _HeldElsewhere(
                "HTTP 409: held by "
                + (", ".join(holders) or "a holder Coolify did not name")
                + ", which this migration did not create - not forcing"
            ) from exc
        log.warning("compensate.source_domains_conflict_forcing", uuid=source_uuid, holders=holders)
        return await send({**body, "force_domain_override": True})


async def own_domain_holders(ctx: MigrationContext, targets: Mapping[str, str]) -> frozenset[str]:
    """Every uuid a domain 409 may name for a resource of THIS migration.

    The plan's source resources and the targets this run created, plus the
    sub-application uuids of each source and target SERVICE: a service's domains
    live on its service_applications, and a conflict names that row (domains.php
    v4.1.2:226-232, v4.5.0:304-310). ``GET /services/{uuid}`` loads
    ``applications`` (ServicesController v4.1.2:736, v4.5.0:885).

    Best-effort: a resource that is already gone, or will not answer,
    contributes only its own uuid — at worst a restore is not forced and fails
    loudly.
    """
    own = set(targets.values())
    services: list[str] = []
    for resource in ctx.plan.resources:
        own.add(resource.snapshot.uuid)
        if resource.snapshot.collection == "services":
            services.append(resource.snapshot.uuid)
            if target := targets.get(resource.snapshot.uuid):
                services.append(target)
    for uuid in services:
        try:
            full = await ctx.api.get_resource("services", uuid)
        except Exception as exc:
            log.debug("compensate.holders_unavailable", uuid=uuid, error=str(exc)[:120])
            continue
        own |= api_resources.sub_application_uuids(full)
    return frozenset(own)


async def undo_partial_create(
    ctx: MigrationContext,
    targets: Mapping[str, str],
    parked: Mapping[str, dict[str, Any]],
    *,
    holders: frozenset[str] | None = None,
) -> list[str]:
    """Undo what an unfinished CREATE did. Returns what could NOT be undone.

    CREATE never writes to a source, so the restore is a comparison that finds
    nothing to send — kept for journals of runs that parked (up to 2.9). The
    targets are deleted first, each only while its source holds its hosts
    (:func:`undo_create_target`). The holders are read BEFORE the delete, while
    a target service's sub-applications can still be listed — or passed in by
    a caller that read them already.

    No restart: CREATE runs before QUIESCE, so the source was never stopped.
    """
    if holders is None:
        holders = (
            await own_domain_holders(ctx, targets) if parked else frozenset(targets.values())
        )
    problems: list[str] = []
    try:
        await undo_create_target(
            ctx, {"target_uuids": dict(targets), "parked_domains": dict(parked)}
        )
    except Exception as exc:
        problems.append(str(exc))
    try:
        await restore_source_domains(ctx, parked, targets=targets, holders=holders)
    except Exception as exc:
        problems.append(str(exc))
    return problems


def _source_name(ctx: MigrationContext, source_uuid: str) -> str:
    for resource in ctx.plan.resources:
        if resource.snapshot.uuid == source_uuid:
            return resource.snapshot.name
    return source_uuid


def _why(exc: Exception) -> str:
    """A short, secret-free reason. Coolify's message, never the request body."""
    if isinstance(exc, CoolifyApiError):
        message = exc.body.get("message") if isinstance(exc.body, dict) else None
        status = f"HTTP {exc.status_code}" if exc.status_code else "API error"
        return f"{status}: {message}" if message else status
    lines = str(exc).splitlines()
    return lines[0][:160] if lines else type(exc).__name__


async def undo_quiesce(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Restart the source. The single most important compensation.

    Everything else is cleanup; this is the one that ends the outage.

    Once per stop, per resource: a resource an earlier rollback of this journal
    already restarted (:func:`_restarted_since_stop`) is serving again, and
    ``/restart`` is a full stop and start — ``docker rm -f`` for a database, a
    redeploy for an image-built app. Every compensation is replayed when the
    operator runs ``rollback <id>`` again (a KEPT target, a kept pin, a failed
    restore), so without this the tool's own remedy took production down twice.
    A restart that failed is not recorded and is tried again.

    An application's ``/restart`` is a deployment, accepted with HTTP 200 before
    it runs, and it can fail after QUIESCE removed the containers — a build
    error, a full queue, or from 4.7.0 Coolify's image-scan gate
    (``domain/deployments.py``). So an application counts as restarted only once
    its deployment FINISHED (:func:`_await_restart_deployments`). The deployment
    uuid is journalled when Coolify accepts it, so a rollback interrupted while
    it watched one watches it again instead of redeploying the source a second
    time (:func:`_restart_deployments_since_stop`).
    """
    records = list(ctx.journal.read())
    already = _restarted_since_stop(records)
    asked = _restart_deployments_since_stop(records)
    failures: list[str] = []
    watch: dict[str, str] = {}
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        if snapshot.uuid in already:
            log.info("compensate.source_already_restarted", name=snapshot.name)
            continue
        if snapshot.uuid in asked and await _restart_stands(ctx, asked[snapshot.uuid]):
            watch[snapshot.uuid] = asked[snapshot.uuid]
            continue
        try:
            # restart, NOT start. QUIESCE removed the container (docker rm -f),
            # but Coolify's status column lags and can still read "running", and
            # /start refuses with 400 "already running" while the source is in
            # fact down. /restart carries no such guard and actually brings it
            # back. Found by the e2e rollback test — the single postgres test
            # never rolled back, so it never hit this.
            answer = await ctx.api.restart(snapshot.collection, snapshot.uuid)
        except Exception as exc:
            failures.append(f"{snapshot.name}: {exc}")
            log.error("compensate.source_restart_failed", name=snapshot.name, error=str(exc)[:200])
            continue
        deployment = deployment_uuid_of(answer) if snapshot.collection == "applications" else None
        if deployment is None:
            # A database or a service queues no deployment, and a "skipped" answer
            # names none (a deployment of that commit is already queued or running;
            # a recorded restart of ours that stands is watched, never asked
            # again): nothing to watch, so accepted is all there is to know.
            _journal_restarted(ctx, snapshot.uuid, snapshot.name)
            continue
        ctx.journal.append(
            "rollback_step",
            state=State.QUIESCE.value,
            detail={
                "compensation": Compensation.RESTART_SOURCE.value,
                "restart_deployment": deployment,
                "source": snapshot.uuid,
            },
        )
        watch[snapshot.uuid] = deployment
    failures.extend(await _await_restart_deployments(ctx, watch))
    if failures:
        # The source stays down, so RELEASE_SOURCE_PINS will find its volumes
        # unmounted and keep their pins; say so where the operator looks first.
        raise RuntimeError(
            "could not restart: "
            + "; ".join(failures)
            + " (its volume pins against Coolify's Docker cleanup stay until it runs again;"
            f" `coolify-migrate rollback {ctx.migration_id}` retries only these restarts"
            " and then releases the pins - or start it yourself, then"
            f" `coolify-migrate unpin {ctx.migration_id}`)"
        )


#: How often a rollback re-reads the deployments of the restarts it asked for.
_DEPLOYMENT_POLL_INTERVAL = 5.0


def _journal_restarted(ctx: MigrationContext, source_uuid: str, name: str) -> None:
    """Record that this stop's restart of one source is done — the once-per-stop mark."""
    log.info("compensate.source_restarted", name=name)
    ctx.journal.append(
        "rollback_step",
        state=State.QUIESCE.value,
        detail={"compensation": Compensation.RESTART_SOURCE.value, "restarted": source_uuid},
    )


async def _restart_stands(ctx: MigrationContext, deployment_uuid: str) -> bool:
    """Whether a restart an interrupted rollback asked for may still bring its source up.

    Queued, running or finished: it stands, and asking again would redeploy a
    source that is coming up. Failed, cancelled or missing: ask again.

    Unreadable stands too, and is watched (:func:`_await_restart_deployments`
    reads it again each round): a read error decides nothing. Asking again on one
    redeployed a source the first restart had brought back, or got a 'skipped'
    answer naming nothing to watch while that restart was still running
    (queue_application_deployment v4.7.0:65-79), and journalled a restart nobody
    had seen finish.
    """
    try:
        found = await ctx.api.get_deployment(deployment_uuid)
    except Exception as exc:
        log.debug(
            "compensate.restart_deployment_unread", uuid=deployment_uuid, error=str(exc)[:120]
        )
        return True
    return judge(deployment_uuid, found).progress is not Progress.FAILED


async def _await_restart_deployments(ctx: MigrationContext, watch: Mapping[str, str]) -> list[str]:
    """Wait for each application restart's deployment to end. Returns the failures.

    A deployment that FINISHED is journalled as restarted. One that failed, was
    cancelled or does not exist (:func:`domain.deployments.judge`) is not: the
    source is still down, RESTART_SOURCE fails, RELEASE_SOURCE_PINS keeps the pins
    without a look, and the next ``rollback <id>`` asks again. A gate block also
    leaves the way out as a notice (:func:`domain.deployments.restart_blocked_notice`).

    Bounded by ``deploy_timeout``. A deployment still pending then is journalled
    as restarted, as every accepted restart was before this watch existed: a slow
    queue must never cost the source a second redeploy. A read that fails decides
    nothing and is tried again on the next round.
    """
    names = {r.snapshot.uuid: r.snapshot.name for r in ctx.plan.resources}
    pending = dict(watch)
    failures: list[str] = []
    waited = 0.0
    while True:
        for source_uuid, deployment_uuid in list(pending.items()):
            try:
                found = await ctx.api.get_deployment(deployment_uuid)
            except Exception as exc:
                log.debug(
                    "compensate.restart_deployment_unread",
                    uuid=deployment_uuid,
                    error=str(exc)[:120],
                )
                continue
            verdict = judge(deployment_uuid, found)
            if verdict.progress is Progress.PENDING:
                continue
            del pending[source_uuid]
            name = names.get(source_uuid, source_uuid)
            if verdict.progress is Progress.FINISHED:
                _journal_restarted(ctx, source_uuid, name)
                continue
            failures.append(f"{name}: {verdict.reason}")
            log.error(
                "compensate.source_restart_deployment_failed", name=name, uuid=deployment_uuid
            )
            if verdict.gate_blocked:
                ctx.notices.append(
                    restart_blocked_notice(name, deployment_uuid, migration_id=ctx.migration_id)
                )
        if not pending:
            return failures
        if waited >= ctx.settings.deploy_timeout:
            break
        await asyncio.sleep(_DEPLOYMENT_POLL_INTERVAL)
        waited += _DEPLOYMENT_POLL_INTERVAL
    for source_uuid, deployment_uuid in pending.items():
        name = names.get(source_uuid, source_uuid)
        log.warning("compensate.source_restart_unfinished", name=name, uuid=deployment_uuid)
        _journal_restarted(ctx, source_uuid, name)
        ctx.notices.append(
            f"{name}'s restart (deployment {deployment_uuid}) had not finished after "
            f"{ctx.settings.deploy_timeout:.0f}s; it may still be running - check it in Coolify. "
            "If it failed, start the source there, then "
            f"coolify-migrate unpin {ctx.migration_id}."
        )
    return failures


def _restart_deployments_since_stop(records: Iterable[JournalRecord]) -> dict[str, str]:
    """Source uuid -> the deployment of the last restart asked for after the last stop. PURE.

    Reset by a stop request like :func:`_restarted_since_stop`; a later request
    for the same source replaces an earlier one.
    """
    asked: dict[str, str] = {}
    for record in records:
        if (
            record.event == "step_started"
            and record.state == State.QUIESCE.value
            and record.detail.get(STOP_REQUESTED) is True
        ):
            asked.clear()
        elif (
            record.event == "rollback_step"
            and record.detail.get("compensation") == Compensation.RESTART_SOURCE.value
            and isinstance(source := record.detail.get("source"), str)
            and isinstance(deployment := record.detail.get("restart_deployment"), str)
        ):
            asked[source] = deployment
    return asked


def _restarted_since_stop(records: Iterable[JournalRecord]) -> set[str]:
    """The source uuids a rollback restarted after this run's last stop request. PURE.

    QUIESCE journals ``stop_requested`` before it asks Coolify to stop anything,
    so a restart recorded after it ended that stop; one recorded before it
    belongs to a stop since superseded.
    """
    restarted: set[str] = set()
    for record in records:
        if (
            record.event == "step_started"
            and record.state == State.QUIESCE.value
            and record.detail.get(STOP_REQUESTED) is True
        ):
            restarted.clear()
        elif (
            record.event == "rollback_step"
            and record.detail.get("compensation") == Compensation.RESTART_SOURCE.value
            and isinstance(uuid := record.detail.get("restarted"), str)
        ):
            restarted.add(uuid)
    return restarted


async def undo_copy(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Drop the target's volumes so a resumed run cannot merge into a partial copy.

    Unpinned first: Docker refuses to remove a volume a container references,
    even with ``-f``, and ``remove_volume`` only logs that refusal.

    Then the bind mounts' targets (:func:`_remove_bind_targets`).
    """
    await pins.release(ctx.target_host, migration_id=ctx.migration_id, side=pins.TARGET)
    volumes: list[str] = undo_info.get("volumes_copied") or []
    for name in volumes:
        await docker.remove_volume(ctx.target_host, name)
    if volumes:
        log.info("compensate.volumes_dropped", count=len(volumes))
    await _remove_bind_targets(ctx)


async def _remove_bind_targets(ctx: MigrationContext) -> None:
    """Delete on the TARGET server only what COPY created for a bind mount.

    Exactly the paths COPY journalled as absent before it wrote them
    (``binds.BIND_TARGET``), each passing :func:`binds.removable`, and none a
    previous rollback already removed (``binds.BINDS_REMOVED``): a replayed
    rollback must not delete what someone created there since. A path that was
    already there, empty, is not this run's to delete — inside the target's own
    directory Coolify's delete of the target takes it (``deleteConfigurations``,
    Application/Service v4.7.2:587-594/226-233); outside, the run says so.

    After FINALIZE (keep or rename) the target may hold the only copy: a target
    path is deleted only while the source still holds its bind
    (:func:`statemachine.source_copy_unproven`), else it is kept and the
    compensation fails loudly.

    Each removal is journalled in its own record the moment it succeeds, not
    once at the end: a later one that fails (a refused path is kept and the
    loop goes on; any other error, an ``SshError`` from a failed ``rm``,
    escapes) must not leave the earlier ones unrecorded, or the replayed
    rollback its failure asks for would ``rm -rf`` them again.
    """
    owed = binds.owed_bind_targets((r.event, r.detail) for r in ctx.journal.read())
    if not owed:
        return
    target_uuids = set(ctx.target_uuids.values()) | set(
        (ctx.journal.undo_info(State.CREATE_TARGET.value).get("target_uuids") or {}).values()
    )
    sources = {
        pair.target_path: pair.source_path
        for found in deserialise_bind_pairs(
            ctx.journal.undo_info(State.DISCOVER.value).get("volume_pairs")
        ).values()
        for pair in found
    }
    guarded = source_copy_unproven(ctx.journal.details())
    source_states: dict[str, str] = {}
    if guarded:
        source_states = await transfer_binds.path_states(
            ctx.source_host, sorted({s for t, s in sources.items() if t in owed})
        )

    removed: list[str] = []
    kept: list[str] = []
    for target in owed.values():
        if target.prior != binds.STATE_MISSING:
            if not any(binds.own_dir_of(target.path, u) for u in target_uuids):
                ctx.notices.append(
                    f"{target.path} on {ctx.plan.target_server.name} was empty before this "
                    "migration and now holds a copy of the source's bind mount. The rollback "
                    "deletes only what the migration created, so it is left as it is: empty it "
                    "by hand if the copy is not wanted."
                )
            continue
        if guarded:
            # No DISCOVER record of its source proves nothing: kept, like a gone one.
            source = sources.get(target.path)
            state = source_states.get(source, binds.STATE_MISSING) if source else None
            if state in (None, binds.STATE_MISSING):
                kept.append(f"{target.path} (the source's {source or 'bind'} is gone)")
                continue
        allowed = {p for p in target.created if binds.removable(p, target_uuids=target_uuids)}
        if target.path not in allowed:
            log.error("compensate.bind_target_unsafe", path=target.path)
            kept.append(f"{target.path} (not a path a rollback may delete)")
            continue
        try:
            await transfer_binds.remove_created(ctx.target_host, target, removable=allowed)
        except TransferError as exc:
            # A path that resolves elsewhere now is not deleted; the others still
            # are, each journalled as it goes.
            log.error("compensate.bind_target_redirected", path=target.path)
            kept.append(f"{target.path} ({exc.args[0] if exc.args else exc})")
            continue
        ctx.journal.append(
            "rollback_step",
            state=State.COPY.value,
            detail={
                "compensation": Compensation.DROP_TARGET_VOLUMES.value,
                binds.BINDS_REMOVED: [target.path],
            },
        )
        removed.append(target.path)

    if removed:
        log.info("compensate.bind_targets_removed", count=len(removed))
    if kept:
        raise RuntimeError(
            f"bind target(s) KEPT on {ctx.plan.target_server.name}: {', '.join(kept)} - they may "
            "hold the only copy of the data; delete them by hand once it is safe"
        )


async def undo_revoke_key(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Revoke the ephemeral key.

    Runs even when the fingerprint is absent from the journal: the revocation is
    keyed on our comment marker, which contains the migration id, so we can
    always find exactly our line and never anyone else's.
    """
    await keys.revoke(source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id)


async def undo_start_target(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Stop the target before its volumes are dropped.

    Ordering matters: a volume in use cannot be removed, and deleting the
    resource before dropping volumes would leak them.

    Every target that may run, not only the ones a completed START lists: each
    one START asked for (``START_REQUESTED``, journalled before the request, so
    a START that failed part-way leaves none running) and each one a checkpoint
    found started by someone else (``STARTED_EARLY``). Stopping one that never
    started is harmless (``statemachine.target_may_run``).

    An application's ``/stop`` holds back no deployment: StopApplication runs on
    the 'high' queue and cancels none (v4.7.2:17), so one still queued or
    building — START's own, when a later target's start failed a moment after,
    or someone else's, when Coolify skipped START's — lands after the stop and
    serves the claimed hosts beside the restarted source; DeleteResourceJob then
    cancels a preview's deployments only (v4.7.2:234-278). So every target
    application's deployments are brought to an end first
    (:func:`_settle_target_deployments`). One still unsettled at
    ``deploy_timeout`` fails this compensation — after the stops are sent, so
    the rest of the rollback still runs.
    """
    started: list[str] = []
    for key in ("started", START_REQUESTED, STARTED_EARLY):
        raw = undo_info.get(key)
        for uuid in raw if isinstance(raw, list) else []:
            if isinstance(uuid, str) and uuid not in started:
                started.append(uuid)
    collections = {t: _collection_of_target(ctx, t) for t in started}
    applications = [t for t in started if collections[t] == "applications"]
    left = (
        await _settle_target_deployments(ctx, applications, _start_deployments(ctx, undo_info))
        if applications
        else []
    )
    for target_uuid in started:
        try:
            await ctx.api.stop(collections[target_uuid], target_uuid)
            log.info("compensate.target_stopped", uuid=target_uuid)
        except Exception as exc:
            log.error("compensate.target_stop_failed", uuid=target_uuid, error=str(exc)[:200])
            raise
    if left:
        names = _target_names(ctx)
        listed = "; ".join(
            f"{names.get(t, t)}: "
            + (f"deployment {d.uuid}" if d.uuid else "its deployments could not be read")
            for t, d in left
        )
        raise RuntimeError(
            f"still queued or running after {ctx.settings.deploy_timeout:.0f}s - {listed}. "
            "A stop cannot hold a deployment back, so it may still bring that target up: "
            "cancel it in the target's Deployments in Coolify, then stop the target there"
        )


#: How many times a rollback re-reads a target's deployments at once, after it
#: asked to cancel one it had not seen, before it waits between reads. A cancel marks
#: the row before it answers (DeployController v4.7.2:268-279), but it also
#: lets Coolify start the next queued deployment (next_after_cancel, :317).
_QUICK_SETTLE_ROUNDS = 3


def _collection_of_target(ctx: MigrationContext, target_uuid: str) -> str:
    source_uuid = next((s for s, t in ctx.target_uuids.items() if t == target_uuid), None)
    return ctx.collection_of(source_uuid) if source_uuid else "applications"


def _target_names(ctx: MigrationContext) -> dict[str, str]:
    """Target uuid -> the name of the source it belongs to."""
    names = {r.snapshot.uuid: r.snapshot.name for r in ctx.plan.resources}
    return {t: names.get(s, s) for s, t in ctx.target_uuids.items()}


def _start_deployments(ctx: MigrationContext, undo_info: dict[str, Any]) -> dict[str, list[str]]:
    """Target uuid -> the deployments START journalled for it (keyed by source uuid there)."""
    raw = undo_info.get("deployments")
    out: dict[str, list[str]] = {}
    for source_uuid, deployment in (raw if isinstance(raw, dict) else {}).items():
        target_uuid = ctx.target_uuids.get(str(source_uuid))
        if target_uuid and isinstance(deployment, str) and deployment:
            out.setdefault(target_uuid, []).append(deployment)
    return out


async def _settle_target_deployments(
    ctx: MigrationContext, targets: list[str], known: Mapping[str, list[str]]
) -> list[tuple[str, Deployment]]:
    """Cancel every queued or running deployment of these target applications.

    Returns what is still unsettled at ``deploy_timeout``. Each round reads every
    target's deployments (``GET /deployments/applications/{uuid}``; START's own
    journalled ones read one by one when the list misses them) and asks Coolify
    to cancel each one still queued or in progress — again on later rounds,
    because a cancel that failed can succeed, and one answered 400 has ended.
    A read that fails counts as unsettled: a stop is safe only once nothing can
    start the target after it. Targets of this run only; never a source.
    """
    seen: set[str] = set()
    waited = 0.0
    quick = 0
    while True:
        flight = await _unsettled_deployments(ctx, targets, known)
        if not flight:
            return []
        fresh = any(d.uuid not in seen for _, d in flight)
        for target_uuid, deployment in flight:
            seen.add(deployment.uuid)
            if deployment.uuid:
                await _cancel_target_deployment(ctx, target_uuid, deployment.uuid)
        if fresh and quick < _QUICK_SETTLE_ROUNDS:
            quick += 1
            continue
        if waited >= ctx.settings.deploy_timeout:
            for target_uuid, deployment in flight:
                log.error(
                    "compensate.target_deployment_unsettled",
                    uuid=target_uuid,
                    deployment=deployment.uuid or None,
                )
            return flight
        await asyncio.sleep(_DEPLOYMENT_POLL_INTERVAL)
        waited += _DEPLOYMENT_POLL_INTERVAL


async def _unsettled_deployments(
    ctx: MigrationContext, targets: list[str], known: Mapping[str, list[str]]
) -> list[tuple[str, Deployment]]:
    """Every target deployment still queued or running — or that could not be read."""
    out: list[tuple[str, Deployment]] = []
    for target_uuid in targets:
        try:
            rows = await ctx.api.application_deployments(target_uuid)
        except MigrationError as exc:
            log.warning(
                "compensate.target_deployments_unread", uuid=target_uuid, error=str(exc)[:120]
            )
            rows = None
            out.append((target_uuid, Deployment(uuid="", status="")))
        listed = {d.uuid: d for d in rows or []}
        for uuid in known.get(target_uuid, []):
            if uuid in listed:
                continue
            try:
                found = await ctx.api.get_deployment(uuid)
            except MigrationError as exc:
                log.warning("compensate.target_deployment_unread", uuid=uuid, error=str(exc)[:120])
                found = Deployment(uuid=uuid, status="")
            if found is not None:
                listed[uuid] = found
        out.extend((target_uuid, d) for d in unsettled(listed.values()))
    return out


async def _cancel_target_deployment(ctx: MigrationContext, target_uuid: str, uuid: str) -> None:
    try:
        cancelled = await ctx.api.cancel_deployment(uuid)
    except MigrationError as exc:  # read again next round; the deadline decides
        log.warning("compensate.target_deployment_cancel_failed", uuid=uuid, error=str(exc)[:120])
        return
    event = "cancelled" if cancelled else "ended"
    log.info(f"compensate.target_deployment_{event}", target=target_uuid, deployment=uuid)


async def undo_restore_source_name(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Undo a rename. Only meaningful for FinalizePolicy.RENAME.

    Nothing to do for DELETE: that is the one irreversible step, which is why it
    requires typed confirmation.

    From 4.7.0 the update applies Coolify's name rule as well. The restored name
    is the one CREATE sent, and the plan refused it unless it passed on the PLAN's
    release (fidelity.name_refusals). An instance upgraded to 4.7 after a FINALIZE
    on 4.6 can 422 it. It is raised like any other failure: the executor runs the
    remaining compensations and ends ROLLBACK_FAILED, and the source keeps its
    ``-old-`` name until the operator renames it in the UI.
    """
    policy = undo_info.get("policy")
    if policy != "rename":
        return
    for resource in ctx.plan.resources:
        try:
            await api_resources.rename(
                ctx.api,
                resource.snapshot.collection,
                resource.snapshot.uuid,
                resource.snapshot.name,
            )
        except Exception as exc:
            log.error("compensate.rename_failed", name=resource.snapshot.name, error=str(exc)[:200])
            raise


def _source_was_stopped(ctx: MigrationContext) -> bool:
    """Did this run (possibly) stop the source? Engine side of the one rule.

    :func:`statemachine.source_was_stopped`: QUIESCE completed, OR it journalled
    its ``stop_requested`` marker — a QUIESCE that failed after its stop request
    never completes, yet the source is down and the rollback owes (and runs)
    its restart, so the unpin must wait for it like after any other restart.
    """
    return source_was_stopped(
        ctx.journal.completed_states(), ctx.journal.undo_info(State.QUIESCE.value)
    )


def _restart_succeeded(records: Iterable[JournalRecord]) -> bool:
    """Did the CURRENT rollback restart the source? PURE.

    The latest RESTART_SOURCE outcome after the last ``rollback_started``: a
    journal can hold several rollbacks, and an earlier one's success says
    nothing about this one.
    """
    ok: bool | None = None
    for record in records:
        if record.event == "rollback_started":
            ok = None
        elif (
            record.event == "rollback_step"
            and record.detail.get("compensation") == Compensation.RESTART_SOURCE.value
            and "ok" in record.detail
        ):
            ok = record.detail["ok"] is True
    return ok is True


#: The shortest settle window: outlasts Coolify's rolling-update health check at
#: its defaults - a 5 s start period, then 10 checks 5 s apart of up to 5 s
#: each - after which a restarted container that failed it is removed.
_MIN_SETTLE = 120.0


def settle_window(settings: Settings) -> float:
    """How long a restarted source must keep its volumes mounted before its pins go.

    The longer of ``stop_timeout`` - the time a stop may take, so a stop still
    in Coolify's queue when the restart was asked for has landed by then - and
    Coolify's default health-check window (:data:`_MIN_SETTLE`).
    """
    return max(settings.stop_timeout, _MIN_SETTLE)


async def undo_release_source_pins(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Unpin the source's volumes - the last act of every rollback.

    Released only on durable evidence: a volume unpinned before the source
    mounts it again for good is exactly what Coolify's ``docker volume prune
    -af`` deletes. Idle pins (volumes nothing mounted before the migration) go
    at once. For the rest it depends on whether this run may have stopped the
    source (:func:`statemachine.source_was_stopped`):

    * no stop was ever requested - the source still serves: one look;
    * a stop may have been requested and this rollback's restart was
      accepted: running containers must mount every pinned volume within
      ``deploy_timeout`` and keep it mounted across :func:`settle_window`;
    * a stop may have been requested and the restart was NOT accepted: kept
      without a look. Coolify queues every stop, so a container seen running
      now may be the one that stop removes next.

    A kept pin is not a compensation failure. The source is no worse off than
    before - better: its data survives the cleanup - so the outcome stays
    ROLLED_BACK and the run report names the pin. ``coolify-migrate unpin
    <id>``, once the source runs, removes it - never ``rollback <id>`` again,
    which replays every compensation and redeploys a source serving by then.
    """
    stopped = _source_was_stopped(ctx)
    restarted = stopped and _restart_succeeded(ctx.journal.read())
    if stopped and not restarted:
        outcome = await pins.release_idle(
            ctx.source_host, migration_id=ctx.migration_id, side=pins.SOURCE
        )
        why = "the source was not restarted, and a stop this run asked for may still land"
    else:
        outcome = await pins.release_when_referenced(
            ctx.source_host,
            migration_id=ctx.migration_id,
            side=pins.SOURCE,
            timeout=ctx.settings.deploy_timeout if restarted else 0.0,
            settle=settle_window(ctx.settings) if restarted else 0.0,
        )
        why = "no running container kept them mounted"
    if not outcome.kept:
        return

    ctx.journal.append(
        "rollback_step",
        state=State.CREATE_TARGET.value,
        detail={
            "compensation": Compensation.RELEASE_SOURCE_PINS.value,
            "pins_kept": list(outcome.kept),
            "unmounted": list(outcome.unmounted),
        },
    )
    log.warning(
        "compensate.source_pins_kept",
        pins=list(outcome.kept),
        unmounted=list(outcome.unmounted),
        restarted=restarted,
    )
    remove = f"docker rm {' '.join(outcome.kept)}"
    unpin = f"coolify-migrate unpin {ctx.migration_id}"
    if restarted:
        remedy = f"Once the source mounts them again: {unpin} (or {remove} there)."
    elif stopped:
        remedy = (
            f"The source is not running: coolify-migrate rollback {ctx.migration_id} "
            "restarts it and then removes the pin. If you start it yourself instead, "
            f"{unpin} once it runs - not the rollback, which would restart it again."
        )
    else:
        remedy = f"Once the source runs: {unpin} (or {remove} there)."
    ctx.notices.append(
        f"{', '.join(outcome.kept)} still pins the source's volume(s) "
        f"{', '.join(outcome.unmounted)} on {ctx.plan.source_server.name}: {why}, so "
        f"without the pin Coolify's Docker cleanup could delete them. {remedy}"
    )


async def source_data_refusal(ctx: MigrationContext) -> str | None:
    """Why no rollback may run because the source lost its data, or ``None``. The saga's guard.

    Asked only once FINALIZE began with keep or rename
    (:func:`statemachine.source_copy_unproven`): before that the source's
    volumes are pinned and nobody deletes the source. After it, every
    compensation that follows aims at the TARGET — STOP_TARGET, then
    DROP_TARGET_VOLUMES, then the delete — and RESTART_SOURCE brings the source
    up on whatever volumes exist then; docker compose creates a missing one
    EMPTY (StartPostgresql v4.5.0:252-255, ``external: false``), so postgres
    runs initdb and the rollback reports ROLLED_BACK over an empty database.

    So it looks first: every source resource must still exist in Coolify, and
    every source volume the migration copied (DISCOVER's journal) on the source
    server. Then those volumes are pinned again (``discovery/pins.py``) before
    anything runs, so Coolify's cleanup cannot take them between this look and
    the restart; RELEASE_SOURCE_PINS, the last compensation, removes the pin
    once the restarted source mounts them.

    A name is not enough: a volume pruned after FINALIZE and recreated EMPTY by
    a redeploy (ApplicationDeploymentJob v4.7.2:5010-5030, ``external: false``)
    has the same one. So each volume's CreatedAt must still be the one COPY
    journalled (``containment.record_source_volumes``); a local volume's is the
    change time of its directory, new for every volume created under the name
    (moby v27.5.1 volume/local/local.go:229-236). A journal without the record
    gets the name check alone. A CreatedAt that cannot be read raises, and the
    saga refuses on that as on any guard that cannot answer.

    The same for every bind mount it copied: its source host path must still
    be what was copied (``binds.source_lost``). A missing one comes back EMPTY
    at the restart, and the target delete that follows takes the copy — Coolify
    ``rm -rf``s the target's directory (DeleteService v4.7.2:30-32,
    Service::deleteConfigurations v4.7.2:226-233), where a relative bind's copy
    lives. DROP_TARGET_VOLUMES keeps such a target too, but by then the
    rollback has already gone on to the restart and the delete.
    """
    if not source_copy_unproven(ctx.journal.details()):
        return None
    lost: list[str] = []
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        try:
            await ctx.api.get_resource(snapshot.collection, snapshot.uuid)
        except CoolifyApiError as exc:
            if exc.status_code != 404:
                return f"the source {snapshot.name} could not be read ({_why(exc)})"
            lost.append(f"{snapshot.name} (no longer in Coolify)")
    volumes = _copied_source_volumes(ctx)
    if volumes:
        present = {v.name for v in await docker.list_volumes(ctx.source_host)}
        lost.extend(f"volume {name}" for name in sorted(set(volumes) - present))
        lost.extend(await _recreated_volumes(ctx, sorted(set(volumes) & present)))
    sources = _copied_source_binds(ctx)
    if sources:
        # A host that will not answer raises, and the saga refuses on that too.
        states = await transfer_binds.path_states(ctx.source_host, sorted(sources))
        lost.extend(
            problem
            for path, pair in sorted(sources.items())
            if (
                problem := binds.source_lost(
                    path, states[path], kind=pair.kind, was_empty=pair.empty
                )
            )
        )
    if lost:
        return (
            f"the source no longer holds its data on {ctx.plan.source_server.name}: "
            + ", ".join(lost)
            + " - a server that deletes unused volumes pruned them after FINALIZE released "
            "their pins, a bind mount's host path was removed, or the kept source was "
            "deleted by hand. The target then holds the only copy, and a rollback would "
            "drop it and restart the source on empty volumes and directories"
        )
    if volumes:
        await pins.ensure(
            ctx.source_host, migration_id=ctx.migration_id, side=pins.SOURCE, volumes=volumes
        )
    return None


async def _recreated_volumes(ctx: MigrationContext, present: list[str]) -> list[str]:
    """The present copied volumes that are not the ones COPY read, by CreatedAt."""
    recorded = containment.recorded_volumes(ctx.journal.undo_info(State.COPY.value))
    names = [name for name in present if name in recorded]
    if not names:
        return []
    current = await containment.volumes_created(ctx.source_host, names)
    return [
        f"volume {name} (created {current.get(name, 'at an unknown time')}, after the copy "
        f"read the one created {recorded[name]}: recreated empty)"
        for name in names
        if name not in current or volume_recreated(recorded[name], current[name])
    ]


def _copied_source_volumes(ctx: MigrationContext) -> list[str]:
    """The source volume names DISCOVER paired — or, without its record, the plan's."""
    pairs = deserialise_volume_pairs(
        ctx.journal.undo_info(State.DISCOVER.value).get("volume_pairs")
    )
    if pairs:
        return sorted({p.source.name for found in pairs.values() for p in found})
    return sorted(
        {
            item.source_name
            for resource in ctx.plan.resources
            for item in resource.manifest.to_migrate
            if item.source_name
        }
    )


def _copied_source_binds(ctx: MigrationContext) -> dict[str, binds.BindPair]:
    """``source path -> pair`` of every bind DISCOVER paired — or, without its record, the plan's.

    Without the record nothing says the source was empty or which kind it was,
    so an empty source then reads as lost too: the answer that refuses.
    """
    raw = ctx.journal.undo_info(State.DISCOVER.value).get("volume_pairs")
    if isinstance(raw, dict):
        return {
            pair.source_path: pair
            for found in deserialise_bind_pairs(raw).values()
            for pair in found
        }
    return {
        item.source_path: binds.BindPair(item.source_path, item.source_path, item.mount_path)
        for resource in ctx.plan.resources
        for item in resource.manifest.to_migrate
        if item.mount_class is MountClass.BIND
    }


def build_compensations() -> dict[Any, Any]:
    return {
        Compensation.DELETE_TARGET_RESOURCE: undo_create_target,
        Compensation.RESTART_SOURCE: undo_quiesce,
        Compensation.DROP_TARGET_VOLUMES: undo_copy,
        Compensation.REVOKE_EPHEMERAL_KEY: undo_revoke_key,
        Compensation.STOP_TARGET: undo_start_target,
        Compensation.RESTORE_SOURCE_NAME: undo_restore_source_name,
        Compensation.RESTORE_SOURCE_FQDN: undo_restore_source_fqdn,
        Compensation.RESTORE_SOURCE_DEPLOY_TRIGGERS: undo_restore_source_deploy_triggers,
        Compensation.DISARM_TARGETS: undo_disarm_targets,
        Compensation.RELEASE_SOURCE_PINS: undo_release_source_pins,
    }
