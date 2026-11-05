"""Coolify-managed DNS: which hosts it may manage. PURE module: no IO.

From v4.4.0 Coolify can create and track DNS records — at Cloudflare only (zones
come from a team's Cloudflare integration tokens, CloudflareDnsProvider
::zonesForTeam v4.4.0:73-76, v4.5.0:80-83). The REST API exposes none of it up to
4.5.x, so whether a given record IS managed cannot be read there; what can be read
is whether its zone is on Cloudflare at all. From 4.6.0 ``GET .../dns`` shows what
Coolify tracks. Both are for MESSAGES and plan warnings only. Safety never depends
on them: the domain hand-over is make-before-break on every version
(domain/domains.py), and the DNS gate blocks an answer it cannot place for every
provider alike (dns/gate.py).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.plan import HostBaseline
from bg_coolify_migrate.domain.versions import Version

#: Cloudflare's own nameservers (full setup) and its proxied CNAME target
#: (partial/CNAME setup) — the two shapes a Cloudflare-hosted host takes in DNS.
_CLOUDFLARE_NS_SUFFIX = ".ns.cloudflare.com"
_CLOUDFLARE_CNAME_SUFFIX = ".cdn.cloudflare.net"


def on_cloudflare(nameservers: Iterable[str], cname_chain: Iterable[str] = ()) -> bool:
    """Whether DNS shows the host's zone on Cloudflare.

    By its authoritative nameservers (``*.ns.cloudflare.com``) or a CNAME into
    ``*.cdn.cloudflare.net`` (a partial setup). Vanity nameservers are missed —
    acceptable, because nothing but wording depends on this.
    """
    return any(ns.lower().rstrip(".").endswith(_CLOUDFLARE_NS_SUFFIX) for ns in nameservers) or any(
        c.lower().rstrip(".").endswith(_CLOUDFLARE_CNAME_SUFFIX) for c in cname_chain
    )


def coolify_may_manage(
    nameservers: Iterable[str], cname_chain: Iterable[str] = (), *, version: Version | None
) -> bool:
    """Whether Coolify could manage this host's record: Cloudflare, from 4.4.0.

    An unknown version counts as the newest, toward the warning.
    """
    return supports("managed_dns", version) and on_cloudflare(nameservers, cname_chain)


def cloudflare_note(baseline: Iterable[HostBaseline], *, version: Version | None) -> str | None:
    """The plan warning for custom hosts whose zone is on Cloudflare, from 4.4.0.

    A warning, never a refusal: the hand-over keeps every such record alive on
    its own (make-before-break), and the cutover stays the operator's because
    Coolify has no way to re-point a record it manages.
    """
    hosts = sorted(
        entry.host
        for entry in baseline
        if coolify_may_manage(entry.nameservers, entry.cname_chain, version=version)
    )
    if not hosts:
        return None
    return (
        f"{', '.join(hosts)}: the zone is on Cloudflare, and Coolify >= 4.4 may manage "
        "these records. The migration hands each hostname over make-before-break, so "
        "Coolify never deletes one. Coolify never re-points a record on its own, so after "
        "the cutover point each at the target in Cloudflare yourself - for a record "
        "Coolify does not manage, the target's Domains > Configure DNS > Replace record "
        "works."
    )


def automatic_dns_hosts(statuses: Iterable[object], *, hosts: Collection[str]) -> list[str]:
    """Hosts for which a domain save on the target can create a DNS record, from 4.6.0.

    ``statuses`` are ``GET /{collection}/{uuid}/dns`` bodies. Mirrors
    ResourceDnsRecords::queueAutomaticRecords (v4.6.0:158-184): exactly one zone of
    the team holds the host, and its token has automatic DNS on. A host Coolify
    already tracks a record for is skipped, because the create then finds that
    record. The route reads the database only, so a record that exists solely in
    Cloudflare still counts here, and Coolify would leave that one alone.
    """
    wanted = {host.lower() for host in hosts}
    found: set[str] = set()
    for status in statuses:
        entries = status.get("hostnames") if isinstance(status, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            host = str(entry.get("hostname") or "").lower()
            providers = entry.get("providers")
            if (
                host in wanted
                and not entry.get("records")
                and isinstance(providers, list)
                and len(providers) == 1
                and isinstance(providers[0], dict)
                and providers[0].get("automatic_dns") is True
            ):
                found.add(host)
    return sorted(found)


def automatic_dns_note(statuses: Iterable[object], *, hosts: Collection[str]) -> str | None:
    """The plan warning for :func:`automatic_dns_hosts`.

    A warning, never a refusal: only a host with no record of its own at Cloudflare
    is affected, which the API cannot tell, and the usual explicit record is safe.
    """
    affected = automatic_dns_hosts(statuses, hosts=hosts)
    if not affected:
        return None
    return (
        f"{', '.join(affected)}: automatic DNS is on for the zone's integration token, so "
        "Coolify >= 4.6 creates a record when the target claims the hostname, while the "
        "source still serves. A host with its own record at Cloudflare is left alone. A "
        "host that resolves only through a wildcard record gets a new record pointing at "
        "the target before the target runs, and a rollback does not remove it. Give such "
        "a host its own record first, or turn automatic DNS off on that token for the "
        "migration."
    )
