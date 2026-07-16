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
from typing import Any

import structlog

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.fields import (
    APPLICATION_CREATE,
    APPLICATION_ROUTE_REQUIRED,
    ENV_FIELDS,
    SERVICE_CREATE,
    SERVICE_CREATE_CUSTOM_COMPOSE,
    STORAGE_CREATE,
    STORAGE_CREATE_SERVICE,
    database_allowed,
    filter_body,
    missing_required,
)
from bg_coolify_migrate.dns import wildcard as dns_wildcard
from bg_coolify_migrate.dns.extract import normalise_host
from bg_coolify_migrate.domain.kinds import ResourceKind, create_route
from bg_coolify_migrate.domain.naming import VolumeEndpoint
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


def _encode_base64_fields(body: dict[str, Any]) -> None:
    """Base64-encode the plaintext-in/base64-out create fields, in place.

    An empty value is dropped, not encoded: Coolify decodes then runs a UTF-8
    check that an empty string fails, so sending ``""`` is itself a 422.
    """
    for field in _BASE64_CREATE_FIELDS:
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
    """
    server = await api.get_server(server_uuid)
    destinations = server.get("destinations") or server.get("standalone_dockers") or []
    if not isinstance(destinations, list) or len(destinations) <= 1:
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

    # connect_to_docker_network is rejected on create (validated out at line 332)
    # but settable on update. Carry it explicitly, so a source that had the
    # service on the predefined network does not silently come up off it. Only
    # when true — the default is false, and a redundant PATCH is just risk.
    if source.get("connect_to_docker_network"):
        await api.patch(f"/services/{uuid}", {"connect_to_docker_network": True})

    log.info("api.service.created", name=snapshot.name, uuid=uuid, custom_compose=is_custom)
    return str(uuid)


_GIT_URL_PREFIXES = ("https://", "http://", "git://", "git@")


def _public_git_url(git_repository: str) -> str:
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
                urls.append(f"https://{remapped}")
        else:
            # Custom (or, with no source wildcard known, treated as custom): it is
            # server-independent and moves with the app verbatim.
            urls.append(stripped)
    # Dedup while preserving order — a source can list the same host twice.
    return ",".join(dict.fromkeys(urls))


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

    # Public git apps come back short (owner/repo) but the public route needs a URL.
    if segment == "public" and body.get("git_repository"):
        body["git_repository"] = _public_git_url(str(body["git_repository"]))

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
        # build_pack=dockercompose forces ports_exposes='80' upstream and REJECTS
        # `domains` (docker_compose_domains is the field). Its compose — and the
        # per-service domains inside it — are loaded from git, so both are dropped.
        body.pop("docker_compose_raw", None)
        body.pop("docker_compose_domains", None)
        body.pop("domains", None)

    # custom_labels / custom_nginx_configuration / dockerfile come back plaintext
    # but the create route validates + decodes them as base64.
    _encode_base64_fields(body)

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
                "host_path": entry.get("host_path"),
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
        body = filter_body(
            {
                "type": "file",
                "mount_path": entry.get("mount_path"),
                "content": content,
                "is_directory": entry.get("is_directory"),
                "fs_path": entry.get("fs_path"),
            },
            allowed,
        )
        if not body.get("mount_path"):
            continue
        await api.create_storage(collection, target_uuid, body)
        created += 1

    log.info("api.storages.created", count=created, target=target_uuid)
    return created


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


async def release_fqdn(api: CoolifyClient, collection: str, uuid: str) -> None:
    """Clear the source's domains so the old proxy stops claiming them.

    Without this, a kept-or-renamed source still has its FQDN, so the old host's
    Traefik keeps the router rule and keeps renewing the certificate — quietly
    consuming ACME budget for a hostname it no longer serves.
    """
    body: dict[str, Any] = {"domains": ""} if collection == "applications" else {}
    if not body:
        return
    try:
        await api.update_resource(collection, uuid, body)
        log.info("api.fqdn.released", uuid=uuid)
    except CoolifyApiError as exc:
        # Not fatal: the source is stopped, so its labels are gone anyway. Worth
        # saying out loud rather than swallowing.
        log.warning("api.fqdn.release_failed", uuid=uuid, error=str(exc)[:200])
