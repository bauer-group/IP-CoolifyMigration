"""The F1 step implementations.

Each returns undo info to journal. The ordering and the compensations are decided
by ``domain/statemachine.py``; this module only performs them.

Every step is written so that its failure is safe: nothing here leaves a state
that :mod:`.compensations` cannot undo, and the source is never destroyed before
FINALIZE.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.api import schedules as api_schedules
from bg_coolify_migrate.api.resources import Placement, public_git_url
from bg_coolify_migrate.discovery import docker, pins, quiesce
from bg_coolify_migrate.dns import extract as dns_extract
from bg_coolify_migrate.dns import managed as dns_managed
from bg_coolify_migrate.dns import resolve as dns_resolve
from bg_coolify_migrate.dns import wildcard as dns_wildcard
from bg_coolify_migrate.dns.gate import (
    Resolution,
    Verdict,
    bound_to_source,
    build_report,
    explain_why_blocking_matters,
    is_definitive,
)
from bg_coolify_migrate.domain import domains, fidelity
from bg_coolify_migrate.domain.kinds import GitAuth, ResourceKind
from bg_coolify_migrate.domain.manifest import DockerMount
from bg_coolify_migrate.domain.naming import (
    VolumeEndpoint,
    VolumePair,
    VolumePairingError,
    compose_volume_suffix,
    pair_by_mount_path,
    pair_by_name_suffix,
)
from bg_coolify_migrate.domain.plan import HostBaseline, ResourcePlan, ResourceSnapshot, ServerRef
from bg_coolify_migrate.domain.statemachine import STOP_REQUESTED, FinalizePolicy, State
from bg_coolify_migrate.domain.versions import parse_version
from bg_coolify_migrate.engine import compensations, env_recovery, keys
from bg_coolify_migrate.engine.context import (
    MigrationContext,
    dns_baseline_of,
    is_volume_row,
    serialise_dns_baseline,
    serialise_mounts,
)
from bg_coolify_migrate.engine.planner import (
    build_manifest,
    inspect_all_mounts,
    observed_labels,
    resource_containers,
    resource_labels,
)
from bg_coolify_migrate.errors import (
    CoolifyApiError,
    DnsGateBlocked,
    MigrationError,
    PreflightError,
    QuiesceError,
    RebuildDriftBlocked,
    RollbackError,
    TransferError,
    VerificationError,
)
from bg_coolify_migrate.transfer import rsync, ssh, verify
from bg_coolify_migrate.transfer.partition import PathEntry, plan_transfer, suggest_parallelism
from bg_coolify_migrate.transfer.ssh import LOOPBACK

log = structlog.get_logger(__name__)


async def step_init(ctx: MigrationContext) -> dict[str, Any]:
    return {
        "project": ctx.plan.project,
        "environment": ctx.plan.environment,
        "source_server": ctx.plan.source_server.name,
        "target_server": ctx.plan.target_server.name,
        "resources": [r.snapshot.name for r in ctx.plan.resources],
    }


async def _label_independent_containers(
    ctx: MigrationContext, resource: ResourcePlan
) -> list[docker.Container]:
    """This resource's containers, found without trusting a single name label.

    Two probes, unioned by container id:

    * every ``coolify.managed`` container carrying the resource uuid in its name,
      image or label values (:func:`~bg_coolify_migrate.discovery.docker.carries_uuid`)
      — the uuid is the one handle a rename cannot touch;
    * every container mounting a volume the plan intends to migrate, via
      ``docker ps --filter volume=``, which reads no labels whatsoever.

    Neither replaces the label query. They exist to answer the one question it
    cannot: is "no containers" a fact about the stack, or a fact about our filter?
    """
    uuid = resource.snapshot.uuid
    found = {
        c.id: c
        for c in await docker.managed_containers(ctx.source_host)
        if docker.carries_uuid(c, uuid)
    }
    volumes = [i.source_name for i in resource.manifest.to_migrate if i.source_name]
    if volumes:
        for container in await docker.containers_mounting(ctx.source_host, volumes=volumes):
            # A STOPPED pin is no hidden writer: it never runs and only keeps
            # Coolify's cleanup off the volume. A running container wearing the
            # label is not a pin (pins.is_pin) and still counts.
            if pins.is_pin(container):
                if not pins.is_pin(container, migration_id=ctx.migration_id):
                    _refuse_foreign_pin_under_delete(ctx, resource, container)
                    log.warning(
                        "preflight.foreign_pin",
                        pin=container.name,
                        migration=container.labels.get(pins.LABEL_MIGRATION),
                        hint="harmless for a keep or rename finalize; remove it with "
                        "docker rm once that migration is finalized or rolled back",
                    )
                continue
            found.setdefault(container.id, container)
    return list(found.values())


def _refuse_foreign_pin_under_delete(
    ctx: MigrationContext, resource: ResourcePlan, pin: docker.Container
) -> None:
    """A pin another migration kept would make this run's delete keep the data.

    FINALIZE under DELETE hands the source's volumes to Coolify's ``docker
    volume rm -f``, which refuses a volume any container references and says
    nothing (v4.5.0:app/Models/StandalonePostgresql.php:180, Application.php:575;
    DeleteResourceJob turns a service's into a notification): the run would
    report the source deleted while its data stays, held by a pin no Coolify
    cleanup removes. A rollback that could not see the source come back keeps
    its pins by design, so this is the expected leftover, not a corner case.
    """
    if ctx.plan.finalize_policy is not FinalizePolicy.DELETE:
        return
    owner = pin.labels.get(pins.LABEL_MIGRATION) or "unknown"
    raise PreflightError(
        f"{resource.snapshot.name}: {pin.name}, a volume pin kept by migration "
        f"{owner}, holds a volume this run would delete",
        hint=(
            "A delete finalize hands the volumes to Coolify's `docker volume rm -f`, which "
            "cannot remove a volume a container references and does not say so - the "
            "source would be reported deleted while its data stays.\n"
            f"Once migration {owner} is finalized or rolled back and the "
            f"source runs, remove the pin on {ctx.plan.source_server.name} "
            f"(`docker rm {pin.name}` - a pin never ran and holds no data), then re-run.\n"
            "Nothing has been changed."
        ),
    )


def _label_disagreement(container: docker.Container, expected: dict[str, str]) -> list[str]:
    """The labels on which this container and our filter disagree. PURE."""
    return [
        f"    {key}={container.labels.get(key, '<absent>')!r} - we filter on {value!r}"
        for key, value in sorted(expected.items())
        if container.labels.get(key) != value
    ]


async def _assert_containers_are_discoverable(ctx: MigrationContext) -> None:
    """Refuse when containers demonstrably exist that our label filter cannot see.

    Coolify stamps ``coolify.projectName`` / ``coolify.resourceName`` into the
    compose it generates AT DEPLOY TIME and never re-stamps them. Rename a project
    and every container still running from before the rename keeps advertising the
    old slug. We AND all three labels, so one stale field hides the whole stack -
    and ``docker ps`` answers an empty list, not an error.

    The consequences are not symmetric, which is why this check does NOT depend on
    the manifest having found anything:

    * Data in a DECLARED, NAMED volume trips QUIESCE's "volumes but no containers"
      guard. Survivable - a refusal and a rollback.
    * Data in a BIND MOUNT or an anonymous volume appears in no API, so the
      manifest is empty, that guard never fires, Coolify stops the source from its
      own DB rows regardless of labels, and the target starts empty. Silent data
      loss.

    Runs at PREFLIGHT, on the cheapest evidence available, so the cost is a few
    seconds and no state - rather than at QUIESCE, which costs a created target and
    a rollback. QUIESCE's guard stays where it is; this does not replace it.
    """
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        expected = resource_labels(
            project=ctx.plan.project, environment=ctx.plan.environment, name=snapshot.name
        )
        by_label = await resource_containers(
            ctx.source_host,
            project=ctx.plan.project,
            environment=ctx.plan.environment,
            name=snapshot.name,
        )
        probed = await _label_independent_containers(ctx, resource)

        # Compared as SETS, not counts, and for every resource rather than only for
        # the ones the label query drew a blank on: a compose stack redeployed
        # service-by-service can carry current labels on one container and stale
        # ones on the next, and a count would call that agreement.
        seen = {c.id for c in by_label}
        missed = [c for c in probed if c.id not in seen]

        if missed:
            running = [c for c in missed if not c.is_stopped]
            detail = _label_disagreement(missed[0], expected)
            raise PreflightError(
                f"{snapshot.name}: {len(missed)} container(s) exist on "
                f"{ctx.plan.source_server.name} that the Coolify label filter cannot see"
                + (f", {len(running)} of them RUNNING" if running else "")
                + f": {', '.join(sorted(c.name for c in missed))}",
                hint=(
                    "Coolify writes the project/resource name labels when it deploys and "
                    "does NOT re-write them when you rename a project, so containers "
                    "deployed before a rename still advertise the old name:\n"
                    + ("\n".join(detail) or "    (no label differs - please report this)")
                    + "\n\nEvery gate that proves the source is quiet filters on those "
                    "labels, so migrating now would mirror volumes with a live writer "
                    "attached and then verify the tear byte-for-byte.\n"
                    f"Redeploy {snapshot.name} on {ctx.plan.source_server.name} to re-stamp "
                    "its labels, then migrate.\n"
                    "Nothing has been changed and the source is untouched."
                ),
            )

        # A resource that neither route can find is genuinely stopped, not hidden.
        # Not refused: an already-stopped stack is migratable, and this is the
        # check that makes it SAFE to migrate one. Establishing that the silence
        # is real is exactly what QUIESCE cannot tell on its own — so QUIESCE
        # proceeds on this finding, with a warning, rather than refusing.
        log.debug(
            "preflight.labels_current",
            resource=snapshot.name,
            containers=len(by_label),
            probed=len(probed),
        )


async def step_preflight(ctx: MigrationContext) -> dict[str, Any]:
    """Everything that must be true before we touch anything.

    Runs before CREATE_TARGET and long before QUIESCE, because discovering a
    missing rsync after the source is stopped converts a preflight failure into
    an outage.
    """
    await ctx.api.assert_can_read_sensitive()

    # ONE Coolify control plane manages both servers, so there is ONE API version
    # here — not one per side. Recorded, never gated: the release registry
    # (domain/coolify_versions.py) answers every release, and an unvalidated one
    # is a doctor warning, not a wall.
    #
    # It earns its round trip twice. As diagnostics: "Which Coolify was this?" is
    # the first question asked of any failed migration, and answering it for the
    # 2.5.6 tags 404 took a bisect of upstream release dates against a merge
    # commit. And as a guard: CREATE builds its bodies for the release the PLAN
    # saw, so an instance upgraded between plan and run must be re-planned, before
    # anything has been touched.
    coolify_version = await ctx.api.version()
    log.info("preflight.coolify", version=coolify_version)
    planned = ctx.plan.coolify_version
    if planned is not None and parse_version(planned) != parse_version(coolify_version):
        raise PreflightError(
            f"the plan was made against Coolify {planned}, but the instance now runs "
            f"{coolify_version.strip()}",
            hint=(
                "Requests are built for the release the plan saw, and what Coolify "
                "accepts or refuses differs between releases. Nothing has been changed "
                "and the source is untouched. Plan again, then run."
            ),
        )

    for host, label in ((ctx.source_host, "source"), (ctx.target_host, "target")):
        await rsync.ensure_installed(host, label=label)
        if not await host.which("docker"):
            raise PreflightError(f"docker is not installed on the {label} server")

    # Before anything expensive: can we actually SEE this stack? Everything that
    # follows - the quiesce gate, the mount capture, the mid-copy restart check -
    # trusts a label filter that a project rename silently invalidates.
    await _assert_containers_are_discoverable(ctx)

    await _assert_target_can_read_git(ctx)

    # Can the source actually REACH the endpoint rsync will dial? step_copy
    # resolves the same tuple, but it runs after quiesce — so a dead endpoint used
    # to cost a stopped source and a rollback instead of an error. It did exactly
    # that on 2026-07-21, which is the reason this check exists at all.
    #
    # Probed only when something will actually be transferred: a stateless
    # migration never opens the socket, and blocking one on an unreachable
    # endpoint would refuse a run that cannot fail this way.
    #
    # An UNDETERMINED probe does NOT block. A missing bash is not evidence of an
    # unreachable target, and a preflight that fails closed on its own blind spot
    # is worse than no preflight — it refuses migrations that would have worked.
    if any(r.manifest.to_migrate for r in ctx.plan.resources):
        endpoint_host, endpoint_port, _ = _transfer_endpoint(ctx)
        reachable = await ssh.can_reach(ctx.source_host, endpoint_host, endpoint_port)
        log.info(
            "preflight.transfer_endpoint",
            host=endpoint_host,
            port=endpoint_port,
            mode="tunnel" if ctx.tunnel_port is not None else "direct",
            reachable=reachable,
        )
        if reachable is False:
            via = (
                "the ssh tunnel through this workstation"
                if ctx.tunnel_port is not None
                else "a direct connection"
            )
            raise PreflightError(
                f"the source cannot reach {endpoint_host}:{endpoint_port}, "
                f"which is where rsync would send {ctx.plan.total_bytes / 1024**2:.1f} MB "
                f"over {via}",
                hint=(
                    "Nothing has been changed and the source is untouched.\n"
                    "For a tunnel this usually means the source's sshd forbids port "
                    "forwarding (AllowTcpForwarding), or the tunnel was closed.\n"
                    "For a direct transfer, check routing and firewalling between the "
                    "two servers on that port."
                ),
            )

    # Previews are not stopped by Coolify's stop endpoint, so they would keep
    # writing during the copy. Refuse now rather than corrupt later.
    if not ctx.delete_previews:
        await quiesce.assert_previews_absent(
            ctx.source_host, label_filters=observed_labels(ctx.plan)
        )

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

    return {
        "free_bytes": free,
        "required_bytes": required,
        "coolify_version": coolify_version,
    }


async def step_plan(ctx: MigrationContext) -> dict[str, Any]:
    """Journal what the plan decided — and the DNS baseline every later check
    compares against (hosts, addresses, TTL, nameserver names: no secrets)."""
    return {
        "total_bytes": ctx.plan.total_bytes,
        "volumes": sum(len(r.manifest.to_migrate) for r in ctx.plan.resources),
        "finalize_policy": ctx.plan.finalize_policy.value,
        "dns_baseline": serialise_dns_baseline(ctx.plan.dns_baseline),
    }


async def step_create_target(ctx: MigrationContext) -> dict[str, Any]:
    """Create every target resource, stopped, with its envs and storages.

    Runs BEFORE quiesce: a failed create then costs zero downtime, and the target
    must exist before volumes can be paired by mount path.

    Domains go make-before-break (domain/domains.py): the SOURCE is never
    written here. Each target claims its source's custom hosts — an application
    after its gates, in a second PATCH; a service through its sub-application
    urls — forcing a conflict only over this migration's own resources, and is
    read back to prove it holds them. The source keeps every host until
    FINALIZE.
    """
    # First, what an interrupted earlier attempt left: its targets, from the
    # journal. A no-op on a fresh run.
    undone = await _undo_interrupted_attempt(ctx)
    # Before ANY write of this attempt: re-read the locked values the plan
    # promised. A refusal here leaves nothing to undo. The values stay in this
    # frame (env_recovery).
    recovered = await env_recovery.recover_for_create(
        ctx.api, ctx.source_host, ctx.plan.resources, version=ctx.coolify_version
    )
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
    # Journal key `parked_domains` is historic (CREATE parked up to 2.9): it holds
    # the bodies that give each source its ORIGINAL domains back after FINALIZE.
    parked: dict[str, dict[str, Any]] = {}
    # Stable per project/environment: the service env filter drops hosts an
    # older run parked (domains.leftover_parking_refusals refuses them at PLAN).
    tag = domains.parking_tag(ctx.plan.project, ctx.plan.environment)
    source_wildcard = placement.source_wildcard
    # The release the plan was made for: every body below is built for it.
    version = ctx.coolify_version
    try:
        sources = {
            r.snapshot.uuid: await ctx.api.get_resource(r.snapshot.collection, r.snapshot.uuid)
            for r in ctx.plan.resources
        }
        # Whose domain conflicts a claim may override (domains.only_own_holders):
        # the plan's sources and their sub-applications, then each target made —
        # and an interrupted attempt's targets, just deleted. Until 4.3.11 a
        # DELETE only queues the job, and the row holds its hosts until the job
        # has finished its SSH cleanup, so this attempt's claim meets them.
        own: set[str] = set(sources) | undone
        for full in sources.values():
            own |= api_resources.sub_application_uuids(full)

        for resource in ctx.plan.resources:
            snapshot = resource.snapshot
            source_full = sources[snapshot.uuid]
            if snapshot.collection == "applications":
                await _refuse_multi_server(ctx, snapshot, source_full)

            # NO TAG TRANSFER. Tags are deliberately not carried — see the note on
            # SERVICE_CREATE in api/fields.py. Coolify's tag endpoints and the `tags`
            # create field exist only on unreleased `main`; on every published release
            # the read 404s and the create 422s.

            # The way back for FINALIZE's release, journalled before any write for
            # this resource. A service's includes the route-key rows its images
            # are routed from (minio, logto, garage).
            route_keys: frozenset[str] = frozenset()
            source_envs: list[dict[str, Any]] | None = None
            if snapshot.collection == "services":
                route_keys = dns_extract.route_env_keys(source_full.get("applications"))
                if route_keys:
                    source_envs = await ctx.api.get_envs(snapshot.collection, snapshot.uuid)
            restore = api_resources.source_restore_body(
                snapshot,
                source_full,
                source_wildcard=source_wildcard,
                version=version,
                source_envs=source_envs,
            )
            if restore is not None:
                parked[snapshot.uuid] = restore
                ctx.journal.append(
                    "step_started",
                    state="create_target",
                    detail={"target_uuids": dict(created), "parked_domains": dict(parked)},
                )

            target_uuid = await api_resources.create_resource(
                ctx.api, snapshot, placement, source_full, version=version
            )
            created[snapshot.uuid] = target_uuid
            ctx.target_uuids[snapshot.uuid] = target_uuid
            own.add(target_uuid)

            # Journal each creation IMMEDIATELY: a crash after creating the third of
            # five resources must still be able to delete all three.
            ctx.journal.append(
                "step_started",
                state="create_target",
                detail={"target_uuids": dict(created), "parked_domains": dict(parked)},
            )

            # A database's settings no create array takes (4.5.1+): one PATCH,
            # now the uuid is journaled.
            if snapshot.collection == "databases":
                await api_resources.finish_database_target(
                    ctx.api,
                    source_full,
                    target_uuid,
                    version=version,
                    target_log_drain=ctx.plan.target_server.log_drain_enabled,
                )

            # A service's domains ride its sub-applications: carried by one PATCH
            # now the uuid is journaled, and before copy_envs (4.5 derives the
            # target's SERVICE_URL_* rows from them in that PATCH).
            if snapshot.collection == "services":
                own |= await api_resources.finish_service_target(
                    ctx.api,
                    snapshot,
                    placement,
                    source_full,
                    target_uuid,
                    own=frozenset(own),
                    version=version,
                )

            await api_resources.copy_envs(
                ctx.api,
                collection=snapshot.collection,
                source_uuid=snapshot.uuid,
                target_uuid=target_uuid,
                source_wildcard=source_wildcard,
                tag=tag,
                recovered=recovered.get(snapshot.uuid),
                route_keys=route_keys,
                version=version,
                compose_app=snapshot.kind is ResourceKind.APP_GIT_COMPOSE,
            )
            await api_resources.copy_storages(
                ctx.api,
                collection=snapshot.collection,
                source_uuid=snapshot.uuid,
                target_uuid=target_uuid,
                kind=snapshot.kind,
                version=version,
            )
            # Cosmetic metadata: copy_tags never raises, so a missing tag route
            # (Coolify <4.2) or a rejected name degrades to "no tags" instead of
            # taking down a target that is otherwise complete.
            await api_resources.copy_tags(
                ctx.api,
                collection=snapshot.collection,
                source_uuid=snapshot.uuid,
                target_uuid=target_uuid,
            )

            # STILL inside CREATE, deliberately: the next step stops the source.
            if snapshot.kind is ResourceKind.APP_GIT_COMPOSE:
                await _assert_compose_target_ready(ctx, resource, target_uuid=target_uuid)
            elif snapshot.kind in (ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE):
                # A service is created from the INSTANCE's template catalog, not the
                # source's compose, and v4.3.10 -> v4.5.0 changed 95 templates (some
                # moved or dropped volume mounts). Its storage rows exist as soon as
                # POST /services returns (`parse(isNew: true)`, synchronous), so the
                # pairing DISCOVER will need can be rehearsed now — while the source
                # still serves — instead of refusing after the stop.
                await _pair_volumes(
                    ctx,
                    snapshot,
                    [
                        VolumeEndpoint(name=i.source_name, mount_path=i.mount_path)
                        for i in resource.manifest.to_migrate
                        if i.source_name
                    ],
                    target_uuid=target_uuid,
                    consequence="The source was never stopped; the created target will be deleted.",
                )

            # Phase two of an application's claim, after its gates: a compose
            # target keeps docker_compose_domains only for services its loaded
            # compose has, and a forced compose CREATE is ignored before 4.5.
            if snapshot.collection == "applications":
                await api_resources.claim_application_domains(
                    ctx.api, snapshot, placement, source_full, target_uuid, own=frozenset(own)
                )

            # After the gates: a dockercompose app's service names, which a task's
            # `container` must match, exist only once its compose has loaded.
            await api_schedules.copy_scheduled_tasks(
                ctx.api,
                collection=snapshot.collection,
                source_uuid=snapshot.uuid,
                target_uuid=target_uuid,
                kind=snapshot.kind,
                version=version,
            )
            await api_schedules.copy_backup_schedules(
                ctx.api,
                collection=snapshot.collection,
                source_uuid=snapshot.uuid,
                target_uuid=target_uuid,
                engine=snapshot.engine,
                version=version,
            )

        # Last, while the source still serves: DNS must still send its custom
        # hosts to it. If a record moved or vanished since the plan, the premise
        # of this migration broke under it — stop with zero downtime.
        await _refuse_moved_dns(ctx)
    except Exception as exc:
        # The saga compensates only COMPLETED steps, so a refusal part-way
        # through CREATE — the compose gate, the service rehearsal, a claim
        # someone else blocks, a 422 on the third of five resources — would
        # strand the targets already made. Undo exactly what this step did, then
        # let the original error speak — unless the undo itself failed, which
        # must not be reported as a plain failed create.
        problems = await compensations.undo_partial_create(ctx, created, parked)
        if problems:
            raise _undo_incomplete(ctx, problems, cause=exc) from exc
        raise

    return {"target_uuids": created}


async def _refuse_moved_dns(ctx: MigrationContext) -> None:
    """Re-check the plan's DNS baseline at the end of CREATE. Refuses on a change.

    Only hosts that resolved to the source at plan time, and only on a
    definitive answer (:func:`dns.gate.is_definitive`) — a timeout proves
    nothing and a refusal here costs a full re-run. A custom host that now
    resolves elsewhere, or to nothing, while the source still serves means a
    record was changed or deleted since the plan (Coolify deletes one it manages
    when a hostname loses its last holder, 4.4+; this run never lets that
    happen): the migration's picture of the world is wrong, so it stops before
    the source is touched.
    """
    baseline = dns_baseline_of(ctx.journal, ctx.plan)
    if not baseline:
        return
    source_ips = await _server_addresses(ctx.plan.source_server)
    on_source = sorted(h for h, entry in baseline.items() if set(entry.addresses) & source_ips)
    if not on_source:
        return
    now = await dns_resolve.resolve_hosts(on_source)
    moved = [
        f"{host} ({', '.join(now[host].addresses) or now[host].error or 'nothing'}, was "
        f"{', '.join(baseline[host].addresses)})"
        for host in on_source
        if host in now
        and is_definitive(now[host])
        and not set(now[host].addresses) & source_ips
    ]
    if not moved:
        return
    raise PreflightError(
        "DNS no longer sends these custom hosts to the source, which still serves them: "
        + "; ".join(moved),
        hint=(
            "Something changed or deleted their records since the plan — a person, a "
            "provider, or Coolify itself (4.4+ deletes a record it manages when its "
            "hostname loses its last holder). The source was never stopped; the targets "
            "created by this run are deleted again. Put the records back (pointing at "
            f"{ctx.plan.source_server.name}), then plan and migrate again."
        ),
    )


async def _refuse_multi_server(
    ctx: MigrationContext, snapshot: ResourceSnapshot, source_full: dict[str, Any]
) -> None:
    """Re-check, at CREATE, the plan's refusal of a multi-server application.

    The plan was refused if the application deployed to additional servers when
    it was made; a server attached in the UI since then would otherwise be
    dropped from the target and left serving next to it. Costs no API call on
    the common path: ``source_full`` is the GET CREATE reads anyway. Runs before
    anything of THIS resource is created; those made earlier in the loop are
    undone by step_create_target's handler.
    """
    if not fidelity.additional_servers(source_full)[0]:
        return
    # The remedy's wording is version-gated; unknown reads as newest.
    version = ctx.coolify_version
    raise PreflightError(
        f"{snapshot.name}: {fidelity.multi_server_refusal(source_full, version=version)}",
        hint=(
            "An additional server was attached after the plan was made. Nothing of "
            f"{snapshot.name} was created; anything created before it in "
            "this run is deleted again. The source was never stopped. Re-plan once "
            "the servers are removed."
        ),
    )


def _undo_incomplete(
    ctx: MigrationContext, problems: list[str], *, cause: Exception
) -> RollbackError:
    """Flag CREATE's undo as unfinished, so ``rollback <id>`` can finish it.

    The run ends ROLLBACK_FAILED (exit 8), and its journal is then finished, so
    resume refuses it — the explicit rollback, which owes ``undo_incomplete``
    states, is the way to retry. The source was never stopped.
    """
    ctx.journal.append("step_started", state="create_target", detail={"undo_incomplete": True})
    return RollbackError(
        "creating the target failed, and undoing it did not complete: " + "; ".join(problems),
        hint=(
            "The source was never stopped and is still serving.\n"
            f"The create failed with: {cause}\n"
            f"Fix what is named above, then finish the undo: "
            f"coolify-migrate rollback {ctx.migration_id}\n"
            f"Journal: {ctx.journal.path}"
        ),
    )


async def _undo_interrupted_attempt(ctx: MigrationContext) -> frozenset[str]:
    """Undo what an INTERRUPTED earlier attempt of this step left, before starting over.

    Returns the uuids that attempt's resources may still hold domains under —
    each target and its sub-applications, read before the delete — for the new
    attempt's own-holder set. On 4.1.2-4.3.10 ``DELETE`` only queues
    DeleteResourceJob (ApplicationsController/ServicesController
    ``delete_by_uuid``), whose database rows go only after its SSH cleanup; the
    new claim's 409 then names them, and a holder outside the set was refused
    as "held elsewhere". From 4.3.11 the row is soft-deleted first.

    A crash, SIGKILL or Ctrl-C inside CREATE never reaches the in-step undo
    (KeyboardInterrupt and CancelledError are BaseException), and the saga
    compensates only completed steps. But the journal holds everything that
    attempt did — each target and each restore body, written before the write.
    A resume that ignored it created a second set of targets, the first set
    orphaned and — once it had claimed its source's custom hosts — holding them,
    so the next claim 409'd on a holder that is no longer ours.

    So: delete those targets (each only while its source holds its hosts), give
    the source its domains back where a journal of an older version parked them,
    then record that this step owns nothing yet, and let the step create afresh.
    A no-op on a fresh run, whose journal holds no records for CREATE.
    """
    earlier = ctx.journal.undo_info(State.CREATE_TARGET.value)
    targets: dict[str, str] = earlier.get("target_uuids") or {}
    parked: dict[str, dict[str, Any]] = earlier.get("parked_domains") or {}
    if not targets and not parked:
        return frozenset()
    log.warning("create.undoing_interrupted_attempt", targets=len(targets), parked=len(parked))
    # Read BEFORE the delete: a deleted service's sub-applications cannot be listed.
    holders = await compensations.own_domain_holders(ctx, targets)
    problems = await compensations.undo_partial_create(ctx, targets, parked, holders=holders)
    if problems:
        raise _undo_incomplete(
            ctx,
            problems,
            cause=RuntimeError("an earlier attempt of this step was interrupted"),
        )
    ctx.journal.append(
        "step_started",
        state=State.CREATE_TARGET.value,
        detail={"target_uuids": {}, "parked_domains": {}, "undo_incomplete": False},
    )
    for source_uuid in targets:
        ctx.target_uuids.pop(source_uuid, None)
    return holders


async def _assert_compose_target_ready(
    ctx: MigrationContext, resource: ResourcePlan, *, target_uuid: str
) -> None:
    """Gate CREATE on the target having actually adopted its compose.

    This is how git access is verified for EVERY auth mode, private repos
    included: not by borrowing credentials — the deploy key lives on the control
    plane and is not ours — but by waiting for Coolify's own LoadComposeFile,
    which clones on the target server with Coolify's own key/GitHub App. Not a
    probe of the machinery; the machinery itself, observed.

    Runs BEFORE quiesce, deliberately. Waiting until DISCOVER meant the same
    refusal cost a production stop plus a rollback (2026-07-22); here
    the source is still serving, and a failure deletes the target and un-parks
    the domains with zero downtime.

    Two gates, in order:

    1. ``docker_compose_raw`` is non-empty — the job cloned and saved it.
    2. The declared storages cover every mount path the plan migrates — the
       parse ran, and the compose at HEAD still declares the volumes the source
       is running (drift surfaces HERE, not after the stop). DISCOVER re-checks
       against the post-stop capture, which stays authoritative.
    """
    snapshot = resource.snapshot
    deadline = ctx.settings.target_storage_timeout
    interval = 3.0
    waited = 0.0
    while True:
        target = await ctx.api.get_resource(snapshot.collection, target_uuid)
        if target.get("docker_compose_raw"):
            break
        if waited >= deadline:
            raise TransferError(
                f"{snapshot.name}: the target never loaded its compose from git ({round(waited)}s)",
                hint=(
                    "Coolify's LoadComposeFile job runs `git ls-remote` and a sparse "
                    "clone ON THE TARGET SERVER'S own shell, with Coolify's own "
                    "credentials. An empty compose after this long means that job "
                    "failed or never ran — a missing git binary or blocked egress to "
                    "the git host reproduces this exactly.\n"
                    f"Try on {ctx.plan.target_server.name}: git ls-remote <repo url>. "
                    "Coolify's failed-jobs list has the original error.\n"
                    "The source was never stopped and keeps serving; the created "
                    "target will be deleted."
                ),
            )
        await asyncio.sleep(interval)
        waited += interval
    log.info("create.compose_loaded", resource=snapshot.name, waited=round(waited, 1))

    migrating = [i for i in resource.manifest.to_migrate if i.source_name]
    if not migrating:
        return

    # Compose volumes are matched by their uuid-stripped KEY, not by mount path:
    # one volume can be mounted at several paths by several services — some
    # behind never-running ``profiles:`` — and Coolify's storage row records only
    # ONE of those sightings. covalida (2026-07-22): uploads lived at
    # /var/www/html/wp-content/uploads in the running containers while BOTH
    # sides' declarations said /srv/uploads (the dormant sftp service's mount);
    # the path-based gate refused a perfectly migratable stack after 180s of
    # waiting for a path that could never appear.
    suffixes = {
        i.source_name: compose_volume_suffix(i.source_name, snapshot.uuid)
        for i in migrating
        if i.source_name
    }
    if all(suffixes.values()):
        expected = {s for s in suffixes.values() if s}
        endpoints = await _await_target_volumes(
            ctx,
            collection=snapshot.collection,
            target_uuid=target_uuid,
            expected=expected,
            key=lambda e: compose_volume_suffix(e.name, target_uuid),
        )
        seen = sorted(s for e in endpoints if (s := compose_volume_suffix(e.name, target_uuid)))
        missing = expected - set(seen)
        if missing:
            raise TransferError(
                f"{snapshot.name}: the target's compose declares no volume for "
                f"key(s) {sorted(missing)}",
                hint=(
                    "The compose Coolify just loaded from the branch HEAD no longer "
                    "declares these volume keys — they were removed or renamed since "
                    "the source was deployed. Migrating would leave that data behind, "
                    "so this stops while the source is still running.\n"
                    f"Target declares: {seen or 'nothing'}\n"
                    "The source was never stopped; the created target will be deleted."
                ),
            )
        return

    # A migrating volume without the uuid prefix (a pinned external name):
    # suffix identity does not apply, fall back to the mount-path gate.
    expected_paths = {i.mount_path for i in migrating}
    endpoints = await _await_target_volumes(
        ctx, collection=snapshot.collection, target_uuid=target_uuid, expected=expected_paths
    )
    missing_paths = expected_paths - {e.mount_path for e in endpoints}
    if missing_paths:
        raise TransferError(
            f"{snapshot.name}: the target's compose declares no volume at {sorted(missing_paths)}",
            hint=(
                "The compose Coolify just loaded from the branch HEAD does not mount "
                "volumes where the running source does — it has drifted since the "
                "source was deployed. Migrating would leave that data behind, so this "
                "stops while the source is still running.\n"
                f"Target declares: {sorted(e.mount_path for e in endpoints) or 'nothing'}\n"
                "The source was never stopped; the created target will be deleted."
            ),
        )


async def _assert_target_can_read_git(ctx: MigrationContext) -> None:
    """Prove the target's own shell can reach every public compose repo.

    A dockercompose application's target gets its compose — and therefore its
    volumes — from Coolify's LoadComposeFile job, which runs ``git ls-remote``
    and a sparse clone ON the target server's shell (`Application::loadComposeFile`,
    ``exec_in_docker: false``). Deploys clone inside the helper container, so a
    server can deploy git apps for years while being unable to LOAD a compose:
    no git binary, or no egress to the git host. That failure otherwise surfaces
    only in DISCOVER, after the source is stopped — an outage plus a rollback for
    something knowable before anything was touched.

    Probes with the same command Coolify will run. Private repos are skipped —
    the deploy key lives on the control plane and is not ours to borrow — and a
    probe that cannot run (no ``timeout`` binary) does not block: a broken probe
    is not evidence of a broken target.
    """
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        if snapshot.kind is not ResourceKind.APP_GIT_COMPOSE:
            continue
        if snapshot.git_auth is not GitAuth.PUBLIC or not snapshot.git_repository:
            log.debug("preflight.git_probe_skipped", resource=snapshot.name, reason="private auth")
            continue

        url = public_git_url(snapshot.git_repository)
        probe = f"timeout 20 git ls-remote {shlex.quote(url)}"
        result = await ctx.target_host.run(probe)
        if result.ok:
            log.info("preflight.git_reachable", resource=snapshot.name, url=url)
            continue

        stderr = result.stderr.strip().splitlines()
        detail = stderr[-1][:200] if stderr else f"exit code {result.exit_status}"
        if "timeout" in detail and "not found" in detail:
            log.warning("preflight.git_probe_undetermined", resource=snapshot.name, detail=detail)
            continue

        # An auth refusal is a different disease than a broken host. git asking
        # for a Username means the host and the network are FINE — the repo is
        # private while the app is treated as public. That app survives on its
        # cached compose; a fresh target has no cache and can never load one.
        # (2026-07-22: this stderr surfaced the real bug — the planner
        # misread the GET's absent `github_app_uuid` as 'public' for a
        # GitHub-App-backed app. The classifier is fixed; this branch remains
        # for apps whose source genuinely is public while the repo is not.)
        auth_markers = (
            "could not read Username",
            "could not read Password",
            "Authentication failed",
            "Permission denied",
            "Repository not found",
            "terminal prompts disabled",
        )
        if any(marker in result.stderr for marker in auth_markers):
            raise PreflightError(
                f"{snapshot.name}: {url} requires authentication, but the app's git "
                "source is PUBLIC",
                hint=(
                    "The repository refused an unauthenticated read — which is exactly "
                    "what Coolify's LoadComposeFile will attempt for a public-source "
                    "app, on this and every server. The source still runs because its "
                    "compose was cached while access worked; a fresh target cannot.\n"
                    f"{ctx.plan.target_server.name} said: {detail}\n"
                    "Give the application a private git source in Coolify (GitHub App, "
                    "or a deploy key on the repo), then re-run — the target is then "
                    "created via the matching private route and Coolify clones with its "
                    "own credentials. Note a public-source app also cannot DEPLOY from "
                    "a private repo, so this needs fixing regardless of the migration.\n"
                    "Nothing has been changed."
                ),
            )

        raise PreflightError(
            f"{snapshot.name}: the target server cannot read {url}",
            hint=(
                "Coolify loads a dockercompose app's compose file by running git ON the "
                "target server's own shell (LoadComposeFile) — unlike deploys, which clone "
                "inside a helper container. Until that works, a target created there never "
                "gets its volumes, and the migration would stop the source only to fail "
                "in DISCOVER.\n"
                f"{ctx.plan.target_server.name} said: {detail}\n"
                "Install git on the target / allow egress to the git host, then re-run. "
                "Nothing has been changed."
            ),
        )


async def step_quiesce(ctx: MigrationContext) -> dict[str, Any]:
    """Stop the whole stack and prove it, by asking the daemon.

    We never trust the stop endpoint: it is async, it does not touch previews,
    and for services it works off DB rows rather than labels.
    """
    if ctx.delete_previews:
        for resource in ctx.plan.resources:
            if not resource.snapshot.has_previews:
                continue
            # NOT `coolify.applicationId`: that label holds Coolify's NUMERIC id,
            # which every API controller makeHidden()s and we therefore never have.
            # Filtering a uuid against it matched nothing, always - so
            # --delete-previews deleted no previews and the preflight gate then
            # refused the migration over the very previews it had been asked to
            # remove.
            containers = await resource_containers(
                ctx.source_host,
                project=ctx.plan.project,
                environment=ctx.plan.environment,
                name=resource.snapshot.name,
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
    captured = await _capture_mounts(ctx)

    # Pin BEFORE the stop: Coolify's stop removes the containers, and from that
    # moment nothing references the source's volumes - which is all Coolify's
    # `docker volume prune -af` asks. A failure here costs nothing.
    source_pins = await _pin_source(ctx)

    try:
        # The source's clock, not ours: it is the window for the event-log check
        # below, and a skewed workstation would silently shrink it.
        since = await quiesce.now_on(ctx.source_host)

        # From here the source may be down whatever this step does next — and it
        # can still fail (a SIGKILLed data holder, the stop gate timing out)
        # without ever completing. The marker makes the rollback owe the restart
        # anyway (statemachine.source_was_stopped).
        ctx.journal.append("step_started", state="quiesce", detail={STOP_REQUESTED: True})
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
            _refuse_if_killed_data_holder(ctx, killed)
    except MigrationError as exc:
        _name_source_pins(ctx, exc, source_pins)
        raise

    # Counts come from the PRE-stop capture. The report cannot know them: a
    # Coolify stop removes containers as it stops them, so the final snapshot is
    # empty precisely when everything worked — it counted 0 for a 4-container
    # stack and journalled that as what the migration stopped.
    return {
        "containers_stopped": sum(len(names) for names in captured.values()),
        "container_names": sorted(n for names in captured.values() for n in names),
        "elapsed": round(report.elapsed, 1),
        "source_pins": source_pins,
    }


async def _pin_source(ctx: MigrationContext) -> list[str]:
    """Pin every volume the source mounts, while it still mounts them.

    The WHOLE source is pinned, not only what will be copied: until FINALIZE it
    is the rollback, and a rollback onto a pruned volume restarts an empty
    database. A pin that cannot be made refuses the stop rather than proceed
    unprotected — nothing has been stopped yet, so the refusal is free.
    """
    volumes = sorted({v for found in ctx.source_pin_volumes.values() for v in found})
    idle = sorted({v for found in ctx.source_idle_volumes.values() for v in found})
    try:
        pinned = await pins.ensure(
            ctx.source_host,
            migration_id=ctx.migration_id,
            side=pins.SOURCE,
            volumes=volumes,
            idle=idle,
        )
    except (MigrationError, ValueError) as exc:
        raise QuiesceError(
            f"could not pin the source's volumes on {ctx.plan.source_server.name} against "
            "Coolify's Docker cleanup",
            hint=(
                "Nothing was stopped; the source keeps serving and the created target will "
                f"be deleted.\n{exc}"
            ),
        ) from exc
    if pinned:
        ctx.journal.append("step_started", state="quiesce", detail={"source_pins": pinned})
    return pinned


def _name_source_pins(ctx: MigrationContext, exc: MigrationError, pinned: list[str]) -> None:
    """Tell the operator which container keeps the stopped source's data alive.

    A QUIESCE that fails after the stop is never completed, but its
    ``stop_requested`` marker makes the rollback restart the source, and the
    pin goes once the restarted source mounts its volumes again. Should that
    restart fail, the source stays down - and its pin deliberately with it.
    """
    if not pinned:
        return
    # No `rollback <id>` advice: the rollback that follows this error already
    # restarts the source and removes the pin, and running it again would
    # restart a source that is serving by then. `unpin` does only the unpin.
    note = (
        f"The source's volumes are pinned by {', '.join(pinned)} - a container that is "
        "never started: no process, writes nothing - so Coolify's Docker cleanup cannot "
        "delete them while the source is down. The rollback restarts the source and "
        "removes the pin once the source keeps its volumes mounted; the run report names "
        f"a pin it has to keep (then `coolify-migrate unpin {ctx.migration_id}` once the "
        "source runs)."
    )
    exc.hint = f"{exc.hint}\n\n{note}" if exc.hint else note


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


def _refuse_if_killed_data_holder(ctx: MigrationContext, killed: list[tuple[str, int]]) -> None:
    """Fail the stop only when a SIGKILLed container owned bytes we are about to copy.

    Exit code 137 means the stop grace period elapsed and Docker killed the
    process. Why that is fatal has always been about the DATA, never about the
    signal: a database killed mid-write has not flushed, so mirroring its volume
    byte-exactly gives you a faithful copy of the tear. There is no ``--force``
    for that and there should not be.

    But the check refused on ANY killed container in the stack, and most stacks
    contain containers that own no bytes at all — an application server, a
    worker, a sidecar. One of those ignoring SIGTERM costs this migration
    precisely nothing, and blocking on it makes an otherwise sound stack
    unmigratable for a reason unrelated to its data. QUIESCE captured which
    containers mounted the volumes in the manifest (:func:`_containers_holding_data`),
    so the question can be asked exactly.

    A kill we let through is still reported — loudly, with names — because it
    means the resource's stop grace period is too short and the operator should
    know, even though this run is unaffected.
    """
    holders = {name for names in ctx.data_containers.values() for name in names}
    at_risk = sorted(name for name, _ in killed if name in holders)
    harmless = sorted(name for name, _ in killed if name not in holders)

    if harmless:
        log.warning(
            "quiesce.killed_without_data",
            containers=harmless,
            hint=(
                "SIGKILLed at the stop grace period, but mounted none of the volumes "
                "being migrated - no bytes are at risk. Raise the resource's stop grace "
                "period in Coolify anyway; an unclean stop is a bug in its shutdown path."
            ),
        )
    if not at_risk:
        return

    raise QuiesceError(
        "container(s) holding migrated data were SIGKILLed rather than stopping "
        f"cleanly: {', '.join(at_risk)}",
        hint=(
            "Exit code 137 means the stop grace period elapsed and Docker killed the "
            "process. A killed database has not flushed, so its volume is a torn "
            "snapshot, and mirroring it byte-exactly would give you a faithful copy "
            "of the tear.\n"
            "Raise the stop grace period on the resource in Coolify and retry. "
            "Nothing has been copied."
        ),
    )


def _containers_holding_data(
    resource: ResourcePlan, containers: list[docker.Container], mounts: list[DockerMount]
) -> tuple[str, ...]:
    """Names of the containers that mount data this migration will copy. PURE.

    The join `docker events` cannot do for us. A mount records the handle it was
    inspected by — the container id — while the event log reports a container by
    NAME, and by the time we read the log the containers are gone, so neither can
    be resolved into the other after the fact.

    Used by the SIGKILL check to tell the two cases apart: a killed process that
    owned bytes we are about to mirror (a torn snapshot, fatal) from one that
    owned none (a stateless app container that ignored SIGTERM, which costs this
    migration nothing).
    """
    migrating_names = {i.source_name for i in resource.manifest.to_migrate if i.source_name}
    migrating_paths = {i.source_path for i in resource.manifest.to_migrate if i.source_path}
    by_id = {c.id or c.name: c.name for c in containers}

    holders = {
        by_id.get(m.container, m.container)
        for m in mounts
        if (m.name and m.name in migrating_names) or (m.source and m.source in migrating_paths)
    }
    return tuple(sorted(holders))


def _source_volumes_to_pin(
    resource: ResourcePlan, containers: list[docker.Container], mounts: list[DockerMount]
) -> tuple[list[str], list[str]]:
    """``(mounted, idle)``: every volume the source mounts, and the planned rest. PURE.

    Mounted, because the whole source must survive until FINALIZE - it is the
    rollback. Planned, because an already-stopped resource has no mounts to
    read and its declared volumes are just as unreferenced. The planned volumes
    no container mounts are IDLE: no restart will mount them either, so a
    rollback releases their pin without waiting for one (``pins.awaited``).

    A PREVIEW's volumes are left alone. Coolify deletes a preview's volumes
    with ``docker volume rm -f`` and, from 4.4, throws when that refuses a
    volume in use (v4.5.0:app/Models/ApplicationPreview.php:72-80) - a pin
    would break the preview delete ``--delete-previews`` just asked for.
    """
    previews = {handle for c in containers if c.is_preview for handle in (c.id, c.name) if handle}
    mounted = {
        m.name for m in mounts if m.type == "volume" and m.name and m.container not in previews
    }
    planned = {i.source_name for i in resource.manifest.to_migrate if i.source_name}
    return sorted(mounted), sorted(planned - mounted)


async def _capture_mounts(ctx: MigrationContext) -> dict[str, list[str]]:
    """Record every container mount before the stop erases the containers.

    Returns the container names seen per resource uuid. They matter beyond
    debugging: the post-stop report can only ever say how many containers are
    LEFT (none, if the stop worked — Coolify removes them), so this capture is
    the one honest answer to "what did we stop?".

    Coolify does not merely stop a stack, it removes it — `docker rm -f` in
    StopDatabase, StopApplication and StopService alike. So the post-stop
    discovery the design calls for cannot read mounts off containers: there are
    none. Anonymous volumes and bind mounts appear in no API, which makes this
    the only record of them that will exist a few seconds from now.

    Refuses to proceed on finding nothing, because "no containers" and "wrong
    label filter" look identical from here, and the difference is a migration
    that copies nothing and says it worked.
    """
    captured: dict[str, list[str]] = {}
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        containers = await resource_containers(
            ctx.source_host,
            project=ctx.plan.project,
            environment=ctx.plan.environment,
            name=snapshot.name,
        )
        if not containers:
            # An ALREADY-STOPPED resource. Migrating one is legitimate and this
            # used to refuse it, on the grounds that a running stack is the only
            # chance to see bind mounts and anonymous volumes. Two things make
            # that refusal wrong now:
            #
            # * The manifest no longer needs a container. `reconcile` migrates a
            #   DECLARED persistent storage whose docker volume EXISTS even when
            #   nothing mounts it - the API is the intent, `volume ls` the
            #   residue - and warns, in the plan the operator confirms, about any
            #   volume that looks like this stack's and is not being moved.
            # * "No containers" is no longer ambiguous. PREFLIGHT looks for each
            #   resource by uuid and by mounted volume, using no name label at
            #   all, and refuses when it finds one the label query missed. So
            #   reaching here means the silence is a fact about the stack.
            #
            # What is genuinely lost is the ability to DISCOVER a mount that
            # appears in no Coolify API - an anonymous volume, an undeclared bind.
            # That is a warning (raised at plan time too, so it is visible before
            # the operator commits), not a refusal: refusing makes a stopped stack
            # unmigratable, and the operator can see exactly which volumes move.
            #
            # Record an EXPLICIT empty capture, not nothing: DISCOVER treats a
            # missing key (None) as a lost capture and aborts, but an empty list
            # correctly means "this resource had no mounts".
            ctx.pre_stop_mounts[snapshot.uuid] = []
            ctx.data_containers[snapshot.uuid] = ()
            (
                ctx.source_pin_volumes[snapshot.uuid],
                ctx.source_idle_volumes[snapshot.uuid],
            ) = _source_volumes_to_pin(resource, [], [])
            captured[snapshot.uuid] = []
            if resource.manifest.to_migrate:
                log.warning(
                    "quiesce.stopped_resource",
                    resource=snapshot.name,
                    volumes=[i.source_name for i in resource.manifest.to_migrate],
                    hint="no container to inspect; undeclared mounts cannot be ruled out",
                )
            else:
                log.info("quiesce.no_containers_no_volumes", resource=snapshot.name)
            continue
        mounts = await inspect_all_mounts(ctx.source_host, containers)
        ctx.pre_stop_mounts[snapshot.uuid] = mounts
        ctx.data_containers[snapshot.uuid] = _containers_holding_data(resource, containers, mounts)
        (
            ctx.source_pin_volumes[snapshot.uuid],
            ctx.source_idle_volumes[snapshot.uuid],
        ) = _source_volumes_to_pin(resource, containers, mounts)
        captured[snapshot.uuid] = sorted(c.name for c in containers)
        log.info(
            "quiesce.mounts_captured",
            resource=snapshot.name,
            containers=len(containers),
            mounts=len(mounts),
            holding_data=len(ctx.data_containers[snapshot.uuid]),
        )

    # Journal it before the stop. The context dies with the process and the
    # containers die with the stop, so after a crash this record is the only
    # description of them that exists anywhere — a resume cannot go and look.
    ctx.journal.append(
        "step_started",
        state="quiesce",
        detail={"pre_stop_mounts": serialise_mounts(ctx.pre_stop_mounts)},
    )
    return captured


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
    compose_from_git: bool = False,
    key: Callable[[VolumeEndpoint], str | None] | None = None,
) -> list[VolumeEndpoint]:
    """Poll the target's declared volumes until they cover ``expected`` keys.

    ``key`` extracts the comparison key from an endpoint — ``mount_path`` by
    default, or the uuid-stripped name suffix for compose apps (see
    :func:`bg_coolify_migrate.domain.naming.pair_by_name_suffix` for why the
    mount path identifies nothing there).

    The target of a dockercompose migration gets its persistent storages from an
    async LoadComposeFile job dispatched at create, so a read right after create
    can miss them. Returns as soon as every expected key is present, or the
    latest reading at timeout — the caller's unpaired-volume check then produces
    the precise, actionable error rather than a silent miss.

    With ``compose_from_git`` (an APP_GIT_COMPOSE target), a timeout is
    disambiguated before returning: the storages come from Coolify cloning the
    repo ON the target server and parsing the compose, and when that job dies the
    app's ``docker_compose_raw`` stays empty forever. That is not a race to wait
    out but an infrastructure failure to name — so it raises here, with the
    failing machinery in the message, instead of surfacing later as a pairing
    refusal that reads like a compose mismatch.
    """
    keyfn = key or (lambda e: e.mount_path)

    def seen_keys(endpoints: list[VolumeEndpoint]) -> set[str]:
        return {k for e in endpoints if (k := keyfn(e)) is not None}

    deadline = ctx.settings.target_storage_timeout
    interval = 3.0
    waited = 0.0
    endpoints = await api_resources.read_volume_endpoints(
        ctx.api, collection=collection, uuid=target_uuid
    )
    while not expected <= seen_keys(endpoints) and waited < deadline:
        await asyncio.sleep(interval)
        waited += interval
        endpoints = await api_resources.read_volume_endpoints(
            ctx.api, collection=collection, uuid=target_uuid
        )
    if not expected <= seen_keys(endpoints):
        log.warning(
            "discover.target_volumes_incomplete",
            waited=round(waited, 1),
            expected=sorted(expected),
            seen=sorted(seen_keys(endpoints)),
        )
        if compose_from_git:
            target = await ctx.api.get_resource(collection, target_uuid)
            if not target.get("docker_compose_raw"):
                raise TransferError(
                    f"the target application never loaded its compose from git "
                    f"({round(waited)}s): its volumes cannot exist yet",
                    hint=(
                        "Coolify's LoadComposeFile job runs `git ls-remote` and a sparse "
                        "clone ON THE TARGET SERVER'S own shell (not in a container) and "
                        "creates the persistent storages from the parsed compose. An empty "
                        "compose after this long means that job failed or never ran.\n"
                        f"On {ctx.plan.target_server.name}, try: git ls-remote <repo url> — "
                        "a missing git binary or blocked egress to the git host reproduces "
                        "this exactly. Coolify's failed-jobs list has the original error.\n"
                        "The source will be restarted and the target deleted."
                    ),
                )
    else:
        log.info("discover.target_volumes_ready", waited=round(waited, 1))
    return endpoints


#: Kinds whose volumes Coolify names ``{uuid}{sep}{compose volume key}``.
_COMPOSE_KINDS = frozenset(
    {ResourceKind.APP_GIT_COMPOSE, ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE}
)
_SERVICE_KINDS = frozenset({ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE})


async def _pair_volumes(
    ctx: MigrationContext,
    snapshot: ResourceSnapshot,
    source_eps: list[VolumeEndpoint],
    *,
    target_uuid: str,
    consequence: str,
) -> list[VolumePair]:
    """Pair source volumes with what Coolify actually created on the target.

    Shared by DISCOVER (authoritative, after the stop) and CREATE (a rehearsal
    for services, before it) so the two can never disagree about what pairs.

    Compose volumes pair by their uuid-stripped name suffix — the compose volume
    KEY — because mount_path identifies nothing there: one volume can be mounted
    at several paths by several services (some behind never-running `profiles:`),
    and two volumes can share one path across services (15 of the 386 v4.5.0
    service templates do, budibase's minio_data and redis_data both at /data).
    Services get the same treatment since their volumes are ``{uuid}_{key}``;
    until 2026-10-26 they paired by mount path and refused those templates after
    the stop. Falls back to mount-path pairing when a source name does not carry
    the uuid prefix.

    Raises:
        TransferError: When pairing refuses — an ambiguity or a source volume
            with no target counterpart, i.e. data that would be left behind.
    """
    if not source_eps:
        return []
    by_suffix = snapshot.kind in _COMPOSE_KINDS and all(
        compose_volume_suffix(e.name, snapshot.uuid) for e in source_eps
    )
    is_service = snapshot.kind in _SERVICE_KINDS

    def suffix_key(e: VolumeEndpoint, uuid: str = target_uuid) -> str | None:
        return compose_volume_suffix(e.name, uuid)

    if is_service:
        # POST /services parses inline (`parse(isNew: true)`, v4.1.2 through
        # v4.5.0), so the rows exist when it returns: polling could never make
        # a missing key appear, it would only delay the refusal by the timeout.
        target_eps = await api_resources.read_volume_endpoints(
            ctx.api, collection=snapshot.collection, uuid=target_uuid
        )
    else:
        # A dockercompose target loads its compose — and its persistent
        # storages — via an async job at create, so /storages is empty for the
        # first seconds. Wait for the volumes we need before pairing, or this
        # races the queue and reports a real volume unpairable.
        target_eps = await _await_target_volumes(
            ctx,
            collection=snapshot.collection,
            target_uuid=target_uuid,
            expected=(
                {s for e in source_eps if (s := compose_volume_suffix(e.name, snapshot.uuid))}
                if by_suffix
                else {e.mount_path for e in source_eps}
            ),
            compose_from_git=snapshot.kind is ResourceKind.APP_GIT_COMPOSE,
            key=suffix_key if by_suffix else None,
        )

    # Both pairers refuse ambiguity and unpaired source volumes — data left
    # behind if allowed through. Those refusals are diagnoses, not crashes:
    # name both sides so the operator can see WHICH compose disagrees,
    # instead of a raw traceback wrapped as "unexpected error in discover"
    # (which is what this was).
    try:
        if not by_suffix:
            return pair_by_mount_path(source_eps, target_eps)
        pairs = pair_by_name_suffix(
            source_eps, target_eps, source_uuid=snapshot.uuid, target_uuid=target_uuid
        )
    except VolumePairingError as exc:
        raise TransferError(
            f"{snapshot.name}: {exc}",
            hint=(
                "Volumes are paired by "
                + ("compose volume key" if by_suffix else "mount_path")
                + ", read back from what Coolify actually created on the target "
                "— never predicted.\n"
                f"Source volumes: {sorted(f'{e.name} @ {e.mount_path}' for e in source_eps)}\n"
                "Target declares: "
                f"{sorted(f'{e.name} @ {e.mount_path}' for e in target_eps) or 'nothing'}\n"
                "A mismatch usually means the target was created from a different "
                "compose than the source runs — the branch HEAD moved, or (services) "
                "the instance's template catalog changed since the source was "
                f"created. {consequence}"
            ),
        ) from exc
    if is_service:
        await _assert_template_kept_its_mounts(
            ctx, snapshot, source_eps, target_eps, target_uuid=target_uuid, consequence=consequence
        )
    # Target volumes with no source counterpart are legitimate here: compose
    # volumes of profile-gated services that never ran on the source, or a
    # template's newer volume. They start empty on the target exactly as they
    # would on the source — say so, but do not refuse.
    paired = {p.target.name for p in pairs}
    unpaired = sorted(e.name for e in target_eps if e.name not in paired)
    if unpaired:
        log.info("discover.target_volumes_start_empty", resource=snapshot.name, volumes=unpaired)
    return pairs


async def _assert_template_kept_its_mounts(
    ctx: MigrationContext,
    snapshot: ResourceSnapshot,
    source_eps: list[VolumeEndpoint],
    target_eps: list[VolumeEndpoint],
    *,
    target_uuid: str,
    consequence: str,
) -> None:
    """Refuse a service whose template moved a volume's mount path.

    Key pairing answers WHICH volume is which; it never asks WHERE the target
    mounts it. A template service's target is built from the instance's current
    catalog, and between v4.1.2 and v4.5.0 several templates kept a volume key and
    moved its mount — opnform's `postgresql-data` went from /var/lib/postgresql/data
    (postgres 16) to /var/lib/postgresql (postgres 18). Paired by key alone, the
    bytes are copied, VERIFY compares two identical volumes and passes, and the
    target's database starts empty beside them.

    Every path the TARGET declares for a key must be one the source declares
    (Coolify's own parse of its compose) or mounts live. Declarations alone
    would refuse a source whose row is stale — an older path the containers no
    longer use — and live mounts alone would miss a second sighting of a volume
    two services share; the union admits both and still catches a moved mount.
    """

    def by_key(endpoints: list[VolumeEndpoint], uuid: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for e in endpoints:
            if key := compose_volume_suffix(e.name, uuid):
                out.setdefault(key, set()).add(e.mount_path.rstrip("/") or "/")
        return out

    known = by_key(source_eps, snapshot.uuid)
    for key, paths in by_key(
        await api_resources.read_volume_endpoints(
            ctx.api, collection=snapshot.collection, uuid=snapshot.uuid
        ),
        snapshot.uuid,
    ).items():
        known.setdefault(key, set()).update(paths)
    declared = by_key(target_eps, target_uuid)
    moved = {
        key: (sorted(known[key]), sorted(paths))
        for key, paths in sorted(declared.items())
        if key in known and not paths <= known[key]
    }
    if moved:
        raise TransferError(
            f"{snapshot.name}: the target mounts volume(s) {sorted(moved)} at a different "
            "path than the source",
            hint=(
                "A service is recreated from the instance's CURRENT template catalog, and "
                "that template has moved where these volumes are mounted since the source "
                "was created — usually together with a new image (a major database "
                "upgrade). Copying the bytes across would pass verification and still "
                "start the target on an empty data directory.\n"
                + "\n".join(f"  {k}: source {s} -> target {t}" for k, (s, t) in moved.items())
                + "\nUpgrade the source to the current template first, or migrate this "
                f"service by hand. {consequence}"
            ),
        )


async def step_discover(ctx: MigrationContext) -> dict[str, Any]:
    """The authoritative manifest, taken with nothing able to write.

    Pairs source to target volumes by ``mount_path`` — read back from what
    Coolify actually created, never predicted, never derived by string-replacing
    a uuid.
    """
    pairs_recorded: dict[str, list[dict[str, str]]] = {}
    source_pins: set[str] = set()

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

        # A volume only `volume ls` or `/storages` show was never mounted, so
        # QUIESCE could not pin it - and no restart would mount it, so it is
        # idle. A no-op for every volume QUIESCE pinned.
        was_mounted = {m.name for m in mounts if m.type == "volume" and m.name}
        migrating = {i.source_name for i in manifest.to_migrate if i.source_name}
        source_pins.update(
            await pins.ensure(
                ctx.source_host,
                migration_id=ctx.migration_id,
                side=pins.SOURCE,
                volumes=sorted(migrating & was_mounted),
                idle=sorted(migrating - was_mounted),
            )
        )

        source_eps = [
            VolumeEndpoint(name=i.source_name, mount_path=i.mount_path)
            for i in manifest.to_migrate
            if i.source_name
        ]
        pairs = await _pair_volumes(
            ctx,
            snapshot,
            source_eps,
            target_uuid=target_uuid,
            consequence="The source will be restarted and the target deleted.",
        )
        ctx.volume_pairs[snapshot.uuid] = pairs

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

    return {"volume_pairs": pairs_recorded, "source_pins": sorted(source_pins)}


def _transfer_endpoint(ctx: MigrationContext) -> tuple[str, int, str | None]:
    """The address rsync dials. Returns ``(host, port, identity_file)``. PURE.

    Reads the decision the RUNNER already made rather than making its own.
    ``maybe_tunnel`` probes reachability once, opens the forward, and publishes
    the result as ``ctx.tunnel_port``. An open forward IS the decision: a port
    means tunnel, no port means direct.

    This used to re-probe here and pick independently, which was a second opinion
    that could disagree with the first. When it did — a blip between the two
    probes was enough — the tunnel branch fell back to
    ``ctx.tunnel_port or target_port`` and returned ``127.0.0.1:22``: the SOURCE's
    own sshd. rsync would have mirrored the volume onto the source under the
    target's path. Verification catches it after the fact; not writing to the
    wrong machine in the first place is better.

    The host is a LITERAL whenever we choose it (see :data:`LOOPBACK`), so the
    source never has to resolve a name we already know the address for. In direct
    mode it is whatever Coolify records as the server's address, which may be a
    hostname — but that path is only reachable BECAUSE the runner's probe already
    resolved and connected to it.
    """
    key = ctx.ephemeral_key
    identity = key.remote_path if key else None

    if ctx.tunnel_port is not None:
        return LOOPBACK, ctx.tunnel_port, identity

    return ctx.plan.target_server.ip, ctx.plan.target_server.port, identity


def _pairs_not_loaded(recorded: object, loaded: dict[str, list[VolumePair]]) -> list[str]:
    """Resource uuids DISCOVER paired volumes for, but this run holds none for. PURE.

    Bind-mount rows do not count: DISCOVER journals them beside the pairs, and
    they never become a :class:`VolumePair`.
    """
    if not isinstance(recorded, dict):
        return []
    return sorted(
        str(uuid)
        for uuid, rows in recorded.items()
        if isinstance(rows, list)
        and any(is_volume_row(row) for row in rows)
        and not loaded.get(str(uuid))
    )


def _assert_pairs_loaded(ctx: MigrationContext, *, step: str) -> None:
    """Refuse a COPY or VERIFY that would do nothing because the pairs went missing.

    Both steps iterate ``ctx.volume_pairs`` and take an empty one as "nothing
    to move" — correct for a stateless stack, and indistinguishable from a
    context that lost them. A resumed run did lose them until the runner learnt
    to rehydrate them from the journal: COPY copied nothing, VERIFY verified
    nothing, and the target started on empty volumes. The journal is the one
    witness that can tell the two apart, so ask it.
    """
    if State.DISCOVER.value not in ctx.journal.completed_states():
        return
    missing = _pairs_not_loaded(
        ctx.journal.undo_info(State.DISCOVER.value).get("volume_pairs"), ctx.volume_pairs
    )
    if not missing:
        return
    names = {r.snapshot.uuid: r.snapshot.name for r in ctx.plan.resources}
    raise TransferError(
        f"{step}: DISCOVER paired volumes for {', '.join(names.get(u, u) for u in missing)}, "
        "but this run holds no pairs for them",
        hint=(
            "Proceeding would copy or verify nothing and still report success. The "
            "journal recorded the pairs; this process did not load them - please "
            "report it.\n"
            "The source will be restarted and the target deleted."
        ),
    )


async def step_copy(ctx: MigrationContext) -> dict[str, Any]:
    """Mirror every volume, byte for byte, in parallel where it is safe."""
    _assert_pairs_loaded(ctx, step="copy")
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

    # Create every target volume, then pin them all, BEFORE the first byte: from
    # here until the target's own containers exist nothing references them, and
    # Coolify's `docker volume prune -af` deletes exactly that.
    targets = [
        pair.target.name
        for resource in ctx.plan.resources
        for pair in ctx.volume_pairs.get(resource.snapshot.uuid, [])
    ]
    for name in targets:
        await docker.create_volume(ctx.target_host, name)
    target_pins = await pins.ensure(
        ctx.target_host, migration_id=ctx.migration_id, side=pins.TARGET, volumes=targets
    )
    ctx.journal.append("step_started", state="copy", detail={"target_pins": target_pins})

    # What the source looks like BEFORE the transfer, to compare against
    # afterwards. Taking this at the end instead — which is what the first cut
    # did — compares the state to itself and can never disagree.
    before = await quiesce.snapshot(ctx.source_host, label_filters=observed_labels(ctx.plan))

    copied: list[str] = []
    for resource in ctx.plan.resources:
        for pair in ctx.volume_pairs.get(resource.snapshot.uuid, []):
            await _copy_one(ctx, pair.source_path, pair.target_path)
            copied.append(pair.target.name)

    # Nothing may have restarted mid-transfer. A `restart:` policy or a dashboard
    # deploy would invalidate the whole copy, and neither raises on its own.
    await quiesce.assert_still_stopped(
        ctx.source_host, label_filters=observed_labels(ctx.plan), since=before
    )
    return {
        "volumes_copied": copied,
        "key_fingerprint": ctx.ephemeral_key.fingerprint,
        "target_pins": target_pins,
    }


async def _copy_one(ctx: MigrationContext, source_path: str, target_path: str) -> None:
    host, port, identity = _transfer_endpoint(ctx)

    entries: list[PathEntry] = []
    listing = await ctx.source_host.run(
        f"cd {shlex.quote(source_path)} 2>/dev/null && ls -A 2>/dev/null"
    )
    if listing.ok:
        # splitlines(), not split(): `ls -A` writes one name per line when its
        # stdout is a pipe, and splitting on whitespace tears a top-level entry
        # with a space in its name into two names that do not exist -- which
        # rsync then rejects with exit 23, after the source is already stopped.
        for name in listing.stdout.splitlines():
            if not name.strip():
                continue
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

    # A split transfer copies the tree through --files-from, which never names
    # the volume ROOT itself — so the destination root keeps the 0:0 ownership
    # `docker volume create` gave it, while the source root is owned by the
    # container user (mysql/postgres = 999). Invisible in the data, but it stops
    # a database from starting, and verify rightly refuses it (covalida,
    # 2026-07-23: db-data, the only volume large enough to be chunked, failed
    # `metadata_differs . 999:999 != 0:0` while the single-stream volumes
    # passed). One non-recursive pass carries the root's own metadata across.
    if plan.is_split:
        await rsync.run(
            ctx.source_host,
            rsync.RsyncSpec(
                source_path=source_path,
                target_path=target_path,
                target_host=host,
                target_user=ctx.plan.target_server.user,
                target_port=port,
                identity_file=identity,
                dirs_only=True,
            ),
        )


async def step_verify(ctx: MigrationContext) -> dict[str, Any]:
    """Content AND metadata, both sides. A content-only check cannot see a chown."""
    _assert_pairs_loaded(ctx, step="verify")
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


async def _server_addresses(server: ServerRef) -> frozenset[str]:
    """A server's addresses for the DNS verdict: its ``ip`` and its wildcard base.

    See :func:`dns_resolve.server_addresses`. Coolify's ``ip`` is often a
    hostname, and the wildcard base is the public address custom records really
    point at.
    """
    return await dns_resolve.server_addresses(server.ip, server.wildcard_domain or None)


def _refuse_hosts_bound_to_source(
    ctx: MigrationContext, hostnames: list[dns_extract.Hostname]
) -> None:
    """Block a target that would serve a host under the SOURCE's wildcard.

    Not a cutover, so --accept-dns does not apply: the wildcard record binds the
    host to the source for good, and the target's ACME challenge can never pass.
    """
    bound = bound_to_source(
        hostnames,
        source_wildcard=ctx.plan.source_server.wildcard_domain or None,
        target_wildcard=ctx.plan.target_server.wildcard_domain or None,
    )
    if not bound:
        return
    raise DnsGateBlocked(
        "the target would serve hostname(s) bound to the SOURCE server's wildcard: "
        + ", ".join(h.host for h in bound),
        hint=(
            f"Their wildcard record points at {ctx.plan.source_server.name} and always "
            "will, so no DNS change moves them, and the target could never obtain a "
            "certificate for them. They were not rewritten onto the target's wildcard "
            "— check that it is configured (or pass --target-wildcard).\n"
            "Change the target's domain in Coolify to a host under the target's "
            "wildcard or a custom domain, then run `coolify-migrate resume <id>`. "
            "--accept-dns does not apply here."
        ),
    )


def _cloudflare_hosts(ctx: MigrationContext, baseline: dict[str, HostBaseline]) -> set[str]:
    """Hosts whose zone the baseline showed on Cloudflare, from Coolify 4.4.0.

    Wording only (dns/managed.py): their checklist line says where the record
    can be changed, since Coolify never re-points one it manages.
    """
    version = ctx.coolify_version
    return {
        host
        for host, entry in baseline.items()
        if dns_managed.coolify_may_manage(entry.nameservers, entry.cname_chain, version=version)
    }


async def step_dns_gate(ctx: MigrationContext) -> dict[str, Any]:
    """Refuse to start the target while a domain IT will serve is not ready for it.

    Reads the TARGET's domains, not the source's: what matters is what the
    TARGET will answer on when it starts — server-bound hosts rewritten onto its
    wildcard, custom hosts it claimed at CREATE.

    Per host (dns/gate.py): one still on the source needs a cutover
    (--accept-dns proceeds in parallel); one on neither server (CDN, proxy, load
    balancer — the origin may still be the source) and one that resolved at
    plan time but no longer does are accepted only one by one
    (--accept-dns-host HOST).
    """
    hostnames = []
    for resource in ctx.plan.resources:
        target_uuid = ctx.target_uuids[resource.snapshot.uuid]
        full = await ctx.api.get_resource(resource.snapshot.collection, target_uuid)
        envs = await ctx.api.get_envs(resource.snapshot.collection, target_uuid)
        if resource.snapshot.collection == "services":
            # What its proxy is configured from: sub-application domains, the
            # route keys of a minio/logto/garage image, a custom compose's own
            # Host() labels — not the derived SERVICE_* rows (dns/extract.py).
            compose = full.get("docker_compose_raw")
            hostnames.extend(
                dns_extract.collect_service(
                    applications=full.get("applications"),
                    envs=envs,
                    compose=compose
                    if resource.snapshot.kind is ResourceKind.SERVICE_COMPOSE
                    and isinstance(compose, str)
                    else None,
                )
            )
            continue
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
    _refuse_hosts_bound_to_source(ctx, real)

    source_ips = await _server_addresses(ctx.plan.source_server)
    target_ips = await _server_addresses(ctx.plan.target_server)

    # A target-wildcard URL already resolves to the target (READY); a custom domain
    # still resolves to the source (CUTOVER_NEEDED). Keep the source-wildcard skip
    # so anything still under the SOURCE wildcard is not resolved into a false
    # cutover (it moved to the target's wildcard).
    src_wildcard = ctx.plan.source_server.wildcard_domain or None
    custom = [h for h in real if not dns_wildcard.under_wildcard(h.host, src_wildcard)]
    bound = [h for h in real if dns_wildcard.under_wildcard(h.host, src_wildcard)]

    baseline = dns_baseline_of(ctx.journal, ctx.plan)
    resolutions = await dns_resolve.resolve_all(custom)
    resolutions.extend(Resolution(hostname=h, addresses=()) for h in bound)
    report = build_report(
        resolutions,
        source_ips=source_ips,
        target_ips=target_ips,
        source_wildcard=src_wildcard,
        baselines={host: entry.addresses for host, entry in baseline.items()},
    )
    ctx.dns_report = report

    unanswered = report.unaccepted(
        accept_cutover=ctx.accept_dns, accepted_hosts=ctx.accepted_hosts
    )
    if unanswered:
        # Refuse by default, but resumably: fix DNS (or accept) and continue.
        raise DnsGateBlocked(
            "DNS is not ready for: "
            + ", ".join(f"{v.hostname.host} ({v.verdict.value})" for v in unanswered)
            + "\n\n"
            + explain_why_blocking_matters(),
            hint="Before starting the target:\n  "
            + "\n  ".join(
                report.cutover_checklist(
                    unanswered, cloudflare_hosts=_cloudflare_hosts(ctx, baseline)
                )
            ),
            report=report,
        )
    if report.blocked:
        # The operator answered every blocking host: a cutover to run in parallel
        # (DNS propagation lags), or a host they vouched for one by one.
        log.warning(
            "dns_gate.proceeding_on_acceptance",
            hosts=[v.hostname.host for v in report.blocked],
            hint="cut the record(s) over to the target / check the accepted origins",
        )

    blocked = [v.hostname.host for v in report.blocked]
    return {
        "hostnames": len(real),
        "server_bound": [v.hostname.host for v in report.server_bound],
        "ready": [v.hostname.host for v in report.ready],
        "cutover_accepted": [
            v.hostname.host for v in report.blocked if v.verdict is Verdict.CUTOVER_NEEDED
        ],
        "accepted": sorted(set(blocked) & ctx.accepted_hosts),
        "ambiguous": [v.hostname.host for v in report.ambiguous],
        "vanished": [v.hostname.host for v in report.vanished],
        "unanswered": [v.hostname.host for v in report.unanswered],
    }


async def step_start_target(ctx: MigrationContext) -> dict[str, Any]:
    started = []
    for resource in ctx.plan.resources:
        target_uuid = ctx.target_uuids[resource.snapshot.uuid]
        await ctx.api.start(resource.snapshot.collection, target_uuid)
        started.append(target_uuid)
    return {"started": started}


#: Container states a deploy passes THROUGH. Seeing one means "not settled yet",
#: never "unhealthy" — so the gate keeps waiting rather than judging.
#:
#: ``gone`` is in here for the same reason it is a *stopped* state in QUIESCE,
#: read the other way round: on the target, a container that vanished between the
#: ``docker ps`` and the ``docker inspect`` is one Coolify is replacing, not one
#: that failed.
_SETTLING_STATES = frozenset({"created", "restarting", "removing", "paused", docker.STATE_GONE})

#: How often to re-ask the target's daemon during the health gate. Named, like
#: ``_STOP_RETRY_INTERVAL``, rather than buried in the loop.
_HEALTH_POLL_INTERVAL = 3.0


@dataclass(frozen=True, slots=True)
class _ResourceHealth:
    """What the target's daemon says about one migrated resource."""

    resource: str
    labels: dict[str, str]
    containers: tuple[docker.Container, ...]

    @property
    def running(self) -> tuple[docker.Container, ...]:
        return tuple(c for c in self.containers if c.state == "running")

    @property
    def settling(self) -> tuple[docker.Container, ...]:
        return tuple(c for c in self.containers if c.state in _SETTLING_STATES)

    @property
    def finished(self) -> tuple[docker.Container, ...]:
        """Ran and exited CLEANLY — a one-shot service that has done its job."""
        return tuple(
            c
            for c in self.containers
            if c.state not in _SETTLING_STATES and c.state != "running" and c.exit_code == 0
        )

    @property
    def failed(self) -> tuple[docker.Container, ...]:
        return tuple(
            c
            for c in self.containers
            if c.state not in _SETTLING_STATES and c.state != "running" and c.exit_code != 0
        )

    @property
    def is_up(self) -> bool:
        """At least one container serving, none broken, none still moving.

        Deliberately NOT "every container is running" — see :func:`step_healthcheck`.
        """
        return bool(self.running) and not self.settling and not self.failed

    def describe(self) -> str:
        if not self.containers:
            return f"  {self.resource}: nothing matched `{_describe_filter(self.labels)}`"
        parts = [
            f"{c.name} ({c.state}"
            + (
                f", exit {c.exit_code}"
                if c.state not in _SETTLING_STATES and c.state != "running"
                else ""
            )
            + ")"
            for c in sorted(self.containers, key=lambda c: c.name)
        ]
        return f"  {self.resource}: " + ", ".join(parts)


def _describe_filter(labels: dict[str, str]) -> str:
    """The exact ``docker ps`` we ran, so an operator can re-run it. PURE."""
    return "docker ps -a " + " ".join(
        f"--filter label={key}={value}" for key, value in sorted(labels.items())
    )


async def _target_health(ctx: MigrationContext, resource: ResourcePlan) -> _ResourceHealth:
    """One resource's containers on the target, with exit codes resolved.

    Uses :func:`quiesce.snapshot` rather than a bare ``docker ps`` because
    ``docker ps`` does not report exit codes, and telling a service that finished
    from one that crashed is the entire question here. It inspects only the
    non-running containers, so an ordinary stack costs exactly one round-trip per
    poll and a stack with one-shot services costs one more per one-shot.
    """
    labels = resource_labels(
        project=ctx.plan.project,
        environment=ctx.plan.environment,
        name=resource.snapshot.name,
    )
    report = await quiesce.snapshot(ctx.target_host, label_filters=labels)
    return _ResourceHealth(
        resource=resource.snapshot.name, labels=labels, containers=report.containers
    )


async def _containers_by_uuid(ctx: MigrationContext, resource: ResourcePlan) -> list[str]:
    """Target containers carrying this resource's uuid, whatever their labels claim.

    The question PREFLIGHT asks of the source, asked of the target: is "no
    containers" a fact about the deploy, or a fact about our filter? Here we know
    the uuid we just created, and :func:`~bg_coolify_migrate.discovery.docker.carries_uuid`
    finds it in the container name, the built image tag or a label VALUE — none of
    which a stale name label can hide.

    Diagnostics only, and only on the failure path: it never decides the gate, it
    only makes the refusal say which of the two happened.
    """
    target_uuid = ctx.target_uuids.get(resource.snapshot.uuid)
    if not target_uuid:
        return []
    try:
        managed = await docker.managed_containers(ctx.target_host)
    except Exception as exc:  # a diagnostic must never mask the real failure
        log.debug("healthcheck.uuid_probe_failed", error=str(exc)[:120])
        return []
    return sorted(f"{c.name} ({c.state})" for c in managed if docker.carries_uuid(c, target_uuid))


async def _healthcheck_failure(
    ctx: MigrationContext, health: list[_ResourceHealth], *, deadline: float
) -> TransferError:
    """Build the refusal, naming what the daemon actually showed us.

    Separate from the gate because the message is the whole value of a timeout:
    the previous one said only "not running: <no containers appeared>", which is
    the same sentence for a stack that failed to deploy and for a filter that
    cannot see it.
    """
    unhealthy = [h for h in health if not h.is_up]
    blind = {h.resource for h in unhealthy if not h.containers}

    probes: list[str] = []
    for resource in ctx.plan.resources:
        if resource.snapshot.name not in blind:
            continue
        found = await _containers_by_uuid(ctx, resource)
        if found:
            probes.append(f"  {resource.snapshot.name}: {', '.join(found)}")

    hint = "Check the deployment logs in Coolify. The source is still intact."
    if probes:
        hint = (
            "Containers for this resource ARE on the target — they carry its uuid — but "
            "the Coolify name labels we filter on do not match:\n"
            + "\n".join(probes)
            + "\n\nThat is what a project renamed after a deploy looks like.\n"
            + hint
        )
    return TransferError(
        f"target did not become healthy within {deadline:.0f}s:\n"
        + "\n".join(h.describe() for h in unhealthy),
        hint=hint,
    )


async def step_healthcheck(ctx: MigrationContext) -> dict[str, Any]:
    """Wait for the target's containers to come up.

    A deploy is asynchronous, so "start returned" means nothing. We poll the
    target's daemon for the same reason we poll the source's. The window is
    deploy_timeout, not stop_timeout: a git-built app clones and builds here first.

    Two things this must NOT demand. It demanded both until 2026-08-27, and each
    on its own makes a stack that is serving traffic sit out the entire window:

    * **Not "every container is running".** ``docker ps -a`` lists what a compose
      stack FINISHED with as well as what it runs — a migration step, a
      permission fixer, anything one-shot — and those stay listed at ``exited
      (0)`` forever. Requiring ``running == total`` therefore makes such a stack
      permanently unhealthy, and the source can never contradict it, because the
      source gate wants the opposite: over there an exited container IS the
      success condition. The test is now "at least one container serving, none
      broken, none still moving", where broken means a non-zero exit, ``dead``,
      or a restart loop — each of which still holds the gate shut.
    * **Not the whole project.** The filter was the plan's — project plus
      environment for an unscoped run — which on the TARGET also matches
      resources that were never part of this migration and are legitimately
      stopped there. Every other gate (PREFLIGHT, the pre-stop mount capture)
      asks per resource with all three name labels; this one was the exception
      and is no longer.
    """
    deadline = ctx.settings.deploy_timeout
    waited = 0.0

    while True:
        health = [await _target_health(ctx, resource) for resource in ctx.plan.resources]
        if all(h.is_up for h in health):
            return {
                "containers": sum(len(h.running) for h in health),
                "finished": sum(len(h.finished) for h in health),
                "waited": round(waited, 1),
            }
        if waited >= deadline:
            raise await _healthcheck_failure(ctx, health, deadline=deadline)
        await asyncio.sleep(_HEALTH_POLL_INTERVAL)
        waited += _HEALTH_POLL_INTERVAL


async def step_finalize(ctx: MigrationContext) -> dict[str, Any]:
    """Apply the finalize policy to the SOURCE. The only irreversible step.

    Releasing the source's custom hosts is its LAST source mutation — after the
    rename, the unpin and the key revoke — and journalled before it happens. It
    is the "break" of make-before-break (domain/domains.py): a source lets go
    only once its target is read to hold every one of those hosts right then
    (:func:`_target_still_holds` — CREATE proved it once, a deploy since can
    undo it), so Coolify only ever re-references a managed DNS record and never
    deletes one. KEEP releases them too: a kept source still holding them is a
    second router and ACME requester the moment anyone restarts it. DELETE
    releases by deleting.
    """
    policy = ctx.plan.finalize_policy
    stamp = ctx.migration_id.split("-")[-1]
    actions: list[str] = []
    # On disk before anything is touched: after a DELETE has begun, no rollback
    # may run, even if this step dies part-way (statemachine.rollback_refusal).
    ctx.journal.append("step_started", state="finalize", detail={"policy": policy.value})

    # Unpinning hands each volume back to Coolify's steady state, and when that
    # happens depends on the side and the policy - see _unpin_target and
    # _unpin_source. Only the DELETE unpin may fail the step.
    target = await _unpin_target(ctx)
    released = list(target.released)
    left = list(target.kept)
    pinned: set[str] = set()
    # A part-way DELETE cannot be rolled back, only finished: what an earlier
    # attempt deleted is on disk, and a resume skips it.
    deleted: list[str] = list(ctx.journal.undo_info(State.FINALIZE.value).get("deleted") or [])
    if policy is FinalizePolicy.DELETE:
        # BEFORE the delete, and fatal: Coolify's `docker volume rm -f` cannot
        # remove a volume a pin references and swallows the refusal for apps and
        # databases (v4.5.0:app/Models/Application.php:575,
        # StandalonePostgresql.php:180), so the "deleted" source would keep its
        # data forever. Nothing irreversible has happened yet. Releasing OUR
        # pins is not enough: another migration's pin does the same.
        pinned = await _refuse_other_references(ctx)
        released += await pins.release(
            ctx.source_host, migration_id=ctx.migration_id, side=pins.SOURCE
        )

    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        if policy is FinalizePolicy.KEEP:
            actions.append(f"kept {snapshot.name}")
            continue

        if policy is FinalizePolicy.RENAME:
            new_name = f"{snapshot.name}-old-{stamp}"
            await api_resources.rename(ctx.api, snapshot.collection, snapshot.uuid, new_name)
            actions.append(f"renamed {snapshot.name} -> {new_name}")
            continue

        if snapshot.uuid not in deleted:
            await _delete_source(ctx, snapshot, pinned, deleted=deleted)
        actions.append(f"deleted {snapshot.name}")

    if policy is not FinalizePolicy.DELETE:
        source = await _unpin_source(ctx)
        released += source.released
        left += source.kept

    await keys.revoke(source=ctx.source_host, target=ctx.target_host, migration_id=ctx.migration_id)

    domains_released: list[str] = []
    if policy is not FinalizePolicy.DELETE:
        domains_released = await _release_source_domains(ctx)
    return {
        "policy": policy.value,
        "actions": actions,
        "original_names": [r.snapshot.name for r in ctx.plan.resources],
        "pins_released": released,
        "pins_left": left,
        "domains_released": domains_released,
    }


async def _release_source_domains(ctx: MigrationContext) -> list[str]:
    """Let the source go of its custom hosts. Never raises; reports what it could not.

    Journalled first: a crash mid-release leaves the record that the source may
    have let go, and RESTORE_SOURCE_FQDN reads the source anyway, so a rollback
    gives back exactly what is missing. A failed release leaves the source
    holding a host the target serves — stopped, so harmless until someone
    restarts it — and says so in the run report.
    """
    owners = [
        r.snapshot.uuid
        for r in ctx.plan.resources
        if r.snapshot.collection in ("applications", "services")
    ]
    if not owners:
        return []
    ctx.journal.append("step_started", state="finalize", detail={"releasing_domains": owners})
    source_wildcard = ctx.plan.source_server.wildcard_domain or None
    released: list[str] = []
    for resource in ctx.plan.resources:
        snapshot = resource.snapshot
        if snapshot.uuid not in owners:
            continue
        if not await _target_still_holds(ctx, snapshot, source_wildcard=source_wildcard):
            continue
        if await api_resources.release_source_domains(
            ctx.api, snapshot, source_wildcard=source_wildcard
        ):
            released.append(snapshot.uuid)
            continue
        ctx.notices.append(
            f"{snapshot.name} (the stopped source) still holds its custom domains, which "
            "the target now serves: remove them from the source in Coolify before anyone "
            "restarts it, or it requests certificates for them next to the target."
        )
    return released


async def _target_still_holds(
    ctx: MigrationContext, snapshot: ResourceSnapshot, *, source_wildcard: str | None
) -> bool:
    """Does the target still hold every custom host its source would release?

    CREATE proved it once, and a deploy can undo that: every deploy of a
    dockercompose app reloads its compose from the branch HEAD and drops the
    docker_compose_domains entry of a service the new compose no longer has
    (Application::reconcileDockerComposeDomains, v4.3.10:2219, v4.5.0:2409-2431)
    — a rename pushed while the run sat at the DNS gate is enough. HEALTHCHECK
    sees containers, not domains. Releasing then removes the LAST holder, and
    from 4.4 Coolify deletes a DNS record it manages. So the source keeps its
    hosts — stopped, harmless until someone restarts it — and the run says so.
    A target or source that cannot be read keeps them too.
    """
    target_uuid = ctx.target_uuids.get(snapshot.uuid)
    try:
        if not target_uuid:
            raise LookupError("no target was recorded for it")
        source = await ctx.api.get_resource(snapshot.collection, snapshot.uuid)
        target = await ctx.api.get_resource(snapshot.collection, target_uuid)
    except Exception as exc:
        reason = (str(exc).splitlines() or [type(exc).__name__])[0]
        log.warning("finalize.target_hosts_unreadable", uuid=snapshot.uuid, error=reason[:200])
        ctx.notices.append(
            f"{snapshot.name} (the stopped source) still holds its custom domains: whether "
            f"its target holds them could not be read ({reason[:120]}), "
            "and they are released only to a target that does. Check the target's domains "
            "in Coolify, then remove them from the source before anyone restarts it."
        )
        return False
    lost = domains.custom_hosts_not_held(
        snapshot, source, target, source_wildcard=source_wildcard
    )
    if not lost:
        return True
    log.error("finalize.target_lost_hosts", uuid=snapshot.uuid, target=target_uuid, hosts=lost)
    ctx.notices.append(
        f"{snapshot.name} (the stopped source) still holds its custom domains: its target "
        f"{target_uuid} no longer holds {', '.join(lost)} - a deploy reloads a compose from "
        "git and drops the domain of a service renamed or removed there since CREATE - so "
        "releasing them would leave them held by nothing (from Coolify 4.4 that deletes a "
        "DNS record Coolify manages). Give the target its domains back in Coolify, then "
        "remove them from the source before anyone restarts it."
    )
    return False


async def _refuse_other_references(ctx: MigrationContext) -> set[str]:
    """Refuse the delete while anything but our own pins mounts a source volume.

    Coolify's delete cannot remove a referenced volume and does not say so
    (apps and databases swallow the ``docker volume rm -f`` refusal; from 4.5
    DeleteResourceJob turns a service's into a team notification and deletes
    the resource anyway), so the run would report "deleted" while the data
    stays. The usual holder is a pin another migration of this source kept -
    its rollback could not see the source come back - and only our own pins
    are released below. Checked BEFORE they go, so a refusal leaves the source
    exactly as protected as it was; nothing has been deleted.

    Returns the volumes our pins hold.
    """
    held = await pins.list_pins(ctx.source_host, migration_id=ctx.migration_id, side=pins.SOURCE)
    pinned = {v for pin in held for v in pin.volumes}
    volumes = pinned | {
        i.source_name for r in ctx.plan.resources for i in r.manifest.to_migrate if i.source_name
    }
    if not volumes:
        return pinned
    holders = sorted(
        c.name
        for c in await docker.containers_mounting(ctx.source_host, volumes=sorted(volumes))
        if not pins.is_pin(c, migration_id=ctx.migration_id, side=pins.SOURCE)
    )
    if not holders:
        return pinned
    server = ctx.plan.source_server.name
    raise MigrationError(
        f"refusing to delete the source: {', '.join(holders)} still mount(s) its volumes "
        f"on {server}",
        hint=(
            "Coolify's delete cannot remove a volume a container references and does not "
            "say so - the run would report the source deleted while its data stays. A "
            "container named bg-coolify-migrate-pin-<id>-... is a volume pin another "
            "migration kept: it never ran and holds no data.\n"
            f"Remove what holds them on {server} (`docker rm <name>` for a pin), then "
            f"`coolify-migrate resume {ctx.migration_id}`. Nothing has been deleted, and "
            "this run's own pins are still in place."
        ),
    )


async def _delete_source(
    ctx: MigrationContext, snapshot: ResourceSnapshot, pinned: set[str], *, deleted: list[str]
) -> None:
    """Delete one source and journal it the moment Coolify has accepted the delete.

    No rollback may run once a DELETE finalize began, so a failure part-way is
    finished by a resume - and a resume must neither delete a source twice nor
    trip over one already gone. A 404 is "already gone": the client retries a
    DELETE that timed out, and the retry of one that landed finds nothing (the
    models soft-delete), as does a source someone deleted by hand meanwhile.
    """
    try:
        await ctx.api.delete_resource(snapshot.collection, snapshot.uuid, delete_volumes=True)
    except CoolifyApiError as exc:
        if exc.status_code != 404:
            await _repin_undeleted(ctx, pinned, deleted=deleted, error=exc)
            raise
        log.info("finalize.source_already_gone", uuid=snapshot.uuid)
    except Exception as exc:
        await _repin_undeleted(ctx, pinned, deleted=deleted, error=exc)
        raise
    deleted.append(snapshot.uuid)
    ctx.journal.append(
        "step_started", state=State.FINALIZE.value, detail={"deleted": list(deleted)}
    )


def _source_volumes_of(ctx: MigrationContext, resource: ResourcePlan) -> set[str]:
    """Every volume this run knows a source resource by: mounted, paired, planned. PURE."""
    uuid = resource.snapshot.uuid
    return (
        {m.name for m in ctx.pre_stop_mounts.get(uuid, []) if m.type == "volume" and m.name}
        | {p.source.name for p in ctx.volume_pairs.get(uuid, [])}
        | {i.source_name for i in resource.manifest.to_migrate if i.source_name}
    )


async def _repin_undeleted(
    ctx: MigrationContext, pinned: set[str], *, deleted: list[str], error: Exception
) -> None:
    """Pin again what a failed DELETE loop left undeleted. Never raises.

    The release before the loop unpinned every source, and a delete that fails
    for resource k leaves k..n stopped with nothing referencing their volumes -
    exactly what Coolify's ``docker volume prune -af`` deletes - while no
    rollback may run (statemachine.rollback_refusal). So every volume our pins
    held goes back under a pin, except those known to belong only to a resource
    whose delete Coolify accepted: its DeleteResourceJob runs asynchronously,
    and a pin would make that delete silently keep the data. The operator hears
    which pin, because it must go before anyone deletes a source by hand.
    """
    gone: set[str] = set()
    remaining: set[str] = set()
    for resource in ctx.plan.resources:
        bucket = gone if resource.snapshot.uuid in deleted else remaining
        bucket |= _source_volumes_of(ctx, resource)
    volumes = sorted(pinned - (gone - remaining))
    if not volumes:
        return
    server = ctx.plan.source_server.name
    try:
        names = await pins.ensure(
            ctx.source_host, migration_id=ctx.migration_id, side=pins.SOURCE, volumes=volumes
        )
    except Exception as exc:  # the delete's own error is the one to raise
        log.error("finalize.repin_failed", error=str(exc)[:200])
        note = (
            f"The sources not yet deleted could NOT be pinned again on {server} ({_why(exc)}): "
            "on a server with 'Delete unused volumes' on, Coolify's next cleanup can delete "
            "their volumes before you finish."
        )
    else:
        log.warning("finalize.repinned", pins=names, volumes=volumes)
        note = (
            f"The sources not yet deleted are pinned again by {', '.join(names)} on {server}. "
            "Resuming releases the pins before it deletes; to delete a source by hand "
            "instead, `docker rm` them first, or Coolify's delete keeps its volumes."
        )
    if isinstance(error, MigrationError):
        error.hint = f"{error.hint}\n\n{note}" if error.hint else note


def _why(exc: Exception) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:160]


async def _unpin_target(ctx: MigrationContext) -> pins.Release:
    """Release the target's pins - but only for volumes its containers now mount.

    HEALTHCHECK proves each resource has a container serving; it never asks
    WHICH volumes are mounted. A paired volume no target container mounts would
    be unreferenced the moment its pin goes, and under DELETE it is the only
    copy left. So it keeps its pin, and the operator hears which volume.

    Never fails the step: rolling back a healthy target over a ``docker rm``
    would trade a stray container for an outage.
    """
    try:
        outcome = await pins.release_when_referenced(
            ctx.target_host, migration_id=ctx.migration_id, side=pins.TARGET, timeout=0.0
        )
    except Exception as exc:  # best-effort: never roll back a healthy target over this
        log.warning("finalize.target_pins_left", error=str(exc)[:200])
        ctx.notices.append(_pins_left_notice(ctx, ctx.plan.target_server.name, error=exc))
        return pins.Release()
    if outcome.kept:
        ctx.notices.append(
            f"{', '.join(outcome.kept)} still pins target volume(s) "
            f"{', '.join(outcome.unmounted)} on {ctx.plan.target_server.name}: no running "
            "target container mounts them, so without the pin Coolify's Docker cleanup could "
            "delete the copied data. Check the resource's storages; once something mounts them: "
            f"coolify-migrate unpin {ctx.migration_id} "
            f"(or docker rm {' '.join(outcome.kept)} there)."
        )
    return outcome


async def _unpin_source(ctx: MigrationContext) -> pins.Release:
    """Release the kept or renamed source's pins - after the rename, never fatally.

    The source now stays stopped with no containers, so on a server with
    "Delete unused volumes" on, Coolify's next cleanup deletes its volumes: a
    kept source is no fallback copy there. Deliberately so - a pin kept for
    good would make a later delete of the renamed source silently keep its data
    (``docker volume rm -f`` refuses, Coolify swallows it). A raise here would
    roll back a healthy target and leave the source renamed: RESTORE_SOURCE_NAME
    is owed only on a completed FINALIZE.
    """
    try:
        released = await pins.release(
            ctx.source_host, migration_id=ctx.migration_id, side=pins.SOURCE
        )
    except Exception as exc:  # best-effort: see the docstring
        log.warning("finalize.source_pins_left", error=str(exc)[:200])
        ctx.notices.append(_pins_left_notice(ctx, ctx.plan.source_server.name, error=exc))
        return pins.Release()
    return pins.Release(released=tuple(released))


def _pins_left_notice(ctx: MigrationContext, server: str, *, error: Exception) -> str:
    return (
        f"could not remove this migration's volume pins on {server} "
        f"({str(error).splitlines()[0][:120]}). Find them with "
        f"`docker ps -a --filter label={pins.LABEL_MIGRATION}={ctx.migration_id}` and "
        "`docker rm` each."
    )


def build_steps() -> dict[Any, Any]:
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
