"""The deploy switches of the sources and the targets, around the stop and the cutover.

IO shell: what to switch, and back to what, is ``domain/deploy_triggers.py``'s.

* :func:`disarm_sources` — QUIESCE, first: every source application's
  auto-deploy (and previews, where on) goes off, its originals journalled first.
* :func:`await_source_deployments` — QUIESCE, next: what is already queued or
  building must end, or the run refuses while the source serves. Both run
  before the preview delete, the mount capture and the pins, because a
  deployment that ends replaces the containers those name.
* :func:`arm_targets` — FINALIZE: each target gets its source's auto-deploy and
  the tag names CREATE read (:func:`read_source_tags`) — read there, while every
  source exists: a DELETE finalize has removed them by now. Each target is
  journalled before its PATCH. :func:`note_kept_sources` says which kept source
  stays disarmed.
* :func:`disarm_targets` — the rollback's DISARM_TARGETS, its first step.
* :func:`restore_sources` — the rollback's RESTORE_SOURCE_DEPLOY_TRIGGERS.

Nothing here is ever logged but booleans, uuids and names: the deployment rows
are reduced in the client, before their logs reach this module.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.domain import deploy_triggers as triggers
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, State
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import CoolifyApiError, QuiesceError, SourceDeploymentPending

log = structlog.get_logger(__name__)

#: How often QUIESCE re-reads the deployment queue while a source deployment is due.
SETTLE_INTERVAL = 5.0


def _why(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:160]


def _applications(ctx: MigrationContext) -> list[ResourceSnapshot]:
    return [r.snapshot for r in ctx.plan.resources if r.snapshot.collection == "applications"]


def _name(ctx: MigrationContext, source_uuid: str) -> str:
    for resource in ctx.plan.resources:
        if resource.snapshot.uuid == source_uuid:
            return resource.snapshot.name
    return source_uuid


def recorded(ctx: MigrationContext) -> dict[str, triggers.Triggers]:
    """The originals QUIESCE journalled, by source uuid. Empty when it journalled none."""
    detail = ctx.journal.undo_info(State.QUIESCE.value)
    return triggers.deserialise(detail.get(triggers.JOURNAL_KEY))


def arm_requested(ctx: MigrationContext) -> list[str]:
    """Every target uuid FINALIZE has journalled before arming it, in order."""
    detail = ctx.journal.undo_info(State.FINALIZE.value)
    return triggers.deserialise_uuids(detail.get(triggers.ARM_REQUESTED))


def _target_name(ctx: MigrationContext, target_uuid: str) -> str:
    source = next((s for s, t in ctx.target_uuids.items() if t == target_uuid), None)
    return f"the target of {_name(ctx, source)}" if source else f"target {target_uuid}"


async def disarm_sources(ctx: MigrationContext) -> dict[str, dict[str, bool]]:
    """Switch every source application's push-driven deploys off. Returns what was sent.

    The first write to a source, deliberately before the stop: a push after it
    would deploy the stopped source (writes after VERIFY nobody copies) or its
    target (invariant 7). The values are read fresh — the plan may be hours old
    — and journalled BEFORE the PATCH, so a crash between the two still leaves
    the way back (RESTORE_SOURCE_DEPLOY_TRIGGERS). A resumed QUIESCE keeps the
    originals already journalled: read now, they would be what this run wrote.

    A failure here refuses before anything is stopped; the rollback switches
    back on what was already switched off.
    """
    originals = recorded(ctx)
    sent: dict[str, dict[str, bool]] = {}
    for snapshot in _applications(ctx):
        try:
            full = await ctx.api.get_resource("applications", snapshot.uuid)
            current = triggers.read_triggers(full)
            if snapshot.uuid not in originals:
                originals[snapshot.uuid] = current
                ctx.journal.append(
                    "step_started",
                    state=State.QUIESCE.value,
                    detail={triggers.JOURNAL_KEY: triggers.serialise(originals)},
                )
            body = await api_resources.update_application_settings(
                ctx.api,
                snapshot.uuid,
                triggers.disarm_body(originals[snapshot.uuid], current),
                version=ctx.coolify_version,
            )
        except CoolifyApiError as exc:
            raise QuiesceError(
                f"could not switch off the auto-deploy of {snapshot.name} before the stop: "
                f"{_why(exc)}",
                hint=(
                    "A push would otherwise deploy the stopped source, or its target before "
                    "the DNS gate. Nothing was stopped: the source keeps serving, what was "
                    "already switched off is switched back on and the created target is "
                    "deleted. Check that the token can write applications, then run the "
                    "migration again."
                ),
            ) from exc
        if body:
            sent[snapshot.uuid] = body
    if sent:
        log.info("quiesce.source_deploys_disarmed", sources=sorted(sent))
    return sent


async def await_source_deployments(ctx: MigrationContext) -> None:
    """Wait until no source application has a deployment queued or running.

    Coolify's stop cancels no deployment, and its queue starts what is waiting
    (domain/deploy_triggers.py), so such a deployment brings the source back up
    after the stop. Read-only: ``GET /deployments``, matched by the application
    uuid each row's ``deployment_url`` names. Bounded by ``deploy_timeout``; a
    read that fails decides nothing until then, and refuses at the end like a
    deployment that did not finish. Never cancels anything.
    """
    sources = {s.uuid: s.name for s in _applications(ctx)}
    if not sources:
        return
    waited = 0.0
    while True:
        problem: str | None = None
        pending: tuple[triggers.ActiveDeployment, ...] = ()
        try:
            pending = triggers.pending_for(await ctx.api.list_active_deployments(), sources)
        except CoolifyApiError as exc:
            problem = _why(exc)
        if problem is None and not pending:
            if waited:
                log.info("quiesce.source_deployments_ended", waited=waited)
            return
        if waited >= ctx.settings.deploy_timeout:
            raise _still_deploying(ctx, sources, pending, problem)
        log.info(
            "quiesce.awaiting_source_deployments",
            deployments=[d.uuid for d in pending],
            unreadable=problem is not None,
        )
        await asyncio.sleep(SETTLE_INTERVAL)
        waited += SETTLE_INTERVAL


def _still_deploying(
    ctx: MigrationContext,
    sources: dict[str, str],
    pending: tuple[triggers.ActiveDeployment, ...],
    problem: str | None,
) -> SourceDeploymentPending:
    timeout = f"{ctx.settings.deploy_timeout:.0f}s"
    if problem is not None:
        what = f"Coolify's deployment queue could not be read for {timeout}: {problem}"
    else:
        what = "\n".join(
            f"{sources.get(d.application_uuid or '', d.application_uuid)}: deployment {d.uuid} "
            f"is still {d.status} after {timeout}"
            for d in pending
        )
    return SourceDeploymentPending(
        f"a source deployment has not ended, so the stop is refused:\n{what}",
        hint=(
            "Coolify's stop cancels no deployment, and its queue starts what is waiting: "
            "the source would come back up after the stop, beside a copy that never sees "
            "its writes. Let the deployment finish, or cancel it in Coolify, then run the "
            "migration again - this tool never cancels one. Nothing was stopped: the "
            "source keeps serving, its auto-deploy is switched back on and the created "
            "target is deleted."
        ),
    )


async def _original(
    ctx: MigrationContext, snapshot: ResourceSnapshot, originals: dict[str, triggers.Triggers]
) -> triggers.Triggers:
    """The source's switches before this run touched them."""
    if snapshot.uuid in originals:
        return originals[snapshot.uuid]
    # No QUIESCE record (a journal from before sources were disarmed): this run
    # never switched the source, so it still shows its own.
    try:
        return triggers.read_triggers(await ctx.api.get_resource("applications", snapshot.uuid))
    except Exception as exc:  # arming must never fail FINALIZE
        log.warning("finalize.source_switches_unread", uuid=snapshot.uuid, error=_why(exc))
        return triggers.Triggers(auto_deploy=None, previews=None)


async def read_source_tags(ctx: MigrationContext) -> dict[str, list[str]]:
    """CREATE: journal every source's tag names for FINALIZE. Returns them. Never raises.

    Only READ here: on the target they would let a tag CI run start it before
    the DNS gate (:func:`arm_targets`). Read now because FINALIZE gives them to
    the targets only after a DELETE policy has removed every source, and a
    deleted source has no tags left to read (:data:`TAGS_JOURNAL_KEY`). A source
    whose tags cannot be read is left out, and FINALIZE asks it again.
    """
    names: dict[str, list[str]] = {}
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        try:
            found = await api_resources.read_tag_names(
                ctx.api, collection=snapshot.collection, uuid=snapshot.uuid
            )
        except Exception as exc:  # tags are never on the critical path (2.5.6)
            log.warning("create.source_tags_unread", uuid=snapshot.uuid, error=_why(exc))
            continue
        if found is not None:
            names[snapshot.uuid] = found
    if names:
        ctx.journal.append(
            "step_started",
            state=State.CREATE_TARGET.value,
            detail={triggers.TAGS_JOURNAL_KEY: names},
        )
    return names


async def _give_tags(
    ctx: MigrationContext,
    snapshot: ResourceSnapshot,
    target_uuid: str,
    journalled: dict[str, list[str]],
) -> None:
    names = journalled.get(snapshot.uuid)
    if names is None:
        # CREATE could not read them, or the journal predates that read (its
        # CREATE copied them itself; adding twice attaches nothing twice). Ask
        # the source - which a DELETE finalize has removed by now.
        await api_resources.copy_tags(
            ctx.api,
            collection=snapshot.collection,
            source_uuid=snapshot.uuid,
            target_uuid=target_uuid,
        )
        return
    await api_resources.add_tag_names(
        ctx.api, collection=snapshot.collection, target_uuid=target_uuid, names=names
    )


async def arm_targets(ctx: MigrationContext) -> list[str]:
    """Give every target its source's auto-deploy, then its tags. Returns the armed uuids.

    FINALIZE, after the cutover: until now nothing may start a target before
    the DNS gate, and both a push (auto-deploy) and ``POST /deploy?tag`` (tags,
    whatever auto-deploy says: DeployController v4.7.2:488-538) would. The tag
    names are the ones CREATE journalled (:func:`read_source_tags`). Never
    raises — the cutover is done, and a target missing a setting or a tag is
    named in a notice instead.

    Each application target is journalled BEFORE its PATCH
    (:data:`triggers.ARM_REQUESTED`), so a rollback that follows — a crash
    after the PATCH included — switches it off again before it stops the target
    (:func:`disarm_targets`). A record that cannot be written arms nothing.
    """
    originals = recorded(ctx)
    tags = triggers.deserialise_tags(
        ctx.journal.undo_info(State.CREATE_TARGET.value).get(triggers.TAGS_JOURNAL_KEY)
    )
    requested = arm_requested(ctx)
    armed: list[str] = []
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        target_uuid = ctx.target_uuids.get(snapshot.uuid)
        if not target_uuid:
            continue
        if snapshot.collection == "applications":
            body = triggers.target_arm_body(await _original(ctx, snapshot, originals))
            try:
                if target_uuid not in requested:
                    ctx.journal.append(
                        "step_started",
                        state=State.FINALIZE.value,
                        detail={triggers.ARM_REQUESTED: [*requested, target_uuid]},
                    )
                    requested.append(target_uuid)
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
        try:
            await _give_tags(ctx, snapshot, target_uuid, tags)
        except Exception as exc:  # the tag calls warn on API errors; nothing else may fail it
            log.warning("finalize.tags_not_copied", uuid=target_uuid, error=_why(exc))
    return armed


def note_kept_sources(ctx: MigrationContext) -> list[str]:
    """Under keep or rename, name the sources this run leaves disarmed. Returns their uuids.

    Nothing is switched back on: the source and its target share their
    repository and webhook, so a push would deploy the stale source beside the
    target. A rollback still owes the restore (RESTORE_SOURCE_DEPLOY_TRIGGERS).
    Under DELETE the source is gone.
    """
    if ctx.plan.finalize_policy is FinalizePolicy.DELETE:
        return []
    originals = recorded(ctx)
    kept = [uuid for uuid, original in originals.items() if triggers.disarm_body(original)]
    if kept:
        ctx.notices.append(
            triggers.kept_source_notice(
                [_name(ctx, uuid) for uuid in kept], migration_id=ctx.migration_id
            )
        )
    return kept


async def disarm_targets(ctx: MigrationContext, undo_info: dict[str, Any]) -> list[str]:
    """Switch off the auto-deploy FINALIZE gave each target. Returns the uuids switched.

    The rollback's DISARM_TARGETS, the first compensation of a rollback after
    FINALIZE (``statemachine._COMPENSATION``): what follows stops the target,
    drops its volumes and waits for the source's restart before the delete, and
    an armed target redeploys on the next push in between. ``undo_info`` is
    FINALIZE's merged journal detail; each uuid :func:`arm_targets` journalled is
    read first and PATCHed only where it is not off already
    (:func:`domain.deploy_triggers.target_disarm_body`), so a replay sends
    nothing. A target that is gone (404 — an earlier rollback deleted it) is
    done. Every target is attempted; the failures are raised together, each
    named with what to switch off by hand.
    """
    failures: list[str] = []
    switched: list[str] = []
    for target_uuid in triggers.deserialise_uuids(undo_info.get(triggers.ARM_REQUESTED)):
        try:
            current = triggers.read_triggers(
                await ctx.api.get_resource("applications", target_uuid)
            )
            body = await api_resources.update_application_settings(
                ctx.api,
                target_uuid,
                triggers.target_disarm_body(current),
                version=ctx.coolify_version,
            )
        except Exception as exc:
            if isinstance(exc, CoolifyApiError) and exc.status_code == 404:
                log.info("compensate.target_already_gone", uuid=target_uuid)
                continue
            log.error("compensate.target_deploys_not_disarmed", uuid=target_uuid, error=_why(exc))
            failures.append(f"{_target_name(ctx, target_uuid)} ({target_uuid}: {_why(exc)})")
            continue
        if body:
            log.info("compensate.target_deploys_disarmed", uuid=target_uuid)
            switched.append(target_uuid)
    if failures:
        raise RuntimeError(
            "auto-deploy NOT switched off for: "
            + "; ".join(failures)
            + " - switch 'Auto Deploy' off on each target's Advanced page in Coolify: "
            "until it is deleted, a push can redeploy it, on empty volumes once the "
            "rollback has dropped them"
        )
    return switched


async def restore_sources(ctx: MigrationContext) -> None:
    """Give every disarmed source back what QUIESCE switched off. Loud, and idempotent.

    Each source is read first and PATCHed only where it still differs
    (:func:`domain.deploy_triggers.restore_body`), because ``rollback <id>``
    replays every compensation. A no-op when QUIESCE journalled nothing. Every
    source is attempted; the failures are raised together, each naming what to
    switch back on by hand.
    """
    failures: list[str] = []
    for uuid, original in recorded(ctx).items():
        if not triggers.disarm_body(original):
            continue
        try:
            current = triggers.read_triggers(await ctx.api.get_resource("applications", uuid))
            body = await api_resources.update_application_settings(
                ctx.api, uuid, triggers.restore_body(original, current), version=ctx.coolify_version
            )
        except Exception as exc:
            log.error("compensate.source_deploys_not_restored", uuid=uuid, error=_why(exc))
            failures.append(f"{_name(ctx, uuid)} ({_why(exc)})")
            continue
        if body:
            log.info("compensate.source_deploys_restored", uuid=uuid, settings=body)
    if failures:
        raise RuntimeError(
            "auto-deploy NOT switched back on for: "
            + "; ".join(failures)
            + " - switch 'Auto Deploy' (and 'Preview Deployments', if it had them) back on "
            "on each source's Advanced page in Coolify"
        )
