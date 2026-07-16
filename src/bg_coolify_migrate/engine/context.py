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
from typing import Any

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.manifest import DockerMount
from bg_coolify_migrate.domain.naming import VolumePair
from bg_coolify_migrate.domain.plan import MigrationPlan
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

    #: source resource uuid -> verification reports, one per volume
    verifications: dict[str, list[VerificationReport]] = field(default_factory=dict)

    ephemeral_key: EphemeralKey | None = None
    tunnel_port: int | None = None

    #: Set by the DNS gate so the report can render it even when we stop early.
    dns_report: object | None = None

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
