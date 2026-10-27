"""Parking a source's domains, and giving them back.

PURE module: no IO.

A custom domain (``shop.example.com``) is the same on source and target, and
Coolify 409s a create that asks for a domain another resource holds. So CREATE
parks the source's custom hosts under an ``old-<tag>.`` prefix — in Coolify's
database only — and journals the body that restores them. Everything that
decides *what* is parked, *what* a restore sends and *when* a restore may
override a conflict lives here; ``engine/`` only performs it.

Two upstream facts make the restore's timing matter (verified v4.1.2-v4.5.0):

* A domain PATCH only writes the database. It deploys nothing unless
  ``instant_deploy`` is sent (ApplicationsController v4.1.2:2660-2680,
  v4.5.0:3752-3793), and with read-only labels on — the default since migration
  2025_01_08 — it regenerates ``custom_labels`` from the new fqdn.
* Every deploy, ``/restart`` included, bakes the stored labels into the new
  container (ApplicationDeploymentJob v4.1.2:3099-3113, v4.5.0:4118-4132).

So a source restarted while its row still holds the parked hosts comes back on
``old-<tag>.`` hosts only, and no later PATCH changes the running container.
The rollback planner therefore restores the domains BEFORE it restarts the
source (``statemachine._PRECEDENCE``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from bg_coolify_migrate.dns.extract import normalise_host
from bg_coolify_migrate.dns.wildcard import under_wildcard
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from bg_coolify_migrate.domain.versions import Version, at_least

#: ``noindex_domains`` exists, and is in the application UPDATE ``$allowedFields``,
#: from v4.3.0 (ApplicationsController v4.3.0:2796, applied after the fqdn at
#: :3115-3120; v4.5.0:3363, :3764-3775).
NOINDEX_DOMAINS_SINCE: Version = (4, 3, 0)


def parking_tag(project: str, environment: str) -> str:
    """The ``<tag>`` in ``old-<tag>.<host>``: six hex digits of the project/env.

    The same digest ends every migration id of this project and environment
    (``runner.make_migration_id``), and it is deliberately STABLE across runs:
    that is what lets the planner recognise hosts an earlier run parked and
    never gave back (:func:`leftover_parking_refusals`).
    """
    return hashlib.sha256(f"{project}/{environment}".encode()).hexdigest()[:6]


def compose_domains(raw: object) -> dict[str, str]:
    """Reduce docker_compose_domains to ``{service: "url,url"}``.

    Coolify STORES and GETs this as a dict ``{service: {"domain": "url,url"}}``
    (a JSON string), though the create route wants a different shape. Tolerant of
    a raw string or an already-decoded dict, and of junk — a bad value yields
    ``{}``, not a crash.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for name, cfg in parsed.items():
        if isinstance(cfg, dict):
            out[str(name)] = str(cfg.get("domain") or "")
        elif isinstance(cfg, str):
            out[str(name)] = cfg
    return out


def park_hosts(domain_str: object, *, source_wildcard: str | None, tag: str) -> str:
    """Rename the CUSTOM hosts in a comma-separated domain string.

    A custom host (``speakup.bauer-group.com``) is the same on source and target,
    so creating the target 409s while the source still holds it. Prefixing an
    ``old-<tag>.`` marker frees the real host for the target; the source keeps
    serving the real one until quiesce (we do not redeploy it). Server-bound hosts
    are remapped onto the target's wildcard and never collide, so they pass
    through untouched.
    """
    if not domain_str:
        return ""
    out: list[str] = []
    for part in str(domain_str).split(","):
        stripped = part.strip()
        if not stripped:
            continue
        host = normalise_host(stripped)
        if host and not under_wildcard(host, source_wildcard):
            out.append(f"https://old-{tag}.{host}")
        else:
            out.append(stripped)
    return ",".join(out)


@dataclass(frozen=True)
class DomainParking:
    """What CREATE sends to free a source's domains, and what gives them back."""

    restore: dict[str, Any] | None
    """The body that restores the source's ORIGINAL domains. ``None`` only when
    the source has no domains at all. Present even when nothing is parked:
    FINALIZE(rename) blanks the domains on success, and a later rollback needs
    the originals."""

    park: dict[str, Any] | None
    """The body that parks the custom hosts. ``None`` when nothing would change
    (only server-bound hosts, which never collide)."""


def domain_parking(
    snapshot: ResourceSnapshot,
    source: Mapping[str, Any],
    *,
    source_wildcard: str | None,
    tag: str,
    version: Version | None = None,
) -> DomainParking:
    """Decide the park and restore bodies for one source resource.

    ``source`` is the resource GET. A dockercompose app carries its domains per
    compose service (``docker_compose_domains``; its ``domains`` field 422s),
    everything else in ``fqdn``. Services and databases have no top-level fqdn,
    so they are neither parked nor restored here.

    The restore body also carries ``noindex_domains`` when the source flags any
    (Coolify >= 4.3.0). Parking changes the fqdn, and the save hook's
    ``syncNoindexDomains`` drops the flags of domains the app no longer has
    (Application.php v4.3.0:280, HasNoindexDomains v4.3.0:49), so restoring the
    domains alone would bring them back indexable. The update applies the flags AFTER the fqdn and keeps only those
    for domains the app then has, so one PATCH restores both.
    """
    restore: dict[str, Any]
    park: dict[str, Any] | None
    if snapshot.kind is ResourceKind.APP_GIT_COMPOSE:
        parsed = compose_domains(source.get("docker_compose_domains"))
        if not parsed:
            return DomainParking(restore=None, park=None)
        original = [{"name": name, "domain": domain} for name, domain in parsed.items()]
        parked = [
            {"name": name, "domain": park_hosts(domain, source_wildcard=source_wildcard, tag=tag)}
            for name, domain in parsed.items()
        ]
        restore = {"docker_compose_domains": original}
        park = {"docker_compose_domains": parked} if parked != original else None
    else:
        original_fqdn = str(source.get("fqdn") or "")
        if not original_fqdn:
            return DomainParking(restore=None, park=None)
        parked_fqdn = park_hosts(original_fqdn, source_wildcard=source_wildcard, tag=tag)
        restore = {"domains": original_fqdn}
        park = {"domains": parked_fqdn} if parked_fqdn != original_fqdn else None

    noindex = _noindex_domains(source.get("noindex_domains"))
    if noindex and at_least(version, NOINDEX_DOMAINS_SINCE):
        restore["noindex_domains"] = noindex
    return DomainParking(restore=restore, park=park)


def _noindex_domains(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [d for d in raw if isinstance(d, str) and d.strip()]


def leftover_parked_hosts(fqdn: object, compose_domains_raw: object, tag: str) -> list[str]:
    """Hosts still carrying THIS project's parking prefix, ``old-<tag>.``.

    Only the exact prefix counts: ``old-shop.example.com`` is somebody's real
    domain, not ours.
    """
    prefix = f"old-{tag}."
    domain_strings = [str(fqdn or ""), *compose_domains(compose_domains_raw).values()]
    hosts: list[str] = []
    for domain_str in domain_strings:
        for part in domain_str.split(","):
            host = normalise_host(part)
            if host and host.startswith(prefix):
                hosts.append(host)
    return list(dict.fromkeys(hosts))


def leftover_parking_refusals(source: Mapping[str, Any], *, name: str, tag: str) -> list[str]:
    """Refuse a source whose domains an earlier migration parked and never gave back.

    Not a judgement call. The tag is stable per project/environment, so a run
    would read ``old-<tag>.shop`` as the source's domain: it would park it again
    (``old-<tag>.old-<tag>.shop``), journal the PARKED host as the one to
    restore, and create the target on it — the real domain then exists in no
    record this tool can see. Checks applications (``fqdn``, compose domains)
    and a service's applications alike.
    """
    found = leftover_parked_hosts(source.get("fqdn"), source.get("docker_compose_domains"), tag)
    for app in source.get("applications") or []:
        if isinstance(app, Mapping):
            found.extend(leftover_parked_hosts(app.get("fqdn"), None, tag))
    if not found:
        return []
    prefix = f"old-{tag}."
    real = []
    for host in dict.fromkeys(found):
        while host.startswith(prefix):
            host = host[len(prefix) :]
        real.append(host)
    return [
        f"{name} still answers on {', '.join(dict.fromkeys(found))}, parked by an earlier "
        f"migration of this project that never gave it back. Set its domain back to "
        f"{', '.join(dict.fromkeys(real))} in Coolify and redeploy, then migrate — a run "
        "now would park the parked host again and lose the real one."
    ]


# ── restoring ────────────────────────────────────────────────────────────────


def may_force_domain_restore(conflict_body: object, own_uuids: Collection[str]) -> bool:
    """Whether a restore that 409'd may be retried with ``force_domain_override``.

    Only when EVERY holder Coolify names is a resource this migration created.
    The conflict check is team-wide and excludes only the resource being updated
    (domains.php v4.1.2:142-184, v4.5.0:250-310), so a target that still exists
    — or, before v4.3.11, one whose DELETE is still queued — makes the source's
    own restore 409. Overriding THAT is correct: the domain is the source's, and
    the target is being torn down.

    Anything else is not ours to take. A third-party resource, the instance's own
    fqdn (``resource_uuid`` null), or a body we cannot read means a real
    ambiguity, and the operator decides. A service conflict names the
    ServiceApplication's uuid, not the service's (v4.5.0:304-310), so callers
    include target services' sub-application uuids in ``own_uuids``.
    """
    if not isinstance(conflict_body, dict):
        return False
    conflicts = conflict_body.get("conflicts")
    if not isinstance(conflicts, list) or not conflicts:
        return False
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            return False
        holder = conflict.get("resource_uuid")
        if not isinstance(holder, str) or holder not in own_uuids:
            return False
    return True


def conflict_holders(conflict_body: object) -> list[str]:
    """``name/uuid`` of every resource a 409 names, for the operator."""
    if not isinstance(conflict_body, dict):
        return []
    conflicts = conflict_body.get("conflicts")
    if not isinstance(conflicts, list):
        return []
    out: list[str] = []
    for conflict in conflicts:
        if isinstance(conflict, dict):
            out.append(f"{conflict.get('resource_name')}/{conflict.get('resource_uuid')}")
    return out


def restore_summary(body: Mapping[str, Any]) -> str:
    """The domains a restore body asks for, as one line an operator can retype.

    Domains are not secrets, and the operator needs exactly these to set them
    back by hand when the restore fails.
    """
    if "domains" in body:
        return str(body.get("domains") or "")
    for key in ("docker_compose_domains", "urls"):
        entries = body.get(key)
        if isinstance(entries, list):
            return ", ".join(
                f"{e.get('name')}={e.get('domain') or e.get('url') or ''}"
                for e in entries
                if isinstance(e, dict)
            )
    return "?"


def dns_rollback_note(
    dns_gate: Mapping[str, Any],
    *,
    source_ip: str,
    source_wildcard: str | None,
    target_wildcard: str | None,
) -> str | None:
    """What a rollback after DNS_GATE cannot do for the operator, or ``None``.

    ``dns_gate`` is that step's journalled detail. Its ``ready`` hosts already
    resolved to the target, and its ``cutover_accepted`` hosts were to be cut
    over in parallel. The rollback gives the source its domains back and
    restarts it, but DNS still sends their traffic to the target it removed —
    so the custom domain stays dead, and the source's proxy fails its ACME
    challenge, until the records point back. Server-bound hosts (under either
    server's wildcard) never moved by DNS and are left out.
    """
    hosts: set[str] = set()
    for key in ("ready", "cutover_accepted"):
        listed = dns_gate.get(key)
        if not isinstance(listed, list):
            continue
        for host in listed:
            if not isinstance(host, str) or not host:
                continue
            if under_wildcard(host, source_wildcard) or under_wildcard(host, target_wildcard):
                continue
            hosts.add(host)
    if not hosts:
        return None
    return (
        f"point DNS for {', '.join(sorted(hosts))} back to {source_ip}: the source "
        "serves them again, but DNS still sends their traffic to the target this "
        "rollback removed"
    )
