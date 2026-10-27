"""High-level orchestration: open the connections, run the saga, close them.

The one place that knows how to assemble everything. Deliberately thin — it wires
and it does not decide.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import structlog

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.discovery import pins
from bg_coolify_migrate.dns import resolve as dns_resolve
from bg_coolify_migrate.dns.gate import is_definitive
from bg_coolify_migrate.domain.domains import dns_rollback_note, parking_tag
from bg_coolify_migrate.domain.plan import MigrationPlan, ServerRef, TransferMode
from bg_coolify_migrate.domain.statemachine import Outcome, State
from bg_coolify_migrate.engine.compensations import (
    build_compensations,
    settle_window,
    source_data_refusal,
)
from bg_coolify_migrate.engine.context import (
    MigrationContext,
    deserialise_mounts,
    deserialise_volume_pairs,
    dns_baseline_of,
)
from bg_coolify_migrate.engine.executor import RunResult, Saga
from bg_coolify_migrate.engine.steps import build_steps
from bg_coolify_migrate.errors import MigrationError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer import ssh
from bg_coolify_migrate.transfer.ssh import HostKeyPrompt, RemoteHost, SshTarget

log = structlog.get_logger(__name__)


def plan_path(state_dir: Path, migration_id: str) -> Path:
    return state_dir / f"{migration_id}.plan.json"


def save_plan(state_dir: Path, migration_id: str, plan: MigrationPlan) -> Path:
    """Persist the plan next to its journal.

    `resume` and `rollback` need it: they must know which resources exist and
    where they came from, and re-planning would ask a stopped stack questions it
    can no longer answer (a stopped container still lists, but the world may have
    moved). The plan is a fact about the decision we made; the journal is a fact
    about what we did.

    Safe to store: a plan carries uuids, names, paths and sizes — never secrets.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = plan_path(state_dir, migration_id)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8", newline="\n")
    return path


def load_plan(state_dir: Path, migration_id: str) -> MigrationPlan:
    """Load a persisted plan.

    Raises:
        MigrationError: If absent — better than silently re-planning against a
            world that has since changed.
    """
    path = plan_path(state_dir, migration_id)
    if not path.exists():
        raise MigrationError(
            f"no saved plan for migration {migration_id!r}",
            hint=(
                f"Expected {path}. Without it we cannot know what the run was doing; "
                "re-planning could disagree with what was actually created."
            ),
        )
    return MigrationPlan.model_validate_json(path.read_text(encoding="utf-8"))


def make_migration_id(project: str, environment: str, when: datetime | None = None) -> str:
    """A short, stable, human-typeable id.

    Includes a timestamp so two migrations of the same project are distinct, and
    a hash so the id survives a project name with spaces or slashes. The hash is
    the domain-parking tag (:func:`domains.parking_tag`), one helper for both.
    """
    stamp = (when or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{parking_tag(project, environment)}"


async def ssh_target_for(api: CoolifyClient, server: ServerRef) -> SshTarget:
    """Build an SSH target, reusing Coolify's own key for that server.

    With a root token `GET /security/keys` returns `private_key`, so we can reach
    the servers exactly as Coolify does rather than asking the operator to
    provision separate access.
    """
    full = await api.get_server(server.uuid)
    private_key: str | None = None

    key_id = full.get("private_key_id")
    if key_id is not None:
        for key in await api.get("/security/keys") or []:
            if not isinstance(key, dict):
                continue
            if key.get("id") == key_id and key.get("private_key"):
                private_key = str(key["private_key"])
                break

    proxy_command = None
    settings = full.get("settings") or {}
    if isinstance(settings, dict) and settings.get("is_cloudflare_tunnel"):
        proxy_command = "cloudflared access ssh --hostname %h"

    return SshTarget(
        host=server.ip,
        user=server.user,
        port=server.port,
        private_key=private_key,
        proxy_command=proxy_command,
    )


@asynccontextmanager
async def open_hosts(
    api: CoolifyClient,
    settings: Settings,
    source: ServerRef,
    target: ServerRef,
    *,
    trust_host_key: bool = False,
    host_key_prompt: HostKeyPrompt | None = None,
) -> AsyncIterator[tuple[RemoteHost, RemoteHost]]:
    """Open both SSH connections, closing them whatever happens."""
    source_target = await ssh_target_for(api, source)
    target_target = await ssh_target_for(api, target)

    async with RemoteHost.connect(
        source_target,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=trust_host_key or settings.trust_host_key,
        host_key_prompt=host_key_prompt,
        connect_timeout=settings.ssh_timeout,
    ) as source_host, RemoteHost.connect(
        target_target,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=trust_host_key or settings.trust_host_key,
        host_key_prompt=host_key_prompt,
        connect_timeout=settings.ssh_timeout,
    ) as target_host:
        yield source_host, target_host


@asynccontextmanager
async def maybe_tunnel(
    ctx: MigrationContext,
) -> AsyncIterator[None]:
    """Open a reverse forward when the transfer will need one.

    The workstation relays TCP only — no byte lands on its disk, so ownership,
    symlinks and xattrs are untouched.
    """
    if ctx.plan.transfer_mode is TransferMode.DIRECT:
        yield
        return

    needs_tunnel = ctx.plan.transfer_mode is TransferMode.TUNNEL
    if not needs_tunnel:
        # `is not True` on purpose: can_reach returns None when the source has no
        # way to answer, and an unknown must fall to the tunnel — it works either
        # way, where a wrong "direct" does not.
        reachable = await ssh.can_reach(
            ctx.source_host, ctx.plan.target_server.ip, ctx.plan.target_server.port
        )
        needs_tunnel = reachable is not True
        log.info(
            "transfer.mode",
            mode="tunnel" if needs_tunnel else "direct",
            reason={
                True: "source can reach target",
                False: "source cannot reach target directly",
                None: "source could not be probed (no bash, no nc)",
            }[reachable],
        )

    if not needs_tunnel:
        yield
        return

    async with ctx.source_host.forward_to(
        ctx.plan.target_server.ip, ctx.plan.target_server.port
    ) as port:
        ctx.tunnel_port = port
        yield


async def _execute(
    ctx: MigrationContext,
    *,
    start_from: State,
    on_state: Callable[[State], Awaitable[None]] | None,
) -> RunResult:
    async with maybe_tunnel(ctx):
        saga = Saga(
            journal=ctx.journal,
            context=ctx,
            steps=build_steps(),
            compensations=build_compensations(),
            guard=source_data_refusal,
            on_state=on_state,
        )
        result = await saga.run(start_from=start_from)
        result.notices.extend(ctx.notices)
        return await _with_rollback_notes(result, ctx)


async def _with_rollback_notes(result: RunResult, ctx: MigrationContext) -> RunResult:
    """Add what a rollback leaves for the operator to do by hand: DNS to flip back.

    Read from the JOURNAL — so an explicit rollback after a crash says it too —
    and from DNS itself, re-resolved now: a host someone already pointed at the
    target, or one whose record vanished since the plan, is in no journal.
    """
    if result.outcome not in (Outcome.ROLLED_BACK, Outcome.ROLLBACK_FAILED):
        return result
    gate = ctx.journal.undo_info(State.DNS_GATE.value)
    now_on_target, vanished = await _dns_after_rollback(ctx, gate)
    note = dns_rollback_note(
        gate,
        source_ip=ctx.plan.source_server.ip,
        source_wildcard=ctx.plan.source_server.wildcard_domain or None,
        target_wildcard=ctx.plan.target_server.wildcard_domain or None,
        now_on_target=now_on_target,
        vanished=vanished,
    )
    if note:
        result.notes.append(note)
    return result


async def _dns_after_rollback(
    ctx: MigrationContext, gate: dict[str, object]
) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """Re-resolve the custom hosts at the end of a rollback.

    Returns the hosts that resolve to the TARGET now, and those that resolved at
    plan time but definitively resolve to nothing now (with their plan-time
    addresses). Best-effort: DNS that cannot be asked adds no note, never a
    failure — the rollback itself is done.
    """
    baseline = dns_baseline_of(ctx.journal, ctx.plan)
    hosts = set(baseline)
    for key in ("ready", "cutover_accepted", "accepted"):
        listed = gate.get(key)
        if isinstance(listed, list):
            hosts.update(h for h in listed if isinstance(h, str) and h)
    if not hosts:
        return [], {}
    try:
        target_ips = await dns_resolve.server_addresses(
            ctx.plan.target_server.ip, ctx.plan.target_server.wildcard_domain or None
        )
        source_ips = await dns_resolve.server_addresses(
            ctx.plan.source_server.ip, ctx.plan.source_server.wildcard_domain or None
        )
        now = await dns_resolve.resolve_hosts(sorted(hosts))
    except Exception as exc:  # the rollback is done; this only adds advice
        log.warning("rollback.dns_recheck_failed", error=str(exc)[:120])
        return [], {}
    # Only what the TARGET alone answers as: an address both servers share (one
    # wildcard base, one CDN) is no reason to tell anyone to flip a record back.
    target_only = target_ips - source_ips
    on_target = sorted(h for h, r in now.items() if set(r.addresses) & target_only)
    vanished = {
        h: baseline[h].addresses
        for h, r in now.items()
        if h in baseline and baseline[h].addresses and not r.addresses and is_definitive(r)
    }
    return on_target, vanished


async def run_migration(
    api: CoolifyClient,
    settings: Settings,
    plan: MigrationPlan,
    *,
    migration_id: str | None = None,
    accept_drift: bool = False,
    accept_dns: bool = False,
    accept_hosts: frozenset[str] = frozenset(),
    delete_previews: bool = False,
    trust_host_key: bool = False,
    host_key_prompt: HostKeyPrompt | None = None,
    on_state: Callable[[State], Awaitable[None]] | None = None,
) -> RunResult:
    """Execute a migration end to end."""
    mid = migration_id or make_migration_id(plan.project, plan.environment)
    state_dir = settings.resolved_state_dir()
    journal = Journal.create(state_dir, mid)
    # Before anything else: resume and rollback are useless without it.
    save_plan(state_dir, mid, plan)

    async with open_hosts(
        api,
        settings,
        plan.source_server,
        plan.target_server,
        trust_host_key=trust_host_key,
        host_key_prompt=host_key_prompt,
    ) as (
        source_host,
        target_host,
    ):
        ctx = MigrationContext(
            api=api,
            settings=settings,
            plan=plan,
            journal=journal,
            migration_id=mid,
            source_host=source_host,
            target_host=target_host,
            accept_drift=accept_drift,
            accept_dns=accept_dns,
            accepted_hosts=frozenset(accept_hosts),
            delete_previews=delete_previews,
        )
        result = await _execute(ctx, start_from=State.INIT, on_state=on_state)
        log.info("migration.finished", id=mid, outcome=result.outcome.value)
        return result


async def resume_migration(
    api: CoolifyClient,
    settings: Settings,
    plan: MigrationPlan,
    migration_id: str,
    *,
    accept_drift: bool = False,
    accept_dns: bool = False,
    accept_hosts: frozenset[str] = frozenset(),
    trust_host_key: bool = False,
    host_key_prompt: HostKeyPrompt | None = None,
    on_state: Callable[[State], Awaitable[None]] | None = None,
) -> RunResult:
    """Continue a blocked or interrupted migration.

    The journal is a hypothesis, not a fact: the executor re-reads it and skips
    only states it recorded as completed, and every step re-checks the world it
    depends on. Geczy's reuse of a stale archive with no validation is the
    anti-pattern.
    """
    journal = Journal.open(settings.resolved_state_dir(), migration_id)
    if journal.is_finished:
        raise MigrationError(
            f"migration {migration_id} already finished",
            hint="Run `coolify-migrate status <id>` to see what happened.",
        )

    async with open_hosts(
        api,
        settings,
        plan.source_server,
        plan.target_server,
        trust_host_key=trust_host_key,
        host_key_prompt=host_key_prompt,
    ) as (
        source_host,
        target_host,
    ):
        ctx = MigrationContext(
            api=api,
            settings=settings,
            plan=plan,
            journal=journal,
            migration_id=migration_id,
            source_host=source_host,
            target_host=target_host,
            accept_drift=accept_drift,
            accept_dns=accept_dns,
            accepted_hosts=frozenset(accept_hosts),
        )
        # Rehydrate what earlier states recorded, so compensations and later
        # steps see the same world.
        ctx.target_uuids.update(journal.undo_info(State.CREATE_TARGET.value).get("target_uuids", {}))
        # Without this a resume past QUIESCE discovers from no mounts, and an
        # empty manifest copies nothing without complaining. The containers were
        # removed by the stop, so the journal is the only copy left.
        ctx.pre_stop_mounts.update(
            deserialise_mounts(journal.undo_info(State.QUIESCE.value).get("pre_stop_mounts"))
        )
        # Without this a resume past DISCOVER copies and verifies nothing: both
        # steps iterate ctx.volume_pairs, and DISCOVER is skipped as completed.
        ctx.volume_pairs.update(
            deserialise_volume_pairs(journal.undo_info(State.DISCOVER.value).get("volume_pairs"))
        )

        result = await _execute(ctx, start_from=State.INIT, on_state=on_state)
        log.info("migration.resumed", id=migration_id, outcome=result.outcome.value)
        return result


async def rollback_migration(
    api: CoolifyClient,
    settings: Settings,
    plan: MigrationPlan,
    migration_id: str,
    *,
    trust_host_key: bool = False,
    host_key_prompt: HostKeyPrompt | None = None,
) -> RunResult:
    """Undo a migration, using only what the journal recorded."""
    journal = Journal.open(settings.resolved_state_dir(), migration_id)

    async with open_hosts(
        api,
        settings,
        plan.source_server,
        plan.target_server,
        trust_host_key=trust_host_key,
        host_key_prompt=host_key_prompt,
    ) as (
        source_host,
        target_host,
    ):
        ctx = MigrationContext(
            api=api,
            settings=settings,
            plan=plan,
            journal=journal,
            migration_id=migration_id,
            source_host=source_host,
            target_host=target_host,
        )
        ctx.target_uuids.update(journal.undo_info(State.CREATE_TARGET.value).get("target_uuids", {}))

        saga = Saga(
            journal=journal,
            context=ctx,
            steps={},
            compensations=build_compensations(),
            guard=source_data_refusal,
        )
        result = await saga.rollback()
        result.notices.extend(ctx.notices)
        result = await _with_rollback_notes(result, ctx)
        log.info("migration.rolled_back", id=migration_id, outcome=result.outcome.value)
        return result


async def unpin_migration(
    api: CoolifyClient,
    settings: Settings,
    plan: MigrationPlan,
    migration_id: str,
    *,
    trust_host_key: bool = False,
    host_key_prompt: HostKeyPrompt | None = None,
) -> dict[str, pins.Release]:
    """Remove the volume pins a FINISHED migration kept, where it is now safe.

    The way out a kept pin's notice names. Re-running ``rollback`` instead
    replays every compensation - its ``/restart`` redeploys a source that is
    serving by then. This does one thing: on each side, release the pins once
    running containers keep every pinned volume mounted across the settle
    window (``compensations.settle_window``); idle pins go at once. Writes
    nothing to the journal and calls no Coolify API.

    Refused while the migration is unfinished: its pins are part of a run that
    a resume or a rollback still has to finish.
    """
    journal = Journal.open(settings.resolved_state_dir(), migration_id)
    if not journal.is_finished:
        raise MigrationError(
            f"migration {migration_id} has not finished",
            hint=(
                "Its pins protect a run that is still open. Resume it "
                f"(`coolify-migrate resume {migration_id}`) or roll it back "
                f"(`coolify-migrate rollback {migration_id}`); either removes them."
            ),
        )
    settle = settle_window(settings)
    async with open_hosts(
        api,
        settings,
        plan.source_server,
        plan.target_server,
        trust_host_key=trust_host_key,
        host_key_prompt=host_key_prompt,
    ) as (source_host, target_host):
        out: dict[str, pins.Release] = {}
        for side, host in ((pins.SOURCE, source_host), (pins.TARGET, target_host)):
            out[side] = await pins.release_when_referenced(
                host, migration_id=migration_id, side=side, timeout=0.0, settle=settle
            )
        log.info(
            "migration.unpinned",
            id=migration_id,
            released=[n for r in out.values() for n in r.released],
            kept=[n for r in out.values() for n in r.kept],
        )
        return out
