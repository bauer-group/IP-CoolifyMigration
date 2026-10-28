"""Coolify-managed DNS: which hosts it may manage. PURE module: no IO.

From v4.4.0 Coolify can create and track DNS records — at Cloudflare only (zones
come from a team's Cloudflare integration tokens, CloudflareDnsProvider
::zonesForTeam v4.4.0:73-76, v4.5.0:80-83). The REST API exposes none of it, so
whether a given record IS managed cannot be read; what can be read is whether
its zone is on Cloudflare at all. That classification is for MESSAGES and plan
warnings only. Safety never depends on it: the domain hand-over is
make-before-break on every version (domain/domains.py), and the DNS gate blocks
an answer it cannot place for every provider alike (dns/gate.py).
"""

from __future__ import annotations

from collections.abc import Iterable

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
        "Coolify never deletes one. Coolify never re-points a record (no API or UI path "
        "for one it manages), so after the cutover point each at the target in Cloudflare "
        "yourself - for a record Coolify does not manage, the target's Domains > "
        "Configure DNS > Replace record works."
    )
