"""The mutable state one migration carries between steps.

Deliberately the ONLY mutable thing in the run. Every decision is made by pure
functions in ``domain/``; the context just holds what those functions produced
plus the handles the IO shells need.

Anything recorded here that a compensation would need must ALSO be journalled —
the context dies with the process, the journal does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeGuard

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.manifest import DockerMount
from bg_coolify_migrate.domain.naming import VolumeEndpoint, VolumePair
from bg_coolify_migrate.domain.plan import HostBaseline, MigrationPlan
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import RemoteHost
from bg_coolify_migrate.transfer.verify import VerificationReport


@dataclass
class EphemeralKey:
    """A short-lived keypair minted for a direct source->target transfer.

    Revoked in a guaranteed compensation. The fingerprint (never the key) is
    journalled so a crashed run still cleans up on the next invocation.
    """

    private_key: str
    public_key: str
    fingerprint: str
    remote_path: str
    """Where the private key lives on the SOURCE, mode 0600."""


@dataclass
class MigrationContext:
    """Everything one F1 run needs.

    Steps read `plan` (immutable) and write their results here. Compensations
    read the journal, not this — after a crash there is no context.
    """

    api: CoolifyClient
    settings: Settings
    plan: MigrationPlan
    journal: Journal
    migration_id: str

    source_host: RemoteHost
    target_host: RemoteHost

    #: source resource uuid -> newly created target uuid
    target_uuids: dict[str, str] = field(default_factory=dict)

    #: source resource uuid -> resolved volume pairs
    volume_pairs: dict[str, list[VolumePair]] = field(default_factory=dict)

    #: source resource uuid -> container mounts, captured by QUIESCE *before* it
    #: stops anything, and consumed by DISCOVER afterwards.
    #:
    #: Not a cache — a rescue. Coolify's stop is `docker stop` followed by
    #: `docker rm -f` (StopDatabase, StopApplication and StopService alike), so
    #: by the time DISCOVER runs there are no containers left to inspect, and
    #: anonymous volumes and bind mounts are declared nowhere else. Discovering
    #: from an empty container list yields an empty manifest, and an empty
    #: manifest copies nothing without complaining once.
    pre_stop_mounts: dict[str, list[DockerMount]] = field(default_factory=dict)

    #: source resource uuid -> names of the containers that mounted data this
    #: migration will copy, recorded by QUIESCE alongside the mounts.
    #:
    #: Exists so the SIGKILL check can tell a torn volume from a killed process
    #: that owned no bytes. `docker events` reports a container by NAME, while a
    #: mount records the id it was inspected by, so the two cannot be joined
    #: after the fact — the containers are gone by then.
    data_containers: dict[str, tuple[str, ...]] = field(default_factory=dict)

    #: source resource uuid -> the volumes QUIESCE pins against Coolify's Docker
    #: cleanup before it stops anything (``discovery/pins.py``). Decided in the
    #: capture because only there are the containers visible: a preview's
    #: volumes must stay unpinned, or Coolify's preview delete fails on them.
    source_pin_volumes: dict[str, list[str]] = field(default_factory=dict)

    #: source resource uuid -> the planned volumes NO container mounted before
    #: the stop. Pinned apart, as idle (``pins.LABEL_HOLD``): no restart will
    #: mount them, so a rollback must not wait for one before unpinning them.
    source_idle_volumes: dict[str, list[str]] = field(default_factory=dict)

    #: source resource uuid -> verification reports, one per volume
    verifications: dict[str, list[VerificationReport]] = field(default_factory=dict)

    ephemeral_key: EphemeralKey | None = None
    tunnel_port: int | None = None

    #: Set by the DNS gate so the report can render it even when we stop early.
    dns_report: object | None = None

    #: What the operator must still do by hand that is neither a failure nor
    #: undone - a volume pin kept because the source did not come back in time.
    #: Copied onto the RunResult by the runner. Names only, never values.
    notices: list[str] = field(default_factory=list)

    accept_drift: bool = False
    """The operator has already answered the compatibility question.

    Set by --accept-drift, or by the wizard once they confirm. Drift is never a
    refusal — we build the target as the source is configured and report what
    could still differ — but unattended we cannot ask, so an unanswered question
    stops the run rather than deciding for them."""

    accept_dns: bool = False
    """The operator accepted a custom-domain DNS cutover.

    Set by --accept-dns, or by the wizard once they confirm. The DNS gate blocks
    by default when a custom domain still points at the source (starting the
    target then burns the ACME rate limit); with this set it only warns and
    proceeds, because DNS propagation lags and the operator has chosen to finish
    the migration and cut the record over in parallel. Server-bound wildcard URLs
    are unaffected — they are rewritten onto the target, never cut over."""

    accepted_hosts: frozenset[str] = frozenset()
    """Hosts the operator accepted ONE BY ONE (--accept-dns-host).

    The only answer to a host that resolves to neither server (a CDN, proxy or
    load balancer whose origin DNS cannot show) or that vanished since the plan:
    --accept-dns answers a plain cutover, never those (dns/gate.py)."""

    delete_previews: bool = False

    @property
    def known_hosts(self) -> Path:
        return self.settings.resolved_known_hosts()

    def collection_of(self, source_uuid: str) -> str:
        for resource in self.plan.resources:
            if resource.snapshot.uuid == source_uuid:
                return resource.snapshot.collection
        raise KeyError(source_uuid)

    def all_target_uuids(self) -> list[tuple[str, str]]:
        """``(collection, target_uuid)`` for everything we created."""
        return [
            (self.collection_of(source_uuid), target_uuid)
            for source_uuid, target_uuid in self.target_uuids.items()
        ]


def serialise_mounts(mounts: dict[str, list[DockerMount]]) -> dict[str, list[dict[str, Any]]]:
    """Pre-stop mounts, JSON-shaped, for the journal."""
    return {uuid: [m.model_dump(mode="json") for m in items] for uuid, items in mounts.items()}


def deserialise_mounts(raw: object) -> dict[str, list[DockerMount]]:
    """The inverse, for a resumed run.

    Absent is tolerated; corrupt is not, and the difference matters more here
    than almost anywhere. An entry that fails to parse must not be quietly
    dropped, because dropping the last entry of a resource leaves an empty list,
    and an empty list does not read as "we lost this" — it reads as "this
    resource has no volumes". DISCOVER would agree, copy nothing, and the
    migration would report success.

    So: a missing capture gives {}, which DISCOVER rejects by name. A malformed
    one raises here, while there is still someone to tell.
    """
    if not isinstance(raw, dict):
        return {}

    out: dict[str, list[DockerMount]] = {}
    for uuid, items in raw.items():
        if not isinstance(items, list):
            raise ValueError(
                f"journal: pre_stop_mounts[{uuid!r}] is {type(items).__name__}, not a list"
            )
        out[str(uuid)] = [DockerMount.model_validate(item) for item in items]
    return out


def is_volume_row(row: object) -> TypeGuard[dict[str, Any]]:
    """Is this journalled DISCOVER row a docker volume pair? PURE.

    DISCOVER journals two shapes under ``volume_pairs``: real pairs, whose
    ``source`` is a volume NAME, and bind mounts, mirrored path-to-path, whose
    ``source`` is an absolute host path. Only the first become a
    :class:`VolumePair`; COPY and VERIFY iterate nothing else.
    """
    if not isinstance(row, dict):
        return False
    source = row.get("source")
    return isinstance(source, str) and bool(source) and not source.startswith("/")


def deserialise_volume_pairs(raw: object) -> dict[str, list[VolumePair]]:
    """DISCOVER's journalled pairs, rebuilt for a resumed run. PURE.

    The context dies with the process; this record does not. A resume after a
    crash inside COPY or VERIFY skips the completed DISCOVER, so without it
    ``ctx.volume_pairs`` is empty and both steps iterate nothing: COPY copies
    nothing, VERIFY verifies nothing, START runs on empty target volumes, and a
    DELETE finalize then removes the only intact copy. Each step's "nothing to
    do" is a legitimate answer for a stateless stack, so neither can tell.

    Same contract as :func:`deserialise_mounts`: absent gives {}, corrupt
    raises, because a dropped row reads as "this volume needs no copy". The
    target endpoint gets the source's mount path: DISCOVER records one, and a
    pair's paths derive from the volume NAMES alone (:attr:`VolumePair.target_path`).
    """
    if not isinstance(raw, dict):
        return {}

    out: dict[str, list[VolumePair]] = {}
    for uuid, rows in raw.items():
        if not isinstance(rows, list):
            raise ValueError(
                f"journal: volume_pairs[{uuid!r}] is {type(rows).__name__}, not a list"
            )
        pairs: list[VolumePair] = []
        for row in rows:
            if not is_volume_row(row):
                continue  # a bind mount: mirrored path-to-path, never a volume pair
            target = row.get("target")
            if not isinstance(target, str) or not target:
                raise ValueError(f"journal: volume_pairs[{uuid!r}] has a row without a target")
            mount_path = str(row.get("mount_path") or "")
            pairs.append(
                VolumePair(
                    source=VolumeEndpoint(name=row["source"], mount_path=mount_path),
                    target=VolumeEndpoint(name=target, mount_path=mount_path),
                )
            )
        out[str(uuid)] = pairs
    return out


def serialise_dns_baseline(baseline: tuple[HostBaseline, ...]) -> dict[str, dict[str, Any]]:
    """The plan's DNS baseline as PLAN journals it: hosts, addresses, TTL, NS names."""
    return {
        entry.host: {
            "resource": entry.resource,
            "addresses": list(entry.addresses),
            "ttl": entry.ttl,
            "nameservers": list(entry.nameservers),
            "cname_chain": list(entry.cname_chain),
        }
        for entry in baseline
    }


def dns_baseline_of(journal: Journal, plan: MigrationPlan) -> dict[str, HostBaseline]:
    """The DNS baseline taken at PLAN, by host — the journal's first, the plan's else.

    The journal's is what this run's PLAN recorded; a plan.json made earlier and
    resumed carries the same. Junk rows are skipped: a lost baseline only means
    a check that cannot run, never a wrong one.
    """
    raw = journal.undo_info("plan").get("dns_baseline")
    if not isinstance(raw, dict) or not raw:
        return {entry.host: entry for entry in plan.dns_baseline}
    out: dict[str, HostBaseline] = {}
    for host, entry in raw.items():
        if not isinstance(host, str) or not isinstance(entry, dict):
            continue
        try:
            out[host] = HostBaseline(
                host=host,
                resource=str(entry.get("resource") or ""),
                addresses=tuple(str(a) for a in entry.get("addresses") or ()),
                ttl=entry.get("ttl") if isinstance(entry.get("ttl"), int) else None,
                nameservers=tuple(str(n) for n in entry.get("nameservers") or ()),
                cname_chain=tuple(str(c) for c in entry.get("cname_chain") or ()),
            )
        except (TypeError, ValueError):
            continue
    return out
