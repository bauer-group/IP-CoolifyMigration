"""Compensating actions.

Each reads its undo info from the **journal**, not from the context: after a
crash there is no context. That is the whole reason the journal records what it
records.

Best-effort but loud. A compensation that fails does not stop the others — a
failure to delete the target must not also prevent restarting the source — but it
is recorded and the run reports honestly at the end.
"""

from __future__ import annotations

from typing import Any

import structlog

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.discovery import docker
from bg_coolify_migrate.engine import keys
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import CoolifyApiError

log = structlog.get_logger(__name__)


async def undo_create_target(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Delete the resources we created and restore the source's original domains.

    ``delete_volumes=True`` is correct here and only here: these are volumes WE
    created on the target minutes ago. The source's volumes are never touched by
    a rollback.

    ORDER matters: the target is deleted FIRST so it releases any domain it holds,
    and only THEN is the source's original domain restored — otherwise reclaiming
    a custom domain the target still holds would 409. Both parts are best-effort so
    a stuck target delete cannot also strand the source without its URL.
    """
    target_uuids: dict[str, str] = undo_info.get("target_uuids") or {}
    delete_failures: list[str] = []
    for source_uuid, target_uuid in target_uuids.items():
        try:
            collection = ctx.collection_of(source_uuid)
        except KeyError:
            log.warning("compensate.unknown_resource", source_uuid=source_uuid)
            continue
        try:
            await ctx.api.delete_resource(collection, target_uuid, delete_volumes=True)
            log.info("compensate.target_deleted", uuid=target_uuid)
        except Exception as exc:
            delete_failures.append(target_uuid)
            log.error("compensate.target_delete_failed", uuid=target_uuid, error=str(exc)[:200])

    # The target is gone (or noted as stuck) — now swing the source's domains back.
    # create_target parked the custom ones and finalize blanked them on success;
    # this puts the originals back so the old stack regains its URL.
    parked: dict[str, dict[str, Any]] = undo_info.get("parked_domains") or {}
    for source_uuid, restore_body in parked.items():
        try:
            collection = ctx.collection_of(source_uuid)
            await _restore_source_domains(ctx, collection, source_uuid, restore_body)
            log.info("compensate.source_domains_restored", uuid=source_uuid)
        except Exception as exc:
            log.error(
                "compensate.source_domains_restore_failed",
                uuid=source_uuid,
                error=str(exc)[:200],
            )

    if delete_failures:
        # Surfaced so the rollback reports honestly; the source restore still ran.
        raise RuntimeError("could not delete target(s): " + ", ".join(delete_failures))


async def _restore_source_domains(
    ctx: MigrationContext, collection: str, source_uuid: str, restore_body: dict[str, Any]
) -> None:
    """Give the source back its own domains, surviving the parking 409.

    Coolify's domain-uniqueness check 409s ("Domain conflicts detected. Use
    force_domain_override=true") when a resource asks for a domain another still
    holds — and the target we just deleted can linger in that check for a beat
    (the delete is async). The plain restore then failed and the source kept
    serving under its parked ``old-<tag>`` name (covalida, 2026-07-23:
    staging.covalida.com was left parked while the source was back up).

    So on a 409 we retry ONCE with ``force_domain_override``. In a rollback that
    is unconditionally correct: the domain is the source's own, and the only
    thing that could still be holding it is the target being torn down.
    """
    try:
        await ctx.api.update_resource(collection, source_uuid, restore_body)
    except CoolifyApiError as exc:
        if exc.status_code != 409:
            raise
        log.warning("compensate.source_domains_conflict_forcing", uuid=source_uuid)
        await ctx.api.update_resource(
            collection, source_uuid, {**restore_body, "force_domain_override": True}
        )


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
        raise RuntimeError("could not restart: " + "; ".join(failures))


async def undo_copy(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Drop the target's volumes so a resumed run cannot merge into a partial copy."""
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


async def undo_restore_source_fqdn(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """No-op: the source's domains are restored by ``undo_create_target``.

    That compensation recorded the originals at create and runs LAST — after the
    target it deletes has released the domain — so it can reclaim a custom domain
    without a 409. Doing it here (a finalize compensation, which runs while the
    target still exists) would conflict, so this stays a no-op and is kept only so
    the state machine's compensation map remains complete.
    """
    return


def build_compensations() -> dict[Any, Any]:
    from bg_coolify_migrate.domain.statemachine import Compensation

    return {
        Compensation.DELETE_TARGET_RESOURCE: undo_create_target,
        Compensation.RESTART_SOURCE: undo_quiesce,
        Compensation.DROP_TARGET_VOLUMES: undo_copy,
        Compensation.REVOKE_EPHEMERAL_KEY: undo_revoke_key,
        Compensation.STOP_TARGET: undo_start_target,
        Compensation.RESTORE_SOURCE_NAME: undo_restore_source_name,
        Compensation.RESTORE_SOURCE_FQDN: undo_restore_source_fqdn,
    }
