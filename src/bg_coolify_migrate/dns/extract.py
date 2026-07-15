"""Hostname extraction.

PURE module: no IO.

Finding every hostname a stack answers on is harder than it looks, because
Coolify spreads them across five different places and a missed one means the DNS
gate passes while a live domain still points at the old server:

1. ``applications.fqdn`` — comma-separated, may carry paths and schemes.
2. ``applications.docker_compose_domains`` — JSON, per compose service.
3. ``SERVICE_FQDN_*`` / ``SERVICE_URL_*`` magic env vars on services.
4. Raw Traefik router rules in ``custom_labels`` and inside compose labels:
   ``traefik.http.routers.x.rule=Host(`a.example.com`)``. These are hidden from
   the API without ``read:sensitive``, which is one more reason the tool demands
   that scope.
5. Caddy labels — Coolify supports both proxies.

Generated vs real
-----------------
Coolify auto-generates ``*.sslip.io`` style hostnames for resources with no real
domain. Those resolve to whatever IP is embedded in the name, so they move with
the server and must NOT gate a migration. Classifying them as generated is what
stops the gate from blocking on a domain nobody uses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

#: Host(`a.com`) / Host(`a.com`, `b.com`) — backticks are Traefik's own quoting.
_TRAEFIK_HOST_RE = re.compile(r"Host(?:SNI)?\(([^)]*)\)", re.IGNORECASE)
_BACKTICKED_RE = re.compile(r"`([^`]+)`")
#: Traefik also accepts single/double quotes in some configurations.
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")

#: Coolify's generated wildcard-DNS domains. These encode the server IP in the
#: hostname itself, so they follow the server and cannot gate a cutover.
_GENERATED_SUFFIXES = (".sslip.io", ".nip.io", ".traefik.me", ".localhost")

_SERVICE_FQDN_RE = re.compile(r"^SERVICE_(?:FQDN|URL)_", re.IGNORECASE)


class HostnameOrigin(StrEnum):
    """Where a hostname was found. Kept for the report, so an operator can see
    which knob to turn."""

    FQDN = "fqdn"
    COMPOSE_DOMAINS = "docker_compose_domains"
    SERVICE_ENV = "service_env"
    TRAEFIK_LABEL = "traefik_label"
    CADDY_LABEL = "caddy_label"


@dataclass(frozen=True, slots=True)
class Hostname:
    """One hostname a stack answers on."""

    host: str
    origin: HostnameOrigin
    is_generated: bool

    def __str__(self) -> str:
        return self.host


def normalise_host(raw: str) -> str | None:
    """Reduce a URL-ish string to a bare hostname.

    Coolify stores fqdn values that may be ``https://a.example.com/path``,
    ``a.example.com:8080`` or just ``a.example.com``. Returns ``None`` for
    anything that is not a usable hostname (empty, a wildcard, a bare path).
    """
    value = raw.strip()
    if not value:
        return None
    value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
    value = value.split("/", 1)[0]
    value = value.split("?", 1)[0]
    # Strip a port, but not an IPv6 literal's colons.
    if not value.startswith("[") and value.count(":") == 1:
        value = value.split(":", 1)[0]
    value = value.strip().rstrip(".").lower()
    if not value or value.startswith("*"):
        return None
    if not re.match(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", value):
        return None
    if "." not in value:
        return None
    return value


def is_generated(host: str) -> bool:
    """Whether Coolify generated this hostname rather than the operator choosing it.

    Generated hostnames encode the server IP (``1-2-3-4.sslip.io``), so they
    follow the server automatically and must never block a cutover.
    """
    return host.endswith(_GENERATED_SUFFIXES)


def _mk(host: str, origin: HostnameOrigin) -> Hostname | None:
    normalised = normalise_host(host)
    if normalised is None:
        return None
    return Hostname(host=normalised, origin=origin, is_generated=is_generated(normalised))


def from_fqdn(fqdn: str | None) -> list[Hostname]:
    """Parse ``applications.fqdn`` / ``service_applications.fqdn``.

    Comma-separated, entries may be full URLs.
    """
    if not fqdn:
        return []
    out = []
    for part in fqdn.split(","):
        host = _mk(part, HostnameOrigin.FQDN)
        if host:
            out.append(host)
    return out


def from_compose_domains(raw: str | None) -> list[Hostname]:
    """Parse ``applications.docker_compose_domains`` (JSON).

    Shape is ``{"service": {"domain": "https://a.com,https://b.com"}}``. Tolerant
    of the alternative list form, and of junk — a malformed value must not crash
    the gate, but it must also not silently yield "no domains", so the caller
    treats an unparseable value as a warning.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    hosts: list[Hostname] = []

    def _collect(value: object) -> None:
        if isinstance(value, str):
            for part in value.split(","):
                host = _mk(part, HostnameOrigin.COMPOSE_DOMAINS)
                if host:
                    hosts.append(host)
        elif isinstance(value, dict):
            for inner in value.values():
                _collect(inner)
        elif isinstance(value, list):
            for inner in value:
                _collect(inner)

    _collect(parsed)
    return hosts


def from_env(entries: list[dict[str, object]]) -> list[Hostname]:
    """Extract hostnames from ``SERVICE_FQDN_*`` / ``SERVICE_URL_*`` variables.

    Coolify auto-generates these for services. They are how a compose service
    learns its own public URL, and they are frequently the ONLY place a service's
    domain is recorded — ``services`` has no ``fqdn`` column of its own.
    """
    out: list[Hostname] = []
    for entry in entries:
        key = str(entry.get("key", ""))
        if not _SERVICE_FQDN_RE.match(key):
            continue
        value = entry.get("value") or entry.get("real_value")
        if not isinstance(value, str):
            continue
        for part in value.split(","):
            host = _mk(part, HostnameOrigin.SERVICE_ENV)
            if host:
                out.append(host)
    return out


def from_traefik_rule(rule: str) -> list[Hostname]:
    """Extract hostnames from a Traefik router rule.

    Handles ``Host(`a`)``, ``Host(`a`, `b`)``, ``HostSNI(`a`)`` and rules joined
    with ``&&`` / ``||``.
    """
    out: list[Hostname] = []
    for match in _TRAEFIK_HOST_RE.finditer(rule):
        inner = match.group(1)
        candidates = _BACKTICKED_RE.findall(inner) or _QUOTED_RE.findall(inner)
        if not candidates:
            candidates = [p.strip() for p in inner.split(",")]
        for candidate in candidates:
            host = _mk(candidate, HostnameOrigin.TRAEFIK_LABEL)
            if host:
                out.append(host)
    return out


def from_labels(labels: dict[str, str]) -> list[Hostname]:
    """Extract hostnames from container labels (Traefik and Caddy).

    Works on labels read from ``docker inspect``, which is how we see hostnames
    the API would hide — and how we see them for a stack whose compose we never
    parsed.
    """
    out: list[Hostname] = []
    for key, value in labels.items():
        lowered = key.lower()
        if lowered.startswith("traefik.") and lowered.endswith(".rule"):
            out.extend(from_traefik_rule(value))
        elif lowered == "caddy" or lowered.startswith("caddy_"):
            for part in value.split():
                host = _mk(part, HostnameOrigin.CADDY_LABEL)
                if host:
                    out.append(host)
    return out


def collect(
    *,
    fqdn: str | None = None,
    compose_domains: str | None = None,
    envs: list[dict[str, object]] | None = None,
    labels: dict[str, str] | None = None,
    extra_labels: str | None = None,
) -> list[Hostname]:
    """Every hostname from every source, deduplicated.

    Dedup keeps the first origin seen, which is stable because the sources are
    consulted in a fixed order. The report shows where each was found so an
    operator knows which field to change.
    """
    found: list[Hostname] = []
    found.extend(from_fqdn(fqdn))
    found.extend(from_compose_domains(compose_domains))
    found.extend(from_env(envs or []))
    found.extend(from_labels(labels or {}))
    if extra_labels:
        for line in extra_labels.splitlines():
            if "rule" in line.lower() and "host" in line.lower():
                found.extend(from_traefik_rule(line))

    seen: dict[str, Hostname] = {}
    for host in found:
        seen.setdefault(host.host, host)
    return sorted(seen.values(), key=lambda h: h.host)


def real_hostnames(hosts: list[Hostname]) -> list[Hostname]:
    """Only operator-chosen hostnames — the ones that can gate a cutover."""
    return [h for h in hosts if not h.is_generated]
