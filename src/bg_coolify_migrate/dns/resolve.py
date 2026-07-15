"""DNS resolution.

IO shell; classification lives in :mod:`.gate`.

**Which resolver?** Not the local one. An operator's workstation may sit behind a
split-horizon resolver that answers with internal addresses, which would make the
gate pass while the public internet still points at the old server — the exact
failure the gate exists to prevent.

So we query the **authoritative** nameservers for the zone, and optionally a
public resolver as a cross-check. Disagreement between them is itself
information: it usually means a change is mid-propagation, and the TTL tells the
operator how long the stale answer will linger.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from bg_coolify_migrate.dns.extract import Hostname
from bg_coolify_migrate.dns.gate import Resolution

log = structlog.get_logger(__name__)

#: Cross-check resolvers. Two independent operators so a single outage or a
#: single poisoned cache cannot silently decide a migration.
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")

_DEFAULT_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class ResolverConfig:
    timeout: float = _DEFAULT_TIMEOUT
    use_authoritative: bool = True
    """Query the zone's own nameservers rather than a cache. Strongly preferred:
    a cached answer can be stale by up to its TTL, and a split-horizon local
    resolver can be wrong indefinitely."""
    public_resolvers: tuple[str, ...] = PUBLIC_RESOLVERS


async def _authoritative_nameservers(host: str, *, timeout: float) -> list[str]:
    """Find the nameservers for the closest enclosing zone.

    Walks up the labels (``a.b.example.com`` -> ``b.example.com`` ->
    ``example.com``) until an NS record set exists, because the hostname itself
    usually has no NS of its own.
    """
    import dns.asyncresolver
    import dns.resolver

    labels = host.split(".")
    for i in range(len(labels) - 1):
        zone = ".".join(labels[i:])
        try:
            answer = await dns.asyncresolver.resolve(zone, "NS", lifetime=timeout)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            continue
        except Exception as exc:
            log.debug("dns.ns.failed", zone=zone, error=str(exc))
            continue

        addresses: list[str] = []
        for rdata in answer:
            ns_name = str(rdata.target).rstrip(".")
            try:
                ns_answer = await dns.asyncresolver.resolve(ns_name, "A", lifetime=timeout)
                addresses.extend(str(r.address) for r in ns_answer)
            except Exception:
                continue
        if addresses:
            return addresses
    return []


async def resolve_one(hostname: Hostname, config: ResolverConfig | None = None) -> Resolution:
    """Resolve one hostname to A/AAAA addresses, with its TTL.

    Never raises: a resolution failure is *data* (``Verdict.UNRESOLVED``), not an
    exception. A hostname that does not resolve is not serving anyone, so it
    cannot block a cutover — but the operator still wants to see it.
    """
    import dns.asyncresolver
    import dns.resolver

    cfg = config or ResolverConfig()
    host = hostname.host

    resolver = dns.asyncresolver.Resolver(configure=True)
    resolver.lifetime = cfg.timeout

    if cfg.use_authoritative:
        try:
            ns = await _authoritative_nameservers(host, timeout=cfg.timeout)
            if ns:
                resolver.nameservers = ns
                log.debug("dns.using_authoritative", host=host, nameservers=ns[:2])
        except Exception as exc:
            log.debug("dns.authoritative_lookup_failed", host=host, error=str(exc))

    addresses: list[str] = []
    ttl: int | None = None
    cname_chain: list[str] = []
    errors: list[str] = []

    for rdtype in ("A", "AAAA"):
        try:
            answer = await resolver.resolve(host, rdtype, lifetime=cfg.timeout)
            addresses.extend(str(r.address) for r in answer)
            ttl = int(answer.rrset.ttl) if answer.rrset is not None else ttl
            if answer.chaining_result and answer.chaining_result.cnames:
                # `cnames` is a list of RRsets, not of Rdata: the target lives on
                # each rdata inside the set, not on the set itself.
                for rrset in answer.chaining_result.cnames:
                    for rdata in rrset:
                        cname_chain.append(str(rdata.target).rstrip("."))
        except dns.resolver.NXDOMAIN:
            errors.append("NXDOMAIN")
            break
        except dns.resolver.NoAnswer:
            continue
        except (dns.resolver.NoNameservers, dns.resolver.LifetimeTimeout) as exc:
            errors.append(type(exc).__name__)
        except Exception as exc:
            errors.append(str(exc)[:80])

    error: str | None = None
    if not addresses:
        error = "; ".join(dict.fromkeys(errors)) or "no A/AAAA records"

    return Resolution(
        hostname=hostname,
        addresses=tuple(dict.fromkeys(addresses)),
        ttl=ttl,
        cname_chain=tuple(dict.fromkeys(cname_chain)),
        error=error,
    )


async def resolve_all(
    hostnames: list[Hostname], config: ResolverConfig | None = None
) -> list[Resolution]:
    """Resolve many hostnames concurrently.

    Bounded concurrency: a stack with 40 domains should not open 40 simultaneous
    DNS conversations with one authoritative server.
    """
    if not hostnames:
        return []

    semaphore = asyncio.Semaphore(8)

    async def _one(host: Hostname) -> Resolution:
        async with semaphore:
            return await resolve_one(host, config)

    return list(await asyncio.gather(*(_one(h) for h in hostnames)))
