"""The F1 step implementations.

Each returns undo info to journal. The ordering and the compensations are decided
by ``domain/statemachine.py``; this module only performs them.

Every step is written so that its failure is safe: nothing here leaves a state
that :mod:`.compensations` cannot undo, and the source is never destroyed before
FINALIZE.
"""

from __future__ import annotations

import asyncio
import ipaddress
import shlex
from typing import Any

import structlog

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.api.resources import Placement
from bg_coolify_migrate.discovery import docker, quiesce
from bg_coolify_migrate.dns import extract as dns_extract
from bg_coolify_migrate.dns import resolve as dns_resolve
from bg_coolify_migrate.dns import wildcard as dns_wildcard
from bg_coolify_migrate.dns.gate import Resolution, build_report, explain_why_blocking_matters
from bg_coolify_migrate.domain.naming import VolumeEndpoint, pair_by_mount_path
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine import keys
from bg_coolify_migrate.engine.context import MigrationContext, serialise_mounts
from bg_coolify_migrate.engine.planner import (
    build_manifest,
    inspect_all_mounts,
    observed_labels,
    resource_containers,
)
from bg_coolify_migrate.errors import (
    DnsGateBlocked,
    PreflightError,
    QuiesceError,
    RebuildDriftBlocked,
    TransferError,
    VerificationError,
)
from bg_coolify_migrate.transfer import rsync, verify
from bg_coolify_migrate.transfer.partition import PathEntry, plan_transfer, suggest_parallelism

log = structlog.get_logger(__name__)


async def step_init(ctx: MigrationContext) -> dict[str, Any]:
    return {
        "project": ctx.plan.project,
        "environment": ctx.plan.environment,
        "source_server": ctx.plan.source_server.name,
        "target_server": ctx.plan.target_server.name,
        "resources": [r.snapshot.name for r in ctx.plan.resources],
    }


async def step_preflight(ctx: MigrationContext) -> dict[str, Any]:
    """Everything that must be true before we touch anything.

    Runs before CREATE_TARGET and long before QUIESCE, because discovering a
    missing rsync after the source is stopped converts a preflight failure into
    an outage.
    """
    await ctx.api.assert_can_read_sensitive()

    for host, label in ((ctx.source_host, "source"), (ctx.target_host, "target")):
        await rsync.ensure_installed(host, label=label)
        if not await host.which("docker"):
            raise PreflightError(f"docker is not installed on the {label} server")

    # Previews are not stopped by Coolify's stop endpoint, so they would keep
    # writing during the copy. Refuse now rather than corrupt later.
    if not ctx.delete_previews:
        await quiesce.assert_previews_absent(ctx.source_host, label_filters=observed_labels(ctx.plan))

    # Proportional disk check against the ACTUAL payload.
    required = int(ctx.plan.total_bytes * ctx.settings.disk_headroom_factor)
    free = await ctx.target_host.free_bytes("/var/lib/docker")
    if free < required:
        raise PreflightError(
            f"target has {free / 1024**3:.1f} GB free but needs "
            f"{required / 1024**3:.1f} GB "
            f"({ctx.plan.total_bytes / 1024**3:.1f} GB payload x "
            f"{ctx.settings.disk_headroom_factor})",
            hint="Free space on the target, or lower DISK_HEADROOM_FACTOR knowingly.",
        )

    # Drift is a question, not a wall. We build the target exactly as the source
    # is configured; whether a newer image or a newer commit is compatible is a
    # judgement about this stack, and the operator has context we do not.
    #
    # Interactively the wizard has already shown these and asked. Unattended we
    # cannot ask, so an unanswered question is a stop — resumable, not a failure.
    undecided = [r for r in ctx.plan.resources if r.needs_confirmation]
    if undecided and not ctx.accept_drift:
        lines = [f"  {r.snapshot.name}: {d}" for r in undecided for d in r.drift_decisions]
        raise RebuildDriftBlocked(
            "the target may not run exactly what the source runs:\n" + "\n".join(lines),
            hint=(
                "This is usually fine — new image versions and moved branches are normal. "
                "Only you can say whether it is compatible for this stack.\n"
                "Re-run interactively to see the detail and answer, or pass --accept-drift "
                "to proceed.\n"
                "Nothing has been changed."
            ),
            report=[r.drift for r in undecided],
        )

    # Hard reasons: these are not judgements and no flag overrides them.
    hard = [
        f"  {r.snapshot.name}: {x}" for r in ctx.plan.resources for x in r.hard_blocking_reasons
    ]
    if hard:
        raise PreflightError("the plan is blocked:\n" + "\n".join(hard))

    return {"free_bytes": free, "required_bytes": required}


async def step_plan(ctx: MigrationContext) -> dict[str, Any]:
    return {
        "total_bytes": ctx.plan.total_bytes,
        "volumes": sum(len(r.manifest.to_migrate) for r in ctx.plan.resources),
        "finalize_policy": ctx.plan.finalize_policy.value,
    }


async def step_create_target(ctx: MigrationContext) -> dict[str, Any]:
    """Create every target resource, stopped, with its envs and storages.

    Runs BEFORE quiesce: a failed create then costs zero downtime, and the target
    must exist before volumes can be paired by mount path.
    """
    project_uuid = await api_resources.ensure_project(ctx.api, ctx.plan.project)
    await api_resources.ensure_environment(ctx.api, project_uuid, ctx.plan.environment)
    destination = await api_resources.resolve_destination(ctx.api, ctx.plan.target_server.uuid)

    placement = Placement(
        project_uuid=project_uuid,
        environment_name=ctx.plan.environment,
        server_uuid=ctx.plan.target_server.uuid,
        destination_uuid=destination,
        source_wildcard=ctx.plan.source_server.wildcard_domain or None,
        target_wildcard=ctx.plan.target_server.wildcard_domain or None,
    )

    created: dict[str, str] = {}
    parked: dict[str, dict[str, Any]] = {}
    tag = ctx.migration_id.split("-")[-1]
    source_wildcard = ctx.plan.source_server.wildcard_domain or None
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        source_full = await ctx.api.get_resource(snapshot.collection, snapshot.uuid)

        # SYNTHETIC KEY: "tags" is NOT part of the GET above — service_by_uuid
        # eager-loads only ['applications', 'databases'] — so it is read separately
        # and merged in here. Everything downstream builds the create body as
        # filter_body({**source, ...}), and "tags" is whitelisted for all three
        # families, so this one assignment is the whole wiring. Tags are settable
        # ONLY on create (the PATCH $allowedFields omits them), which is why this
        # has to happen before create_resource rather than as a follow-up call.
        #
        # None, not [], when the source has no tags: filter_body drops None but
        # would forward an empty list, and "we have no opinion" is expressed by
        # omission — same reasoning as the connect_to_docker_network PATCH below,
        # where a redundant write is just risk.
        tag_names = await ctx.api.get_tag_names(snapshot.collection, snapshot.uuid)
        source_full["tags"] = tag_names or None

        # Free the source's custom domains so the target can claim them — Coolify
        # 409s on a duplicate. Journal the restore body BEFORE creating, so a
        # failed create still un-parks them on rollback. Server-bound domains are
        # remapped onto the target's wildcard and never collide, so park leaves
        # them alone.
        restore_body = await api_resources.park_source_domains(
            ctx.api, snapshot, source_full, source_wildcard=source_wildcard, tag=tag
        )
        if restore_body is not None:
            parked[snapshot.uuid] = restore_body
            ctx.journal.append(
                "step_started",
                state="create_target",
                detail={"target_uuids": dict(created), "parked_domains": dict(parked)},
            )

        target_uuid = await api_resources.create_resource(ctx.api, snapshot, placement, source_full)
        created[snapshot.uuid] = target_uuid
        ctx.target_uuids[snapshot.uuid] = target_uuid

        # Journal each creation IMMEDIATELY: a crash after creating the third of
        # five resources must still be able to delete all three (and un-park).
        ctx.journal.append(
            "step_started",
            state="create_target",
            detail={"target_uuids": dict(created), "parked_domains": dict(parked)},
        )

        await api_resources.copy_envs(
            ctx.api,
            collection=snapshot.collection,
            source_uuid=snapshot.uuid,
            target_uuid=target_uuid,
        )
        await api_resources.copy_storages(
            ctx.api,
            collection=snapshot.collection,
            source_uuid=snapshot.uuid,
            target_uuid=target_uuid,
            kind=snapshot.kind,
        )

    return {"target_uuids": created}


async def step_quiesce(ctx: MigrationContext) -> dict[str, Any]:
    """Stop the whole stack and prove it, by asking the daemon.

    We never trust the stop endpoint: it is async, it does not touch previews,
    and for services it works off DB rows rather than labels.
    """
    if ctx.delete_previews:
        for resource in ctx.plan.resources:
            if not resource.snapshot.has_previews:
                continue
            containers = await docker.list_containers(
                ctx.source_host,
                label_filters={"coolify.applicationId": resource.snapshot.uuid},
            )
            for container in containers:
                if container.is_preview:
                    await ctx.api.delete(
                        f"/applications/{resource.snapshot.uuid}/previews/"
                        f"{container.pull_request_id}"
                    )

    # Capture the mounts while the containers still exist. Coolify's stop is
    # `docker stop` then `docker rm -f`, so this is the last moment anyone can
    # ask a container what it had mounted.
    await _capture_mounts(ctx)

    # The source's clock, not ours: it is the window for the event-log check
    # below, and a skewed workstation would silently shrink it.
    since = await quiesce.now_on(ctx.source_host)

    await _request_stop(ctx)

    report = await quiesce.wait_until_stopped(
        ctx.source_host,
        label_filters=observed_labels(ctx.plan),
        timeout=ctx.settings.stop_timeout,
    )

    # "Every container is gone" is not "every container shut down cleanly", and
    # after a Coolify stop the containers are always gone. The exit codes only
    # survive in the daemon's event log, so that is where we look.
    killed = await quiesce.killed_since(
        ctx.source_host, since=since, label_filters=observed_labels(ctx.plan)
    )
    if killed:
        names = ", ".join(sorted(name for name, _ in killed))
        raise QuiesceError(
            f"container(s) were SIGKILLed rather than stopping cleanly: {names}",
            hint=(
                "Exit code 137 means the stop grace period elapsed and Docker killed the "
                "process. A killed database has not flushed, so its volume is a torn "
                "snapshot, and mirroring it byte-exactly would give you a faithful copy "
                "of the tear.\n"
                "Raise the stop grace period on the resource in Coolify and retry. "
                "Nothing has been copied."
            ),
        )

    return {
        "containers_stopped": len(report.containers),
        "container_names": sorted(c.name for c in report.containers),
        "elapsed": round(report.elapsed, 1),
    }


#: How long to keep re-asking Coolify to stop a stack it believes is already
#: down. The refresh comes from ServerManagerJob, scheduled `everyMinute()`
#: (Console/Kernel.php), so this is three ticks: enough for a refresh to land
#: even if we arrive just after one and the queue is briefly behind. Not a round
#: number picked for comfort — 90s was, and it fell between ticks.
_STOP_REFUSAL_WINDOW = 180.0
_STOP_RETRY_INTERVAL = 5.0


async def _storages_or_none(
    ctx: MigrationContext, collection: str, uuid: str
) -> dict[str, Any] | None:
    """The API's declared storages, or None if it will not say.

    Worth re-reading after the stop even though nothing has changed: unlike the
    containers, this endpoint still answers, and it carries the mount_path that
    pairing turns on.
    """
    try:
        return await ctx.api.get_storages(collection, uuid)
    except Exception as exc:
        log.debug("discover.storages_unavailable", uuid=uuid, error=str(exc)[:120])
        return None


async def _capture_mounts(ctx: MigrationContext) -> None:
    """Record every container mount before the stop erases the containers.

    Coolify does not merely stop a stack, it removes it — `docker rm -f` in
    StopDatabase, StopApplication and StopService alike. So the post-stop
    discovery the design calls for cannot read mounts off containers: there are
    none. Anonymous volumes and bind mounts appear in no API, which makes this
    the only record of them that will exist a few seconds from now.

    Refuses to proceed on finding nothing, because "no containers" and "wrong
    label filter" look identical from here, and the difference is a migration
    that copies nothing and says it worked.
    """
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        containers = await resource_containers(
            ctx.source_host,
            project=ctx.plan.project,
            environment=ctx.plan.environment,
            name=snapshot.name,
        )
        if not containers:
            # No containers is only a problem when there is data to copy. The plan's
            # manifest is the signal: if it found volumes, a running stack is the ONLY
            # chance to capture them, so refuse (silently copying nothing for a
            # stateful resource is the failure we exist to prevent). If it found none
            # — a stateless/rebuilt resource, or one that is simply stopped — there is
            # nothing to capture, so carry on and let it be recreated on the target.
            if resource.manifest.to_migrate:
                raise PreflightError(
                    f"{snapshot.name}: has volumes to migrate but no running containers "
                    f"on {ctx.plan.source_server.name}",
                    hint=(
                        "Coolify removes containers when it stops them, so a running "
                        "stack is the only chance to see what its volumes mounted. Start "
                        "the resource, then migrate. If it IS running, its containers may "
                        "not carry the labels we filter on."
                    ),
                )
            # Record an EXPLICIT empty capture, not nothing: DISCOVER treats a
            # missing key (None) as a lost capture and aborts, but an empty list
            # correctly means "this resource had no mounts".
            ctx.pre_stop_mounts[snapshot.uuid] = []
            log.info("quiesce.no_containers_no_volumes", resource=snapshot.name)
            continue
        mounts = await inspect_all_mounts(ctx.source_host, containers)
        ctx.pre_stop_mounts[snapshot.uuid] = mounts
        log.info(
            "quiesce.mounts_captured",
            resource=snapshot.name,
            containers=len(containers),
            mounts=len(mounts),
        )

    # Journal it before the stop. The context dies with the process and the
    # containers die with the stop, so after a crash this record is the only
    # description of them that exists anywhere — a resume cannot go and look.
    ctx.journal.append(
        "step_started",
        state="quiesce",
        detail={"pre_stop_mounts": serialise_mounts(ctx.pre_stop_mounts)},
    )


async def _request_stop(ctx: MigrationContext) -> None:
    """Ask Coolify to stop every resource, coping with its stale status column.

    `POST /{kind}/{uuid}/stop` returns 400 "already stopped" — **without
    dispatching anything** — whenever the resource's `status` column contains
    'exited' or 'stopped'. That column defaults to 'exited' and is advanced by a
    background job, so it lags the daemon: shortly after a deploy Coolify will
    refuse to stop a container that is serving traffic, and no amount of waiting
    afterwards helps, because no stop was ever requested.

    So a refusal is checked against the daemon rather than believed. If the
    containers really are down, we are finished. If they are not, Coolify is
    merely behind, and we re-ask until it catches up.

    Gives up quietly at the window's end rather than raising: the gate that
    follows is the one authorised to fail, and it fails with the list of
    containers still running — which is what the operator needs to see. Failing
    here would replace that with a story about an API call.
    """
    pending = [(r.snapshot.collection, r.snapshot.uuid) for r in ctx.plan.resources]
    deadline = asyncio.get_running_loop().time() + _STOP_REFUSAL_WINDOW

    while True:
        refused = [
            (collection, uuid)
            for collection, uuid in pending
            if not await ctx.api.stop(collection, uuid)
        ]
        if not refused:
            return

        report = await quiesce.snapshot(ctx.source_host, label_filters=observed_labels(ctx.plan))
        if report.is_quiesced:
            log.info("quiesce.already_down", resources=len(refused))
            return

        if asyncio.get_running_loop().time() >= deadline:
            log.warning(
                "quiesce.stop_refused",
                resources=[uuid for _, uuid in refused],
                running=sorted(c.name for c in report.running),
                detail="Coolify reports these stopped; the daemon disagrees",
            )
            return

        log.debug("quiesce.stop_refused_retrying", running=len(report.running))
        await asyncio.sleep(_STOP_RETRY_INTERVAL)
        pending = refused


async def _await_target_volumes(
    ctx: MigrationContext,
    *,
    collection: str,
    target_uuid: str,
    expected: set[str],
) -> list[VolumeEndpoint]:
    """Poll the target's declared volumes until they cover ``expected`` mount paths.

    The target of a dockercompose migration gets its persistent storages from an
    async LoadComposeFile job dispatched at create, so a read right after create
    can miss them. Returns as soon as every expected mount path is present, or the
    latest reading at timeout — the caller's unpaired-volume check then produces
    the precise, actionable error rather than a silent miss.
    """
    deadline = ctx.settings.target_storage_timeout
    interval = 3.0
    waited = 0.0
    endpoints = await api_resources.read_volume_endpoints(
        ctx.api, collection=collection, uuid=target_uuid
    )
    while not expected <= {e.mount_path for e in endpoints} and waited < deadline:
        await asyncio.sleep(interval)
        waited += interval
        endpoints = await api_resources.read_volume_endpoints(
            ctx.api, collection=collection, uuid=target_uuid
        )
    if not expected <= {e.mount_path for e in endpoints}:
        log.warning(
            "discover.target_volumes_incomplete",
            waited=round(waited, 1),
            expected=sorted(expected),
            seen=sorted(e.mount_path for e in endpoints),
        )
    else:
        log.info("discover.target_volumes_ready", waited=round(waited, 1))
    return endpoints


async def step_discover(ctx: MigrationContext) -> dict[str, Any]:
    """The authoritative manifest, taken with nothing able to write.

    Pairs source to target volumes by ``mount_path`` — read back from what
    Coolify actually created, never predicted, never derived by string-replacing
    a uuid.
    """
    pairs_recorded: dict[str, list[dict[str, str]]] = {}

    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        target_uuid = ctx.target_uuids[snapshot.uuid]

        # The containers are gone — Coolify removed them as it stopped them — so
        # the mounts come from QUIESCE's capture. `docker volume ls` and the API
        # both survive the stop and are re-read here, which is what keeps this
        # authoritative rather than a replay: a volume that vanished during the
        # stop, or one created late, still shows up now.
        mounts = ctx.pre_stop_mounts.get(snapshot.uuid)
        if mounts is None:
            # Defaulting to [] here would rebuild the exact bug this capture
            # exists to fix: an empty manifest copies nothing and says so to
            # nobody. The containers are gone by now, so there is no recovering
            # this — say what happened instead of inventing an answer.
            raise TransferError(
                f"{snapshot.name}: no pre-stop mount capture available",
                hint=(
                    "QUIESCE records what each container had mounted before Coolify "
                    "removes it, and DISCOVER cannot re-derive it afterwards. If this "
                    "is a resumed run, its journal predates the capture.\n"
                    "Roll back and start again: `coolify-migrate rollback <id>`."
                ),
            )

        manifest = await build_manifest(
            ctx.source_host,
            mounts=mounts,
            api_storages=await _storages_or_none(ctx, snapshot.collection, snapshot.uuid),
            uuid=snapshot.uuid,
            measure=True,
        )
        if manifest.is_blocked:
            reasons = "; ".join(i.reason for i in manifest.refused)
            raise TransferError(f"{snapshot.name}: {reasons}")

        source_eps = [
            VolumeEndpoint(name=i.source_name, mount_path=i.mount_path)
            for i in manifest.to_migrate
            if i.source_name
        ]
        # A dockercompose target loads its compose — and its persistent storages —
        # via an async job at create, so /storages is empty for the first seconds.
        # Wait for the volumes we need before pairing, or DISCOVER races the queue
        # and reports a real volume unpairable.
        if source_eps:
            target_eps = await _await_target_volumes(
                ctx,
                collection=snapshot.collection,
                target_uuid=target_uuid,
                expected={e.mount_path for e in source_eps},
            )
        else:
            target_eps = await api_resources.read_volume_endpoints(
                ctx.api, collection=snapshot.collection, uuid=target_uuid
            )

        pairs = pair_by_mount_path(source_eps, target_eps) if source_eps else []
        ctx.volume_pairs[snapshot.uuid] = pairs

        # An unpaired source volume is data we were asked to move and did not.
        # Silence here is what "migrated successfully, moved nothing" is made of.
        unpaired = {e.name for e in source_eps} - {p.source.name for p in pairs}
        if unpaired:
            raise TransferError(
                f"{snapshot.name}: no target volume matches {sorted(unpaired)}",
                hint=(
                    "Volumes are paired by mount_path, read back from what Coolify "
                    "actually created on the target. A source volume with no partner "
                    "would be left behind silently, so this stops instead.\n"
                    f"Target volumes seen: {sorted(e.mount_path for e in target_eps)}"
                ),
            )
        pairs_recorded[snapshot.uuid] = [
            {"source": p.source.name, "target": p.target.name, "mount_path": p.source.mount_path}
            for p in pairs
        ]

        # Bind mounts have no docker volume; mirror them path-to-path.
        for item in manifest.to_migrate:
            if item.source_name is None:
                pairs_recorded[snapshot.uuid].append(
                    {
                        "source": item.source_path,
                        "target": item.source_path,
                        "mount_path": item.mount_path,
                    }
                )

    return {"volume_pairs": pairs_recorded}


async def _transfer_endpoint(ctx: MigrationContext) -> tuple[str, int, str | None]:
    """Decide direct vs tunnel. Returns ``(host, port, identity_file)``.

    The tunnel solves reachability, not authentication — the ephemeral key is
    needed either way.
    """
    key = ctx.ephemeral_key
    identity = key.remote_path if key else None
    target_ip = ctx.plan.target_server.ip
    target_port = ctx.plan.target_server.port

    if ctx.plan.transfer_mode is TransferMode.TUNNEL:
        return "localhost", ctx.tunnel_port or target_port, identity

    if ctx.plan.transfer_mode is TransferMode.DIRECT:
        return target_ip, target_port, identity

    # AUTO: probe whether the source can reach the target at all.
    probe = await ctx.source_host.run(
        f"timeout 5 sh -c 'exec 3<>/dev/tcp/{shlex.quote(target_ip)}/{target_port}' 2>/dev/null"
    )
    if probe.ok:
        log.info("transfer.mode", mode="direct", reason="source can reach target")
        return target_ip, target_port, identity

    log.info("transfer.mode", mode="tunnel", reason="source cannot reach target directly")
    return "localhost", ctx.tunnel_port or target_port, identity


async def step_copy(ctx: MigrationContext) -> dict[str, Any]:
    """Mirror every volume, byte for byte, in parallel where it is safe."""
    # A stateless / rebuilt resource has no volume pairs. Skip the ephemeral-key
    # setup and the restart re-check entirely: there is nothing to copy, and doing
    # the work anyway only adds a way for a no-data migration to fail spuriously.
    if not any(ctx.volume_pairs.get(r.snapshot.uuid) for r in ctx.plan.resources):
        log.info("copy.nothing", reason="no volumes to migrate")
        return {"volumes_copied": [], "key_fingerprint": None}

    ctx.ephemeral_key = await keys.install(
        source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id
    )
    # Journal the fingerprint immediately so a crash still revokes it later.
    ctx.journal.append(
        "step_started",
        state="copy",
        detail={"key_fingerprint": ctx.ephemeral_key.fingerprint},
    )

    # What the source looks like BEFORE the transfer, to compare against
    # afterwards. Taking this at the end instead — which is what the first cut
    # did — compares the state to itself and can never disagree.
    before = await quiesce.snapshot(ctx.source_host, label_filters=observed_labels(ctx.plan))

    copied: list[str] = []
    for resource in ctx.plan.resources:
        for pair in ctx.volume_pairs.get(resource.snapshot.uuid, []):
            await docker.create_volume(ctx.target_host, pair.target.name)
            await _copy_one(ctx, pair.source_path, pair.target_path)
            copied.append(pair.target.name)

    # Nothing may have restarted mid-transfer. A `restart:` policy or a dashboard
    # deploy would invalidate the whole copy, and neither raises on its own.
    await quiesce.assert_still_stopped(
        ctx.source_host, label_filters=observed_labels(ctx.plan), since=before
    )
    return {"volumes_copied": copied, "key_fingerprint": ctx.ephemeral_key.fingerprint}


async def _copy_one(ctx: MigrationContext, source_path: str, target_path: str) -> None:
    host, port, identity = await _transfer_endpoint(ctx)

    entries: list[PathEntry] = []
    listing = await ctx.source_host.run(
        f"cd {shlex.quote(source_path)} 2>/dev/null && ls -A 2>/dev/null"
    )
    if listing.ok:
        for name in listing.stdout.split():
            size, _ = await docker.path_size(ctx.source_host, f"{source_path}/{name}")
            entries.append(PathEntry(relpath=name, bytes=size))

    hardlinks = await ctx.source_host.run(
        f"find {shlex.quote(source_path)} -type f -links +1 -print -quit 2>/dev/null"
    )
    has_hardlinks = bool(hardlinks.stdout.strip())

    total, _ = await docker.path_size(ctx.source_host, source_path)
    parallel = min(
        ctx.settings.transfer_parallel,
        suggest_parallelism(entry_count=len(entries), total_bytes=total),
    )
    plan = plan_transfer(
        entries, max_parallel=parallel, has_hardlinks=has_hardlinks, total_bytes=total
    )
    log.info(
        "copy.plan",
        source=source_path,
        chunks=plan.parallelism,
        reason=plan.reason,
        bytes=total,
    )

    async def run_chunk(paths: tuple[str, ...]) -> None:
        spec = rsync.RsyncSpec(
            source_path=source_path,
            target_path=target_path,
            target_host=host,
            target_user=ctx.plan.target_server.user,
            target_port=port,
            identity_file=identity,
            paths=paths,
            compress=ctx.settings.transfer_compress,
            bandwidth_limit_kbps=ctx.settings.transfer_bandwidth_kbps,
        )
        await rsync.run(ctx.source_host, spec)

    await asyncio.gather(*(run_chunk(chunk.paths) for chunk in plan.chunks))


async def step_verify(ctx: MigrationContext) -> dict[str, Any]:
    """Content AND metadata, both sides. A content-only check cannot see a chown."""
    total_diffs = 0
    verified: list[str] = []

    for resource in ctx.plan.resources:
        reports = []
        for pair in ctx.volume_pairs.get(resource.snapshot.uuid, []):
            report = await verify.verify_volume(
                ctx.source_host,
                ctx.target_host,
                source_path=pair.source_path,
                target_path=pair.target_path,
                parallel=ctx.settings.verify_parallel,
            )
            reports.append(report)
            total_diffs += len(report.differences)
            verified.append(pair.target.name)
        ctx.verifications[resource.snapshot.uuid] = reports

    if total_diffs:
        details: list[str] = []
        for reports in ctx.verifications.values():
            for report in reports:
                details.extend(d.describe() for d in report.differences[:5])
        raise VerificationError(
            f"{total_diffs} difference(s) between source and target:\n"
            + "\n".join(f"  {d}" for d in details[:20]),
            hint=(
                "The target will NOT be started and its volumes will be dropped. Your "
                "source is untouched. A metadata_differs on uid/gid means ownership "
                "changed — exactly what stops a database from starting."
            ),
        )

    return {"volumes_verified": verified, "differences": 0}


async def _server_addresses(host: str) -> frozenset[str]:
    """Resolve a server's ``ip`` field to actual addresses for the DNS verdict.

    Coolify's ``ip`` is often a HOSTNAME (``0046-20.cloud.bauer-group.com``), and
    a domain's A record must be compared to an address, not to a name — comparing
    ``1.2.3.4`` to ``0046-20…`` never matches, so every custom domain would read
    as ELSEWHERE and the gate could never fire. An IP literal is used as-is; a
    hostname that will not resolve falls back to itself, no worse than before.
    """
    host = (host or "").strip()
    if not host:
        return frozenset()
    try:
        ipaddress.ip_address(host)
        return frozenset({host})
    except ValueError:
        pass
    resolution = await dns_resolve.resolve_one(
        dns_extract.Hostname(
            host=host, origin=dns_extract.HostnameOrigin.FQDN, is_generated=False
        )
    )
    return frozenset(resolution.addresses) or frozenset({host})


async def step_dns_gate(ctx: MigrationContext) -> dict[str, Any]:
    """Refuse to start the target while a domain IT will serve still points at the source.

    Reads the TARGET's domains, not the source's: create_target may have PARKED
    the source's custom domains to free them, so the source no longer shows them.
    What matters is what the TARGET will answer on when it starts.
    """
    hostnames = []
    for resource in ctx.plan.resources:
        target_uuid = ctx.target_uuids[resource.snapshot.uuid]
        full = await ctx.api.get_resource(resource.snapshot.collection, target_uuid)
        envs = await ctx.api.get_envs(resource.snapshot.collection, target_uuid)
        hostnames.extend(
            dns_extract.collect(
                fqdn=full.get("fqdn"),
                compose_domains=full.get("docker_compose_domains"),
                envs=envs,
                labels=None,
            )
        )

    real = dns_extract.real_hostnames(
        sorted({h.host: h for h in hostnames}.values(), key=lambda h: h.host)
    )
    if not real:
        return {"hostnames": 0, "verdict": "no real hostnames"}

    # Coolify's server `ip` is often a hostname; resolve both sides to addresses so
    # a domain's A record is compared to an address, not a name.
    source_ips = await _server_addresses(ctx.plan.source_server.ip)
    target_ips = await _server_addresses(ctx.plan.target_server.ip)

    # A target-wildcard URL already resolves to the target (READY); a custom domain
    # still resolves to the source (CUTOVER_NEEDED). Keep the source-wildcard skip
    # so anything still under the SOURCE wildcard is not resolved into a false
    # cutover (it moved to the target's wildcard).
    src_wildcard = ctx.plan.source_server.wildcard_domain or None
    custom = [h for h in real if not dns_wildcard.under_wildcard(h.host, src_wildcard)]
    bound = [h for h in real if dns_wildcard.under_wildcard(h.host, src_wildcard)]

    resolutions = await dns_resolve.resolve_all(custom)
    resolutions.extend(Resolution(hostname=h, addresses=()) for h in bound)
    report = build_report(
        resolutions,
        source_ips=source_ips,
        target_ips=target_ips,
        source_wildcard=src_wildcard,
    )
    ctx.dns_report = report

    if report.is_blocked:
        blocked_hosts = ", ".join(v.hostname.host for v in report.blocked)
        if not ctx.accept_dns:
            # A live custom domain still points at the source. Refuse by default,
            # but resumably: flip DNS (or pass --accept-dns) and continue.
            raise DnsGateBlocked(
                "DNS still points at the source for: "
                + blocked_hosts
                + "\n\n"
                + explain_why_blocking_matters(),
                hint="Cutover:\n  " + "\n  ".join(report.cutover_checklist()),
                report=report,
            )
        # The operator accepted the cutover risk (custom domains only — DNS
        # propagation lags). Proceed and finalize; the target may serve a stale
        # certificate until DNS catches up.
        log.warning(
            "dns_gate.proceeding_despite_cutover",
            hosts=[v.hostname.host for v in report.blocked],
            hint="operator accepted DNS drift; cut the record(s) over to the target",
        )

    return {
        "hostnames": len(real),
        "server_bound": [v.hostname.host for v in report.server_bound],
        "ready": [v.hostname.host for v in report.ready],
        "cutover_accepted": [v.hostname.host for v in report.blocked] if ctx.accept_dns else [],
        "ambiguous": [v.hostname.host for v in report.ambiguous],
    }


async def step_start_target(ctx: MigrationContext) -> dict[str, Any]:
    started = []
    for resource in ctx.plan.resources:
        target_uuid = ctx.target_uuids[resource.snapshot.uuid]
        await ctx.api.start(resource.snapshot.collection, target_uuid)
        started.append(target_uuid)
    return {"started": started}


async def step_healthcheck(ctx: MigrationContext) -> dict[str, Any]:
    """Wait for the target's containers to come up.

    A deploy is asynchronous, so "start returned" means nothing. We poll the
    target's daemon for the same reason we poll the source's. The window is
    deploy_timeout, not stop_timeout: a git-built app clones and builds here first.
    """
    deadline = ctx.settings.deploy_timeout
    labels = observed_labels(ctx.plan)
    waited = 0.0
    interval = 3.0

    while waited < deadline:
        containers = await docker.list_containers(ctx.target_host, label_filters=labels)
        running = [c for c in containers if c.state == "running"]
        if containers and len(running) == len(containers):
            return {"containers": len(containers), "waited": round(waited, 1)}
        await asyncio.sleep(interval)
        waited += interval

    containers = await docker.list_containers(ctx.target_host, label_filters=labels)
    not_running = [c.name for c in containers if c.state != "running"]
    raise TransferError(
        f"target did not become healthy within {deadline:.0f}s; not running: "
        + ", ".join(not_running or ["<no containers appeared>"]),
        hint="Check the deployment logs in Coolify. The source is still intact.",
    )


async def step_finalize(ctx: MigrationContext) -> dict[str, Any]:
    """Apply the finalize policy to the SOURCE. The only irreversible step."""
    policy = ctx.plan.finalize_policy
    stamp = ctx.migration_id.split("-")[-1]
    actions: list[str] = []

    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        if policy is FinalizePolicy.KEEP:
            actions.append(f"kept {snapshot.name}")
            continue

        if policy is FinalizePolicy.RENAME:
            new_name = f"{snapshot.name}-old-{stamp}"
            await api_resources.rename(ctx.api, snapshot.collection, snapshot.uuid, new_name)
            # Without releasing the FQDN the old proxy keeps the router rule and
            # keeps renewing its certificate for a hostname it no longer serves.
            await api_resources.release_fqdn(
                ctx.api, snapshot.collection, snapshot.uuid, kind=snapshot.kind
            )
            actions.append(f"renamed {snapshot.name} -> {new_name}")
            continue

        await ctx.api.delete_resource(snapshot.collection, snapshot.uuid, delete_volumes=True)
        actions.append(f"deleted {snapshot.name}")

    await keys.revoke(source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id)
    return {
        "policy": policy.value,
        "actions": actions,
        "original_names": [r.snapshot.name for r in ctx.plan.resources],
    }


def build_steps() -> dict[Any, Any]:
    from bg_coolify_migrate.domain.statemachine import State

    return {
        State.INIT: step_init,
        State.PREFLIGHT: step_preflight,
        State.PLAN: step_plan,
        State.CREATE_TARGET: step_create_target,
        State.QUIESCE: step_quiesce,
        State.DISCOVER: step_discover,
        State.COPY: step_copy,
        State.VERIFY: step_verify,
        State.DNS_GATE: step_dns_gate,
        State.START_TARGET: step_start_target,
        State.HEALTHCHECK: step_healthcheck,
        State.FINALIZE: step_finalize,
    }
