"""A source's domains on their way to the target — and back on a rollback.

PURE module: no IO.

Make-before-break, for applications and services alike
-------------------------------------------------------
A custom domain (``shop.example.com``) is the same on source and target. The
source keeps holding it until FINALIZE; the TARGET claims it first, with
``force_domain_override`` sent only when every holder Coolify names in the 409
is this migration's own (:func:`only_own_holders`). The source lets go only at
FINALIZE, as its last mutation, and a rollback after that gives it back BEFORE
the target that holds it is deleted.

Why the order is load-bearing (Coolify managed DNS, v4.4.0..v4.5.0): removing a
hostname from a resource queues a release job (ApplicationsController
v4.4.0:3252,3595, v4.5.0:3361,3784 -> ManagedDnsRecordCleanup::
queueReleaseOfRemovedHostnames v4.4.0/v4.5.0:76-93), and so does deleting a
resource that references a record (Traits/ReleasesManagedDnsRecords). The job
DELETES a Coolify-owned Cloudflare record when no live application, preview or
service application of the team HOLDS the hostname in ``fqdn`` or
``docker_compose_domains`` at the moment it runs, and only re-references it
otherwise (ManagedDnsRecordCleanup v4.4.0:229-285, v4.5.0:294-350; holders
v4.5.0:358-398). Parking the source before the target held the host — what
this tool did up to 2.9 — raced that job and could delete the record of a live
site before QUIESCE. With the hand-over below some live resource holds every
custom host at every instant, so Coolify can only ever re-reference the record,
never delete it.

Two upstream facts make a rollback's restore order matter as well (v4.1.2-v4.5.0):

* A domain PATCH only writes the database. It deploys nothing unless
  ``instant_deploy`` is sent (ApplicationsController v4.1.2:2660-2680,
  v4.5.0:3752-3793), and with read-only labels on — the default since migration
  2025_01_08 — it regenerates ``custom_labels`` from the new fqdn.
* Every deploy, ``/restart`` included, bakes the stored labels into the new
  container (ApplicationDeploymentJob v4.1.2:3099-3113, v4.5.0:4118-4132).

So the rollback planner restores the domains BEFORE it restarts the source
(``statemachine._PRECEDENCE``), and deletes a target holding custom hosts only
once its source holds them again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from bg_coolify_migrate.dns.extract import is_generated, normalise_host, route_env_keys
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

    Versions up to 2.9 parked a source's custom hosts under this prefix. The same
    digest ends every migration id of this project and environment
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


# ── which hosts are contested ────────────────────────────────────────────────


def _parts(domain_str: object) -> list[str]:
    return [p.strip() for p in str(domain_str or "").split(",") if p.strip()]


def is_custom(host: str, *, source_wildcard: str | None) -> bool:
    """A host source and target would BOTH hold: neither generated nor server-bound.

    A server-bound host (under the source server's wildcard) is rewritten onto
    the target's wildcard, and a generated one (``*.sslip.io``) names the source
    server's IP and is not carried — so neither is ever claimed by both sides.
    """
    return not is_generated(host) and not under_wildcard(host, source_wildcard)


def custom_hosts(domain_str: object, *, source_wildcard: str | None) -> list[str]:
    """The custom hosts in a comma-separated domain string, deduplicated, in order."""
    out: list[str] = []
    for part in _parts(domain_str):
        host = normalise_host(part)
        if host and is_custom(host, source_wildcard=source_wildcard):
            out.append(host)
    return list(dict.fromkeys(out))


def without_custom(domain_str: object, *, source_wildcard: str | None) -> str:
    """``domain_str`` without its custom hosts — server-bound and generated parts kept."""
    return ",".join(
        part
        for part in _parts(domain_str)
        if not ((host := normalise_host(part)) and is_custom(host, source_wildcard=source_wildcard))
    )


def without_generated(domain_str: object) -> str:
    """``domain_str`` without Coolify-generated (sslip.io-style) hosts.

    A generated host is ``{name}-{uuid}.{server-ip}.sslip.io`` (shared.php
    v4.5.0:1494-1522): it names the SOURCE's uuid and IP, so on the target it
    would route a hostname that resolves to the source. The target generates
    its own instead.
    """
    return ",".join(
        part for part in _parts(domain_str) if not ((host := normalise_host(part)) and is_generated(host))
    )


def resource_custom_hosts(
    snapshot: ResourceSnapshot, source: Mapping[str, Any], *, source_wildcard: str | None
) -> list[str]:
    """Every custom host an APPLICATION source holds (fqdn or compose domains)."""
    if snapshot.kind is ResourceKind.APP_GIT_COMPOSE:
        values: Iterable[str] = compose_domains(source.get("docker_compose_domains")).values()
    else:
        values = [str(source.get("fqdn") or "")]
    out: list[str] = []
    for value in values:
        out.extend(custom_hosts(value, source_wildcard=source_wildcard))
    return list(dict.fromkeys(out))


def source_custom_hosts(
    snapshot: ResourceSnapshot,
    source: Mapping[str, Any],
    envs: Iterable[Mapping[str, Any]] = (),
    *,
    source_wildcard: str | None,
) -> list[str]:
    """Every custom host a source serves — what DNS must follow it to the target.

    An application's ``fqdn`` or compose domains; a service's sub-application
    ``url``/``fqdn`` and the route-key rows of the SPECIFIC_SERVICES images it
    runs (``envs`` is its env list). Databases serve none.
    """
    if snapshot.collection == "applications":
        return resource_custom_hosts(snapshot, source, source_wildcard=source_wildcard)
    if snapshot.collection != "services":
        return []
    values: list[str] = []
    for app in source.get("applications") or []:
        if isinstance(app, Mapping):
            values.extend(str(app.get(k) or "") for k in ("url", "fqdn"))
    keys = route_env_keys(source.get("applications"))
    values.extend(
        str(env.get("value") or "")
        for env in envs
        if str(env.get("key") or "") in keys and isinstance(env.get("value"), str)
    )
    out: list[str] = []
    for value in values:
        out.extend(custom_hosts(value, source_wildcard=source_wildcard))
    return list(dict.fromkeys(out))


# ── what a resource holds ────────────────────────────────────────────────────


def held_hosts(resource: Mapping[str, Any]) -> set[str]:
    """Every host a resource GET says it holds — what Coolify's holder check reads.

    ``fqdn`` and ``docker_compose_domains`` of an application, the ``url`` /
    ``fqdn`` of each of a service's applications (ManagedDnsRecordCleanup
    ::hostnamesOf v4.5.0:378-398 reads exactly these columns).
    """
    hosts: set[str] = set()
    values = [str(resource.get("fqdn") or ""), *compose_domains(resource.get("docker_compose_domains")).values()]
    for app in resource.get("applications") or []:
        if isinstance(app, Mapping):
            values.append(str(app.get("url") or ""))
            values.append(str(app.get("fqdn") or ""))
    for value in values:
        for part in _parts(value):
            host = normalise_host(part)
            if host:
                hosts.add(host)
    return hosts


def body_hosts(body: Mapping[str, Any]) -> list[str]:
    """Every host a journalled restore body names, deduplicated, in order."""
    values: list[str] = []
    if "domains" in body:
        values.append(str(body.get("domains") or ""))
    for key, field in (("docker_compose_domains", "domain"), ("urls", "url")):
        entries = body.get(key)
        if isinstance(entries, list):
            values.extend(str(e.get(field) or "") for e in entries if isinstance(e, Mapping))
    out: list[str] = []
    for value in values:
        out.extend(h for h in (normalise_host(p) for p in _parts(value)) if h)
    return list(dict.fromkeys(out))


def missing_custom_hosts(
    body: Mapping[str, Any], resource: Mapping[str, Any], *, source_wildcard: str | None
) -> list[str]:
    """The custom hosts of a restore body that ``resource`` does NOT hold now."""
    held = held_hosts(resource)
    return [
        h
        for h in body_hosts(body)
        if is_custom(h, source_wildcard=source_wildcard) and h not in held
    ]


# ── restoring ────────────────────────────────────────────────────────────────


def restore_body(
    snapshot: ResourceSnapshot,
    source: Mapping[str, Any],
    *,
    version: Version | None = None,
) -> dict[str, Any] | None:
    """The body that gives an APPLICATION back its original domains, or ``None``.

    Journalled by CREATE before any write for the resource. Before FINALIZE the
    source never lost a host, so a rollback finds nothing to send
    (:func:`restore_patch`); after FINALIZE released its custom hosts this is
    what puts them back. ``source`` is the resource GET: a dockercompose app
    carries its domains per compose service (``docker_compose_domains``; its
    ``domains`` field 422s), every other application in ``fqdn``. Services and
    databases have no top-level fqdn — a service's body is built from its
    sub-applications (``api/resources.service_restore_body``).

    The body also carries ``noindex_domains`` when the source flags any (Coolify
    >= 4.3.0). The release changes the fqdn, and the save hook's
    ``syncNoindexDomains`` drops the flags of domains the app no longer has
    (Application.php v4.3.0:280, HasNoindexDomains v4.3.0:49), so restoring the
    domains alone would bring them back indexable. The update applies the flags
    AFTER the fqdn and keeps only those for domains the app then has, so one
    PATCH restores both.
    """
    body: dict[str, Any]
    if snapshot.kind is ResourceKind.APP_GIT_COMPOSE:
        parsed = compose_domains(source.get("docker_compose_domains"))
        if not parsed:
            return None
        body = {"docker_compose_domains": [{"name": n, "domain": d} for n, d in parsed.items()]}
    elif snapshot.collection == "applications":
        original = str(source.get("fqdn") or "")
        if not original:
            return None
        body = {"domains": original}
    else:
        return None
    noindex = _noindex_domains(source.get("noindex_domains"))
    if noindex and at_least(version, NOINDEX_DOMAINS_SINCE):
        body["noindex_domains"] = noindex
    return body


def _noindex_domains(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [d for d in raw if isinstance(d, str) and d.strip()]


def url_key(value: object) -> tuple[str, ...]:
    """A URL list in comparable form: Coolify lowercases and strips the slash."""
    return tuple(sorted(p.rstrip("/").lower() for p in _parts(value)))


def restore_patch(body: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any] | None:
    """What an application's restore must PATCH, or ``None`` when nothing differs.

    ``current`` is the source's GET. Not writing when the source already holds
    its domains matters: before FINALIZE that is every rollback, and a PATCH
    would queue Coolify's release machinery for nothing (and, on a live source,
    re-render its labels). The whole journalled body goes out once anything
    differs — the noindex flags ride along.
    """
    if "domains" in body:
        if url_key(body.get("domains")) == url_key(current.get("fqdn")):
            return None
        return dict(body)
    entries = body.get("docker_compose_domains")
    if isinstance(entries, list):
        now = compose_domains(current.get("docker_compose_domains"))
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if url_key(entry.get("domain")) != url_key(now.get(str(entry.get("name")), "")):
                return dict(body)
        return None
    return None


def restore_summary(body: Mapping[str, Any]) -> str:
    """The domains a restore body asks for, as one line an operator can retype.

    Domains are not secrets, and the operator needs exactly these to set them
    back by hand when the restore fails.
    """
    if "domains" in body:
        return str(body.get("domains") or "")
    parts: list[str] = []
    for key, label in (("docker_compose_domains", "name"), ("urls", "name"), ("route_envs", "key")):
        entries = body.get(key)
        if isinstance(entries, list):
            parts.extend(
                f"{e.get(label)}={e.get('domain') or e.get('url') or ''}"
                for e in entries
                if isinstance(e, dict)
            )
    return ", ".join(parts) or "?"


# ── conflicts ────────────────────────────────────────────────────────────────


def _conflicts(conflict_body: object) -> list[Any] | None:
    if not isinstance(conflict_body, dict):
        return None
    conflicts = conflict_body.get("conflicts")
    return conflicts if isinstance(conflicts, list) else None


def only_own_holders(conflict_body: object, own_uuids: Collection[str]) -> bool:
    """Whether a domain 409 may be retried with ``force_domain_override``.

    The ONE rule for every claim and every restore: only when EVERY holder
    Coolify names is this migration's own — the plan's source resources, the
    sub-applications of its source services, the targets this run created and
    their sub-applications. The conflict check is team-wide and excludes only
    the resource being updated (domains.php v4.1.2:142-184, v4.5.0:250-310), so a
    target claiming its source's host 409s on the source, and a source taking
    its host back 409s on the target. Overriding THOSE is the hand-over.

    Anything else is not ours to take. A third-party resource, the instance's own
    fqdn (``resource_uuid`` null), or a body we cannot read means a real
    ambiguity, and the operator decides. A service conflict names the
    ServiceApplication's uuid, not the service's (v4.5.0:304-310), which is why
    sub-application uuids belong in ``own_uuids``.
    """
    conflicts = _conflicts(conflict_body)
    if not conflicts:
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
    out: list[str] = []
    for conflict in _conflicts(conflict_body) or []:
        if isinstance(conflict, dict):
            out.append(f"{conflict.get('resource_name')}/{conflict.get('resource_uuid')}")
    return out


def foreign_holders(conflict_body: object, own_uuids: Collection[str]) -> list[str]:
    """The holders a 409 names that are NOT ours, each with its type and uuid.

    The message must let an operator tell a stray resource from their own
    source: a service conflict reads "already in use by service 'NAME'" and an
    orphaned target carries the source's NAME, so the uuid is what tells them
    apart. A holder Coolify did not identify (the instance fqdn) says so.
    """
    out: list[str] = []
    for conflict in _conflicts(conflict_body) or []:
        if not isinstance(conflict, dict):
            continue
        holder = conflict.get("resource_uuid")
        if isinstance(holder, str) and holder in own_uuids:
            continue
        domain = conflict.get("domain") or "?"
        if not isinstance(holder, str) or not holder:
            out.append(f"{domain}: the Coolify instance itself (its own fqdn)")
            continue
        kind = conflict.get("resource_type") or "resource"
        out.append(f"{domain}: {kind} {conflict.get('resource_name')!r} (uuid {holder})")
    return out


# ── leftovers of the old parking ─────────────────────────────────────────────


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

    Versions up to 2.9 parked a source's custom hosts under ``old-<tag>.`` at
    CREATE. Not a judgement call: the tag is stable per project/environment, so a
    run now would read ``old-<tag>.shop`` as the source's domain, carry the
    PARKED host to the target and journal it as the one to restore — the real
    domain then exists in no record this tool can see. Checks applications
    (``fqdn``, compose domains) and a service's applications alike.
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
        "now would carry the parked host to the target and lose the real one."
    ]


# ── DNS after a rollback ─────────────────────────────────────────────────────


def dns_rollback_note(
    dns_gate: Mapping[str, Any],
    *,
    source_ip: str,
    source_wildcard: str | None,
    target_wildcard: str | None,
    now_on_target: Iterable[str] = (),
    vanished: Mapping[str, tuple[str, ...]] | None = None,
) -> str | None:
    """What a rollback cannot do for the operator about DNS, or ``None``.

    The flip-back checklist. ``dns_gate`` is that step's journalled detail: its
    ``ready`` hosts already resolved to the target, its ``cutover_accepted`` and
    ``accepted`` hosts were to be cut over in parallel. ``now_on_target`` are
    hosts the rollback's own re-resolution found on the target — a cutover made
    by hand, which no journal saw. The rollback gives the source its domains
    back and restarts it, but DNS still sends their traffic to the target it
    removed — so the custom domain stays dead, and the source's proxy fails its
    ACME challenge, until the records point back. ``vanished`` are hosts that
    resolved at plan time and resolve to nothing now (their plan-time
    addresses): their records must be recreated. Server-bound hosts (under
    either server's wildcard) never moved by DNS and are left out.
    """
    hosts: set[str] = set()
    listed: list[object] = list(now_on_target)
    for key in ("ready", "cutover_accepted", "accepted"):
        found = dns_gate.get(key)
        if isinstance(found, list):
            listed.extend(found)
    for host in listed:
        if not isinstance(host, str) or not host:
            continue
        if under_wildcard(host, source_wildcard) or under_wildcard(host, target_wildcard):
            continue
        hosts.add(host)
    parts: list[str] = []
    if hosts:
        parts.append(
            f"point DNS for {', '.join(sorted(hosts))} back to {source_ip}: the source "
            "serves them again, but DNS still sends their traffic to the target this "
            "rollback removed"
        )
    for host, was in sorted((vanished or {}).items()):
        parts.append(
            f"recreate the DNS record of {host} -> {source_ip}: it resolved to "
            f"{', '.join(was)} when the migration was planned and resolves to nothing now"
        )
    return "; ".join(parts) or None
