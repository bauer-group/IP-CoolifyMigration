"""The DNS cutover gate.

PURE module: no IO — resolution happens in :mod:`.resolve`, classification here.

Why this gate exists, concretely. If we start the target while DNS still points
at the source:

* Traefik on the new host requests an ACME certificate. The HTTP-01 / TLS-ALPN-01
  challenge is routed **by DNS** to the OLD host, which knows nothing about it.
  The challenge fails.
* Let's Encrypt rate-limits **5 failed validations per account/hostname/hour**
  and 50 certificates per registered domain per week. A retry loop burns the
  budget for the domain, so even a correct cutover an hour later cannot get a
  certificate.
* Meanwhile two proxies claim the same ``Host()`` rule, and which one answers
  depends on where the request lands.

So the gate is not a nicety — starting early actively damages the ability to
migrate at all. It is also *resumable by design*: you flip DNS, then continue.

What blocks, and how an operator answers it (per host, never globally, for
anything but a plain cutover):

* CUTOVER_NEEDED — the host still resolves to the source. ``--accept-dns`` (or
  ``--accept-dns-host HOST``) proceeds and cuts it over in parallel.
* ELSEWHERE — the host resolves to neither server: a CDN, a proxy or a load
  balancer (Cloudflare's orange cloud among them) whose ORIGIN DNS cannot show.
  It may well still be the source — invariant 7 in all but name — so it blocks
  for every provider until the operator accepts that one host
  (``--accept-dns-host HOST``) after checking the origin.
* VANISHED — the host resolved at plan time and DNS now answers that it does
  not (NXDOMAIN, an empty answer): a deleted record (Coolify deletes a record it
  manages when a hostname loses its last holder, 4.4+), which Traefik's ACME
  would now burn validations on. Accepted per host too.
* UNANSWERED — the host resolved at plan time and DNS could not be asked now (a
  timeout, SERVFAIL): nothing is known about where it points, so it blocks, and
  a resume asks again. Accepted per host too.

An address BOTH servers answer as — a wildcard base they share, or one CDN in
front of both wildcards — proves neither server: a host that resolves only to
such addresses is ELSEWHERE, never READY or CUTOVER_NEEDED.

A host that did not resolve at plan time either stays UNRESOLVED and passes:
it serves nobody (the e2e rig's ``.invalid`` hosts).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum

from bg_coolify_migrate.dns.extract import Hostname, HostnameOrigin
from bg_coolify_migrate.dns.wildcard import under_wildcard


class Verdict(StrEnum):
    """What DNS says about one hostname."""

    READY = "ready"
    """Already resolves to the target. Nothing to do."""

    CUTOVER_NEEDED = "cutover_needed"
    """Still resolves to the source. A custom domain that must be repointed."""

    ELSEWHERE = "elsewhere"
    """Resolves to neither server — typically a CDN/proxy (Cloudflare's orange
    cloud) or a load balancer in front. DNS then tells us nothing about the
    origin, which may still be the source. Blocks until the operator accepts
    that host explicitly."""

    VANISHED = "vanished"
    """Resolved at plan time, and DNS now answers that it does not (NXDOMAIN, no
    records): a record was deleted between plan and start. Blocks until
    recreated, or accepted explicitly."""

    UNANSWERED = "unanswered"
    """Resolved at plan time, and DNS could not be asked now (a timeout, SERVFAIL,
    no reachable nameserver). That proves no deletion, but nothing proves the
    host left the source either. Blocks until a resume gets an answer, or the
    host is accepted explicitly."""

    UNRESOLVED = "unresolved"
    """NXDOMAIN or no answer, and no answer at plan time either. Not blocking:
    the domain is not serving anyone right now anyway."""

    GENERATED = "generated"
    """A Coolify-generated *.sslip.io style name that encodes the server IP and
    therefore follows the server. Never blocks."""

    SERVER_BOUND = "server_bound"
    """A URL under the SOURCE server's wildcard base (``pdf-tool.app.0046-20…``).
    The wildcard's DNS record binds it to that one server, so it cannot cut over
    — it is rewritten onto the TARGET server's wildcard at create time instead.
    The target answers on its own hostname, so this never gates."""


#: The verdicts that stop the start until DNS changes or the operator accepts.
BLOCKING: frozenset[Verdict] = frozenset(
    {Verdict.CUTOVER_NEEDED, Verdict.ELSEWHERE, Verdict.VANISHED, Verdict.UNANSWERED}
)


@dataclass(frozen=True, slots=True)
class Resolution:
    """The resolved facts for one hostname."""

    hostname: Hostname
    addresses: tuple[str, ...]
    ttl: int | None = None
    cname_chain: tuple[str, ...] = ()
    error: str | None = None
    nameservers: tuple[str, ...] = ()
    """The authoritative nameservers of the host's zone, by NAME — what tells a
    Cloudflare zone apart (``dns/managed.py``). Messages only, never gating."""


def is_definitive(resolution: Resolution) -> bool:
    """Whether a resolution is an ANSWER rather than a failure to get one.

    Addresses, NXDOMAIN or an empty answer are answers. A timeout or an
    unreachable nameserver says nothing about the record, so nothing that cannot
    be resumed may be decided on it.
    """
    if resolution.addresses:
        return True
    error = resolution.error or ""
    return "NXDOMAIN" in error or "no A/AAAA records" in error


@dataclass(frozen=True, slots=True)
class HostVerdict:
    """One hostname's verdict, with everything needed to explain it."""

    hostname: Hostname
    verdict: Verdict
    addresses: tuple[str, ...]
    ttl: int | None = None
    detail: str = ""
    baseline: tuple[str, ...] = ()
    """What the host resolved to at plan time (VANISHED says what to recreate)."""

    @property
    def blocks(self) -> bool:
        return self.verdict in BLOCKING


@dataclass(frozen=True, slots=True)
class DnsGateReport:
    """The gate's decision for a whole migration."""

    verdicts: tuple[HostVerdict, ...]
    source_ips: tuple[str, ...]
    target_ips: tuple[str, ...]

    @property
    def blocked(self) -> tuple[HostVerdict, ...]:
        return tuple(v for v in self.verdicts if v.blocks)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked)

    def unaccepted(
        self, *, accept_cutover: bool, accepted_hosts: Collection[str] = ()
    ) -> tuple[HostVerdict, ...]:
        """The blocking verdicts the operator has NOT answered.

        ``accept_cutover`` (``--accept-dns``) answers a plain cutover only — the
        host resolves to the source, and the operator chose to finish and flip
        it in parallel. ELSEWHERE, VANISHED and UNANSWERED say something is wrong
        with what DNS shows, or that it showed nothing, so only an explicit,
        per-host acceptance answers them.
        """
        return tuple(
            v
            for v in self.blocked
            if v.hostname.host not in accepted_hosts
            and not (accept_cutover and v.verdict is Verdict.CUTOVER_NEEDED)
        )

    @property
    def ambiguous(self) -> tuple[HostVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict is Verdict.ELSEWHERE)

    @property
    def vanished(self) -> tuple[HostVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict is Verdict.VANISHED)

    @property
    def unanswered(self) -> tuple[HostVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict is Verdict.UNANSWERED)

    @property
    def ready(self) -> tuple[HostVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict is Verdict.READY)

    @property
    def server_bound(self) -> tuple[HostVerdict, ...]:
        """URLs rewritten onto the target's wildcard — reported, never gating."""
        return tuple(v for v in self.verdicts if v.verdict is Verdict.SERVER_BOUND)

    def cutover_checklist(
        self,
        verdicts: tuple[HostVerdict, ...] | None = None,
        *,
        cloudflare_hosts: Collection[str] = (),
    ) -> list[str]:
        """Actionable instructions, not a diagnosis.

        The operator needs to know which record to change, to what, and how long
        the old answer will linger. A long TTL is the difference between a
        five-minute cutover and an hour of split traffic. ``cloudflare_hosts``
        (zone on Cloudflare, Coolify >= 4.4) get the one place they can be
        changed: Coolify never re-points a record it manages.
        """
        lines: list[str] = []
        # Never an address the source answers as too: a record pointed there
        # would reach the source as much as the target.
        shared = set(self.source_ips) & set(self.target_ips)
        own = [ip for ip in self.target_ips if ip not in shared]
        target = own[0] if own else "<target ip>"
        for verdict in self.blocked if verdicts is None else verdicts:
            host = verdict.hostname.host
            ttl = f" (TTL {verdict.ttl}s)" if verdict.ttl else ""
            current = ", ".join(verdict.addresses) or "?"
            if verdict.verdict is Verdict.ELSEWHERE:
                where = (
                    "an address both servers answer as"
                    if shared & set(verdict.addresses)
                    else "neither server"
                )
                line = (
                    f"{host}: resolves to {current}, {where} - make sure its origin "
                    f"(CDN, proxy, load balancer) is {target}, then accept it: "
                    f"--accept-dns-host {host}"
                )
            elif verdict.verdict is Verdict.VANISHED:
                was = ", ".join(verdict.baseline) or "?"
                line = (
                    f"{host}: resolved to {was} at plan time, now to nothing - recreate the "
                    f"record -> {target}, or accept it: --accept-dns-host {host}"
                )
            elif verdict.verdict is Verdict.UNANSWERED:
                was = ", ".join(verdict.baseline) or "?"
                line = (
                    f"{host}: resolved to {was} at plan time, and DNS could not be asked "
                    f"now - resume to ask again; if it does point at {target}, accept it: "
                    f"--accept-dns-host {host}"
                )
            else:
                line = f"{host}: {current} -> {target}{ttl}"
            if host in cloudflare_hosts:
                line += (
                    " [Cloudflare zone: change the record at Cloudflare; Coolify cannot "
                    "re-point a record it manages - for one it does not, the target's "
                    "Domains > Configure DNS > Replace record works]"
                )
            lines.append(line)
        return lines

    @property
    def max_ttl(self) -> int:
        return max((v.ttl or 0 for v in self.blocked), default=0)


def classify(
    resolution: Resolution,
    *,
    source_ips: frozenset[str],
    target_ips: frozenset[str],
    source_wildcard: str | None = None,
    baseline: tuple[str, ...] = (),
) -> HostVerdict:
    """Decide what one hostname's DNS state means. PURE.

    The ordering matters: a server-bound URL (under the source server's
    wildcard) and a generated hostname are both decided before DNS is even
    consulted, because each encodes the server and would otherwise look like
    ``CUTOVER_NEEDED`` forever — the first is rewritten onto the target's
    wildcard, the second follows the server by its embedded IP. ``baseline`` is
    what the host resolved to at plan time (``()`` when it did not).
    """
    if source_wildcard and under_wildcard(resolution.hostname.host, source_wildcard):
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.SERVER_BOUND,
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            detail="under the source server's wildcard; rewritten onto the target's wildcard",
        )

    if resolution.hostname.is_generated:
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.GENERATED,
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            detail="Coolify-generated hostname; it encodes the server IP and follows the server",
        )

    if resolution.error or not resolution.addresses:
        if baseline and not is_definitive(resolution):
            # A timeout or SERVFAIL is no answer: calling it a deleted record sent
            # operators to recreate one that still exists. It blocks all the same
            # — the source is stopped by now, and nothing proves the host left it.
            return HostVerdict(
                hostname=resolution.hostname,
                verdict=Verdict.UNANSWERED,
                addresses=(),
                ttl=resolution.ttl,
                detail=(
                    f"resolved to {', '.join(baseline)} at plan time; DNS could not be "
                    f"asked now ({resolution.error or 'no answer'}) - a resume asks again"
                ),
                baseline=baseline,
            )
        if baseline:
            return HostVerdict(
                hostname=resolution.hostname,
                verdict=Verdict.VANISHED,
                addresses=(),
                ttl=resolution.ttl,
                detail=(
                    f"resolved to {', '.join(baseline)} at plan time, now to nothing "
                    f"({resolution.error or 'no A/AAAA records'}) - its record was deleted "
                    "or its zone broke since"
                ),
                baseline=baseline,
            )
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.UNRESOLVED,
            addresses=(),
            ttl=resolution.ttl,
            detail=resolution.error or "no A/AAAA records",
        )

    addresses = set(resolution.addresses)
    # An address both servers answer as — a wildcard base they share, or one CDN
    # in front of both wildcards — tells them apart no more than a CDN does.
    # Counted as either, it read as READY first, and a host still on the source
    # passed the gate.
    shared = source_ips & target_ips
    on_target = addresses & (target_ips - shared)
    on_source = addresses & (source_ips - shared)

    if on_target:
        # Already pointing at the target. If it ALSO points at the source we are
        # mid-cutover with both live, which is fine to proceed from — the target
        # is what we are about to start.
        detail = "resolves to the target"
        if on_source:
            detail = "resolves to BOTH source and target (cutover in progress)"
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.READY,
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            detail=detail,
        )

    if on_source:
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.CUTOVER_NEEDED,
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            detail="still resolves to the source server",
        )

    if addresses & shared:
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.ELSEWHERE,
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            detail=(
                "resolves only to an address BOTH servers answer as — a wildcard base "
                "they share, or one CDN in front of both. DNS cannot tell which one it "
                "reaches, and it may still be the source: check it, then accept this host."
            ),
        )

    return HostVerdict(
        hostname=resolution.hostname,
        verdict=Verdict.ELSEWHERE,
        addresses=resolution.addresses,
        ttl=resolution.ttl,
        detail=(
            "resolves to neither source nor target — a CDN, reverse proxy or load "
            "balancer (e.g. Cloudflare) in front. DNS cannot tell us where the origin "
            "points, and it may still be the source: check it, then accept this host."
        ),
    )


def build_report(
    resolutions: list[Resolution],
    *,
    source_ips: frozenset[str],
    target_ips: frozenset[str],
    source_wildcard: str | None = None,
    baselines: Mapping[str, tuple[str, ...]] | None = None,
) -> DnsGateReport:
    """Classify every hostname into one report."""
    verdicts = tuple(
        classify(
            r,
            source_ips=source_ips,
            target_ips=target_ips,
            source_wildcard=source_wildcard,
            baseline=(baselines or {}).get(r.hostname.host, ()),
        )
        for r in resolutions
    )
    return DnsGateReport(
        verdicts=verdicts,
        source_ips=tuple(sorted(source_ips)),
        target_ips=tuple(sorted(target_ips)),
    )


def elsewhere_note(
    baseline: Mapping[str, tuple[str, ...]],
    *,
    source_ips: frozenset[str],
    target_ips: frozenset[str],
) -> str | None:
    """The plan's heads-up for hosts that ALREADY resolve to neither server. PURE.

    ``baseline`` is ``{host: plan-time addresses}``. Each such host will be
    ELSEWHERE at the gate, which runs after the source is stopped — knowing now
    lets the operator check the origin and accept the host on the run itself.
    An address both servers answer as counts for neither, as in :func:`classify`.
    """
    either = source_ips ^ target_ips
    hosts = sorted(
        host for host, addresses in baseline.items() if addresses and not set(addresses) & either
    )
    if not hosts:
        return None
    return (
        f"{', '.join(hosts)} resolve(s) to neither server today (or only to an address "
        "both answer as) - a CDN, proxy or load "
        "balancer in front? DNS cannot show its origin, so the DNS gate will stop before "
        "the target starts until each is accepted by name: check that its origin will "
        "point at the target, then pass --accept-dns-host HOST (on run, or on resume)."
    )


def bound_to_source(
    hostnames: list[Hostname], *, source_wildcard: str | None, target_wildcard: str | None
) -> list[Hostname]:
    """TARGET hosts still under the SOURCE server's wildcard. PURE.

    Each was never remapped: its wildcard record binds it to the source, so the
    target would serve — and request a certificate for — a hostname that can only
    ever resolve to the old server. Invariant 7, not a judgement and not a
    cutover: no DNS change fixes it. A host also under the TARGET's wildcard is
    exempt (both servers share it). ``service_env`` rows are exempt as well: they
    are derived copies, report-only as before, never what the proxy routes.
    """
    return [
        h
        for h in hostnames
        if h.origin is not HostnameOrigin.SERVICE_ENV
        and under_wildcard(h.host, source_wildcard)
        and not under_wildcard(h.host, target_wildcard)
    ]


def explain_why_blocking_matters() -> str:
    """The message shown when the gate blocks.

    Deliberately explains the mechanism rather than just saying "blocked". An
    operator who understands *why* will not reach for a --force flag (there
    isn't one), and will not misread the gate as a bug.
    """
    return (
        "Starting the target now would be actively harmful, not merely premature:\n"
        "  • Traefik on the new host would request an ACME certificate, but the\n"
        "    HTTP-01 challenge is routed BY DNS — to the OLD host, which knows\n"
        "    nothing about it. The challenge fails.\n"
        "  • Let's Encrypt rate-limits 5 failed validations per hostname per hour.\n"
        "    A retry loop burns that budget, so even a correct cutover later cannot\n"
        "    obtain a certificate for a while.\n"
        "  • Two proxies would claim the same Host() rule, and which one answers\n"
        "    depends on where the request happens to land.\n"
        "\n"
        "Nothing is lost: the target is created and its data is verified. Flip DNS,\n"
        "then run `coolify-migrate resume <id>` to continue from here."
    )
