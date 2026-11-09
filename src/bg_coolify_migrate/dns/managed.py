"""Coolify-managed DNS: which hosts it may manage. PURE module: no IO.

From v4.4.0 Coolify can create and track DNS records — at Cloudflare only (zones
come from a team's Cloudflare integration tokens, CloudflareDnsProvider
::zonesForTeam v4.4.0:73-76, v4.5.0:80-83). The REST API exposes none of it up to
4.5.x, so whether a given record IS managed cannot be read there; what can be read
is whether its zone is on Cloudflare at all. From 4.6.0 ``GET .../dns`` shows what
Coolify tracks, and ``GET /dns/zones`` the team's zones. All of it is for MESSAGES
and plan warnings only. Safety never depends on them: the domain hand-over is
make-before-break on every version (domain/domains.py), and the DNS gate blocks an
answer it cannot place for every provider alike (dns/gate.py).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from bg_coolify_migrate.dns.wildcard import wildcard_base
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


def target_hosts_get_records(collections: Collection[str], *, version: Version | None) -> bool:
    """Whether CREATE can make Coolify queue records for the TARGET's own hosts.

    From 4.7.0 every API create of an application or a service queues one for each
    hostname of the new resource (FEATURES "automatic_dns_on_api_create"): the
    server-bound hosts CREATE rewrote onto the target's wildcard, and the ones
    Coolify generates there. A service got them on 4.6.0 already, at CREATE's
    ``urls`` PATCH (api/resources.finish_service_target), which adds the rewritten
    hosts (ServicesController v4.6.0:1341). A database holds no domain. Only for a
    version KNOWN to do it: the note reads GET /dns/zones, which 4.6.0 added.
    """
    if supports("automatic_dns_on_api_create", version, if_unknown=False):
        return bool({"applications", "services"} & set(collections))
    return "services" in collections and supports(
        "automatic_dns_on_api_save", version, if_unknown=False
    )


def _zone_name(row: object) -> str:
    name = row.get("zone") if isinstance(row, dict) else None
    return name.strip().lower().rstrip(".") if isinstance(name, str) else ""


def zones_holding(host: str, zones: object) -> list[dict[str, object]]:
    """The ``GET /dns/zones`` rows Coolify picks for ``host``.

    Mirrors CloudflareDnsProvider::findZones (v4.7.0:69-78): the zone that IS the
    host or a parent of it, and of several, only the longest. One row per zone and
    token (DnsController v4.6.0 = v4.7.0:111-131), so two tokens on one zone are
    two rows, and Coolify then queues nothing (ResourceDnsRecords v4.7.0:166-168).
    """
    host = host.strip().lower().rstrip(".")
    rows = zones if isinstance(zones, list) else []
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and (name := _zone_name(row))
        and (host == name or host.endswith("." + name))
    ]
    longest = max((len(_zone_name(row)) for row in matches), default=0)
    return [row for row in matches if len(_zone_name(row)) == longest]


def target_records_note(zones: object, *, target_wildcard: str | None) -> str | None:
    """The plan warning for :func:`target_hosts_get_records`, when the zone is the team's.

    ``zones`` is the ``GET /dns/zones`` body: the team's Cloudflare zones, without
    the token's automatic or proxied setting, so this says "may". DNS-only records
    (the default) point at the target, which the DNS gate reads as READY. Proxied
    ones (the token's opt-in, IntegrationToken v4.7.0:136-138) resolve to
    Cloudflare, which the gate reads as ELSEWHERE, after the source is stopped. A
    warning, never a refusal: the zone list does not say whether the token proxies.
    ``GET .../dns`` does (``providers[].proxied_dns``, ResourceDnsRecords v4.6.0 =
    v4.7.0:73-79), but only for the hostnames of a resource that exists, and the
    target's do not until CREATE.
    """
    base = wildcard_base(target_wildcard)
    if not base:
        return None
    held = zones_holding(base, zones)
    if len(held) != 1:
        return None
    zone = _zone_name(held[0])
    token = held[0].get("integration_token_name")
    by_token = f" (integration token {token!r})" if isinstance(token, str) and token else ""
    return (
        f"{base}, the target server's wildcard, lies in the team's Cloudflare zone "
        f"{zone}{by_token}. With automatic DNS on for that token (the default), Coolify "
        "may create a record for each of the target's own hosts under it at CREATE, "
        "pointing at the target server (from 4.7 for every resource the API creates, "
        "from 4.6 for a service's URLs). DNS-only records are harmless. If the token "
        "creates proxied records, those hosts resolve to Cloudflare, and the DNS gate "
        "finds them on neither server after the source is stopped: pass "
        "--accept-dns-host for each of them on the run, or turn proxied DNS off on that "
        "token for the migration."
    )
