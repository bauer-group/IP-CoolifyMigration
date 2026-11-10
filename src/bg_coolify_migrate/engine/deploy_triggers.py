"""The deploy switches of the targets, around the cutover.

IO shell: what to switch, and to what, is ``domain/deploy_triggers.py``'s.

* :func:`arm_targets` — FINALIZE: each target gets its source's auto-deploy.

Nothing here is ever logged but booleans, uuids and names.
"""

from __future__ import annotations

import structlog

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.domain import deploy_triggers as triggers
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from bg_coolify_migrate.engine.context import MigrationContext

log = structlog.get_logger(__name__)


def _why(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:160]


async def _original(ctx: MigrationContext, snapshot: ResourceSnapshot) -> triggers.Triggers:
    """The source's switches: this run never touches them, so it shows its own."""
    try:
        return triggers.read_triggers(await ctx.api.get_resource("applications", snapshot.uuid))
    except Exception as exc:  # arming must never fail FINALIZE
        log.warning("finalize.source_switches_unread", uuid=snapshot.uuid, error=_why(exc))
        return triggers.Triggers(auto_deploy=None, previews=None)


async def arm_targets(ctx: MigrationContext) -> list[str]:
    """Give every application target its source's auto-deploy. Returns the armed uuids.

    FINALIZE, after the cutover: until now a push (auto-deploy) must not start a
    target before the DNS gate. Never raises — the cutover is done, and a target
    missing its setting is named in a notice instead.
    """
    armed: list[str] = []
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        target_uuid = ctx.target_uuids.get(snapshot.uuid)
        if not target_uuid or snapshot.collection != "applications":
            continue
        body = triggers.target_arm_body(await _original(ctx, snapshot))
        try:
            await api_resources.update_application_settings(
                ctx.api, target_uuid, body, version=ctx.coolify_version
            )
            armed.append(target_uuid)
        except Exception as exc:  # the cutover is done: a notice, never a rollback
            log.warning("finalize.target_not_armed", uuid=target_uuid, error=_why(exc))
            ctx.notices.append(
                triggers.arm_failed_notice(
                    snapshot.name, target_uuid, body[triggers.AUTO_DEPLOY], reason=_why(exc)
                )
            )
    return armed
