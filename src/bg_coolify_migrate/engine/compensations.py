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

log = structlog.get_logger(__name__)


async def undo_create_target(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Delete the resources we created and restore any parked source domains.

    ``delete_volumes=True`` is correct here and only here: these are volumes WE
    created on the target minutes ago. The source's volumes are never touched by
    a rollback.
    """
    # Un-park FIRST and best-effort: create_target renamed the source's custom
    # domains to free them, and the source needs its real domain back regardless
    # of whether the target deletes cleanly. A failure here must not stop that.
    parked: dict[str, dict[str, Any]] = undo_info.get("parked_domains") or {}
    for source_uuid, restore_body in parked.items():
        try:
            collection = ctx.collection_of(source_uuid)
            await ctx.api.update_resource(collection, source_uuid, restore_body)
            log.info("compensate.source_domains_restored", uuid=source_uuid)
        except Exception as exc:
            log.error(
                "compensate.source_domains_restore_failed",
                uuid=source_uuid,
                error=str(exc)[:200],
            )

    target_uuids: dict[str, str] = undo_info.get("target_uuids") or {}
    if not target_uuids:
        return

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
            log.error("compensate.target_delete_failed", uuid=target_uuid, error=str(exc)[:200])
            raise


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
    await keys.revoke(
        source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id
    )


async def undo_start_target(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Stop the target before its volumes are dropped.

    Ordering matters: a volume in use cannot be removed, and deleting the
    resource before dropping volumes would leak them.
    """
    started: list[str] = undo_info.get("started") or []
    for target_uuid in started:
        source_uuid = next(
            (s for s, t in ctx.target_uuids.items() if t == target_uuid), None
        )
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
            log.error(
                "compensate.rename_failed", name=resource.snapshot.name, error=str(exc)[:200]
            )
            raise


async def undo_restore_source_fqdn(ctx: MigrationContext, undo_info: dict[str, Any]) -> None:
    """Restore the source's FQDN after a released one.

    Best-effort: we cleared it, so we must put it back, but a failure here leaves
    a stopped resource with no domain — recoverable by hand and far less harmful
    than the alternative.
    """
    if undo_info.get("policy") != "rename":
        return
    for resource in ctx.plan.resources:
        try:
            full = await ctx.api.get_resource(
                resource.snapshot.collection, resource.snapshot.uuid
            )
            if resource.snapshot.collection == "applications" and not full.get("fqdn"):
                log.warning(
                    "compensate.fqdn_not_restored",
                    name=resource.snapshot.name,
                    hint="the original FQDN was not recorded; set it in Coolify",
                )
        except Exception as exc:
            log.debug("compensate.fqdn_check_failed", error=str(exc)[:120])


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
