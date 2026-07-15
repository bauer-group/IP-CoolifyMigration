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
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bg_coolify_migrate.dns.extract import Hostname


class Verdict(StrEnum):
    """What DNS says about one hostname."""

    READY = "ready"
    """Already resolves to the target. Nothing to do."""

    CUTOVER_NEEDED = "cutover_needed"
    """Still resolves to the source. BLOCKS."""

    ELSEWHERE = "elsewhere"
    """Resolves to neither — typically a CDN/proxy (Cloudflare's orange cloud)
    in front. DNS then tells us nothing about the origin, so we cannot decide.
    Surfaced for a human rather than guessed at."""

    UNRESOLVED = "unresolved"
    """NXDOMAIN or no answer. Suspicious but not blocking: the domain is not
    serving anyone right now anyway."""

    GENERATED = "generated"
    """A Coolify-generated *.sslip.io style name that encodes the server IP and
    therefore follows the server. Never blocks."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """The resolved facts for one hostname."""

    hostname: Hostname
    addresses: tuple[str, ...]
    ttl: int | None = None
    cname_chain: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HostVerdict:
    """One hostname's verdict, with everything needed to explain it."""

    hostname: Hostname
    verdict: Verdict
    addresses: tuple[str, ...]
    ttl: int | None = None
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.verdict is Verdict.CUTOVER_NEEDED


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

    @property
    def ambiguous(self) -> tuple[HostVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict is Verdict.ELSEWHERE)

    @property
    def ready(self) -> tuple[HostVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict is Verdict.READY)

    def cutover_checklist(self) -> list[str]:
        """Actionable instructions, not a diagnosis.

        The operator needs to know which record to change, to what, and how long
        the old answer will linger. A long TTL is the difference between a
        five-minute cutover and an hour of split traffic.
        """
        lines: list[str] = []
        target = self.target_ips[0] if self.target_ips else "<target ip>"
        for verdict in self.blocked:
            ttl = f" (TTL {verdict.ttl}s)" if verdict.ttl else ""
            current = ", ".join(verdict.addresses) or "?"
            lines.append(f"{verdict.hostname.host}: {current} -> {target}{ttl}")
        return lines

    @property
    def max_ttl(self) -> int:
        return max((v.ttl or 0 for v in self.blocked), default=0)


def classify(
    resolution: Resolution,
    *,
    source_ips: frozenset[str],
    target_ips: frozenset[str],
) -> HostVerdict:
    """Decide what one hostname's DNS state means. PURE.

    The ordering matters: a generated hostname is decided before anything else,
    because it encodes the server IP and would otherwise look like
    ``CUTOVER_NEEDED`` forever.
    """
    if resolution.hostname.is_generated:
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.GENERATED,
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            detail="Coolify-generated hostname; it encodes the server IP and follows the server",
        )

    if resolution.error or not resolution.addresses:
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.UNRESOLVED,
            addresses=(),
            ttl=resolution.ttl,
            detail=resolution.error or "no A/AAAA records",
        )

    addresses = set(resolution.addresses)

    if addresses & target_ips:
        # Already pointing at the target. If it ALSO points at the source we are
        # mid-cutover with both live, which is fine to proceed from — the target
        # is what we are about to start.
        detail = "resolves to the target"
        if addresses & source_ips:
            detail = "resolves to BOTH source and target (cutover in progress)"
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.READY,
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            detail=detail,
        )

    if addresses & source_ips:
        return HostVerdict(
            hostname=resolution.hostname,
            verdict=Verdict.CUTOVER_NEEDED,
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            detail="still resolves to the source server",
        )

    return HostVerdict(
        hostname=resolution.hostname,
        verdict=Verdict.ELSEWHERE,
        addresses=resolution.addresses,
        ttl=resolution.ttl,
        detail=(
            "resolves to neither source nor target — likely a CDN or reverse proxy "
            "(e.g. Cloudflare) in front. DNS cannot tell us where the origin points; "
            "check the CDN's origin setting before starting the target."
        ),
    )


def build_report(
    resolutions: list[Resolution],
    *,
    source_ips: frozenset[str],
    target_ips: frozenset[str],
) -> DnsGateReport:
    """Classify every hostname into one report."""
    verdicts = tuple(
        classify(r, source_ips=source_ips, target_ips=target_ips) for r in resolutions
    )
    return DnsGateReport(
        verdicts=verdicts,
        source_ips=tuple(sorted(source_ips)),
        target_ips=tuple(sorted(target_ips)),
    )


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
