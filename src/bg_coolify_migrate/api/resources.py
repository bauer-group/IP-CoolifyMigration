"""Resource re-creation: the mapping from a source snapshot to API calls.

IO shell. The decisions (which kind, which route, which strategy) are made in
``domain/``; this module only performs them.

Two rules govern everything here:

1. **Never round-trip a GET into a POST.** Coolify enforces ``$allowedFields``
   and returns 422 per unknown field. Every body is built from an explicit
   whitelist in :mod:`.fields`.
2. **Never let the target start on its own.** Every create passes
   ``instant_deploy=False``. Nothing may run before the DNS gate has decided.

Storage handling differs sharply by kind, and getting it wrong is silent data
loss:

* **Databases** — the volume is created by the model's ``created`` hook as
  ``{engine}-data-{new_uuid}``. We do NOT create it; we read it back. We must
  pin ``image`` though, because the hook parses the tag to choose the mount path.
* **Compose-backed** (services, dockercompose apps) — volumes come from Coolify
  parsing the compose. ``shouldBeReadOnlyInUI`` makes them un-POSTable anyway.
  Post the same compose, let Coolify materialise them, read them back.
* **Plain applications** — persistent storages are user-defined, so these we do
  POST. Note upstream forces ``name = '{uuid}-{name}'`` regardless of what we
  send.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Collection
from typing import Any
from urllib.parse import urlsplit

import structlog

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.fields import (
    APPLICATION_CREATE,
    APPLICATION_ROUTE_REQUIRED,
    ENV_FIELDS,
    SERVICE_CREATE,
    SERVICE_CREATE_CUSTOM_COMPOSE,
    SERVICE_UPDATE,
    STORAGE_CREATE,
    STORAGE_CREATE_SERVICE,
    database_allowed,
    filter_body,
    missing_required,
)
from bg_coolify_migrate.dns import wildcard as dns_wildcard
from bg_coolify_migrate.dns.extract import is_generated, normalise_host
from bg_coolify_migrate.domain.kinds import ResourceKind, create_route
from bg_coolify_migrate.domain.naming import VolumeEndpoint, resource_config_dir
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from bg_coolify_migrate.errors import CoolifyApiError, UnsupportedResource

log = structlog.get_logger(__name__)


class Placement:
    """Where a resource is being created."""

    def __init__(
        self,
        *,
        project_uuid: str,
        environment_name: str,
        server_uuid: str,
        destination_uuid: str | None = None,
        source_wildcard: str | None = None,
        target_wildcard: str | None = None,
    ) -> None:
        self.project_uuid = project_uuid
        self.environment_name = environment_name
        self.server_uuid = server_uuid
        self.destination_uuid = destination_uuid
        # The source/target servers' wildcard bases, so a server-bound URL can be
        # rewritten onto the target's wildcard at create. Not part of as_body():
        # they drive domain rewriting, they are not create fields themselves.
        self.source_wildcard = source_wildcard
        self.target_wildcard = target_wildcard

    def as_body(self) -> dict[str, Any]:
        return {
            "project_uuid": self.project_uuid,
            "environment_name": self.environment_name,
            "server_uuid": self.server_uuid,
            "destination_uuid": self.destination_uuid,
        }


def _encode_compose(raw: str) -> str:
    """Coolify requires ``docker_compose_raw`` base64-encoded, and validates it."""
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


#: Application-create fields Coolify STORES as plaintext but VALIDATES as base64
#: on the way in (``isBase64Encoded``), then decodes and stores. Sending the
#: source's plaintext value verbatim is a 422 "should be base64 encoded". Same
#: asymmetry as docker_compose_raw. custom_labels is the one that bit the compose
#: app; the other two would 422 next for a dockerfile / custom-nginx app.
_BASE64_CREATE_FIELDS = ("custom_labels", "custom_nginx_configuration", "dockerfile")

#: The same asymmetry on every database create route, since at least v4.1.2
#: (DatabasesController: `isBase64Encoded($request->postgres_conf)` -> 422 "The
#: postgres_conf should be base64 encoded."). Copied verbatim, any source with a
#: custom config could never be migrated: the create 422'd before the stop.
_BASE64_DATABASE_FIELDS = (
    "postgres_conf",
    "mysql_conf",
    "mariadb_conf",
    "mongo_conf",
    "redis_conf",
    "keydb_conf",
)


def _encode_base64_fields(
    body: dict[str, Any], fields: tuple[str, ...] = _BASE64_CREATE_FIELDS
) -> None:
    """Base64-encode the plaintext-in/base64-out create fields, in place.

    An empty value is dropped, not encoded: Coolify decodes then runs a UTF-8
    check that an empty string fails, so sending ``""`` is itself a 422.
    """
    for field in fields:
        if field not in body:
            continue
        value = body[field]
        text = "" if value is None else str(value)
        if text.strip():
            body[field] = base64.b64encode(text.encode("utf-8")).decode("ascii")
        else:
            # None or empty: Coolify has()=true and 422s on both. Drop it.
            body.pop(field, None)


def _check(body: dict[str, Any], required: frozenset[str], what: str) -> None:
    missing = missing_required(body, required)
    if missing:
        raise UnsupportedResource(
            f"cannot create {what}: missing required field(s) {sorted(missing)}",
            hint="This usually means the source resource is missing data we cannot invent.",
        )


async def resolve_destination(api: CoolifyClient, server_uuid: str) -> str | None:
    """The server's docker destination, when it has more than one.

    ``destination_uuid`` is only mandatory if the server has several; sending it
    unnecessarily is harmless, but guessing wrong is not. Returns ``None`` when
    the server has a single destination and Coolify can pick it itself.

    Reads the dedicated ``/servers/{uuid}/destinations`` route. This used to read
    ``destinations`` / ``standalone_dockers`` off ``GET /servers/{uuid}``, where
    they have never been present on any version — ``server_by_uuid`` loads only
    ``['settings']`` — so the list was always empty, this always returned None, and
    the multi-destination guard below was unreachable dead code. Discovered
    2026-08-16 against a live 4.3.2 instance, where the fields came back ``null``.
    """
    destinations = await api.get_server_destinations(server_uuid)

    if destinations is None:
        # Coolify <4.2 has no destinations route. We cannot enumerate them, so we
        # cannot detect a multi-destination server — exactly the old behaviour,
        # but now a known blind spot rather than an accident. Coolify picks, which
        # is right for the single-destination case that this covers in practice.
        log.debug("api.destinations.route_absent", server=server_uuid)
        return None

    if len(destinations) <= 1:
        return None

    # More than one: we must choose, and we cannot. Prefer the one Coolify marks
    # as the network for this server, else fail loudly rather than pick at random.
    for destination in destinations:
        if isinstance(destination, dict) and destination.get("network") == "coolify":
            return str(destination.get("uuid"))
    raise UnsupportedResource(
        f"server {server_uuid} has {len(destinations)} docker destinations and none is "
        "the default 'coolify' network",
        hint="Pass --destination explicitly; we will not guess which one you meant.",
    )


async def ensure_project(api: CoolifyClient, name: str) -> str:
    """Find or create a project by name. Returns its uuid."""
    for project in await api.list_projects():
        if project.get("name") == name or project.get("uuid") == name:
            return str(project["uuid"])
    created = await api.post("/projects", {"name": name})
    uuid = (created or {}).get("uuid")
    if not uuid:
        raise CoolifyApiError(f"creating project {name!r} returned no uuid", body=created)
    log.info("api.project.created", name=name, uuid=uuid)
    return str(uuid)


async def ensure_environment(api: CoolifyClient, project_uuid: str, name: str) -> str:
    """Find or create an environment within a project. Returns its name."""
    project = await api.get_project(project_uuid)
    for environment in project.get("environments") or []:
        if isinstance(environment, dict) and environment.get("name") == name:
            return name
    await api.post(f"/projects/{project_uuid}/environments", {"name": name})
    log.info("api.environment.created", project=project_uuid, name=name)
    return name


# ── create ───────────────────────────────────────────────────────────────────


async def create_database(
    api: CoolifyClient, snapshot: ResourceSnapshot, placement: Placement, source: dict[str, Any]
) -> str:
    """Create a standalone database. Returns the new uuid.

    ``image`` is pinned from the source deliberately: ``StandalonePostgresql``'s
    ``created`` hook parses the tag (``/:(?:pg)?(\\d+)/i``) to decide between
    ``/var/lib/postgresql`` (>=18) and ``/var/lib/postgresql/data``. An unpinned
    image can put the mount path somewhere the mirrored bytes are not.
    """
    if snapshot.engine is None:
        raise UnsupportedResource(f"database {snapshot.name!r} has no engine")

    allowed = database_allowed(snapshot.engine.value)
    body = filter_body({**source, **placement.as_body()}, allowed)
    body["name"] = snapshot.name
    body["instant_deploy"] = False
    # The GET returns the stored plaintext config; the create validates base64.
    _encode_base64_fields(body, _BASE64_DATABASE_FIELDS)
    if snapshot.image:
        body["image"] = snapshot.image
    # A public port on the source would collide or expose the target prematurely.
    body.pop("public_port", None)
    body["is_public"] = False

    _check(body, frozenset({"project_uuid", "server_uuid"}), f"database {snapshot.name!r}")

    created = await api.post(f"/databases/{snapshot.engine.value}", body)
    uuid = (created or {}).get("uuid")
    if not uuid:
        raise CoolifyApiError(f"creating database {snapshot.name!r} returned no uuid", body=created)
    log.info("api.database.created", name=snapshot.name, uuid=uuid, image=snapshot.image)
    return str(uuid)


async def create_service(
    api: CoolifyClient, snapshot: ResourceSnapshot, placement: Placement, source: dict[str, Any]
) -> str:
    """Create a service, from a template or from raw compose. Returns the new uuid.

    ``type`` and ``docker_compose_raw`` are mutually exclusive upstream: sending
    both is a 422.
    """
    is_custom = snapshot.kind is ResourceKind.SERVICE_COMPOSE
    allowed = SERVICE_CREATE_CUSTOM_COMPOSE if is_custom else SERVICE_CREATE

    body = filter_body({**source, **placement.as_body()}, allowed)
    body["name"] = snapshot.name
    body["instant_deploy"] = False

    if is_custom:
        if not snapshot.docker_compose_raw:
            raise UnsupportedResource(
                f"service {snapshot.name!r} has no compose to copy",
                hint=(
                    "This usually means the API token lacks read:sensitive, which makes "
                    "Coolify omit docker_compose_raw with no error."
                ),
            )
        body.pop("type", None)
        body["docker_compose_raw"] = _encode_compose(snapshot.docker_compose_raw)
    else:
        body.pop("docker_compose_raw", None)
        body["type"] = snapshot.service_type

    _check(body, frozenset({"project_uuid", "server_uuid"}), f"service {snapshot.name!r}")

    created = await api.post("/services", body)
    uuid = (created or {}).get("uuid")
    if not uuid:
        raise CoolifyApiError(f"creating service {snapshot.name!r} returned no uuid", body=created)

    # Nothing else is written here. connect_to_docker_network and the domains go
    # in ONE follow-up PATCH (finish_service_target), which the engine sends only
    # after it has journaled this uuid: a write that fails here would strand a
    # target no rollback knows about.
    log.info("api.service.created", name=snapshot.name, uuid=uuid, custom_compose=is_custom)
    return str(uuid)


_GIT_URL_PREFIXES = ("https://", "http://", "git://", "git@")


def public_git_url(git_repository: str) -> str:
    """A full git URL for the ``/applications/public`` route.

    Coolify stores a PUBLIC app's repo as the short ``owner/repo`` form: on create
    it parses the URL and keeps only the path (``ApplicationsController`` does
    ``git_repository = segment(1)/segment(2)`` and sets the default GitHub source).
    But the same endpoint validates ``git_repository`` with ``ValidGitRepositoryUrl``
    — it must be a full URL. So a public app that GETs back as ``owner/repo`` is
    rejected on re-create with "must start with https://…". We rebuild the
    github.com URL (Coolify's default public source); anything already a URL is left
    untouched. The private-github-app / deploy-key routes accept the short form, so
    this is only for ``public``.
    """
    if git_repository.startswith(_GIT_URL_PREFIXES):
        return git_repository
    return f"https://github.com/{git_repository.strip('/')}"


def _remap_domains(
    fqdn: str | None, *, source_wildcard: str | None, target_wildcard: str | None
) -> str:
    """Rewrite a comma-separated fqdn onto the target server. PURE.

    Server-bound URLs — those under the SOURCE server's wildcard — are rewritten
    onto the target's wildcard (``pdf-tool.app.0046-20…`` -> ``…app.0047-20…``),
    so the app keeps its subdomain on the new host. Custom domains are carried
    verbatim: they are server-independent and move by a DNS cutover, which the
    DNS gate reasons about separately. Returns a comma-separated list of full
    URLs, or ``""`` when there is nothing to set (Coolify then auto-generates a
    target-wildcard URL via ``autogenerate_domain``).
    """
    if not fqdn:
        return ""
    urls: list[str] = []
    for part in str(fqdn).split(","):
        stripped = part.strip()
        host = normalise_host(stripped)
        if host is None:
            continue
        if dns_wildcard.under_wildcard(host, source_wildcard):
            # Server-bound: rewrite onto the target's wildcard. If the target has
            # no wildcard we cannot place it there — drop it (carrying the
            # source-bound host would point the target at the SOURCE) and let
            # Coolify auto-generate a working URL via autogenerate_domain.
            remapped = dns_wildcard.remap_host(host, source_wildcard, target_wildcard)
            if remapped:
                urls.append(_with_host(stripped, remapped))
        else:
            # Custom (or, with no source wildcard known, treated as custom): it is
            # server-independent and moves with the app verbatim.
            urls.append(stripped)
    # Dedup while preserving order — a source can list the same host twice.
    return ",".join(dict.fromkeys(urls))


def _with_host(url: str, host: str) -> str:
    """``url`` with its host replaced, keeping scheme, port and path. PURE.

    The port is the one that matters: Coolify routes a domain written with
    ``:8080`` to 8080 (from 4.3.15 by moving it into a hidden per-domain
    override on save) and everything else to the first exposed port. Rebuilding
    the URL from the bare host silently moved such an app onto the wrong port.
    """
    parts = urlsplit(url if "://" in url else f"https://{url}")
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    return f"{parts.scheme or 'https'}://{host}{port}{parts.path.rstrip('/')}"


def _parse_compose_domains(raw: object) -> dict[str, str]:
    """Reduce docker_compose_domains to ``{service: "url,url"}``. PURE.

    Coolify STORES and GETs this as a dict ``{service: {"domain": "url,url"}}``
    (a JSON string), though the create route wants a different shape (see
    :func:`_compose_domains_body`). Tolerant of a raw string or an already-decoded
    dict, and of junk — a bad value yields ``{}``, not a crash.
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


def _compose_domains_body(
    raw: object, *, source_wildcard: str | None, target_wildcard: str | None
) -> list[dict[str, str]] | None:
    """Rewrite a compose app's per-service domains for the target create body.

    The create route wants an ARRAY of ``{name, domain}`` (it 422s on ``domains``
    for a dockercompose app). Read the source's per-service domains, remap the
    server-bound URLs inside each onto the target's wildcard (custom domains
    kept), and return the array. ``None`` when there is nothing to send.
    """
    parsed = _parse_compose_domains(raw)
    if not parsed:
        return None
    return [
        {
            "name": name,
            "domain": _remap_domains(
                domain, source_wildcard=source_wildcard, target_wildcard=target_wildcard
            ),
        }
        for name, domain in parsed.items()
    ]


def _blank_compose_domains(raw: object) -> list[dict[str, str]] | None:
    """The docker_compose_domains array with every domain blanked. PURE.

    Releasing a compose app's domains cannot go through the ``domains`` field
    (422 for dockercompose); it is done by resubmitting each service with an
    empty domain. ``None`` when the source had none to release.
    """
    parsed = _parse_compose_domains(raw)
    if not parsed:
        return None
    return [{"name": name, "domain": ""} for name in parsed]


def _park_hosts(domain_str: object, *, source_wildcard: str | None, tag: str) -> str:
    """Rename the CUSTOM hosts in a comma-separated domain string. PURE.

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
        if host and not dns_wildcard.under_wildcard(host, source_wildcard):
            out.append(f"https://old-{tag}.{host}")
        else:
            out.append(stripped)
    return ",".join(out)


async def park_source_domains(
    api: CoolifyClient,
    snapshot: ResourceSnapshot,
    source: dict[str, Any],
    *,
    source_wildcard: str | None,
    tag: str,
) -> dict[str, Any] | None:
    """Free the source's custom domains so the target can claim them.

    Coolify 409s ("Domain conflicts detected") when a create asks for a domain
    another resource already holds. So before creating the target we rename the
    source's custom domains with an ``old-<tag>`` marker (in Coolify's DB only —
    the running source is not redeployed, so it keeps serving the real domain
    until quiesce).

    ALWAYS returns the body that restores the source's ORIGINAL domains, even when
    nothing is parked: finalize blanks the source's domains on success, and the
    rollback needs the originals to swing the URL back onto the old stack. Returns
    ``None`` only when the source has no domains at all.

    Services are never parked — only their restore body is returned (see
    :func:`service_restore_body`).
    """
    if snapshot.collection == "services":
        return service_restore_body(source, source_wildcard=source_wildcard)
    if snapshot.kind is ResourceKind.APP_GIT_COMPOSE:
        parsed = _parse_compose_domains(source.get("docker_compose_domains"))
        if not parsed:
            return None
        original = [{"name": name, "domain": domain} for name, domain in parsed.items()]
        parked = [
            {"name": name, "domain": _park_hosts(domain, source_wildcard=source_wildcard, tag=tag)}
            for name, domain in parsed.items()
        ]
        if parked != original:
            await api.update_resource(
                snapshot.collection, snapshot.uuid, {"docker_compose_domains": parked}
            )
            log.info("api.source_domains.parked", uuid=snapshot.uuid, tag=tag)
        return {"docker_compose_domains": original}

    original_fqdn = str(source.get("fqdn") or "")
    if not original_fqdn:
        return None
    parked_fqdn = _park_hosts(original_fqdn, source_wildcard=source_wildcard, tag=tag)
    if parked_fqdn != original_fqdn:
        await api.update_resource(snapshot.collection, snapshot.uuid, {"domains": parked_fqdn})
        log.info("api.source_domains.parked", uuid=snapshot.uuid, tag=tag)
    return {"domains": original_fqdn}


def _unrewritable_server_bound(
    fqdn: str | None, *, source_wildcard: str | None, target_wildcard: str | None
) -> list[str]:
    """Server-bound hosts that could NOT be rewritten, because the target has no
    wildcard to place them on. PURE.

    These get dropped by :func:`_remap_domains` and Coolify then auto-generates a
    sslip.io URL instead of the expected ``…{target-wildcard}`` one — a degraded
    but functional outcome the operator should see, not discover later.
    """
    if not fqdn or not source_wildcard or target_wildcard:
        return []
    out: list[str] = []
    for part in str(fqdn).split(","):
        host = normalise_host(part.strip())
        if host and dns_wildcard.under_wildcard(host, source_wildcard):
            out.append(host)
    return out


async def create_application(
    api: CoolifyClient, snapshot: ResourceSnapshot, placement: Placement, source: dict[str, Any]
) -> str:
    """Create an application. Returns the new uuid."""
    route = create_route(snapshot.kind, auth=snapshot.git_auth)
    segment = route.rsplit("/", 1)[1]

    body = filter_body({**source, **placement.as_body()}, APPLICATION_CREATE)
    body["name"] = snapshot.name
    body["instant_deploy"] = False

    # The GET never carries these — it serialises source_type/source_id and
    # private_key_id, which the PLANNER resolved into the uuids the private
    # create routes require. Without them the filtered body cannot satisfy
    # APPLICATION_ROUTE_REQUIRED and _check below refuses, loudly.
    if snapshot.github_app_uuid:
        body["github_app_uuid"] = snapshot.github_app_uuid
    if snapshot.private_key_uuid:
        body["private_key_uuid"] = snapshot.private_key_uuid

    # Public git apps come back short (owner/repo) but the public route needs a URL.
    if segment == "public" and body.get("git_repository"):
        body["git_repository"] = public_git_url(str(body["git_repository"]))

    # Rewrite the URLs onto the TARGET rather than dropping or copying them.
    # Server-bound URLs (under the source server's wildcard) move to the target's
    # wildcard so the app keeps its subdomain on the new host; custom domains are
    # carried as-is (they move by DNS cutover, gated separately). coolify-mover
    # copies fqdn verbatim, pointing two servers at the same server-bound
    # hostname — which can never work: the wildcard binds it to one server.
    # Read from the SOURCE, not the filtered body: Coolify stores the URL in the
    # `fqdn` column, which is not a create input (``domains`` is) and so is
    # filtered out of `body`.
    raw_fqdn = source.get("fqdn") or source.get("domains")
    target_domains = _remap_domains(
        raw_fqdn,
        source_wildcard=placement.source_wildcard,
        target_wildcard=placement.target_wildcard,
    )
    dropped = _unrewritable_server_bound(
        raw_fqdn,
        source_wildcard=placement.source_wildcard,
        target_wildcard=placement.target_wildcard,
    )
    if dropped:
        log.warning(
            "api.application.wildcard_unresolved",
            name=snapshot.name,
            hosts=dropped,
            hint=(
                "target server has no wildcard_domain — the URL falls back to sslip.io. "
                "Configure it on the server, or pass --target-wildcard."
            ),
        )
    body.pop("fqdn", None)
    if target_domains:
        body["domains"] = target_domains
    else:
        body.pop("domains", None)

    if snapshot.kind is ResourceKind.APP_GIT_COMPOSE:
        # build_pack=dockercompose loads the compose from git (drop the raw) and
        # REJECTS `domains` — per-service domains live in docker_compose_domains,
        # which the create route wants as an ARRAY of {name, domain}. Remap the
        # server-bound URLs inside onto the target so the app keeps its URLs.
        body.pop("docker_compose_raw", None)
        body.pop("domains", None)
        compose_domains = _compose_domains_body(
            source.get("docker_compose_domains"),
            source_wildcard=placement.source_wildcard,
            target_wildcard=placement.target_wildcard,
        )
        if compose_domains:
            body["docker_compose_domains"] = compose_domains
        else:
            body.pop("docker_compose_domains", None)

    # custom_labels / custom_nginx_configuration / dockerfile come back plaintext
    # but the create route validates + decodes them as base64.
    _encode_base64_fields(body)

    # A domain-less stack (e.g. one reached only through a cloudflared tunnel)
    # must STAY domain-less. Coolify auto-generates a wildcard/sslip.io domain
    # when none is given (autogenerate_domain defaults to true), which would plant
    # a URL the source never had. Suppress it ONLY when the SOURCE itself had no
    # domains — not when we merely dropped a server-bound URL we could not remap
    # (no target wildcard), where the sslip.io fallback is the wanted behaviour.
    source_has_domains = bool(source.get("fqdn")) or bool(
        _parse_compose_domains(source.get("docker_compose_domains"))
    )
    if not source_has_domains:
        body["autogenerate_domain"] = False

    required = frozenset({"project_uuid", "server_uuid"}) | APPLICATION_ROUTE_REQUIRED[segment]
    _check(body, required, f"application {snapshot.name!r} via {route}")

    created = await api.post(route, body)
    uuid = (created or {}).get("uuid")
    if not uuid:
        raise CoolifyApiError(
            f"creating application {snapshot.name!r} returned no uuid", body=created
        )
    log.info("api.application.created", name=snapshot.name, uuid=uuid, route=route)
    return str(uuid)


async def create_resource(
    api: CoolifyClient, snapshot: ResourceSnapshot, placement: Placement, source: dict[str, Any]
) -> str:
    """Dispatch to the right create call for this kind. Returns the new uuid."""
    if snapshot.kind is ResourceKind.DATABASE:
        return await create_database(api, snapshot, placement, source)
    if snapshot.kind in (ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE):
        return await create_service(api, snapshot, placement, source)
    return await create_application(api, snapshot, placement, source)


# ── service domains ──────────────────────────────────────────────────────────
#
# A service's domains live on its SUB-APPLICATIONS (service_applications.fqdn);
# the services GET has no fqdn of its own. That column is what the target's proxy
# labels are generated from (parsers.php v4.5.0:3389) and what every directly
# declared SERVICE_FQDN_*/SERVICE_URL_* row is rebuilt from on each parse
# (v4.5.0:2626-2723), so it is the thing to carry. The one write path valid on
# every release in range is PATCH /services/{uuid} `urls`, items exactly
# {name, url} ('array:name,url', ServicesController v4.1.2:971, v4.5.0:1259);
# the per-sub-application PATCH exists only from 4.2.0.
#
# Services are NOT parked before the stop, unlike applications. The target takes
# the source's hosts with force_domain_override, and only when every conflicting
# holder is one of the source's own sub-applications. So nothing is written to
# the live source before FINALIZE: no re-parse of its compose, no 4.5 updateCompose
# rewrite of its env rows, no managed-DNS release job (4.4+) deleting a record it
# still serves — and nothing for a rollback to put back.


def service_app_urls(source: dict[str, Any]) -> dict[str, str]:
    """``{sub-application name: "url,url"}`` as the source routes them. PURE.

    ``url`` before ``fqdn``: from 4.3.15 a saved domain's explicit port moves into
    the hidden ``domain_port_overrides`` and ``fqdn`` comes back portless, while
    the appended ``url`` re-composes it (ServiceApplication v4.5.0:18,215-242).
    Before 4.3.15 the port is inline in ``fqdn`` and ``url`` is absent. Carrying
    ``fqdn`` would route the target to the first exposed port instead.
    """
    out: dict[str, str] = {}
    for app in source.get("applications") or []:
        if not isinstance(app, dict) or not app.get("name"):
            continue
        raw = app.get("url") or app.get("fqdn")
        if not isinstance(raw, str):
            continue
        value = ",".join(part.strip() for part in raw.split(",") if part.strip())
        if value:
            out[str(app["name"])] = value
    return out


def _drop_generated(value: str) -> str:
    """``value`` without its Coolify-generated (sslip.io-style) hosts. PURE.

    A generated host is ``{name}-{uuid}.{server-ip}.sslip.io`` (shared.php
    v4.5.0:1494-1522): it names the SOURCE's uuid and IP, so on the target it
    would route a hostname that resolves to the source. The target's own parse
    generates its own instead.
    """
    return ",".join(
        part for part in value.split(",") if not ((host := normalise_host(part)) and is_generated(host))
    )


def _url_key(value: str) -> tuple[str, ...]:
    """A URL list in comparable form: Coolify lowercases and strips the slash."""
    return tuple(p.strip().rstrip("/").lower() for p in value.split(",") if p.strip())


def service_custom_urls(
    source: dict[str, Any], *, source_wildcard: str | None
) -> list[dict[str, str]]:
    """``[{name, url}]`` for the sub-applications holding a CUSTOM host. PURE.

    Custom hosts are the only ones source and target both claim: a server-bound
    host is remapped onto the target's wildcard and a generated one is not
    carried. Each entry keeps the sub-application's full original ``url`` —
    server-bound and generated parts included — because that is what a restore
    has to put back.
    """
    out: list[dict[str, str]] = []
    for name, value in service_app_urls(source).items():
        hosts = [h for h in (normalise_host(part) for part in value.split(",")) if h]
        if any(
            not is_generated(h) and not dns_wildcard.under_wildcard(h, source_wildcard)
            for h in hosts
        ):
            out.append({"name": name, "url": value})
    return out


def service_restore_body(
    source: dict[str, Any], *, source_wildcard: str | None
) -> dict[str, Any] | None:
    """The body that gives the source back its custom URLs, or ``None``. PURE.

    Journaled as ``parked_domains`` before any write for the resource. Nothing
    parks a service, so a rollback before FINALIZE finds the source unchanged and
    :func:`restore_service_urls` sends nothing; the body matters once FINALIZE has
    released the hosts.
    """
    urls = service_custom_urls(source, source_wildcard=source_wildcard)
    return {"urls": urls} if urls else None


def service_urls_body(
    source: dict[str, Any],
    *,
    target_names: Collection[str],
    source_wildcard: str | None,
    target_wildcard: str | None,
) -> tuple[list[dict[str, str]], list[str]]:
    """The target's ``urls`` entries, and the source sub-applications it lacks. PURE.

    Generated hosts are dropped, server-bound ones rewritten onto the target's
    wildcard, custom ones carried verbatim — port and path kept
    (:func:`_remap_domains`). A sub-application left with nothing is not sent, so
    the target keeps the URL its own parse generated. A name the target does not
    have lands in ``missing``: Coolify answers it with a 422 ("Service container
    with '{name}' not found.", ServicesController v4.1.2:94, v4.5.0:187).
    """
    entries: list[dict[str, str]] = []
    missing: list[str] = []
    for name, value in service_app_urls(source).items():
        url = _remap_domains(
            _drop_generated(value), source_wildcard=source_wildcard, target_wildcard=target_wildcard
        )
        if not url:
            continue
        if name not in target_names:
            missing.append(name)
            continue
        entries.append({"name": name, "url": url})
    return entries, missing


def _conflicts(body: object) -> list[dict[str, Any]]:
    """The ``conflicts`` of a 409 body, tolerating any other shape. PURE."""
    conflicts = body.get("conflicts") if isinstance(body, dict) else None
    if not isinstance(conflicts, list):
        return []
    return [c for c in conflicts if isinstance(c, dict)]


def only_own_conflicts(conflicts: list[dict[str, Any]], own: Collection[str]) -> bool:
    """Whether every holder of a conflicting domain is one of ``own``. PURE.

    ``resource_uuid`` is the HOLDING ServiceApplication's (or Application's) uuid,
    and ``null`` for the instance's own domain (domains.php v4.1.2:142-257,
    v4.5.0:250-335) — which never counts as own. No conflicts is not "own" either.
    """
    holders = [c.get("resource_uuid") for c in conflicts]
    return bool(holders) and all(isinstance(h, str) and h in own for h in holders)


async def finish_service_target(
    api: CoolifyClient,
    snapshot: ResourceSnapshot,
    placement: Placement,
    source: dict[str, Any],
    target_uuid: str,
) -> int:
    """Carry the source's domains, and its network flag, to a created service.

    One ``PATCH /services/{target}``, sent only after the engine journaled the
    uuid. Returns the number of sub-applications whose URL was carried.

    On a 409 it retries ONCE with ``force_domain_override`` — and only when every
    holder is one of the source's own sub-applications, which keep their hosts
    until FINALIZE releases them. Any other holder (another resource of the team,
    the instance) is refused: forcing would put two routers on one host. Note the
    check is port-blind against a portless holder from 4.3.15 (the request key
    keeps its port, the stored fqdn lost it), so a port-bearing URL may never 409
    at all; FINALIZE releases the source's copy either way.
    """
    target = await api.get_resource("services", target_uuid)
    names = {
        str(app["name"])
        for app in target.get("applications") or []
        if isinstance(app, dict) and app.get("name")
    }
    entries, missing = service_urls_body(
        source,
        target_names=names,
        source_wildcard=placement.source_wildcard,
        target_wildcard=placement.target_wildcard,
    )
    for name, value in service_app_urls(source).items():
        dropped = _unrewritable_server_bound(
            value, source_wildcard=placement.source_wildcard, target_wildcard=placement.target_wildcard
        )
        if dropped:
            log.warning(
                "api.service.wildcard_unresolved",
                name=snapshot.name,
                sub_application=name,
                hosts=dropped,
                hint=(
                    "target server has no wildcard_domain — the URL falls back to sslip.io. "
                    "Configure it on the server, or pass --target-wildcard."
                ),
            )
    if missing:
        raise UnsupportedResource(
            f"service {snapshot.name!r}: the target has no sub-application(s) {sorted(missing)} "
            "to carry the source's domains to",
            hint=(
                "The target was created from this instance's CURRENT template catalog, "
                "which names these containers differently, so their domains would be "
                "served by nothing. The source was never stopped and keeps serving; the "
                "created target will be deleted. Migrate this service by hand, or "
                "re-create the source from the current template first."
            ),
        )

    body: dict[str, Any] = {}
    # Rejected on create before 4.5 (AGENTS.md), settable on update. Only when
    # true: the default is false, and a redundant write is just risk.
    if source.get("connect_to_docker_network"):
        body["connect_to_docker_network"] = True
    if entries:
        body["urls"] = entries
    if not body:
        return 0
    body = filter_body(body, SERVICE_UPDATE)

    try:
        await api.update_resource("services", target_uuid, body)
    except CoolifyApiError as exc:
        if exc.status_code != 409:
            raise
        conflicts = _conflicts(exc.body)
        own = {
            str(app["uuid"])
            for app in source.get("applications") or []
            if isinstance(app, dict) and app.get("uuid")
        }
        if not only_own_conflicts(conflicts, own):
            held = "; ".join(str(c.get("message") or c.get("domain")) for c in conflicts)
            raise CoolifyApiError(
                f"service {snapshot.name!r}: the target cannot claim its domain(s): "
                + (held or "Coolify named no holder"),
                status_code=409,
                body=exc.body,
                hint=(
                    "Another resource of this team — or the Coolify instance itself — "
                    "holds it. Free it there, then migrate again; this tool never forces a "
                    "domain away from anything but the source itself. The source was never "
                    "stopped and keeps serving; the created target will be deleted."
                ),
            ) from exc
        log.info("api.service.urls_forced", name=snapshot.name, target=target_uuid)
        await api.update_resource(
            "services", target_uuid, {**body, "force_domain_override": True}
        )

    log.info("api.service.urls_carried", name=snapshot.name, target=target_uuid, count=len(entries))
    return len(entries)


async def restore_service_urls(api: CoolifyClient, uuid: str, body: dict[str, Any]) -> bool:
    """Give a service back the URLs in ``body``, PATCHing only what differs.

    Idempotent, and silent when nothing differs — the normal case, since nothing
    parks a service: a rollback before FINALIZE finds the source as it was. Not
    writing then matters. Every PATCH re-parses the live source's compose
    (v4.5.0:1336-1342), and on 4.5 applyServiceUrls rewrites its
    SERVICE_URL_*/SERVICE_FQDN_* rows on the spot (updateCompose, v4.5.0:214).

    A ``force_domain_override`` in ``body`` is passed on (the caller's 409 retry).
    Returns whether a PATCH was sent.
    """
    wanted = [e for e in body.get("urls") or [] if isinstance(e, dict) and e.get("name")]
    if not wanted:
        return False
    source = await api.get_resource("services", uuid)
    present = {
        str(app.get("name")) for app in source.get("applications") or [] if isinstance(app, dict)
    }
    current = service_app_urls(source)
    diff: list[dict[str, str]] = []
    for entry in wanted:
        name, url = str(entry["name"]), str(entry.get("url") or "")
        if name not in present:
            # A 422 for one vanished container would fail the restore of all.
            log.warning("api.service.restore_skipped", uuid=uuid, sub_application=name)
            continue
        if _url_key(current.get(name, "")) != _url_key(url):
            diff.append({"name": name, "url": url})
    if not diff:
        log.debug("api.service.urls_unchanged", uuid=uuid)
        return False
    patch: dict[str, Any] = {"urls": diff}
    if body.get("force_domain_override"):
        patch["force_domain_override"] = True
    await api.update_resource("services", uuid, filter_body(patch, SERVICE_UPDATE))
    log.info("api.service.urls_restored", uuid=uuid, count=len(diff))
    return True


async def release_service_urls(
    api: CoolifyClient, uuid: str, *, source_wildcard: str | None
) -> None:
    """Blank the source's custom sub-application URLs at FINALIZE. NEVER raises.

    The target serves those hosts now. A kept or renamed source still holding
    them would put a second router on each the moment anyone restarts it — what
    parking prevents for applications — so this runs under KEEP as well as
    RENAME. Blanked, not renamed: a null fqdn is regenerated on the next parse as
    a generated or source-wildcard host, which collide with nothing, whereas an
    ``old-<tag>`` name would collide with the next migration of this project
    (the tag is the project/environment digest, not per run). ``""`` arrives as
    null (ConvertEmptyStringsToNull), and a blank url sets fqdn=null
    (ServicesController v4.1.2:118-121, v4.5.0:209-212).

    From 4.4 removing a hostname queues ReleaseRemovedDnsHostnamesJob; the live
    target uses it, so a Coolify-managed record is re-referenced, not deleted
    (ManagedDnsRecordCleanup v4.5.0:322-333). Not fatal on failure, like
    :func:`release_fqdn`: the source is stopped, so nothing routes it yet.
    """
    try:
        source = await api.get_resource("services", uuid)
        urls = service_custom_urls(source, source_wildcard=source_wildcard)
        if not urls:
            return
        await api.update_resource(
            "services", uuid, {"urls": [{"name": e["name"], "url": ""} for e in urls]}
        )
        log.info("api.service.urls_released", uuid=uuid, count=len(urls))
    except CoolifyApiError as exc:
        log.warning(
            "api.service.urls_release_failed",
            uuid=uuid,
            error=str(exc)[:200],
            hint="Clear the source's domains by hand before restarting it.",
        )


# ── env vars ─────────────────────────────────────────────────────────────────


def build_env_entries(source_envs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepare env vars for the target. PURE.

    Copies ``value``, NOT ``real_value``. ``real_value`` is an accessor that
    RESOLVES Coolify's magic variables — a ``SERVICE_FQDN_*`` would come back
    already expanded to the *source's* domain, and writing that to the target
    would bake the old hostname in permanently. The raw ``value`` keeps the magic
    intact so Coolify re-resolves it for the new resource.

    Entries whose ``value`` key is absent are dropped with no attempt to guess:
    that state means the token lacked ``read:sensitive`` and the caller should
    never have got this far (the client asserts scope at startup).
    """
    entries: list[dict[str, Any]] = []
    for env in source_envs:
        if "value" not in env:
            continue
        entry = filter_body(env, ENV_FIELDS)
        if entry.get("key"):
            entries.append(entry)
    return entries


async def copy_envs(
    api: CoolifyClient, *, collection: str, source_uuid: str, target_uuid: str
) -> int:
    """Copy environment variables source -> target. Returns the count.

    Coolify auto-generates ``SERVICE_*`` variables (passwords, FQDNs) for a new
    service, so the target arrives with its OWN generated secrets. The bulk
    upsert matches by ``key`` and therefore overwrites them with the source's —
    which is essential: the mirrored data belongs to the source's credentials.
    """
    source_envs = await api.get_envs(collection, source_uuid)
    entries = build_env_entries(source_envs)
    if not entries:
        log.info("api.envs.none", source=source_uuid)
        return 0
    await api.set_envs_bulk(collection, target_uuid, entries)
    log.info("api.envs.copied", count=len(entries), target=target_uuid)
    return len(entries)


# ── tags ─────────────────────────────────────────────────────────────────────

#: Upstream requires each tag name to be at least 2 characters. A shorter one on
#: the source is dropped rather than sent: it would 422 the call and take the
#: whole (perfectly good) tag set with it.
_MIN_TAG_LENGTH = 2


async def copy_tags(
    api: CoolifyClient, *, collection: str, source_uuid: str, target_uuid: str
) -> int:
    """Copy tags source -> target. Returns the count copied. NEVER raises.

    Tags are cosmetic metadata, and this runs after the target exists. A failure
    here must not fail a migration that has otherwise succeeded — losing a label
    is an annoyance, aborting a cutover over one is an outage. So every failure
    path is a warning and a zero.

    Deliberately a separate post-create call rather than a ``tags`` create field.
    Upstream does accept ``tags`` on create from 4.2 onward, but putting it in the
    create body places cosmetic metadata on the critical path: on Coolify 4.1.2 an
    unlisted create field is a 422 that destroys the whole resource. Shipping that
    once (2.5.6) broke every migration against every instance that existed. This
    shape works on both, and degrades to "no tags" on the version that lacks the
    route instead of taking the resource down with it.
    """
    try:
        names = await api.get_tag_names(collection, source_uuid)
    except CoolifyApiError as exc:
        log.warning("api.tags.read_failed", uuid=source_uuid, error=str(exc)[:200])
        return 0

    if names is None:
        log.debug("api.tags.route_absent", uuid=source_uuid)
        return 0

    usable = [n for n in names if len(n) >= _MIN_TAG_LENGTH]
    if dropped := [n for n in names if len(n) < _MIN_TAG_LENGTH]:
        log.warning("api.tags.too_short", uuid=source_uuid, names=dropped)
    if not usable:
        return 0

    try:
        await api.add_tags(collection, target_uuid, usable)
    except CoolifyApiError as exc:
        log.warning("api.tags.copy_failed", target=target_uuid, error=str(exc)[:200])
        return 0

    log.info("api.tags.copied", count=len(usable), target=target_uuid)
    return len(usable)


# ── storages ─────────────────────────────────────────────────────────────────


def storage_endpoints(storages: dict[str, Any]) -> list[VolumeEndpoint]:
    """Turn a ``/storages`` response into pairing endpoints. PURE.

    Only persistent storages have a docker volume; file storages are content
    written by ``ServerStorageSaveJob`` and are recreated through the API, not
    mirrored.
    """
    out: list[VolumeEndpoint] = []
    for entry in storages.get("persistent_storages") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        mount_path = entry.get("mount_path")
        if name and mount_path:
            out.append(VolumeEndpoint(name=str(name), mount_path=str(mount_path)))
    return out


async def copy_storages(
    api: CoolifyClient,
    *,
    collection: str,
    source_uuid: str,
    target_uuid: str,
    kind: ResourceKind,
) -> int:
    """Recreate user-defined storages on the target. Returns the count created.

    Deliberately a no-op for compose-backed resources and databases:

    * Compose volumes are materialised by Coolify parsing the compose, and
      ``shouldBeReadOnlyInUI`` makes them un-POSTable.
    * Database volumes are created by the model's ``created`` hook.

    Trying to POST them would 422 at best and double-create at worst. Their
    volumes are read back and paired by mount path instead.
    """
    if kind in (
        ResourceKind.DATABASE,
        ResourceKind.APP_GIT_COMPOSE,
        ResourceKind.SERVICE_TEMPLATE,
        ResourceKind.SERVICE_COMPOSE,
    ):
        log.debug("api.storages.skipped", kind=kind.value, reason="materialised by Coolify")
        return 0

    source = await api.get_storages(collection, source_uuid)
    allowed = STORAGE_CREATE_SERVICE if collection == "services" else STORAGE_CREATE
    created = 0

    for entry in source.get("persistent_storages") or []:
        if not isinstance(entry, dict):
            continue
        body = filter_body(
            {
                "type": "persistent",
                # Upstream re-prefixes with the NEW uuid whatever we send, so we
                # pass the bare name and let it do that.
                "name": _strip_uuid_prefix(str(entry.get("name", "")), source_uuid),
                "mount_path": entry.get("mount_path"),
                # `or None`: a cleared form stores '' — filter_body keeps '', and
                # v4.3.22+ 422s the key itself, whatever its value.
                "host_path": entry.get("host_path") or None,
            },
            allowed,
        )
        if not body.get("mount_path"):
            continue
        await api.create_storage(collection, target_uuid, body)
        created += 1

    for entry in source.get("file_storages") or []:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if isinstance(content, str) and content in (
            "[binary file]",
            "[file too large to display]",
        ):
            # The API cannot round-trip it; the manifest already warned, and the
            # path is mirrored with rsync instead.
            continue
        is_host_file = entry.get("is_host_file") is True
        body = filter_body(
            {
                "type": "file",
                "mount_path": entry.get("mount_path"),
                # A host-file mount points at a file the server already has; the
                # create 422s "Content is not valid for host file mounts" on any.
                "content": None if is_host_file else content,
                "is_directory": entry.get("is_directory"),
                "is_host_file": entry.get("is_host_file"),
                "fs_path": retarget_fs_path(
                    entry.get("fs_path"), kind=kind, source_uuid=source_uuid, target_uuid=target_uuid
                ),
            },
            allowed,
        )
        if not body.get("mount_path"):
            continue
        await api.create_storage(collection, target_uuid, body)
        created += 1

    log.info("api.storages.created", count=created, target=target_uuid)
    return created


def retarget_fs_path(
    fs_path: object, *, kind: ResourceKind, source_uuid: str, target_uuid: str
) -> object:
    """Move a file mount's host path from the source's config dir to the target's. PURE.

    The GET returns ``fs_path`` absolute — ``/data/coolify/applications/{source}/…``.
    Up to 4.3.x the create ignored it for files and confined them to the new
    resource's dir; from 4.4 it honours an absolute path verbatim. Sent unchanged,
    the target's files would live under the SOURCE uuid on the target host, a tree
    no deletion of the target ever removes (and writing outside the resource dir
    needs the deploy ability). Paths outside the config dir are genuine host paths
    and pass through untouched.
    """
    if not isinstance(fs_path, str):
        return fs_path
    source_dir = resource_config_dir(kind, source_uuid)
    if fs_path == source_dir or fs_path.startswith(f"{source_dir}/"):
        return resource_config_dir(kind, target_uuid) + fs_path[len(source_dir) :]
    return fs_path


def _strip_uuid_prefix(name: str, uuid: str) -> str:
    """Remove the ``{uuid}-`` prefix upstream added, so we can send the bare name."""
    prefix = f"{uuid}-"
    return name[len(prefix) :] if name.startswith(prefix) else name


async def read_volume_endpoints(
    api: CoolifyClient, *, collection: str, uuid: str
) -> list[VolumeEndpoint]:
    """Read back what Coolify actually created, for pairing.

    THE authority for target volume names. We never predict them: ``POST
    /storages`` forces ``{uuid}-{name}``, DB hooks force ``{engine}-data-{uuid}``,
    and compose volumes are re-derived on parse. What Coolify says exists is what
    exists.
    """
    storages = await api.get_storages(collection, uuid)
    return storage_endpoints(storages)


# ── lifecycle ────────────────────────────────────────────────────────────────


async def rename(api: CoolifyClient, collection: str, uuid: str, name: str) -> None:
    await api.update_resource(collection, uuid, {"name": name})
    log.info("api.renamed", uuid=uuid, name=name)


async def release_fqdn(
    api: CoolifyClient, collection: str, uuid: str, *, kind: ResourceKind | None = None
) -> None:
    """Clear the source's domains so the old proxy stops claiming them.

    Without this, a kept-or-renamed source still has its FQDN, so the old host's
    Traefik keeps the router rule and keeps renewing the certificate — quietly
    consuming ACME budget for a hostname it no longer serves.

    A dockercompose app is special: its ``domains`` field is rejected (422), so
    it is released by resubmitting docker_compose_domains with blank domains.
    """
    if collection != "applications":
        return
    try:
        if kind is ResourceKind.APP_GIT_COMPOSE:
            full = await api.get_resource("applications", uuid)
            blanked = _blank_compose_domains(full.get("docker_compose_domains"))
            if blanked is None:
                return
            body: dict[str, Any] = {"docker_compose_domains": blanked}
        else:
            body = {"domains": ""}
        await api.update_resource(collection, uuid, body)
        log.info("api.fqdn.released", uuid=uuid)
    except CoolifyApiError as exc:
        # Not fatal: the source is stopped, so its labels are gone anyway. Worth
        # saying out loud rather than swallowing.
        log.warning("api.fqdn.release_failed", uuid=uuid, error=str(exc)[:200])
