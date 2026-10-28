"""What an API re-creation cannot carry to the target, decided at plan time.

PURE module: no IO. Every function reads something already captured — a GET, an
env list, a compose document — and returns human-readable lines. The planner
turns REFUSALS into hard blocks and WARNINGS into plan warnings.

Why this exists: Coolify 4.3.11-4.5.0 moved state to places the REST API cannot
reach, or started refusing on create what it had stored for years. The drift
canary saw eight new create fields; an audit of the release tags (2026-10-26,
v4.3.10 -> v4.5.0) found the rest. Each rule cites the release it starts in and
is gated on it through the release registry (domain/coolify_versions.py: a named
FEATURE, or the gate of the request field it is about), so it never refuses a
migration on an instance where it cannot fail that way.

Refusal vs warning follows AGENTS.md. Refuse what is not a judgement: a create
that WILL 422, a deploy that WILL fail after the source is stopped, data that
would silently be left behind. Warn about a setting the target will not share,
because whether that matters is the operator's call about their stack.

No line produced here ever contains a secret's VALUE — key names and lengths
only. These lines are printed and journaled, and invariant 5 does not bend for a
helpful error message.
"""

from __future__ import annotations

import base64
import binascii
import re
import shlex
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from bg_coolify_migrate.dns.extract import normalise_host
from bg_coolify_migrate.domain import compose as compose_mod
from bg_coolify_migrate.domain import locked_envs
from bg_coolify_migrate.domain.coolify_versions import (
    carries,
    every_application_route_carries,
    supports,
)
from bg_coolify_migrate.domain.domains import compose_domains, custom_hosts
from bg_coolify_migrate.domain.kinds import DatabaseEngine
from bg_coolify_migrate.domain.locked_envs import is_locked
from bg_coolify_migrate.domain.versions import Version

_WEBHOOK_SECRET_FIELDS = (
    "manual_webhook_secret_github",
    "manual_webhook_secret_gitlab",
    "manual_webhook_secret_bitbucket",
    "manual_webhook_secret_gitea",
)
#: app/Rules/ManualWebhookSecret.php::MIN_LENGTH (v4.4.0+), counted with mb_strlen.
_WEBHOOK_SECRET_MIN_LENGTH = 16
#: ApplicationsController v4.5.0:1354 `'string|nullable|max:72'` (mb_strlen).
_BASIC_AUTH_MAX_LENGTH = 72
#: bootstrap/helpers/api.php sharedDataApplications, identical v4.1.2-v4.5.0:
#: `['nullable', 'string', 'max:1000', 'regex:/^[a-zA-Z0-9 \-_.\/:=@,+]+$/']`.
_HEALTH_CHECK_COMMAND = re.compile(r"[a-zA-Z0-9 \-_./:=@,+]{1,1000}")

#: app/Support/RemoteSecretReferences.php::PATTERN (v4.4.0+).
_VAULT_REFERENCE = locked_envs.VAULT_REFERENCE

_REDIS_FAMILY = frozenset({DatabaseEngine.REDIS, DatabaseEngine.KEYDB, DatabaseEngine.DRAGONFLY})


def _settings(source: dict[str, Any]) -> dict[str, Any]:
    settings = source.get("settings")
    return settings if isinstance(settings, dict) else {}


# ── applications ─────────────────────────────────────────────────────────────


def application_refusals(source: dict[str, Any], *, version: Version | None) -> list[str]:
    """What makes this application's create 422, or its routing silently wrong."""
    out: list[str] = []

    if multi_server := multi_server_refusal(source, version=version):
        out.append(multi_server)

    if supports("webhook_secret_min_length", version):
        short = [
            f"{field} ({len(value)} characters)"
            for field in _WEBHOOK_SECRET_FIELDS
            if isinstance(value := source.get(field), str)
            and 0 < len(value) < _WEBHOOK_SECRET_MIN_LENGTH
        ]
        if short:
            out.append(
                f"manual webhook secret(s) shorter than {_WEBHOOK_SECRET_MIN_LENGTH} "
                f"characters: {', '.join(short)}. Coolify >= 4.4 keeps such a secret on "
                "the application that already has it, but refuses it on create, so the "
                "target would be rejected outright. Rotate it on the source — and in the "
                "git host's webhook settings — to 16+ characters, or clear it."
            )

    if supports("basic_auth_max_length", version):
        password = source.get("http_basic_auth_password")
        if isinstance(password, str) and len(password) > _BASIC_AUTH_MAX_LENGTH:
            out.append(
                f"the HTTP basic auth password is {len(password)} characters; Coolify "
                f">= 4.5 refuses more than {_BASIC_AUTH_MAX_LENGTH} on create (it was "
                "never checked before), so the target would be rejected outright. "
                "Shorten it on the source — or clear it if basic auth is off — then "
                "migrate. Truncating it here would silently change the credential."
            )

    command = source.get("health_check_command")
    if isinstance(command, str) and command and not _HEALTH_CHECK_COMMAND.fullmatch(command):
        # The UI's "Enable healthcheck" button saves without validating, so a
        # stored command can fail the API's own rule (identical v4.1.2-v4.5.0).
        out.append(
            f"the health check command ({len(command)} characters) contains characters "
            "Coolify's API refuses on create — it allows only letters, digits, spaces and "
            "- _ . / : = @ , + and at most 1000 characters — so the target would be "
            "rejected outright. Simplify it on the source (e.g. move it into a script "
            "in the image), then migrate."
        )

    if supports("port_overrides_hidden", version):
        hidden = hidden_port_overrides(source)
        if hidden:
            exposed = (
                80 if _settings(source).get("is_static") else _first_port(source.get("ports_exposes"))
            )
            out.append(
                f"the source routes a domain to port(s) {hidden}, but ports_exposes "
                f"starts with {exposed}. Since 4.3.15 Coolify keeps a domain's explicit "
                "port in a hidden column no API read returns, so the target would route "
                f"every domain to {exposed} — and releasing the source's custom domains at "
                "finalize would erase that port on the source as well, beyond what a "
                "rollback restores. "
                "If every domain uses one port, put it first in ports_exposes on the "
                "source; otherwise migrate this application by hand."
            )

    if supports("compose_port_overrides_hidden", version):
        out.extend(_compose_port_refusals(source, version=version))

    return out


def _count(value: object) -> int:
    """A non-negative count off the wire: an int or a digit string, never a bool."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def additional_servers(source: dict[str, Any]) -> tuple[int, tuple[str, ...]]:
    """How many additional servers an application deploys to, and their names. PURE.

    Every Application query carries a global ``withCount(additional_servers,
    additional_networks)`` scope (Application.php v4.1.2:244-249, v4.5.0:306-311),
    so GET /applications/{uuid} returns both ``*_count`` keys on every supported
    release; both count rows of the one ``additional_destinations`` pivot. The
    ``additional_servers`` rows themselves are serialised only because the status
    accessor lazy-loads them (v4.5.0:1019-1020) — so the count decides, and the
    names are display-only enrichment.

    Those rows are full Server models: on a root token they can carry proxy
    configuration (DNS provider tokens) and nested settings
    (api.php v4.5.0:130-134), so only ``name`` and ``uuid`` are ever read. Before
    4.5 the pivot could hold duplicate rows (deduplicated by migration
    2026_09_21_070754), so names are listed once each.
    """
    counts = [
        _count(source.get("additional_servers_count")),
        _count(source.get("additional_networks_count")),
    ]
    names: list[str] = []
    entries = source.get("additional_servers")
    if isinstance(entries, list):
        counts.append(len(entries))
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            label = entry.get("name") or entry.get("uuid")
            if label and str(label) not in names:
                names.append(str(label))
    return max(counts), tuple(names)


def multi_server_refusal(source: dict[str, Any], *, version: Version | None) -> str | None:
    """An application that also deploys to additional servers. NOT version-gated.

    The hazard exists on every release: the target is created on one server, so
    the additional deployments are dropped; QUIESCE can poll only the source
    server's daemon (invariant 9), while Coolify's fan-out stop returns early on
    the first non-functional server up to 4.3.x (StopApplication v4.1.2:18-25);
    and FINALIZE's delete orphans the per-server volumes (DeleteResourceJob
    v4.5.0:70-71,99-100). Only the REMEDY depends on the release: the
    destinations route exists from 4.3.0, add_destination's restrictions from 4.4.
    """
    count, names = additional_servers(source)
    if not count:
        return None
    re_add = (
        "in its Servers tab or with POST /applications/{uuid}/destinations"
        if supports("application_destinations_api", version)
        else "in its Servers tab"
    )
    rules = (
        " On Coolify >= 4.4 this removal is one-way for an application with persistent "
        "storage or built from docker compose, because Coolify will not add servers back "
        "to such an application. Deploying to several servers also needs a Docker image "
        "set in the General tab."
        if supports("multi_server_restrictions", version)
        else ""
    )
    listed = " (" + ", ".join(names) + ")" if names else ""
    return (
        f"this application also deploys to {count} additional server(s){listed}. The target "
        "would be created on the target server only, so those deployments would be "
        "dropped. Coolify's stop of the source does reach them, but this tool can check "
        "the daemon of the source server only, and a copy still running there would keep "
        "serving the domain next to the target. Remove each additional server on the "
        "source in the application's Servers tab (Configuration) while that server is "
        "reachable. Coolify stops the application there before detaching, but on an "
        "unreachable server it skips the stop and detaches anyway. The API's DELETE "
        "/applications/{uuid}/destinations/{destination_uuid} never stops anything; it "
        f"only detaches. Then migrate, and add the servers back to the target {re_add}, "
        "except the migration's target server: it becomes the primary, and Coolify "
        "refuses an additional destination on the primary's own server. If the "
        "application has persistent storage, each additional server holds its OWN copy "
        "of the volumes. Only the primary server's copy is migrated, and removing a "
        "server leaves its copy on that server." + rules
    )


def application_warnings(
    source: dict[str, Any], *, version: Version | None, source_wildcard: str | None = None
) -> list[str]:
    """Settings the target will not share. Silent on a stock application.

    A setting the create carries on ``version`` (domain/coolify_versions.py) is
    not reported: from 4.5.1 that includes the default COOLIFY_* variables and
    readonly labels. ``source_wildcard`` tells the app's own custom domains from
    the hosts bound to the source server (:func:`labels_route_beyond_domains`).
    """
    out: list[str] = []
    settings = _settings(source)

    if source.get("is_http_basic_auth_enabled") and (
        source.get("http_basic_auth_hash_algorithm") == "argon2id"
    ):
        out.append(
            "HTTP basic auth hashes with argon2id on the source; the target hashes the "
            "same password with bcrypt, so credentials keep working. argon2id cannot be "
            "sent on create — it is rejected unless the target server runs Caddy >= "
            "2.11 — so re-select it on the target by hand if it does."
        )
    if resolver := settings.get("traefik_certificate_resolver"):
        out.append(
            f"traefik_certificate_resolver={resolver!r} cannot be migrated, so the target "
            "uses its server's default resolver and may obtain its certificate "
            "differently. Resolver names are defined per server: make sure the target "
            "server's Traefik has it, then set it on the target by hand."
        )
    if settings.get("is_permanent_redirect_enabled") is True:
        out.append(
            "permanent redirects (301/308) cannot be migrated; the target answers the "
            "HTTP-to-HTTPS and www redirects with temporary 302/307 until you re-enable "
            "it by hand."
        )
    if settings.get("include_coolify_default_variables") is False and not (
        every_application_route_carries("include_coolify_default_variables", version)
    ):
        out.append(_DEFAULT_VARIABLES_WARNING)
    if settings.get("is_container_label_readonly_enabled") is False and source.get("custom_labels"):
        if not every_application_route_carries("is_container_label_readonly_enabled", version):
            out.append(
                "the source has readonly labels OFF, so its container labels may be "
                "hand-edited. The setting cannot be migrated, and a target created with it "
                "on regenerates its labels from its own domains — hand-made routers, "
                "middlewares or headers are lost. Re-apply them on the target by hand."
            )
        elif beyond := labels_route_beyond_domains(source, source_wildcard=source_wildcard):
            out.append(
                "the source has readonly labels OFF, but its labels route "
                f"{beyond}, which are not among its own custom domains — hosts bound to "
                "the source server, generated ones, or ones the DNS gate never checks. "
                "Kept verbatim, they would make the target answer (and request "
                "certificates) for hosts that still point at the source, so the target is "
                "created with readonly labels ON instead: it regenerates its labels from "
                "its own domains, and hand-made routers, middlewares or headers are lost. "
                "Re-apply them on the target by hand."
            )
    if settings.get("is_static") is True:
        out.append(
            "the source is a static site, and that setting rides under `settings`, which "
            "the create body never carries: the target is built and run as a regular "
            "application instead of being served by Coolify's static web server. Mark it "
            "static on the target by hand before switching traffic over."
        )
    if source.get("is_maintenance_enabled") is True:
        out.append(_MAINTENANCE_WARNING)
    if line := dockerfile_healthcheck_warning(source, version=version):
        out.append(line)
    if supports("compose_port_overrides_hidden", version) and (
        compose_hidden_port_overrides(source, version=version) is None
    ):
        out.append(_COMPOSE_PORTS_UNCHECKED_WARNING)
    if supports("run_options_parser", version) and (
        flags := newly_mapped_run_options(source.get("custom_docker_run_options"))
    ):
        out.append(
            f"custom_docker_run_options use {flags}, which Coolify ignored before 4.5 "
            "and applies from 4.5 on. The target — and the source's own next deploy — "
            "runs with them; check it behaves as the running source does."
        )
    return out


def dockerfile_healthcheck_warning(source: dict[str, Any], *, version: Version | None) -> str | None:
    """The source runs its Dockerfile's HEALTHCHECK; the target runs Coolify's check.

    ``custom_healthcheck_found`` is in no create array and is set only by a deploy
    that parses the Dockerfile while Coolify's check is DISABLED, or on init
    (Application::parseHealthcheckFromDockerfile, v4.5.0:2686-2743, v4.5.1:2737).
    So a source with it set and Coolify's check enabled runs the Dockerfile's
    check — up to 4.5.0 Coolify adds its own only when none was found
    (ApplicationDeploymentJob v4.5.0:4196-4198) — while the target never learns
    of it and runs Coolify's configured check: path, port and type. From 4.5.1 an
    enabled Coolify check replaces the Dockerfile's on every deploy (v4.5.1:
    4471-4486), so the source's own next deploy switches too, and so does a
    rollback's restart of the source (a full deploy for these build packs,
    :263); an unhealthy new container is removed and the deploy fails (:5256).

    A warning, not a refusal: whether Coolify's configured check passes in this
    image cannot be known at plan time.
    """
    if source.get("custom_healthcheck_found") is not True:
        return None
    if source.get("health_check_enabled") is False:
        return None
    line = (
        "the source's Dockerfile has its own HEALTHCHECK while Coolify's health check "
        "is enabled. The running source uses the Dockerfile's check, but the target "
        "cannot know the Dockerfile had one, so it runs Coolify's configured check "
        "instead (its path, port and type). If that check does not pass in this image "
        "— a wrong path or port, no curl or wget — the target's deploy fails after the "
        "source is stopped, and the run rolls back."
    )
    if supports("coolify_healthcheck_replaces_dockerfile", version):
        return (
            line + " From Coolify 4.5.1 the source switches too: its next deploy, and a "
            "rollback's restart of it, run Coolify's check — so an unverified check can "
            "leave the SOURCE down after a rollback. Before migrating, either disable "
            "Coolify's health check on the source (the Dockerfile HEALTHCHECK then "
            "applies on both), or redeploy the source once to prove the configured "
            "check passes."
        )
    return (
        line + " Before migrating, either disable Coolify's health check on the source "
        "(the Dockerfile HEALTHCHECK then applies on both), or make sure its configured "
        "check passes in this image."
    )


_DEFAULT_VARIABLES_WARNING = (
    "include_coolify_default_variables is off on the source and cannot be migrated: "
    "the target's containers get COOLIFY_APP_NAME, COOLIFY_PROJECT_NAME, "
    "COOLIFY_ENVIRONMENT_NAME and COOLIFY_SERVER_IP until you turn it off by hand."
)

_MAINTENANCE_WARNING = (
    "the source is in maintenance mode, which cannot be migrated: the target comes up "
    "serving the real application instead of the maintenance page. Re-enable it on the "
    "target by hand before switching traffic if that is not what you want."
)


def _first_port(raw: object) -> int | None:
    for part in str(raw or "").split(","):
        if part.strip().isdigit():
            return int(part.strip())
    return None


def _label_text(raw: object) -> str:
    """custom_labels as text. The UI stores them base64, the API stores plaintext."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    if "traefik." in raw or "caddy" in raw:
        return raw
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return raw


_TRAEFIK_PORT = re.compile(r"\.loadbalancer\.server\.port=(\d+)")
_CADDY_PORT = re.compile(r"\{\{\s*upstreams\s+(\d+)\s*\}\}")

#: A Traefik router rule's host matchers; HostRegexp cannot be enumerated.
_TRAEFIK_HOST_RULE = re.compile(r"\b(Host|HostSNI|HostRegexp)\(([^)]*)\)")
_BACKTICKED = re.compile(r"`([^`]*)`")
#: A Caddy site address label (`caddy_0=https://a.example.com`, docker.php
#: generateLabelsApplication).
_CADDY_SITE = re.compile(r"^\s*caddy(?:_\d+)?\s*=\s*(.+?)\s*$", re.MULTILINE)


def labels_route_beyond_domains(
    source: dict[str, Any], *, source_wildcard: str | None
) -> list[str]:
    """Hosts the source's custom_labels route that are not its own custom domains.

    Asked before a target keeps those labels VERBATIM (readonly labels off,
    carried from 4.5.1): the deploy then routes exactly what the labels say
    (ApplicationDeploymentJob v4.5.1:4393-4399), never regenerating them from the
    target's domains. A server-bound host is rewritten onto the target's wildcard
    in its domains but not in hand-kept labels, a generated host names the
    source's IP, and a host only the labels name is one the DNS gate never
    checks — each would make the target route, and request a certificate for, a
    host that still resolves to the source (invariant 7). Empty means only the
    app's own custom domains are routed, which the DNS gate guards. A HostRegexp
    rule cannot be enumerated and is reported as such. PURE.
    """
    text = _label_text(source.get("custom_labels"))
    if not text:
        return []
    own = set(custom_hosts(source.get("fqdn"), source_wildcard=source_wildcard))
    routed: list[str] = []
    for matcher, args in _TRAEFIK_HOST_RULE.findall(text):
        if matcher == "HostRegexp":
            routed.append(f"HostRegexp({args.strip()})")
            continue
        routed.extend(h for raw in _BACKTICKED.findall(args) if (h := normalise_host(raw)))
    for site in _CADDY_SITE.findall(text):
        routed.extend(
            h for raw in re.split(r"[,\s]+", site.strip("\"'")) if raw and (h := normalise_host(raw))
        )
    return sorted({h for h in routed if h not in own})


def hidden_port_overrides(source: dict[str, Any]) -> list[int]:
    """Ports the proxy routes to that neither ports_exposes[0] nor the fqdn explain.

    Coolify (>= 4.3.15) strips an explicit ``:port`` out of every domain it saves
    into ``domain_port_overrides`` — in ``$hidden``, made visible by no controller —
    and routes each domain to its override, falling back to the FIRST exposed port.
    The GET therefore shows a portless fqdn, and the only place the routing
    survives is the generated proxy labels. A port the labels route to that is
    neither the fallback nor written in the fqdn is an override the target cannot
    get. Compose applications route per service and are not checked here.
    """
    if source.get("build_pack") == "dockercompose":
        return []
    # generateLabelsApplication (v4.5.0 docker.php:1229) routes a static site to 80
    # whatever ports_exposes says — switching an app to static never rewrites it.
    exposed = 80 if _settings(source).get("is_static") else _first_port(source.get("ports_exposes"))
    if exposed is None:
        return []
    labels = _label_text(source.get("custom_labels"))
    routed = {int(p) for p in _TRAEFIK_PORT.findall(labels)} | {
        int(p) for p in _CADDY_PORT.findall(labels)
    }
    explicit: set[int] = set()
    for url in str(source.get("fqdn") or "").split(","):
        try:
            if (port := urlsplit(url.strip()).port) is not None:
                explicit.add(port)
        except ValueError:
            continue
    return sorted(routed - {exposed} - explicit)


_COMPOSE_PORTS_UNCHECKED_WARNING = (
    "the parsed compose (docker_compose) could not be read — or Coolify (4.5.1+) "
    "replaced part of a domain's labels or ports with <locked> because a locked "
    "variable's value occurs there — so it is unknown whether a compose domain "
    "routes to an explicit port. Since 4.3.15 Coolify keeps such a "
    "port in a hidden column no API read returns: the target would route the domain to "
    "the service's first exposed port, and releasing the source's custom domains at "
    "finalize would erase the port on the source beyond what a rollback restores. Check "
    "the domains' ports in Coolify before migrating."
)


def compose_hidden_port_overrides(
    source: dict[str, Any], *, version: Version | None = None
) -> dict[str, list[int]] | None:
    """Per compose service: ports its domain routes to that a portless domain cannot.

    A ``docker_compose_domains`` save strips each domain's ``:port`` into the
    hidden ``domain_port_overrides`` — in the UI from 4.3.15 (Livewire
    Application/Domains.php v4.3.15:1995-2011), through the API from 4.3.18
    (normalizeDockerComposeDomainPorts, ApplicationsController v4.3.18:1459/3379,
    v4.4.0:6372, v4.5.0:6535-6551) — and the parser routes by that override,
    falling back for a portless domain to (``version``; unknown reads as newest):
    the application's first ``ports_exposes`` on 4.3.15 (parsers.php
    v4.3.15:1361), the service's first ``expose``/``ports`` entry of any protocol
    and then ``ports_exposes`` on 4.3.16-4.3.17 (v4.3.16:1361), the service's
    first tcp entry from 4.3.18 (v4.3.18:1357, v4.5.0:2158-2161, docker.php
    :883-906, :1015-1017). The GET shows only the portless domain. FINALIZE's
    release replaces the domain list, and ``DomainPortOverrides::normalize``
    drops overrides for domains that are gone (v4.5.0
    app/Support/DomainPortOverrides.php:27-75) — so neither the target nor a
    rollback's restore can bring the port back.

    The routing survives only in the generated labels of the parsed compose
    (``docker_compose``, visible with read:sensitive, v4.5.0:122-126). Coolify's
    own Traefik services are named ``http(s)-<n>-<uuid>[-<service>]``
    (docker.php :1019-1025), which keeps a port label the operator wrote into the
    compose — and which survives anyway — out of it.

    From 4.5.1 that GET replaces every locked value of 4+ characters, wherever
    it occurs, with ``<locked>`` (redactLockedValuesInGeneratedCompose,
    shared.php v4.5.1:5812-5868; ApplicationsController :156-159) — a locked
    PORT=3000 turns ``...port=3000`` into ``...port=<locked>``, which reads as
    no port at all. A domain'd service whose labels, expose or ports carry it
    therefore makes the answer unknown, never "nothing hidden". Elsewhere — an
    ``environment:`` list, another service — it is ordinary and ignored.

    Returns ``{}`` when nothing is hidden, and ``None`` when a compose app has
    domains but its parsed compose cannot be read, or not for those domains.
    """
    if source.get("build_pack") != "dockercompose":
        return {}
    domains_by_service = {
        name: domain
        for name, domain in compose_domains(source.get("docker_compose_domains")).items()
        if domain.strip()
    }
    if not domains_by_service:
        return {}
    services = _parsed_compose_services(source.get("docker_compose"))
    if services is None:
        return None
    if supports("locked_values_redacted_in_compose", version) and any(
        _routing_redacted(name, services) for name in domains_by_service
    ):
        return None
    uuid = re.escape(str(source.get("uuid") or ""))
    generated = re.compile(
        rf"traefik\.http\.services\.https?-\d+-{uuid}[^=]*\.loadbalancer\.server\.port=(\d+)"
    )
    out: dict[str, list[int]] = {}
    for name, domain in domains_by_service.items():
        body = services.get(name) or {}
        labels = _compose_label_text(body.get("labels"))
        routed = {int(p) for p in generated.findall(labels)} | {
            int(p) for p in _CADDY_PORT.findall(labels)
        }
        fallback = _compose_fallback_port(source, body, version=version)
        explicit: set[int] = set()
        for url in domain.split(","):
            try:
                if (port := urlsplit(url.strip()).port) is not None:
                    explicit.add(port)
            except ValueError:
                continue
        hidden = sorted(routed - explicit - ({fallback} if fallback is not None else set()))
        if hidden:
            out[name] = hidden
    return out


#: What Coolify writes in place of a locked value (shared.php v4.5.1:5850-5853).
_REDACTED = "<locked>"


def _routing_redacted(name: str, services: dict[str, dict[str, Any]]) -> bool:
    """Whether redaction touched what routes the domain'd service ``name``.

    Its labels, expose and ports — or its name itself, when it is missing and
    some service's name carries the marker.
    """
    body = services.get(name)
    if body is None:
        return any(_REDACTED in str(other) for other in services)
    entries = [*(body.get("expose") or []), *(body.get("ports") or [])]
    text = "\n".join([_compose_label_text(body.get("labels")), *map(str, entries)])
    return _REDACTED in text


def _compose_fallback_port(
    source: dict[str, Any], service: dict[str, Any], *, version: Version | None
) -> int | None:
    """Where the compose parser routes a portless domain on this release."""
    exposed = 80 if _settings(source).get("is_static") else _first_port(source.get("ports_exposes"))
    if not supports("compose_service_port_fallback", version):
        return exposed
    if not supports("compose_tcp_port_fallback", version):
        first = _first_compose_port(service, tcp_only=False)
        return first if first is not None else exposed
    return _first_compose_port(service)


def _compose_port_refusals(source: dict[str, Any], *, version: Version | None) -> list[str]:
    hidden = compose_hidden_port_overrides(source, version=version)
    if not hidden:
        return []
    detail = "; ".join(f"{name} -> {ports}" for name, ports in sorted(hidden.items()))
    return [
        f"compose service domains route to port(s) their domain does not name ({detail}). "
        "Since 4.3.15 Coolify keeps such a port in a hidden column no API read returns, "
        "so the target would route each domain to its service's first exposed port — and "
        "releasing the source's custom domains at finalize would erase the port on the "
        "source as well, beyond what a rollback restores. Make that port the service's "
        "first `expose:` entry in the compose (in git), redeploy the source, then migrate; "
        "or migrate it by hand."
    ]


def _parsed_compose_services(raw: object) -> dict[str, dict[str, Any]] | None:
    """``services:`` of the parsed compose, or None when it cannot be read."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw
    if "services" not in raw:
        try:
            text = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            return None
    try:
        return compose_mod.services(compose_mod.parse(text))
    except compose_mod.ComposeError:
        return None


def _compose_label_text(labels: object) -> str:
    if isinstance(labels, dict):
        return "\n".join(f"{k}={v}" for k, v in labels.items())
    if isinstance(labels, list):
        return "\n".join(str(item) for item in labels)
    return ""


def _first_compose_port(service: dict[str, Any], *, tcp_only: bool = True) -> int | None:
    """firstDockerComposeServicePort (docker.php v4.5.0:883-906): first tcp target.

    ``tcp_only=False`` is its 4.3.16-4.3.17 form, which took any protocol.
    """
    entries = list(service.get("expose") or []) + list(service.get("ports") or [])
    for entry in entries:
        if isinstance(entry, dict):
            if tcp_only and str(entry.get("protocol") or "tcp") != "tcp":
                continue
            port = str(entry.get("target") or "")
        else:
            text = str(entry)
            if tcp_only and "/" in text and text.rsplit("/", 1)[1] != "tcp":
                continue
            port = text.split("/", 1)[0].rsplit(":", 1)[-1]
        if port.isdigit() and 1 <= int(port) <= 65535:
            return int(port)
    return None


#: Flags convertDockerRunToCompose maps from v4.5.0 (bootstrap/helpers/constants.php
#: DOCKER_RUN_OPTION_MAPPING) that every earlier release silently ignored.
_NEWLY_MAPPED_FLAGS = frozenset(
    {"--add-host", "--pids-limit", "--read-only", "--runtime", "--tmpfs", "--interactive", "--tty"}
)
#: docker.php v4.5.0:1797 maps any `^-[dit]+$` token carrying i or t (-it, -dit, -td).
_NEWLY_MAPPED_SHORT = re.compile(r"-[dit]*[it][dit]*")
#: The only flags whose `=false` the old parser read as true (docker.php v4.5.0:1704).
_FALSE_NOW_HONOURED = frozenset({"--init", "--privileged", "--read-only"})


def newly_mapped_run_options(raw: object) -> list[str]:
    """The custom docker run flags whose meaning changed in Coolify 4.5.0."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    found: set[str] = set()
    for token in tokens:
        flag, _, value = token.partition("=")
        if flag in _FALSE_NOW_HONOURED and value.lower() == "false":
            # `--init=false` and friends were read as TRUE before 4.5.
            found.add(token)
        elif flag in _NEWLY_MAPPED_FLAGS or _NEWLY_MAPPED_SHORT.fullmatch(token):
            found.add(flag)
    return sorted(found)


# ── services and databases ───────────────────────────────────────────────────


def service_warnings(source: dict[str, Any]) -> list[str]:
    """Service-level and per-sub-application settings the target will not share."""
    out: list[str] = []
    if source.get("include_coolify_default_variables") is False:
        out.append(_DEFAULT_VARIABLES_WARNING)
    if source.get("is_maintenance_enabled") is True:
        out.append(_MAINTENANCE_WARNING)
    tls = sorted(
        str(app.get("name"))
        for app in source.get("applications") or []
        if isinstance(app, dict)
        and (app.get("traefik_certificate_resolver") or app.get("is_permanent_redirect_enabled"))
    )
    if tls:
        out.append(
            f"sub-application(s) {tls} set a certificate resolver or permanent redirects, "
            "which can be set only per sub-application after create and are not "
            "migrated: the target uses its server's default resolver and temporary "
            "redirects until you re-apply them by hand."
        )
    return out


def service_refusals(source: dict[str, Any], *, version: Version | None) -> list[str]:
    """Sub-application URLs the target's ``urls`` update would reject, or must not see.

    Only what WILL fail. Every subtler collision — a trailing slash, http vs
    https from 4.4, a port-bearing URL against a portless one — either resolves
    through CREATE's own-conflict retry or surfaces there as a 409/422, which is
    before the stop.
    """
    parts = _service_url_parts(source)
    out: list[str] = []

    # Coolify's URL validator accepts user:password@host (domains.php
    # v4.5.0:41-49). Carried, it would ride the urls PATCH, Coolify's conflict
    # messages, our logs and the journal's restore body: refused before any of
    # those, naming only the sub-application (invariants 5 and 6).
    with_credentials = sorted({name for name, url in parts if _has_userinfo(url)})
    if with_credentials:
        out.append(
            f"sub-application(s) {with_credentials} carry a URL with credentials in it "
            "(user:password@host). It would be sent to Coolify for the target and "
            "repeated in its errors, and this tool never handles a credential it could "
            "log. Remove the credentials from the URL on the source (Coolify's basic "
            "auth does not live there), then migrate."
        )

    # applyServiceUrls rejects the WHOLE request when one URL is set on two
    # containers (ServicesController v4.1.2:73-76, v4.5.0:166-169) — before any
    # conflict check, so no retry helps. From 4.2.0 it compares the URLs with
    # scheme and host lowercased; 4.1.x compares the trimmed strings.
    normalised = supports("service_urls_normalised", version)
    owners: dict[str, set[str]] = {}
    for name, url in parts:
        if name not in with_credentials:
            owners.setdefault(_duplicate_key(url, normalised=normalised), set()).add(name)
    for key, names in sorted(owners.items()):
        if len(names) > 1:
            out.append(
                f"sub-applications {sorted(names)} share the URL {key!r}. Coolify rejects "
                "the target's whole domain update when one URL is set on two containers, "
                "so none of this service's domains could be carried. Give each "
                "sub-application its own URL on the source, then migrate."
            )
    return out


def _service_url_parts(source: dict[str, Any]) -> list[tuple[str, str]]:
    """``(sub-application name, url)`` for every URL a sub-application routes. PURE.

    ``url`` before ``fqdn``, like the carry (resources.service_app_urls): from
    4.3.15 only ``url`` keeps a domain's port.
    """
    out: list[tuple[str, str]] = []
    for app in source.get("applications") or []:
        if not isinstance(app, dict) or not app.get("name"):
            continue
        raw = app.get("url") or app.get("fqdn")
        if not isinstance(raw, str):
            continue
        out.extend((str(app["name"]), p.strip()) for p in raw.split(",") if p.strip())
    return out


def _has_userinfo(url: str) -> bool:
    try:
        return "@" in urlsplit(url if "://" in url else f"https://{url}").netloc
    except ValueError:
        return "@" in url


def _duplicate_key(url: str, *, normalised: bool) -> str:
    """The form Coolify compares for duplicates. PURE.

    normalizeApplicationDomainUrl (ValidationPatterns v4.5.0:677-727) lowercases
    the scheme and host and keeps port, path, query and fragment as written.
    """
    if not normalised:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    key = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    if parts.query:
        key += f"?{parts.query}"
    if parts.fragment:
        key += f"#{parts.fragment}"
    return key


def database_warnings(
    source: dict[str, Any], *, engine: DatabaseEngine | None = None, tls_carried: bool = False
) -> list[str]:
    out: list[str] = []
    if source.get("include_coolify_default_variables") is False:
        out.append(_DEFAULT_VARIABLES_WARNING)
    if lost := database_tls_lost_warning(source, engine=engine, carried=tls_carried):
        out.append(lost)
    if identity := database_tls_identity_warning(source, engine=engine):
        out.append(identity)
    return out


def unsupported_engine_refusal(source: dict[str, Any]) -> str:
    """For a database whose engine this tool cannot recreate (e.g. SQLite, 4.4+)."""
    kind = source.get("type") or source.get("database_type") or source.get("image") or "unknown"
    return (
        f"database engine {kind!r} is not supported. SQLite (Coolify >= 4.4) keeps its "
        "data in a volume applications mount directly, and this tool cannot recreate "
        "that pairing through the API. Migrate it by hand."
    )


# ── database TLS ─────────────────────────────────────────────────────────────
#
# Standalone database TLS is two columns, readable in every GET since
# v4.0.0-beta.399 (fillable, not $hidden, no cast — StandalonePostgresql
# v4.5.0:49-50,71-91) and settable through NO create or update $allowedFields and
# no dedicated route at any tag v4.1.2-v4.5.0. The only API path that carries them
# is POST /databases/{uuid}/clone (v4.3.0+, routes/api.php:340), a whole-row
# server-side copy this tool does not create through. Every create helper only
# fill()s validated request fields (bootstrap/helpers/databases.php v4.5.0:83-95),
# so a target always gets the column default — false for every engine since
# beta.400 (migration 2025_03_26_104103). The reverse flip (source off, target on)
# therefore cannot happen.
#
# Read here from enable_ssl / ssl_mode / the engine ONLY — never from file
# storages: on a TLS source those carry server.key, and the storages GET returns
# its content to a read:sensitive token (DatabasesController v4.5.0:138-145).
# The plan is written by runner.save_plan without the journal's secret guard, so
# how these lines are built is the only thing keeping a secret out of them.

#: The TLS columns. Kept out of api/fields.py on purpose (no array accepts them);
#: test_database_tls_is_still_unsettable fails the day a release does.
DATABASE_TLS_FIELDS: frozenset[str] = frozenset({"enable_ssl", "ssl_mode"})

#: Only these engines have an ssl_mode column (migration 2025_01_27_102616).
_SSL_MODE_ENGINES = frozenset(
    {DatabaseEngine.POSTGRESQL, DatabaseEngine.MYSQL, DatabaseEngine.MONGODB}
)
_SSL_MODE_SHAPE = re.compile(r"[A-Za-z_-]{1,32}")


def _tls_on(value: object) -> bool:
    """No model casts enable_ssl, so accept what PDO may hand back. Absent is off."""
    return value is True or value in (1, "1", "t", "true")


def _ssl_mode(source: dict[str, Any], engine: DatabaseEngine | None) -> str | None:
    mode = source.get("ssl_mode") if engine in _SSL_MODE_ENGINES else None
    return mode if isinstance(mode, str) and _SSL_MODE_SHAPE.fullmatch(mode) else None


#: What a client of the source sees once the target serves plaintext. Evidence
#: per engine at v4.5.0: StartMysql:189-197 / StartMariadb:186-193
#: (--require-secure-transport=1), StartMongodb:205-241 (--tlsMode),
#: StartRedis:313 / StartKeydb:309 (--tls-port 6380), StartDragonfly:204-213
#: (--tls on the main port, no plaintext — FlushDatabase:18-19 agrees),
#: StartPostgresql:192-198 (ssl=on only; the mode is enforced client-side).
_TLS_LOST_CONSEQUENCE: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRESQL: (
        "Clients connecting with sslmode=require, verify-ca or verify-full — Coolify's "
        "own connection URL carries the mode — fail against it; prefer/allow fall back "
        "to plaintext unnoticed. If pg_hba.conf in the data directory uses hostssl "
        "rules, the target refuses EVERY TCP client while its health check still passes."
    ),
    DatabaseEngine.MYSQL: (
        "The source refuses plaintext (require_secure_transport); the target accepts "
        "it, and clients with ssl-mode=VERIFY_CA/VERIFY_IDENTITY fail."
    ),
    DatabaseEngine.MARIADB: (
        "The source refuses plaintext (require_secure_transport); the target accepts "
        "it, and clients that require TLS fail."
    ),
    DatabaseEngine.MONGODB: (
        "Clients with tls=true — Coolify's own connection URL carries it — fail to "
        "connect. Coolify switched TLS on by migration for every MongoDB that existed "
        "at v4.0.0-beta.399 or was created before beta.400, so it may be on here "
        "without anyone having chosen it."
    ),
    DatabaseEngine.REDIS: (
        "The source serves TLS on port 6380 next to 6379; rediss://…:6380 clients get "
        "connection refused."
    ),
    DatabaseEngine.KEYDB: (
        "The source serves TLS on port 6380 next to 6379; rediss://…:6380 clients get "
        "connection refused."
    ),
    DatabaseEngine.DRAGONFLY: (
        "The source serves TLS on its main port 6379 (no plaintext); the target serves "
        "plaintext there, so rediss:// clients fail and plaintext clients now connect."
    ),
}


def database_tls_lost_warning(
    source: dict[str, Any], *, engine: DatabaseEngine | None, carried: bool
) -> str | None:
    """The source serves TLS and the target will not. None when TLS is carried.

    A WARNING, not a refusal: the database deploys and runs fine without TLS, and
    whether its clients need TLS is the operator's call about their stack. The
    target is never public either way (create_database forces is_public=False).
    """
    if carried or not _tls_on(source.get("enable_ssl")):
        return None
    mode = _ssl_mode(source, engine)
    consequence = (engine is not None and _TLS_LOST_CONSEQUENCE.get(engine)) or (
        "Clients that require TLS fail to connect."
    )
    return (
        f"TLS is on for this database{f' (ssl_mode={mode!r})' if mode else ''}, but no "
        "create or update field and no dedicated route carries enable_ssl/ssl_mode "
        "(Coolify 4.1.2-4.5.0), so the target starts WITHOUT TLS. "
        f"{consequence} "
        # HasDatabaseStatusInfo::instantSaveSSL (v4.5.0:105-118) only saves the
        # column, and StartPostgresql & co. apply it at the next start — so a
        # toggle on the stopped target before START costs no outage at all.
        "The target is created stopped before the source is quiesced. Toggle SSL on "
        f"it in the UI as soon as it appears{f' (same mode, {mode!r})' if mode else ''} "
        "so its first start already serves TLS; otherwise toggle it and restart it "
        "right after cutover."
    )


def database_tls_identity_warning(
    source: dict[str, Any], *, engine: DatabaseEngine | None
) -> str | None:
    """The target's certificate identity differs, however TLS gets there.

    Kept apart from the lost warning on purpose: carrying TLS (by hand today, by
    a clone-based create some day) must not silence this. StartPostgresql
    v4.5.0:47-87 takes the CA of the DESTINATION server (Server::ensureCaCertificate)
    and issues CN = the database uuid, SAN = DNS:<uuid> + IP:<that server's IP>
    (SslHelper v4.5.0:53,76,162); each server's CA lives at
    /data/coolify/ssl/coolify-ca.crt on that server (SslHelper v4.5.0:243-256).
    """
    if not _tls_on(source.get("enable_ssl")):
        return None
    mode = _ssl_mode(source, engine)
    client_certs = (
        " The source also demands client certificates (ssl_mode='verify-full'): "
        "clients must present ones signed by the target server's CA instead."
        if engine is DatabaseEngine.MONGODB and mode == "verify-full"
        else ""
    )
    return (
        "once TLS is on for the target, its certificate is issued by the TARGET "
        "server's CA (/data/coolify/ssl/coolify-ca.crt on that server) for the "
        "target's new uuid and IP — not by the source server's. Clients that verify "
        "the server (verify-ca/verify-full, ssl-ca, tlsCAFile) or pin the source "
        "server's CA must be given the target's." + client_certs
    )


#: Directives in a carried engine config that switch TLS on by themselves. Fixed
#: labels, so a refusal names WHICH directive without echoing the config (whose
#: other lines may hold a password). Anchored to non-comment lines, and the
#: explicit-off forms (tls-port 0, require_secure_transport=OFF, mode: disabled)
#: do not count — those cannot fail the target. Each pattern follows its engine's
#: own grammar, not the canonical spelling: postgresql.conf makes the ``=``
#: optional and takes any unambiguous boolean prefix (``t``, ``ye``; never the
#: ambiguous ``o``), Redis lowercases directive names, my.cnf accepts a
#: ``loose-`` prefix, and mongod.conf is YAML, so flow style is valid.
_Directives = tuple[tuple[str, re.Pattern[str]], ...]

_POSTGRES_TLS: _Directives = (
    (
        "ssl",
        re.compile(
            r"^\s*ssl(?:\s*=\s*|\s+)'?(on|t|tr|tru|true|y|ye|yes|1)'?\s*(#|$)", re.M | re.I
        ),
    ),
)
#: MySQL and MariaDB read the same my.cnf options.
_MY_CNF_TLS: _Directives = (
    ("ssl-cert", re.compile(r"^\s*(?:loose[-_])?ssl[-_]cert\s*=", re.M | re.I)),
    ("ssl-key", re.compile(r"^\s*(?:loose[-_])?ssl[-_]key\s*=", re.M | re.I)),
    ("ssl-ca", re.compile(r"^\s*(?:loose[-_])?ssl[-_]ca\s*=", re.M | re.I)),
    (
        "require_secure_transport",
        re.compile(
            r"^\s*(?:loose[-_])?require[-_]secure[-_]transport\s*(=\s*(on|1|true)\b|$)",
            re.M | re.I,
        ),
    ),
)
#: Redis and its fork KeyDB share the keys.
_REDIS_CONF_TLS: _Directives = (
    ("tls-port", re.compile(r"^\s*tls-port\s+['\"]?[1-9]", re.M | re.I)),
    ("tls-cert-file", re.compile(r"^\s*tls-cert-file\s", re.M | re.I)),
    ("tls-key-file", re.compile(r"^\s*tls-key-file\s", re.M | re.I)),
)
#: mongod.conf YAML; the ssl/PEMKeyFile spellings are mongod's pre-4.2 aliases.
_MONGO_CONF_TLS: _Directives = (
    (
        "net.tls.mode",
        re.compile(r"^[^#\n]*\bmode\s*:\s*['\"]?(require|prefer|allow)(TLS|SSL)\b", re.M),
    ),
    ("certificateKeyFile", re.compile(r"^[^#\n]*\b(certificateKeyFile|PEMKeyFile)\s*:", re.M)),
)

#: Engine -> (the carried config field, its TLS directives). Dragonfly has no
#: config field and ClickHouse no TLS at all (supportsSsl() is false).
_TLS_CONFIG_DIRECTIVES: dict[DatabaseEngine, tuple[str, _Directives]] = {
    DatabaseEngine.POSTGRESQL: ("postgres_conf", _POSTGRES_TLS),
    DatabaseEngine.MYSQL: ("mysql_conf", _MY_CNF_TLS),
    DatabaseEngine.MARIADB: ("mariadb_conf", _MY_CNF_TLS),
    DatabaseEngine.MONGODB: ("mongo_conf", _MONGO_CONF_TLS),
    DatabaseEngine.REDIS: ("redis_conf", _REDIS_CONF_TLS),
    DatabaseEngine.KEYDB: ("keydb_conf", _REDIS_CONF_TLS),
}


def database_tls_config_refusals(
    source: dict[str, Any], *, engine: DatabaseEngine | None
) -> list[str]:
    """A TLS source whose carried engine config turns TLS on by itself.

    The *_conf fields are carried verbatim, but the certificate mounts that
    Coolify's SSL setting adds are not (and the setting itself never arrives), so
    the target points its engine at certificate files that do not exist there: it
    fails to start — or refuses every client — after the source is stopped. That
    is not a judgement, so it refuses. Silent unless the source has TLS on: a
    config that enables TLS against files in its own data volume works on the
    target, because the volume is copied.
    """
    if engine is None or not _tls_on(source.get("enable_ssl")):
        return []
    spec = _TLS_CONFIG_DIRECTIVES.get(engine)
    if spec is None:
        return []
    field, directives = spec
    config = source.get(field)
    if not isinstance(config, str) or not config.strip():
        return []
    found = [label for label, pattern in directives if pattern.search(config)]
    if not found:
        return []
    return [
        f"TLS is on for this database and its custom configuration turns TLS on by "
        f"itself ({field} sets {', '.join(found)}). The target gets that configuration "
        "verbatim but neither the SSL setting nor the source's certificate files, so "
        "after the source is stopped it would fail to start or refuse every client. "
        "Remove the TLS lines from the source's custom configuration — Coolify's SSL "
        "setting applies TLS on its own — then migrate; or migrate this database by hand."
    ]


# ── compose ──────────────────────────────────────────────────────────────────


def compose_refusals(raw: str | None, *, version: Version | None) -> list[str]:
    """Compose shapes a 4.4+/4.5+ target cannot reproduce or will not accept."""
    if not raw:
        return []
    try:
        doc = compose_mod.parse(raw)
    except compose_mod.ComposeError:
        # The planner already logs an unparseable compose; refusing here would
        # duplicate a diagnosis that is not this module's to make.
        return []

    out: list[str] = []
    volumes = doc.get("volumes")
    if supports("compose_volumes_as_written", version) and isinstance(volumes, dict):
        for key in sorted(volumes, key=str):
            decl = volumes[key]
            if not isinstance(decl, dict):
                continue
            if _is_external(decl):
                # parsers.php::composeExternalVolumeDockerName (v4.5.0:614).
                external = decl["external"]
                name = (
                    decl.get("name")
                    or (external.get("name") if isinstance(external, dict) else None)
                    or key
                )
                out.append(
                    f"compose volume {key!r} is external. Coolify >= 4.4 uses it under "
                    f"its literal name {name!r} instead of a per-resource volume and "
                    "records no storage for it, so there is nothing on the target to "
                    "pair it with — and it may be shared with other stacks there. "
                    "Migrate it by hand."
                )
            elif decl.get("driver_opts") or decl.get("driver") not in (None, "", "local"):
                out.append(
                    f"compose volume {key!r} sets driver/driver_opts. From Coolify 4.4 "
                    "they are applied, so the bytes live where the driver puts them (a "
                    "bind device or a network share), not in the volume's own directory: "
                    "the copy would move nothing and verification would compare two "
                    "empty directories and pass. Migrate it by hand."
                )

    if supports("compose_scale_check", version):
        for name, service in sorted(compose_mod.services(doc).items()):
            deploy = service.get("deploy")
            for label, value in (
                ("scale", service.get("scale")),
                ("deploy.replicas", deploy.get("replicas") if isinstance(deploy, dict) else None),
            ):
                if _int_above_one(value):
                    out.append(
                        f"compose service {name!r} sets {label} to {value}. Coolify >= 4.5 "
                        "refuses that while parsing — after the target row is saved, "
                        "leaving a half-created resource. Remove it on the source first."
                    )
    return out


def _is_external(decl: dict[str, Any]) -> bool:
    """bootstrap/helpers/parsers.php::isComposeExternalVolume (v4.5.0:456)."""
    if "external" not in decl:
        return False
    external = decl["external"]
    if isinstance(external, dict):
        return True
    return external is True or str(external).strip().lower() in ("1", "true", "on", "yes")


def _int_above_one(value: object) -> bool:
    try:
        return int(str(value)) > 1
    except ValueError:
        return False


def compose_redump_warning(*, version: Version | None) -> list[str]:
    """Only true before 4.4: from then on the compose is stored verbatim."""
    if supports("compose_stored_verbatim", version):
        return []
    return [
        "Coolify re-dumps compose through Yaml::dump(Yaml::parse(...)); "
        "comments and formatting will be lost on the target"
    ]


# ── storages ─────────────────────────────────────────────────────────────────


def storage_refusals(storages: dict[str, Any] | None, *, version: Version | None) -> list[str]:
    """Persistent storages this Coolify can no longer create through its API.

    Only for resources whose storages are POSTed (plain applications) — compose
    and database volumes are materialised by Coolify itself. Refuses exactly
    where the registry's ``host_path`` row is closed (v4.3.22 on, and an
    unknown release): the field CREATE would have to drop.
    """
    if not storages or carries("storages", "host_path", version):
        return []
    out: list[str] = []
    for entry in storages.get("persistent_storages") or []:
        if not isinstance(entry, dict):
            continue
        host_path = entry.get("host_path")
        if isinstance(host_path, str) and host_path.strip():
            out.append(
                f"persistent storage {entry.get('name')!r} at {entry.get('mount_path')!r} "
                f"binds host path {host_path!r}. Coolify >= 4.3.22 can no longer create a "
                "host-path storage through its API, and recreating it as a plain volume "
                "would mount an empty volume over the mirrored data. Turn it into a "
                "directory file mount on the source, or create it on the target by hand."
            )
    return out


# ── environment variables ────────────────────────────────────────────────────

#: The magic-variable commands whose value generateEnvValue() GENERATES — upstream's
#: own list from v4.5.1 (EnvironmentVariable::GENERATED_MAGIC_VARIABLE_COMMANDS,
#: :68-74), and the same switch at every release in range (shared.php
#: v4.1.2:1427-1496, v4.5.0:2076-2145). Matched as SERVICE_<COMMAND>, the
#: <COMMAND> whole or followed by `_` — PASSWORD covers PASSWORD_64, HEX covers
#: HEX_32/64/128, BASE64 its _32/_64/_128 — so SERVICE_USERNAME_X is not one.
GENERATED_MAGIC_COMMANDS: tuple[str, ...] = (
    "PASSWORD",
    "PASSWORDWITHSYMBOLS",
    "BASE64",
    "REALBASE64",
    "HEX",
    "USER",
    "LOWERCASEUSER",
    "SUPABASEANON",
    "SUPABASESERVICE",
)
_GENERATED_SERVICE_SECRET = re.compile(rf"^SERVICE_({'|'.join(GENERATED_MAGIC_COMMANDS)})(_|$)")
_CREDENTIAL_KEY = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE", re.IGNORECASE)


def is_generated_magic(key: str) -> bool:
    """Whether Coolify generates this SERVICE_* key's value itself. PURE."""
    return bool(_GENERATED_SERVICE_SECRET.match(key))


def target_regenerates(
    key: str, *, collection: str, engine: DatabaseEngine | None, compose_app: bool = False
) -> bool:
    """A locked key the target will hold a DIFFERENT value for, not merely lack.

    A new service generates its own generated magic variables — SERVICE_PASSWORD_*,
    SERVICE_USER_*, SERVICE_HEX_*, SERVICE_SUPABASEANON_* … (:data:`GENERATED_MAGIC_COMMANDS`;
    until it mirrored upstream's list only four commands were known here, so a
    locked SERVICE_HEX_32_X was merely "created without" while the target
    generated a different one) — and the bulk upsert that would overwrite them
    skips a value it cannot read. So does
    a dockercompose APPLICATION: applicationParser firstOrCreates the same magic
    rows (v4.1.2 parsers.php:630, v4.3.10:668, v4.5.0:1436-1444). A Redis takes
    its server password from REDIS_PASSWORD and gets a random one. NOT
    KeyDB/Dragonfly: their create makes no REDIS_PASSWORD row, so a target
    without the locked one falls back to the carried keydb/dragonfly_password.
    """
    if collection == "services" or (collection == "applications" and compose_app):
        return is_generated_magic(key)
    return engine is DatabaseEngine.REDIS and key == "REDIS_PASSWORD"


def _locked_production(envs: list[dict[str, Any]]) -> list[str]:
    """Keys of locked production rows still without a value (recovered ones have one)."""
    return sorted(str(e["key"]) for e in envs if is_locked(e) and not e.get("is_preview"))


def _with_reasons(keys: list[str], unrecovered: Mapping[str, str] | None) -> str:
    """``['A', 'B'] (A: why; B: why)`` — key names and reasons, never a value."""
    reasons = "; ".join(f"{k}: {unrecovered[k]}" for k in keys if unrecovered and k in unrecovered)
    return f"{keys} ({reasons})" if reasons else f"{keys}"


def env_refusals(
    envs: list[dict[str, Any]],
    *,
    collection: str,
    engine: DatabaseEngine | None,
    version: Version | None,
    compose_app: bool = False,
    unrecovered: Mapping[str, str] | None = None,
) -> list[str]:
    """Environment variables whose copy would fail the target after the stop.

    ``envs`` is the source's list with every locked value that could be read back
    filled in (locked_envs.merged_view); ``unrecovered`` says why the rest could
    not be, per key.
    """
    out: list[str] = []

    if supports("secret_manager", version):
        referencing = sorted(
            str(e["key"])
            for e in envs
            if e.get("key")
            and not e.get("is_literal")
            and isinstance(e.get("value"), str)
            and _VAULT_REFERENCE.search(e["value"])
        )
        if referencing:
            out.append(
                f"environment variable(s) {referencing} reference a secret manager "
                "({{vault.KEY}}). The link to the secret manager is returned by no API "
                "read, so the target would be created without it and its first deploy "
                "would fail — after the source had already been stopped. Remove the "
                "references, or migrate this resource by hand."
            )

    regenerated = [
        k
        for k in _locked_production(envs)
        if target_regenerates(k, collection=collection, engine=engine, compose_app=compose_app)
    ]
    if regenerated:
        out.append(
            f"locked (is_shown_once) variable(s) {_with_reasons(regenerated, unrecovered)} "
            "could not be read back from the source server, so the target would run with "
            "its OWN freshly generated value while the copied data was initialised with the "
            "source's — every client would fail to authenticate. Fix the cause above, or "
            "delete and re-add them unlocked with the same value, then migrate."
        )
    return out


def env_warnings(
    envs: list[dict[str, Any]],
    *,
    collection: str,
    engine: DatabaseEngine | None,
    compose: str | None,
    version: Version | None,
    compose_app: bool = False,
    unrecovered: Mapping[str, str] | None = None,
    builds: bool = False,
) -> list[str]:
    """Environment variables the target will get differently. Keys only, never values.

    ``envs`` and ``unrecovered`` as for :func:`env_refusals`; ``builds`` says the
    target rebuilds this application from source (START_TARGET, after the stop).
    """
    out: list[str] = []

    locked = [
        k
        for k in _locked_production(envs)
        if not target_regenerates(k, collection=collection, engine=engine, compose_app=compose_app)
    ]
    if locked:
        credentials = [k for k in locked if _CREDENTIAL_KEY.search(k)]
        out.append(
            f"locked (is_shown_once) variable(s) {_with_reasons(locked, unrecovered)} could "
            "not be read back from the source server, so the target is created WITHOUT "
            "them. Set them on the target by hand before switching traffic over."
            + (f" {credentials} look like credentials." if credentials else "")
        )
        build_only = [
            k for k in locked if unrecovered and unrecovered.get(k) == locked_envs.BUILD_TIME_ONLY
        ]
        if builds and build_only:
            out.append(
                f"{build_only} are build-time variables of an application the target "
                "rebuilds from source. That build runs AFTER the source is stopped and "
                "without them: a build that needs them fails, and the run rolls back. "
                "Unlock them on the source, or make sure the build does not need them."
            )
    previews = sorted(str(e["key"]) for e in envs if is_locked(e) and e.get("is_preview"))
    if previews:
        out.append(
            f"locked preview variable(s) {previews} are not carried "
            f"({locked_envs.PREVIEW_ONLY}): the target's pull-request deployments run "
            "without them until you set them by hand."
        )

    if supports("secret_manager", version):
        literal = sorted(
            str(e["key"])
            for e in envs
            if e.get("key")
            and e.get("is_literal")
            and isinstance(e.get("value"), str)
            and _VAULT_REFERENCE.search(e["value"])
        )
        if literal:
            out.append(
                f"literal variable(s) {literal} contain a {{{{vault.KEY}}}} reference. If "
                "the source links a secret manager it resolves them; the target has no "
                "link and passes the reference text through as the value."
            )

    if supports("exact_env_escaping", version):
        changed = sorted(
            str(e["key"])
            for e in envs
            if e.get("key")
            and _saved_before_exact_escaping(e)
            and legacy_escaping_changes_value(e, collection=collection, engine=engine)
        )
        if changed:
            credentials = [k for k in changed if _CREDENTIAL_KEY.search(k)]
            out.append(
                f"variable(s) {changed} were saved before Coolify 4.5 and keep its old "
                "escaping on the source, but are created anew on the target, where Coolify "
                "passes them exactly — so the container sees a different value "
                "(backslashes, quotes, a ' #' tail, quoting of literal values). Check them "
                "after the first start."
                + (
                    f" {credentials} look like credentials: if the copied data was "
                    "initialised with the old form, authentication will fail."
                    if credentials
                    else ""
                )
            )

    if supports("redis_password_quoting", version) and engine in _REDIS_FAMILY:
        out.extend(_redis_password_warnings(envs, engine=engine))

    if compose and supports("exact_env_escaping", version):
        empty = sorted(
            str(e["key"])
            for e in envs
            if e.get("key")
            and (e.get("value") == "" or (e.get("value") is None and e.get("preserve_compose_null")))
            and _compose_distinguishes_unset(str(e["key"]), compose)
        )
        if empty:
            out.append(
                f"variable(s) {empty} are empty on the source, which Coolify still writes "
                "as KEY= there. The target stores them as unset — its API turns '' into "
                "null — and leaves them out, and this compose reads them in a way that "
                "tells the two apart (${KEY-default}, ${KEY?}, or a bare pass-through). "
                "A ${KEY?} makes the target refuse to start; give them a value first."
            )
    return out


#: Whether a row keeps the legacy escaping — read off its `version` stamp.
_saved_before_exact_escaping = locked_envs.saved_before_exact_escaping


#: bootstrap/helpers/docker.php::escapeEnvVariables (v4.5.0:1991) — the characters
#: whose presence makes escapeEnvVariables($value) !== $value.
_LEGACY_ESCAPED = frozenset({"\\", "\r", "\t", "\x00", '"', "'"})


def legacy_escaping_changes_value(
    env: dict[str, Any], *, collection: str, engine: DatabaseEngine | None
) -> bool:
    """Port of HasSecretManager::legacyEscapingChangesValue (v4.5.0:189-219). PURE.

    Whether a legacy-escaped row puts a different value into the container than an
    exact one with the same saved value would. Reads the stored `value`: upstream
    reads the resolved one, so a {{team.X}}-style shared reference is judged by
    its reference text — the one approximation here.
    """
    value = env.get("value")
    if not isinstance(value, str) or not value.strip() or _VAULT_REFERENCE.search(value):
        return False
    app_or_service = collection in ("applications", "services")

    if env.get("key") == "REDIS_PASSWORD" and engine in _REDIS_FAMILY and "$" in value:
        return True
    if value.startswith(("{", "[")) and _is_json(value):
        return "$" in value or (collection != "applications" and "\n" in value)
    if env.get("is_literal") or env.get("is_multiline"):
        # Service and application .env files strip the old single quotes;
        # database environment lists keep them.
        return "'" in value if app_or_service else True
    return bool(_LEGACY_ESCAPED & set(value)) or (app_or_service and " #" in value)


#: PHP json_validate: strict JSON, NaN/Infinity rejected (Python's json takes them).
_is_json = locked_envs.is_json


#: StartRedis::requirePassArgument (v4.5.0:334-341, flag since v4.4.0): a legacy row
#: keeps the UNQUOTED --requirepass when the password contains one of these, and
#: Compose then splits it.
_SHELL_SPLITTING = frozenset("\\'\";&|<> \t\r\n")


def _redis_password_warnings(
    envs: list[dict[str, Any]], *, engine: DatabaseEngine | None
) -> list[str]:
    """legacy_password_quoting (v4.4.0): $hidden, true for every pre-4.4 row, false
    for every row created since — so always false on the target."""
    value = next((e.get("value") for e in envs if e.get("key") == "REDIS_PASSWORD"), None)
    if not isinstance(value, str) or not value:
        return []
    if engine is DatabaseEngine.REDIS:
        if _SHELL_SPLITTING & set(value) or "{{" in value:
            return [
                "REDIS_PASSWORD contains shell or quote characters (or a {{...}} "
                "reference). A Redis created before Coolify 4.4 passes it to "
                "--requirepass unquoted, so its server password is not the stored value; "
                "the target quotes it and enforces the stored value exactly. Clients "
                "configured with the source's effective password will fail to "
                "authenticate — check them after the first start."
            ]
        return []
    return [
        "a REDIS_PASSWORD variable is set. A KeyDB/Dragonfly created before Coolify 4.4 "
        "ignores it in favour of its own password field; the target uses it AS the "
        "server password. If the two differ, clients of the target authenticate "
        "differently — make them equal on the source first."
    ]


def _compose_distinguishes_unset(key: str, compose: str) -> bool:
    """Does this compose read KEY in a way where unset and '' differ?

    ``${KEY-default}`` and ``${KEY?err}`` (no colon), and a bare pass-through
    (``- KEY`` or ``KEY:`` with no value). The colon forms treat both alike.
    """
    k = re.escape(key)
    return bool(
        re.search(rf"\$\{{{k}[-?]", compose)
        or re.search(rf"^\s*-\s*{k}\s*$", compose, re.MULTILINE)
        or re.search(rf"^\s*{k}:\s*$", compose, re.MULTILINE)
    )


# ── servers ──────────────────────────────────────────────────────────────────


def server_refusals(
    target_server: dict[str, Any], *, collection: str, source: dict[str, Any]
) -> list[str]:
    """Server roles (Coolify >= 4.4) that make the target's deploy impossible."""
    role = _settings(target_server).get("server_role")
    name = target_server.get("name") or target_server.get("uuid")
    if role == "build":
        return [
            f"target server {name!r} is a build server, and Coolify refuses to host "
            "resources on one. Pick a server whose role is 'deployment' or 'both'."
        ]
    if (
        role == "deployment"
        and collection == "applications"
        and source.get("build_pack") not in ("dockerimage", "dockercompose", None)
        and not source.get("docker_registry_image_name")
    ):
        return [
            f"target server {name!r} is set to deployments only, so this application "
            "must be built on a build server and pulled by image name — and it has no "
            "docker_registry_image_name. Its deploy would fail after the source is "
            "stopped. Set an image name on the source first, or pick another server."
        ]
    return []


def _truthy(value: object) -> bool:
    """A settings flag as Laravel may serialise it: bool, 0/1, or a string."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t")
    return value is True or value == 1


def docker_cleanup(server: dict[str, Any]) -> tuple[bool, str]:
    """Does Coolify's Docker cleanup prune unused volumes on this server, and when?

    ``settings.delete_unused_volumes`` turns ``docker volume prune -af`` on
    (v4.1.2:CleanupDocker.php:61-63, v4.5.0:166-168). The job runs on
    ``docker_cleanup_frequency``, and then on every run with
    ``force_docker_cleanup`` (the default), otherwise only at or above
    ``docker_cleanup_threshold`` percent disk usage
    (v4.5.0:app/Jobs/DockerCleanupJob.php:115,136,156). ``GET /servers/{uuid}``
    serialises all four at every supported release, so no version gate.

    Returns ``(prunes, when)``; ``(False, "")`` when off or not reported.
    """
    settings = _settings(server)
    if not _truthy(settings.get("delete_unused_volumes")):
        return False, ""
    frequency = settings.get("docker_cleanup_frequency")
    schedule = (
        f"cron {frequency.strip()!r}"
        if isinstance(frequency, str) and frequency.strip()
        else "its schedule"
    )
    if _truthy(settings.get("force_docker_cleanup")):
        return True, f"{schedule}, on every run"
    threshold = settings.get("docker_cleanup_threshold")
    if isinstance(threshold, int | str) and str(threshold).strip().isdigit():
        return True, f"{schedule}, when disk usage is at or above {str(threshold).strip()}%"
    return True, f"{schedule}, when disk usage crosses its threshold"


def server_warnings(source_server: dict[str, Any], target_server: dict[str, Any]) -> list[str]:
    """Server-level settings that change what every migrated resource gets."""
    src, tgt = _settings(source_server), _settings(target_server)
    if "traefik_certificate_resolver" not in src or "traefik_certificate_resolver" not in tgt:
        return []
    if src["traefik_certificate_resolver"] == tgt["traefik_certificate_resolver"]:
        return []
    return [
        f"the source server issues certificates with resolver "
        f"{src['traefik_certificate_resolver']!r} and the target server with "
        f"{tgt['traefik_certificate_resolver']!r} (null means the proxy's own "
        "certificates): every migrated domain will get its certificate differently."
    ]
