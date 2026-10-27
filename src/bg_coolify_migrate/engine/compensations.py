"""Compensating actions.

Each reads its undo info from the **journal**, not from the context: after a
crash there is no context. That is the whole reason the journal records what it
records.

Best-effort but loud. A compensation that fails does not stop the others — a
failure to delete the target must not also prevent restarting the source — but it
is recorded and the run reports honestly at the end.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import structlog

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.discovery import docker, pins
from bg_coolify_migrate.domain import domains
from bg_coolify_migrate.domain.statemachine import Compensation, State, source_was_stopped
from bg_coolify_migrate.engine import keys
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import CoolifyApiError
from bg_coolify_migrate.journal.store import JournalRecord

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
            "Coolify delete a DNS record it manages)"
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
) -> list[str]:
    """Undo what an unfinished CREATE did. Returns what could NOT be undone.

    CREATE never writes to a source, so the restore is a comparison that finds
    nothing to send — kept for journals of runs that parked (up to 2.9). The
    targets are deleted first, each only while its source holds its hosts
    (:func:`undo_create_target`). The holders are read BEFORE the delete, while
    a target service's sub-applications can still be listed.

    No restart: CREATE runs before QUIESCE, so the source was never stopped.
    """
    holders = await own_domain_holders(ctx, targets) if parked else frozenset(targets.values())
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
    """
    failures: list[str] = []
    for resource in ctx.plan.resources:
        try:
            # restart, NOT start. QUIESCE removed the container (docker rm -f),
            # but Coolify's status column lags and can still read "running", and
            # /start refuses with 400 "already running" while the source is in
            # fact down. /restart carries no such guard and actually brings it
            # back. Found by the e2e rollback test — the single postgres test
            # never rolled back, so it never hit this.
            await ctx.api.restart(resource.snapshot.collection, resource.snapshot.uuid)
            log.info("compensate.source_restarted", name=resource.snapshot.name)
        except Exception as exc:
            failures.append(f"{resource.snapshot.name}: {exc}")
            log.error(
                "compensate.source_restart_failed",
                name=resource.snapshot.name,
                error=str(exc)[:200],
            )
    if failures:
        # The source stays down, so RELEASE_SOURCE_PINS will find its volumes
        # unmounted and keep their pins; say so where the operator looks first.
        raise RuntimeError(
            "could not restart: "
            + "; ".join(failures)
            + " (its volume pins against Coolify's Docker cleanup stay until it runs again)"
        )


async def undo_copy(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Drop the target's volumes so a resumed run cannot merge into a partial copy.

    Unpinned first: Docker refuses to remove a volume a container references,
    even with ``-f``, and ``remove_volume`` only logs that refusal.
    """
    await pins.release(ctx.target_host, migration_id=ctx.migration_id, side=pins.TARGET)
    volumes: list[str] = undo_info.get("volumes_copied") or []
    for name in volumes:
        await docker.remove_volume(ctx.target_host, name)
    if volumes:
        log.info("compensate.volumes_dropped", count=len(volumes))


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
    """
    started: list[str] = undo_info.get("started") or []
    for target_uuid in started:
        source_uuid = next((s for s, t in ctx.target_uuids.items() if t == target_uuid), None)
        collection = ctx.collection_of(source_uuid) if source_uuid else "applications"
        try:
            await ctx.api.stop(collection, target_uuid)
            log.info("compensate.target_stopped", uuid=target_uuid)
        except Exception as exc:
            log.error("compensate.target_stop_failed", uuid=target_uuid, error=str(exc)[:200])
            raise


async def undo_restore_source_name(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Undo a rename. Only meaningful for FinalizePolicy.RENAME.

    Nothing to do for DELETE: that is the one irreversible step, which is why it
    requires typed confirmation.
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


async def undo_release_source_pins(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Unpin the source's volumes - the last act of every rollback.

    Released only once something other than a pin mounts every pinned volume:
    a volume unpinned before the restarted source mounts it again is exactly
    what Coolify's ``docker volume prune -af`` deletes. Waited for up to
    ``deploy_timeout`` only when this rollback restarted the source; otherwise
    one look - the source is still serving (QUIESCE failed before the stop), or
    down with no restart coming (QUIESCE failed after it, or the restart
    failed), and then the pin must stay.

    A kept pin is not a compensation failure. The source is no worse off than
    before - better: its data survives the cleanup - so the outcome stays
    ROLLED_BACK and the run report names the pin. ``rollback <id>`` again, once
    the source runs, removes it.
    """
    restarted = _source_was_stopped(ctx) and _restart_succeeded(ctx.journal.read())
    outcome = await pins.release_when_referenced(
        ctx.source_host,
        migration_id=ctx.migration_id,
        side=pins.SOURCE,
        timeout=ctx.settings.deploy_timeout if restarted else 0.0,
    )
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
    ctx.notices.append(
        f"{', '.join(outcome.kept)} still pins the source's volume(s) "
        f"{', '.join(outcome.unmounted)} on {ctx.plan.source_server.name}: nothing mounts "
        "them yet, so without the pin Coolify's Docker cleanup could delete them. Once the "
        f"source runs again: coolify-migrate rollback {ctx.migration_id} "
        f"(or docker rm {' '.join(outcome.kept)} there)."
    )


def build_compensations() -> dict[Any, Any]:
    return {
        Compensation.DELETE_TARGET_RESOURCE: undo_create_target,
        Compensation.RESTART_SOURCE: undo_quiesce,
        Compensation.DROP_TARGET_VOLUMES: undo_copy,
        Compensation.REVOKE_EPHEMERAL_KEY: undo_revoke_key,
        Compensation.STOP_TARGET: undo_start_target,
        Compensation.RESTORE_SOURCE_NAME: undo_restore_source_name,
        Compensation.RESTORE_SOURCE_FQDN: undo_restore_source_fqdn,
        Compensation.RELEASE_SOURCE_PINS: undo_release_source_pins,
    }
