"""The migration plan: what we will do, decided before we do anything.

PURE module: no IO. Everything here is a total function of a captured snapshot,
so ``coolify-migrate plan`` can produce the complete plan — manifest, strategy,
drift verdict — without mutating a thing. That is what makes ``--dry-run``
meaningful, unlike coolify-mover's, which short-circuits *before* all the code
that actually breaks and therefore validates nothing.

The migration unit is a **project/environment**, not a resource. A Coolify
project is App + Postgres + Redis wired together over an internal network; moving
them one at a time guarantees an inconsistent window and, since Coolify connects
resources by internal DNS name, a broken one. A single resource is simply the
n=1 case.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from bg_coolify_migrate.domain import schedules
from bg_coolify_migrate.domain.drift import RebuildDriftReport
from bg_coolify_migrate.domain.kinds import BuildPack, DatabaseEngine, GitAuth, ResourceKind
from bg_coolify_migrate.domain.manifest import VolumeManifest
from bg_coolify_migrate.domain.statemachine import FinalizePolicy


class Strategy(StrEnum):
    """How a single resource gets from source to target."""

    COPY_DATA = "copy_data"
    """Recreate through the API and mirror the volumes. No build, no git, zero
    drift. Databases, services and dockerimage apps all take this path — which
    is why the user's real pain ("die Daten") is fully solved for them."""

    REBUILD = "rebuild"
    """Recreate through the API and let Coolify rebuild from git. Mirrors data
    byte-exactly but ships whatever HEAD builds, so it is gated by
    ``drift.assess_rebuild_drift``."""

    RECREATE_ONLY = "recreate_only"
    """Nothing to mirror and nothing to build — e.g. a dockerimage app with no
    volumes. The migration is recreate + deploy + DNS.

    Note this is NOT "the stateless case": a stateless app that BUILDS is
    :attr:`REBUILD`, because its code drifts exactly like a stateful one's even
    though there is no data to protect. Users assume "no volumes = trivial"; that
    assumption is what the strategy split exists to contradict."""


class TransferMode(StrEnum):
    """How bytes physically travel. Never through the operator's filesystem."""

    DIRECT = "direct"
    """Source pushes straight to target over SSH. Fastest; the operator's laptop
    can disconnect mid-transfer."""

    TUNNEL = "tunnel"
    """Reverse port-forward through the workstation, used when the source cannot
    reach the target. rsync still runs server-to-server; only the TCP stream is
    relayed, so ownership/symlinks/xattrs are untouched."""

    AUTO = "auto"
    """Probe for direct reachability, fall back to tunnel. The default."""


class ServerRef(BaseModel):
    """A Coolify-managed server."""

    model_config = ConfigDict(frozen=True)

    uuid: str
    name: str
    ip: str
    user: str = "root"
    port: int = 22
    wildcard_domain: str = ""
    """The server's wildcard base (Coolify ``settings.wildcard_domain``), e.g.
    ``app.0046-20.cloud.bauer-group.com``. Every resource on this server gets a
    default URL under it, and that URL is BOUND to this server — migrating a
    resource rewrites the URL onto the target server's wildcard. Empty when the
    server has none configured."""
    prunes_unused_volumes: bool = False
    """Coolify's Docker cleanup on this server deletes every volume no container
    references (``settings.delete_unused_volumes``). A warning, never a
    refusal: the migration pins what it exposes either way (discovery/pins.py),
    and whether the server should prune is the operator's call."""
    docker_cleanup: str = ""
    """When that cleanup runs, in words — e.g. ``cron '0 0 * * *', on every run``."""


class HostBaseline(BaseModel):
    """What one of the source's custom hosts resolved to when the plan was made.

    The DNS gate compares against it (a host that resolved then and does not now
    was deleted — VANISHED), CREATE re-checks it while the source still serves,
    and a rollback re-checks it at its end. Hostnames, addresses, a TTL and the
    zone's nameserver NAMES — never a secret; journalled at PLAN.
    """

    model_config = ConfigDict(frozen=True)

    host: str
    resource: str = ""
    """The source resource that holds it, for the operator."""
    addresses: tuple[str, ...] = ()
    ttl: int | None = None
    nameservers: tuple[str, ...] = ()
    cname_chain: tuple[str, ...] = ()


class ResourceRow(BaseModel):
    """One migratable resource, fully qualified. The unit of ``list``.

    Carries its whole path — server, project, environment — so ``list`` can render
    the complete server -> project -> environment -> resource tree in one pass, and
    each level's uuid is present so ``plan``/``run`` can be driven entirely by uuid
    (the only unambiguous handle when a name carries spaces, slashes or other
    specials).
    """

    model_config = ConfigDict(frozen=True)

    project: str
    project_uuid: str
    environment: str
    name: str
    uuid: str
    kind: str
    """The Coolify collection — application | service | database — not the fine kind."""
    server: str
    """Server name, or empty when it could not be resolved."""
    server_uuid: str


class ResourceSnapshot(BaseModel):
    """Captured facts about one source resource.

    Deliberately a value object: no methods that touch the network. Everything
    the planner needs is here, so planning is a pure function of it.
    """

    model_config = ConfigDict(frozen=True)

    uuid: str
    name: str
    collection: str
    kind: ResourceKind

    build_pack: BuildPack | None = None
    engine: DatabaseEngine | None = None
    service_type: str | None = None
    image: str | None = None
    """For databases: MUST be pinned to the source tag. The model hook derives
    the volume mount path from it (Postgres >=18 moves to /var/lib/postgresql),
    so an unpinned image can land the mirrored bytes where nothing looks."""

    git_repository: str | None = None
    git_branch: str | None = None
    git_auth: GitAuth = GitAuth.NONE

    github_app_uuid: str | None = None
    """Resolved by the planner from the source's ``source_id`` via /github-apps.
    The GET serialises only the raw ``source_type``/``source_id`` columns —
    ``github_app_uuid`` is a CREATE-request field that never appears in reads,
    and treating its absence as 'public' created credential-less targets."""

    private_key_uuid: str | None = None
    """Resolved from ``private_key_id`` via /security/keys, same reasoning."""

    docker_compose_raw: str | None = None
    """Decoded, not base64. Before 4.4 Coolify re-dumped this through
    Yaml::dump(Yaml::parse()) on the service path, destroying comments and
    formatting; from 4.4 it is stored verbatim."""

    running_image: str | None = None
    """From `docker inspect` of the running container. The ONLY trustworthy
    source of the deployed commit: the image tag is `{uuid}:{sha}` by
    construction. `git_commit_sha` is not updated by a normal deploy, and
    SOURCE_COMMIT is user-overridable and falls back to the string 'unknown'."""

    builds: bool = False
    """Whether this resource actually builds. For git build packs always True;
    for compose-backed kinds it comes from compose.builds_from_source()."""

    has_previews: bool = False
    """Preview containers exist. They are NOT stopped by the API's stop endpoint
    (StopApplication filters pullRequestId=0), so they would keep writing during
    a 'quiesced' copy. Detected and blocked, never ignored."""

    @property
    def running_commit(self) -> str | None:
        """The commit that produced the running image, parsed from its tag."""
        if not self.running_image or ":" not in self.running_image:
            return None
        tag = self.running_image.rsplit(":", 1)[1]
        return tag or None


class ResourcePlan(BaseModel):
    """What we will do with one resource."""

    model_config = ConfigDict(frozen=True)

    snapshot: ResourceSnapshot
    strategy: Strategy
    manifest: VolumeManifest = Field(default_factory=VolumeManifest)
    drift: RebuildDriftReport | None = None
    warnings: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    """What the target provably cannot reproduce — a create that will 422, a
    deploy that will fail after the stop, data that would be left behind. Decided
    by domain/fidelity.py from the captured GETs."""
    recover_env_keys: tuple[str, ...] = ()
    """Locked variables the plan read back from the source server and promised to
    carry (domain/locked_envs.py). KEY NAMES ONLY — this model is saved as
    plan.json. CREATE re-reads them and refuses, before any write, if one of
    them can no longer be read."""

    @property
    def hard_blocking_reasons(self) -> tuple[str, ...]:
        """Reasons that CANNOT be overridden by any flag.

        Kept apart from drift because drift is overridable (``--accept-rebuild-
        drift``) and these are not. Conflating the two makes the flag a lie: it
        gets accepted, and the migration aborts anyway on the generic check.
        """
        out: list[str] = list(self.refusals)
        out.extend(f"{i.source_name or i.mount_path}: {i.reason}" for i in self.manifest.refused)
        if self.snapshot.has_previews:
            out.append(
                "preview deployments are running; the API's stop endpoint does not stop them "
                "(StopApplication filters pullRequestId=0), so they would keep writing during "
                "the copy"
            )
        return tuple(out)

    @property
    def drift_decisions(self) -> tuple[str, ...]:
        """Things the operator should adjudicate before we proceed.

        NOT blocking. We build the target exactly as the source is configured and
        then say what could still differ — a moved branch, a floating tag. Whether
        that is compatible is a judgement about their stack, so we ask rather than
        refuse. ``--accept-drift`` answers it in advance when unattended.
        """
        return tuple(f.summary for f in self.drift.needs_decision) if self.drift else ()

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        """Every reason this resource may not be migrated, full stop.

        Only hard reasons: drift is a question, not a refusal. The SINGLE source
        of truth for blocking — :attr:`is_blocked` is derived from it rather than
        re-deriving the conditions, because two parallel implementations of "is
        this blocked?" drift apart, and the dangerous direction is the one where a
        reason is listed in the report while the migration proceeds anyway.
        """
        return self.hard_blocking_reasons

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking_reasons)

    @property
    def needs_confirmation(self) -> bool:
        """True if there is drift the operator should look at before we run."""
        return bool(self.drift_decisions)


class MigrationPlan(BaseModel):
    """The unit of work: the resources of one environment, migrated atomically.

    The resource is the atom — environments and projects only decide *which*
    resources a plan carries. A whole-environment plan holds every resource in the
    environment; a resource-scoped plan holds exactly one, and ``selected_resources``
    records that so the quiesce gates watch only it and leave its siblings running.
    A whole-project migration is several of these plans, one per environment.
    """

    model_config = ConfigDict(frozen=True)

    project: str
    environment: str
    source_server: ServerRef
    target_server: ServerRef
    resources: tuple[ResourcePlan, ...] = ()
    finalize_policy: FinalizePolicy = FinalizePolicy.RENAME
    transfer_mode: TransferMode = TransferMode.AUTO
    selected_resources: tuple[str, ...] = ()
    """Resource names this plan was deliberately narrowed to. Empty means the whole
    environment — the quiesce gates then watch the stack. Non-empty means a scoped
    run: the gates watch only these, so siblings left running do not make the stop
    gate wait forever or the restart check trip."""
    notes: tuple[str, ...] = ()
    """Warnings about the move as a whole rather than one resource — e.g. the two
    servers issuing certificates differently."""
    dns_baseline: tuple[HostBaseline, ...] = ()
    """Every custom host of the plan's sources as DNS answered at plan time
    (:class:`HostBaseline`). Empty for an old plan.json, which then simply has
    no VANISHED verdicts."""
    volume_backups_possible: bool = False
    """The instance can hold volume-backup schedules (4.3.0+, or an unknown
    version), which no API reads (domain/schedules.py). A flag rather than a
    note: what a DELETE finalize does to them depends on ``finalize_policy``,
    which is swapped onto a built plan."""

    @property
    def is_resource_scoped(self) -> bool:
        return bool(self.selected_resources)

    @property
    def volume_backup_resources(self) -> tuple[str, ...]:
        """The resources whose volumes may carry unreadable volume-backup schedules."""
        if not self.volume_backups_possible:
            return ()
        return tuple(r.snapshot.name for r in self.resources if r.manifest.items)

    @property
    def total_bytes(self) -> int:
        return sum(r.manifest.total_bytes for r in self.resources)

    @property
    def is_blocked(self) -> bool:
        return any(r.is_blocked for r in self.resources)

    @property
    def blocked_resources(self) -> tuple[ResourcePlan, ...]:
        return tuple(r for r in self.resources if r.is_blocked)

    @property
    def warnings(self) -> tuple[str, ...]:
        out: list[str] = list(self.notes)
        out.extend(_volume_prune_notes(self))
        out.extend(
            schedules.volume_backup_notes(
                self.volume_backup_resources,
                deletes_source=self.finalize_policy is FinalizePolicy.DELETE,
            )
        )
        for r in self.resources:
            out.extend(f"{r.snapshot.name}: {w}" for w in r.warnings)
            out.extend(f"{r.snapshot.name}: {w}" for w in r.manifest.warnings)
        return tuple(out)


def _volume_prune_notes(plan: MigrationPlan) -> list[str]:
    """What a server that prunes unused volumes means for this move. PURE.

    Derived from the plan on every read rather than baked into ``notes``: the
    run command swaps ``finalize_policy`` onto a plan that is already built
    (``model_copy``), and the second note depends on it.
    """
    out: list[str] = []
    seen: set[str] = set()
    for role, server in (("source", plan.source_server), ("target", plan.target_server)):
        if not server.prunes_unused_volumes or server.uuid in seen:
            continue
        seen.add(server.uuid)
        out.append(
            f"{role} server {server.name!r} has 'Delete unused volumes' on: Coolify's Docker "
            f"cleanup ({server.docker_cleanup or 'scheduled'}) runs `docker volume prune -af`, "
            "which deletes every volume no container references, and Coolify's stop removes "
            "a resource's containers. This migration pins every volume it stops or fills "
            "with a never-started container (bg-coolify-migrate-pin-...) until finalize or "
            "rollback."
        )
    source = plan.source_server
    if source.prunes_unused_volumes and plan.finalize_policy is not FinalizePolicy.DELETE:
        out.append(
            f"after finalize the source stays stopped ({plan.finalize_policy.value}) with no "
            f"containers, so the next Docker cleanup on {source.name!r} deletes its volumes: "
            f"it is no fallback copy there. Turn 'Delete unused volumes' off on "
            f"{source.name!r}, or back the data up, if you want one."
        )
    return out


def select_strategy(kind: ResourceKind, *, builds: bool, has_volumes: bool) -> Strategy:
    """Pick the migration strategy for one resource.

    Pure and total. The three inputs are exactly what matters:

    * ``kind`` decides which API route recreates it.
    * ``builds`` — from ``kinds.always_builds`` OR ``compose.builds_from_source``
      — decides whether code can drift. This is why it is a parameter rather
      than derived from ``kind``: a compose stack with ``build:`` builds, and one
      with only ``image:`` does not, and both are the same ``kind``.
    * ``has_volumes`` distinguishes a stateless app (nothing to mirror) from a
      stateful one.
    """
    if builds:
        return Strategy.REBUILD
    if not has_volumes:
        return Strategy.RECREATE_ONLY
    return Strategy.COPY_DATA


def requires_drift_gate(strategy: Strategy) -> bool:
    """True if this strategy can ship different code than the source runs.

    ``RECREATE_ONLY`` is included when it rebuilds: a stateless app has no data
    to protect but its code drifts exactly the same way.
    """
    return strategy is Strategy.REBUILD
