"""Builds a MigrationPlan from live data. Reads only — never mutates.

This is what makes ``coolify-migrate plan`` meaningful. It exercises every risky
decision — discovery, volume classification, drift assessment, DNS — and produces
the complete plan, without changing anything. If `plan` is clean, `run` has
already had its judgement calls made.

Contrast with ``coolify-mover --dry-run``, which short-circuits *before* all its
SQL and rsync code and therefore validates none of the parts that break.

Note the manifest built here is **provisional**: a running stack can still create
volumes. The authoritative one is taken after the quiesce, by the DISCOVER step.
Planning against a provisional manifest is fine — its job is to catch problems
early, not to be the source of truth.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import structlog

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.fields import (
    application_settings_warnings,
    database_health_check_warnings,
    env_key_warnings,
)
from bg_coolify_migrate.discovery import docker
from bg_coolify_migrate.discovery.docker import (
    LABEL_ENVIRONMENT,
    LABEL_PROJECT,
    LABEL_RESOURCE,
)
from bg_coolify_migrate.dns import gate as dns_gate
from bg_coolify_migrate.dns import managed as dns_managed
from bg_coolify_migrate.dns import resolve as dns_resolve
from bg_coolify_migrate.dns import wildcard as dns_wildcard
from bg_coolify_migrate.domain import compose as compose_mod
from bg_coolify_migrate.domain import domains, fidelity
from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.deployments import image_scan_gate_warnings
from bg_coolify_migrate.domain.drift import RebuildDriftReport, assess_rebuild_drift
from bg_coolify_migrate.domain.kinds import (
    BuildPack,
    DatabaseEngine,
    ResourceKind,
    always_builds,
    classify,
    git_auth,
    may_build,
)
from bg_coolify_migrate.domain.manifest import (
    DockerMount,
    DockerVolume,
    VolumeManifest,
    reconcile,
)
from bg_coolify_migrate.domain.naming import slugify
from bg_coolify_migrate.domain.plan import (
    HostBaseline,
    MigrationPlan,
    ResourcePlan,
    ResourceRow,
    ResourceSnapshot,
    ServerRef,
    TransferMode,
    select_strategy,
)
from bg_coolify_migrate.domain.schedules import may_hold_volume_backups
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.domain.versions import Version, parse_version
from bg_coolify_migrate.engine import env_recovery
from bg_coolify_migrate.engine.schedules import plan_schedules
from bg_coolify_migrate.errors import (
    EmptyEnvironment,
    MigrationError,
    PreflightError,
    UnsupportedResource,
)
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)


def decode_compose(raw: str | None) -> str | None:
    """Decode ``docker_compose_raw``, which may or may not be base64.

    Coolify stores it decoded but accepts it encoded, and different endpoints
    have returned different things over time. Probing beats assuming: a compose
    we fail to decode is a compose whose volumes we cannot enumerate.
    """
    if not raw:
        return None
    stripped = raw.strip()
    if stripped.startswith(("version:", "services:", "#", "name:")):
        return raw
    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return raw
    return decoded if decoded.strip() else raw


def server_ref(server: dict[str, Any]) -> ServerRef:
    # The wildcard base lives under the eager-loaded `settings` relation
    # (servers are the one endpoint Coolify loads it for). It is how a resource
    # gets a default URL, and thus what we rewrite that URL onto for the target.
    settings = server.get("settings")
    wildcard = str(settings.get("wildcard_domain") or "") if isinstance(settings, dict) else ""
    prunes, cleanup = fidelity.docker_cleanup(server)
    return ServerRef(
        log_drain_enabled=fidelity.server_log_drain_enabled(server),
        uuid=str(server.get("uuid", "")),
        name=str(server.get("name", "?")),
        ip=str(server.get("ip", "")),
        # `or "root"`, not a get-default: Coolify's localhost self-record carries
        # user='' (empty, not missing), so `get("user", "root")` returns "" and
        # F2 would SSH with an empty username — Permission denied. The DB column
        # even defaults to 'root'; the record just overrides it with blank. Same
        # shape as the port fallback right below. Found by the F2 e2e migration.
        user=str(server.get("user") or "root"),
        port=int(server.get("port", 22) or 22),
        wildcard_domain=wildcard,
        prunes_unused_volumes=prunes,
        docker_cleanup=cleanup,
    )


async def find_server(api: CoolifyClient, name_or_uuid: str) -> dict[str, Any]:
    for server in await api.list_servers():
        if name_or_uuid in (server.get("uuid"), server.get("name")):
            return server
    raise PreflightError(
        f"no server named {name_or_uuid!r}",
        hint="Run `coolify-migrate doctor` to list servers.",
    )


async def find_project(api: CoolifyClient, name_or_uuid: str) -> dict[str, Any]:
    for project in await api.list_projects():
        if name_or_uuid in (project.get("uuid"), project.get("name")):
            return project
    raise PreflightError(
        f"no project named {name_or_uuid!r}",
        hint=(
            "Run `coolify-migrate list` to see every project and the server it runs on. "
            "The name is the project's, not `team/app` — pass just the project name."
        ),
    )


#: Coolify's environment endpoint groups databases by engine, one key each, and
#: the plurals are irregular (`redis`, not `redises`; `dragonflies`, not
#: `dragonflys`). There is no `databases` key — the Environment model has a
#: databases() method, but the API controller never calls it.
_ENGINE_KEYS = (
    "postgresqls",
    "redis",
    "mongodbs",
    "mysqls",
    "mariadbs",
    "keydbs",
    "dragonflies",
    "clickhouses",
)


def server_uuid_of(resource: dict[str, Any]) -> str | None:
    """The uuid of the server a resource runs on, whatever shape it arrived in.

    The three kinds hang their server off different relations, so there is no one
    field to read:

    * Service       — ``server()`` belongsTo, so ``server`` / ``server_uuid``
    * Application   — ``destination()`` morphTo only; server via the destination
    * Standalone DB — same as Application

    The destination is a StandaloneDocker or a SwarmDocker (hence morphTo), and
    both belong to a server. Reading ``server_uuid`` alone — which is what this
    used to do — finds nothing on an application or a database, and the migration
    stops at "could not determine the source server" for the two kinds that
    matter most.

    Returns None when only ``destination.server_id`` is available; the caller
    resolves that against /servers, since it needs an API round trip.
    """
    direct = resource.get("server_uuid")
    if direct:
        return str(direct)

    server = resource.get("server")
    if isinstance(server, dict) and server.get("uuid"):
        return str(server["uuid"])

    destination = resource.get("destination")
    if isinstance(destination, dict):
        nested = destination.get("server")
        if isinstance(nested, dict) and nested.get("uuid"):
            return str(nested["uuid"])
    return None


def _uuid_of(server: dict[str, Any] | None) -> str | None:
    """A server record's uuid, or None."""
    return str(server["uuid"]) if server and server.get("uuid") else None


#: A resource whose GET names no server it could be placed on. Refused when the
#: plan is built — the plan's own check (plan.foreign_server_refusal) cannot
#: tell an unknown server from an old plan.json, and refuses neither.
_UNPLACED_RESOURCE = (
    "the API did not say which server this resource runs on, so the plan cannot "
    "tell it runs on the source server, where the run looks for its containers and "
    "volumes. Check the resource's server in Coolify, then plan again."
)


async def resolve_server(api: CoolifyClient, resource: dict[str, Any]) -> dict[str, Any] | None:
    """The server record a resource runs on, resolving by id if need be."""
    uuid = server_uuid_of(resource)
    if uuid:
        return await api.get_server(uuid)

    # Nothing nested: fall back to the numeric id on the destination. Present
    # even when the relation was not eager-loaded.
    destination = resource.get("destination")
    server_id = destination.get("server_id") if isinstance(destination, dict) else None
    if server_id is None:
        return None
    for server in await api.list_servers():
        if server.get("id") == server_id:
            return server
    return None


async def environment_resources(
    api: CoolifyClient, project_uuid: str, environment: str
) -> list[tuple[str, dict[str, Any]]]:
    """``(collection, resource)`` for everything in one environment.

    Reads the environment endpoint and then cross-checks databases against the
    flat ``/databases`` list. The second pass is not belt-and-braces, it is the
    only way to see three of the eight engines:

        $environment->load(['applications', 'postgresqls', 'redis',
                            'mongodbs', 'mysqls', 'mariadbs', 'services']);

    That is the whole eager-load in ProjectController. `keydbs`, `dragonflies`
    and `clickhouses` are relations on the model that the controller forgets, so
    the endpoint simply never mentions them — no key, no error. Trusting it alone
    means a project with a ClickHouse migrates and leaves the ClickHouse behind,
    which is precisely the silent loss this tool exists to prevent. `/databases`
    goes through `$project->databases()`, which merges all eight.

    We read the per-engine keys anyway rather than skipping straight to the flat
    list: they are cheap, they are authoritative for the five that do load, and
    if upstream ever fixes the eager-load we pick the rest up without a change.
    """
    detail = await api.get(f"/projects/{project_uuid}/{environment}")
    if not isinstance(detail, dict):
        raise PreflightError(f"environment {environment!r} not found in project {project_uuid}")

    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()

    def take(collection: str, resource: object) -> None:
        if not isinstance(resource, dict):
            return
        uuid = str(resource.get("uuid", ""))
        if not uuid or uuid in seen:
            return
        seen.add(uuid)
        out.append((collection, resource))

    for collection in ("applications", "services"):
        for resource in detail.get(collection) or []:
            take(collection, resource)

    # `databases` for the shapes that have it; the per-engine keys for the shape
    # this Coolify actually returns.
    for key in ("databases", *_ENGINE_KEYS):
        for resource in detail.get(key) or []:
            take("databases", resource)

    environment_id = detail.get("id")
    if environment_id is not None:
        for resource in await api.get("/databases") or []:
            if isinstance(resource, dict) and resource.get("environment_id") == environment_id:
                take("databases", resource)

    if out:
        return out

    # Flat shape fallback, for versions that answer with one typed list.
    for resource in detail.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        kind = str(resource.get("type", ""))
        if "database" in kind or kind in {e.value for e in DatabaseEngine}:
            take("databases", resource)
        elif kind == "service":
            take("services", resource)
        else:
            take("applications", resource)
    return out


def _server_uuid_from_record(record: dict[str, Any], uuid_by_id: dict[Any, str]) -> str | None:
    """Server uuid for a resource record, without an extra API round trip.

    ``server_uuid_of`` reads the three relation shapes; this adds the numeric
    ``destination.server_id`` fallback resolved against a prebuilt id->uuid map, so
    a whole listing needs one ``/servers`` call rather than one ``get_server`` per
    resource.
    """
    uuid = server_uuid_of(record)
    if uuid:
        return uuid
    destination = record.get("destination")
    server_id = destination.get("server_id") if isinstance(destination, dict) else None
    if server_id is not None:
        return uuid_by_id.get(server_id)
    return None


def _server_maps(raw_servers: list[dict[str, Any]]) -> tuple[dict[str, str], dict[Any, str]]:
    """``(uuid -> name, id -> uuid)`` for resolving a resource's server in one pass."""
    name_by_uuid = {str(s.get("uuid")): str(s.get("name", "?")) for s in raw_servers}
    uuid_by_id = {
        s["id"]: str(s["uuid"]) for s in raw_servers if s.get("id") is not None and s.get("uuid")
    }
    return name_by_uuid, uuid_by_id


async def _rows_for_project(
    api: CoolifyClient,
    project: dict[str, Any],
    name_by_uuid: dict[str, str],
    uuid_by_id: dict[Any, str],
) -> list[ResourceRow]:
    """Every resource of one project as fully-qualified :class:`ResourceRow`.

    Reuses :func:`environment_resources`, so the set is the authoritative one (it
    catches the three database engines Coolify's environment endpoint forgets). The
    server is resolved from the environment record, falling back to a per-resource
    GET only when that record omits the destination relation.
    """
    project_uuid = str(project.get("uuid", ""))
    project_name = str(project.get("name", project_uuid))

    detail = await api.get_project(project_uuid)
    env_names = [
        str(env["name"])
        for env in detail.get("environments") or []
        if isinstance(env, dict) and env.get("name")
    ] or ["production"]

    rows: list[ResourceRow] = []
    for environment in env_names:
        try:
            resources = await environment_resources(api, project_uuid, environment)
        except MigrationError:
            # A missing environment or a transient read is not fatal to the whole
            # listing — skip this one, keep enumerating the rest.
            continue
        for collection, record in resources:
            server_uuid = _server_uuid_from_record(record, uuid_by_id)
            if server_uuid is None:
                full = await api.get_resource(collection, str(record.get("uuid", "")))
                server_uuid = _server_uuid_from_record(full, uuid_by_id)
            rows.append(
                ResourceRow(
                    project=project_name,
                    project_uuid=project_uuid,
                    environment=environment,
                    name=str(record.get("name") or record.get("uuid") or "?"),
                    uuid=str(record.get("uuid", "")),
                    # Singularise the collection for display: applications -> application.
                    kind=collection[:-1] if collection.endswith("s") else collection,
                    server=name_by_uuid.get(server_uuid or "", ""),
                    server_uuid=server_uuid or "",
                )
            )
    return rows


async def list_all_resources(
    api: CoolifyClient,
) -> tuple[tuple[ResourceRow, ...], tuple[ServerRef, ...]]:
    """Every resource across every project and environment. Reads only.

    The whole discovery answer in one call: what can I migrate, and from where.
    ``doctor`` proves the token and lists servers; this lists everything else so an
    operator never has to piece it together. Deliberately does NOT require
    ``read:sensitive`` — names, uuids and servers are not secret.
    """
    raw_servers = await api.list_servers()
    servers = tuple(server_ref(s) for s in raw_servers)
    name_by_uuid, uuid_by_id = _server_maps(raw_servers)

    rows: list[ResourceRow] = []
    for project in await api.list_projects():
        if not project.get("uuid"):
            continue
        rows.extend(await _rows_for_project(api, project, name_by_uuid, uuid_by_id))
    return tuple(rows), servers


async def list_project_resources(
    api: CoolifyClient, name_or_uuid: str
) -> tuple[str, tuple[ResourceRow, ...], tuple[ServerRef, ...]]:
    """One project's resources (across its environments). Reads only.

    ``list <project>`` — the same tree as :func:`list_all_resources`, scoped to one
    project so it fetches only that project's environments.
    """
    raw_servers = await api.list_servers()
    servers = tuple(server_ref(s) for s in raw_servers)
    name_by_uuid, uuid_by_id = _server_maps(raw_servers)

    project = await find_project(api, name_or_uuid)
    rows = await _rows_for_project(api, project, name_by_uuid, uuid_by_id)
    return str(project.get("name", name_or_uuid)), tuple(rows), servers


def _engine_of(collection: str, resource: dict[str, Any]) -> DatabaseEngine | None:
    if collection != "databases":
        return None
    raw = str(resource.get("type") or resource.get("database_type") or "")
    # Coolify reports e.g. "standalone-postgresql" or "postgresql".
    for engine in DatabaseEngine:
        if engine.value in raw:
            return engine
    image = str(resource.get("image") or "")
    for engine in DatabaseEngine:
        if engine.value in image or (engine is DatabaseEngine.POSTGRESQL and "postgres" in image):
            return engine
    return None


def resource_labels(*, project: str, environment: str, name: str) -> dict[str, str]:
    """Label filter identifying ONE resource's containers on the daemon.

    Coolify's own code finds containers with `--filter label=coolify.{kind}Id={id}`,
    and copying that from outside is a trap: every API controller calls
    `makeHidden(['id', ...])`, so the numeric id is never disclosed — not with a
    root token, not with read:sensitive, not ever. Filtering on it means
    filtering on the empty string, which matches nothing and reports no error.
    The stack looks like it has no volumes and the migration moves nothing.

    What every managed container does carry, from `defaultLabels()` and
    `defaultDatabaseLabels()` alike, is the slugified project / environment /
    resource-name triple. That is visible from outside and is what we use.

    Slugified with our own slugify, which has to agree with Laravel's Str::slug
    byte for byte — see test_slug_matches_laravel in the e2e suite, because a
    disagreement here means a filter that silently matches nothing.
    """
    return {
        LABEL_PROJECT: slugify(project),
        LABEL_ENVIRONMENT: slugify(environment),
        LABEL_RESOURCE: slugify(name),
    }


async def _resolve_git_auth(
    api: CoolifyClient, full: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Resolve the credentials a git app's create route will need.

    Returns ``(github_app_uuid, private_key_uuid)`` — at most one set.

    Transcribed from ``Application::deploymentType()`` (v4.1.2): a real deploy
    key (``private_key_id > 0``) takes precedence over the source; the localhost
    key (``id == 0``) counts only when no source is configured; otherwise the
    ``source`` morph decides.

    Why this exists: the GET serialises the raw columns — ``source_type``,
    ``source_id``, ``private_key_id`` — and NEVER a ``github_app_uuid``, which is
    a create-request field. This tool used to read the create field off the GET,
    found nothing, and classified every GitHub-App-backed application as PUBLIC —
    so the target was created credential-less and its LoadComposeFile died asking
    for a Username (2026-07-22, twice). The ids are mapped to the uuids
    the create routes want via /github-apps and /security/keys; an unmappable id
    is an ERROR, never a fall-back to public, because silently-public is exactly
    the failure being prevented.

    Raises:
        UnsupportedResource: For a GitLab-App source — no release up to 4.7.1
            has a create route that can reproduce it — or a Gitea-App source:
            its route (``private-gitea-app``, routes/api.php v4.7.0:266) is not
            wired here, so it is refused on every release rather than created
            as public. Also for a source id the token cannot see.
    """
    if not full.get("git_repository"):
        return None, None

    name = str(full.get("name") or full.get("uuid") or "?")
    private_key_id = full.get("private_key_id")
    source_type = str(full.get("source_type") or "")
    source_id = full.get("source_id")

    if isinstance(private_key_id, int) and private_key_id > 0:
        return None, await _private_key_uuid(api, private_key_id, resource=name)

    if source_type and source_id is not None:
        if not source_type.endswith("GithubApp"):
            raise UnsupportedResource(
                f"{name}: git source {source_type!r} cannot be recreated",
                hint=(
                    "This tool recreates public, GitHub-App and deploy-key sources "
                    "only. Gitea-App sources are not supported yet (Coolify's create "
                    "route for them is new in 4.7.0), and Coolify has none for "
                    "GitLab-App sources. Recreate the application with one of those "
                    "sources, then migrate."
                ),
            )
        apps = await api.get("/github-apps")
        match = next((a for a in apps if isinstance(a, dict) and a.get("id") == source_id), None)
        if match is None:
            raise UnsupportedResource(
                f"{name}: its GitHub App (source_id={source_id}) is not visible to this API token",
                hint=(
                    "GET /github-apps lists the token team's apps plus system-wide "
                    "ones; this app's source is neither. Without its uuid the target "
                    "cannot be created with credentials — and creating it public "
                    "would strand it unable to clone. Make the GitHub App "
                    "system-wide, or use a token of the owning team."
                ),
            )
        if match.get("is_public"):
            # The seeded "Public GitHub" source: same clone behaviour as the
            # public route, so classify it as such rather than passing a uuid.
            return None, None
        return str(match.get("uuid")), None

    if private_key_id == 0:
        return None, await _private_key_uuid(api, 0, resource=name)

    return None, None


async def _private_key_uuid(api: CoolifyClient, key_id: int, *, resource: str) -> str:
    keys = await api.get("/security/keys")
    match = next((k for k in keys if isinstance(k, dict) and k.get("id") == key_id), None)
    if match is None or not match.get("uuid"):
        raise UnsupportedResource(
            f"{resource}: its deploy key (private_key_id={key_id}) is not visible "
            "to this API token",
            hint=(
                "GET /security/keys lists the token team's keys; this app's key is "
                "not among them, so the target cannot be created with credentials."
            ),
        )
    return str(match["uuid"])


async def snapshot_resource(
    api: CoolifyClient,
    source_host: RemoteHost,
    *,
    collection: str,
    resource: dict[str, Any],
    project: str,
    environment: str,
) -> tuple[ResourceSnapshot, list[docker.Container], dict[str, Any]]:
    """Capture everything the planner needs about one resource.

    Returns the snapshot, its containers, and the full API record — the last so
    callers can read fields the snapshot does not model without fetching the same
    resource again.
    """
    uuid = str(resource["uuid"])
    full = await api.get_resource(collection, uuid)

    build_pack_raw = full.get("build_pack")
    build_pack = BuildPack(build_pack_raw) if build_pack_raw else None
    service_type = full.get("service_type") or (
        full.get("type") if collection == "services" else None
    )
    kind = classify(collection, build_pack=build_pack, service_type=service_type)

    compose_raw = decode_compose(full.get("docker_compose_raw"))

    # Does it build? NOT a property of the kind: a compose declaring `build:`
    # builds from source exactly like a nixpacks app.
    builds = always_builds(kind, build_pack=build_pack)
    if not builds and may_build(kind) and compose_raw:
        try:
            builds = compose_mod.builds_from_source(compose_mod.parse(compose_raw))
        except compose_mod.ComposeError as exc:
            log.warning("planner.compose_unparseable", uuid=uuid, error=str(exc)[:200])

    containers = await resource_containers(
        source_host, project=project, environment=environment, name=str(full.get("name", ""))
    )
    base = [c for c in containers if not c.is_preview]
    running_image = await docker.image_of(source_host, base[0].id or base[0].name) if base else None

    github_app_uuid, private_key_uuid = await _resolve_git_auth(api, full)

    snapshot = ResourceSnapshot(
        uuid=uuid,
        name=str(full.get("name", uuid)),
        collection=collection,
        kind=kind,
        build_pack=build_pack,
        engine=_engine_of(collection, full),
        service_type=str(service_type) if service_type else None,
        image=str(full["image"]) if full.get("image") else None,
        git_repository=full.get("git_repository"),
        git_branch=full.get("git_branch"),
        git_auth=git_auth(
            git_repository=full.get("git_repository"),
            github_app_uuid=github_app_uuid,
            private_key_uuid=private_key_uuid,
        ),
        github_app_uuid=github_app_uuid,
        private_key_uuid=private_key_uuid,
        docker_compose_raw=compose_raw,
        running_image=running_image,
        builds=builds,
        has_previews=any(c.is_preview for c in containers),
    )
    return snapshot, containers, full


async def resource_containers(
    source_host: RemoteHost, *, project: str, environment: str, name: str
) -> list[docker.Container]:
    """Every container of one resource, running or not (`docker ps -a`)."""
    return await docker.list_containers(
        source_host,
        label_filters=resource_labels(project=project, environment=environment, name=name),
    )


async def inspect_all_mounts(
    source_host: RemoteHost, containers: list[docker.Container]
) -> list[DockerMount]:
    """Every mount declared by these containers.

    Split out of build_manifest because of *when* it has to run. Coolify's stop
    is `docker stop` followed by **`docker rm -f`** — in StopDatabase,
    StopApplication and StopService alike — so once a stack is quiesced its
    containers are gone, and with them the only record of anonymous volumes and
    bind mounts. This must be called while they still exist.
    """
    mounts: list[DockerMount] = []
    for container in containers:
        mounts.extend(await docker.inspect_mounts(source_host, container.id or container.name))
    return mounts


async def build_manifest(
    source_host: RemoteHost,
    *,
    mounts: list[DockerMount],
    api_storages: dict[str, Any] | None,
    uuid: str,
    measure: bool = True,
) -> VolumeManifest:
    """Reconcile the three discovery sources into a manifest.

    docker inspect is the truth, the API is the intent, `volume ls` is the
    residue. Each alone misses something — see domain/manifest.py.

    Takes mounts rather than containers precisely because the caller may no
    longer have any: see inspect_all_mounts.
    """

    volumes: list[DockerVolume] = []
    try:
        volumes = await docker.list_volumes(source_host, name_filter=uuid)
    except Exception as exc:
        log.debug("planner.volume_ls_failed", error=str(exc)[:120])

    storages: list[Any] = []
    if api_storages:
        from bg_coolify_migrate.domain.manifest import ApiStorage

        for entry in api_storages.get("persistent_storages") or []:
            if isinstance(entry, dict) and entry.get("mount_path"):
                storages.append(
                    ApiStorage(
                        kind="persistent",
                        name=entry.get("name"),
                        mount_path=str(entry["mount_path"]),
                        host_path=entry.get("host_path"),
                    )
                )
        for entry in api_storages.get("file_storages") or []:
            if isinstance(entry, dict) and entry.get("mount_path"):
                content = entry.get("content")
                storages.append(
                    ApiStorage(
                        kind="file",
                        mount_path=str(entry["mount_path"]),
                        is_directory=entry.get("is_directory"),
                        content_is_placeholder=isinstance(content, str)
                        and content in ("[binary file]", "[file too large to display]"),
                    )
                )

    manifest = reconcile(
        docker_mounts=mounts,
        api_storages=storages,
        docker_volumes=volumes,
        uuid_prefixes=frozenset({uuid}),
    )

    if not measure:
        return manifest

    # Sizes drive a PROPORTIONAL disk check. Geczy checks a fixed 1 GB floor and
    # never compares against the total it just computed.
    sized = []
    for item in manifest.items:
        if item.decision.value != "migrate":
            sized.append(item)
            continue
        size, count = await docker.path_size(source_host, item.source_path)
        sized.append(item.model_copy(update={"bytes": size, "file_count": count}))
    return VolumeManifest(items=tuple(sized), warnings=manifest.warnings)


def resource_images(snapshot: ResourceSnapshot) -> tuple[str, ...]:
    """Every image reference this resource will pull. PURE.

    We build the target with the SAME references, so these are exactly what the
    target will resolve — possibly to a different image than the source runs. A
    service's are its own compose's, template services included: their target is
    given the source's compose right after the create
    (resources.carry_source_compose), not the catalog's current one.
    """
    images: list[str] = []
    if snapshot.image:
        images.append(snapshot.image)
    if snapshot.docker_compose_raw:
        try:
            doc = compose_mod.parse(snapshot.docker_compose_raw)
        except compose_mod.ComposeError:
            return tuple(images)
        for body in compose_mod.services(doc).values():
            image = body.get("image")
            # A service that BUILDs has no upstream tag to drift on; its `image`
            # names the build output.
            if image and not body.get("build"):
                images.append(str(image))
    return tuple(dict.fromkeys(images))


def pulled_images(snapshot: ResourceSnapshot, full: dict[str, Any]) -> tuple[str, ...]:
    """Every image reference the target's server pulls for this resource. PURE.

    :func:`resource_images`, plus a dockerimage application's image: its GET has
    no ``image``, the reference is ``docker_registry_image_name``, which CREATE
    carries for that build pack (api/fields APPLICATION_ROUTE_REQUIRED). A build's
    base images are inside a Dockerfile no GET returns, so they are not here; what
    a build PUSHES is domain/fidelity.pushed_image_name.
    """
    images = list(resource_images(snapshot))
    name = full.get("docker_registry_image_name")
    if snapshot.kind is ResourceKind.APP_DOCKERIMAGE and isinstance(name, str) and name.strip():
        images.append(name.strip())
    return tuple(dict.fromkeys(images))


async def assess_drift(
    source_host: RemoteHost, snapshot: ResourceSnapshot
) -> RebuildDriftReport | None:
    """Compare what the target will run against what the source runs."""
    images = resource_images(snapshot)
    is_database = snapshot.kind is ResourceKind.DATABASE

    if not snapshot.builds:
        # Not building does not mean not drifting: a floating image tag still
        # resolves at deploy time.
        if not images:
            return None
        return assess_rebuild_drift(
            resource_name=snapshot.name,
            builds=False,
            images=images,
            is_database=is_database,
        )

    head_commit: str | None = None
    if snapshot.git_repository and snapshot.git_branch:
        result = await source_host.run(
            f"git ls-remote {snapshot.git_repository} refs/heads/{snapshot.git_branch} "
            "2>/dev/null | head -1 | cut -f1"
        )
        head_commit = result.stdout.strip() or None if result.ok else None

    return assess_rebuild_drift(
        resource_name=snapshot.name,
        builds=True,
        running_commit=snapshot.running_commit,
        head_commit=head_commit,
        images=images,
        is_database=is_database,
    )


async def build_plan(
    api: CoolifyClient,
    source_host: RemoteHost,
    *,
    project: str,
    environment: str,
    target_server: str,
    only_resource: str | None = None,
    finalize_policy: FinalizePolicy = FinalizePolicy.RENAME,
    transfer_mode: TransferMode = TransferMode.AUTO,
    source_wildcard: str | None = None,
    target_wildcard: str | None = None,
    measure: bool = True,
) -> MigrationPlan:
    """Produce the complete plan. Reads only.

    ``only_resource`` (a resource name or uuid) narrows the plan to a single
    resource; the whole environment is planned otherwise. The narrowing is what
    makes resource-scoped migration safe — see :func:`observed_labels`.
    """
    project_data = await find_project(api, project)
    project_uuid = str(project_data["uuid"])
    # The label filter that discovers a resource's containers slugifies the
    # project NAME (Coolify writes coolify.projectName=Str::slug(name)). `project`
    # here may be a UUID — a resource-scoped run resolves to (project_uuid, ...) —
    # so we MUST slugify the resolved name, not the raw arg. Getting this wrong
    # matched no containers, so a volume-bearing stack looked empty and its data
    # was silently left behind. Found migrating GlobaLeaks by uuid.
    project_name = str(project_data.get("name") or project)
    target = await find_server(api, target_server)
    # find_server matches against the LIST endpoint, which does not eager-load the
    # `settings` relation (only server-by-uuid does — same reason is_reachable
    # lives there). Re-fetch so target_server.wildcard_domain is populated: it is
    # what server-bound URLs get rewritten onto.
    target_uuid = str(target.get("uuid", ""))
    if target_uuid:
        target = await api.get_server(target_uuid) or target

    # The ONE read of the instance's release for this run: the plan's rules use
    # it here, and CREATE builds its bodies for the same release off the plan
    # (domain/coolify_versions.py). PREFLIGHT re-reads it only to refuse a run
    # whose instance was upgraded since. Fail-soft and fail-TOWARD-checking: an
    # unknown version applies every rule and sends only what every release takes.
    raw_version: str | None
    try:
        raw_version = (await api.version()).strip()
    except Exception as exc:  # diagnostics only, never fatal
        log.debug("planner.version_unavailable", error=str(exc)[:120])
        raw_version = None
    coolify_version = parse_version(raw_version)

    resources = await environment_resources(api, project_uuid, environment)
    if not resources:
        raise EmptyEnvironment(
            f"no resources in {project}/{environment}",
            hint="Check the environment name (default: production).",
        )

    if only_resource is not None:
        resources = [
            (collection, resource)
            for collection, resource in resources
            if only_resource in (resource.get("name"), resource.get("uuid"))
        ]
        if not resources:
            raise PreflightError(
                f"no resource named {only_resource!r} in {project}/{environment}",
                hint=f"Run `coolify-migrate list {project}` to see its resources.",
            )

    plans: list[ResourcePlan] = []
    source_server: dict[str, Any] | None = None
    # (snapshot, GET, envs) of each source, for the DNS baseline after the loop.
    served: list[tuple[ResourceSnapshot, dict[str, Any], list[dict[str, Any]]]] = []
    # GET /applications, read once and only if an image name makes it matter.
    team_applications: list[Any] | None = None

    for collection, resource in resources:
        snapshot, containers, full = await snapshot_resource(
            api,
            source_host,
            collection=collection,
            resource=resource,
            project=project_name,
            environment=environment,
        )

        # The plan's source server is the first resource's, and the run discovers,
        # stops and copies every resource on it: record where each one runs, so
        # the plan refuses a resource on another (plan.foreign_server_refusal).
        if source_server is None:
            source_server = await resolve_server(api, full)
            server_uuid = _uuid_of(source_server)
        else:
            server_uuid = server_uuid_of(full) or _uuid_of(await resolve_server(api, full))
        snapshot = snapshot.model_copy(update={"server_uuid": server_uuid})

        api_storages: dict[str, Any] | None = None
        try:
            api_storages = await api.get_storages(collection, snapshot.uuid)
        except Exception as exc:
            log.debug("planner.storages_unavailable", uuid=snapshot.uuid, error=str(exc)[:120])

        manifest = await build_manifest(
            source_host,
            mounts=await inspect_all_mounts(source_host, containers),
            api_storages=api_storages,
            uuid=snapshot.uuid,
            measure=measure,
        )
        drift = await assess_drift(source_host, snapshot)
        strategy = select_strategy(
            snapshot.kind, builds=snapshot.builds, has_volumes=bool(manifest.to_migrate)
        )

        warnings: list[str] = []
        refusals: list[str] = []
        if server_uuid is None:
            refusals.append(_UNPLACED_RESOURCE)
        # What holds only while FINALIZE keeps the source (keep, rename): the plan
        # applies these under its policy at read time (ResourcePlan.kept_source_refusals).
        kept_refusals: list[str] = []
        kept_warnings: list[str] = []
        compose_backed = snapshot.kind in (
            ResourceKind.APP_GIT_COMPOSE,
            ResourceKind.SERVICE_TEMPLATE,
            ResourceKind.SERVICE_COMPOSE,
        )
        if snapshot.kind in (ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE):
            # Both service kinds' targets run the source's compose — a template's
            # given it by PATCH, which re-dumps it before 4.4 like the create.
            warnings.extend(fidelity.compose_redump_warning(version=coolify_version))
            refusals.extend(fidelity.service_compose_refusals(snapshot.docker_compose_raw))
        if compose_backed:
            refusals.extend(
                fidelity.compose_refusals(snapshot.docker_compose_raw, version=coolify_version)
            )
        refusals.extend(
            fidelity.server_refusals(
                target, collection=collection, source=full, version=coolify_version
            )
        )
        refusals.extend(
            domains.leftover_parking_refusals(
                full, name=snapshot.name, tag=domains.parking_tag(project_name, environment)
            )
        )
        # Every kind's create takes name and description only within the UI's rules
        # from 4.7.0. The name CREATE sends is the snapshot's; a bad description is
        # left out of the body rather than refused (fidelity.unsendable_description).
        refusals.extend(fidelity.name_refusals(snapshot.name, version=coolify_version))
        if description := fidelity.description_warning(full, version=coolify_version):
            warnings.append(description)

        if collection == "databases":
            warnings.extend(database_health_check_warnings(full))
            # tls_carried: flip to (version >= 4.3.0 and the create path is clone) if
            # CREATE moves to POST /databases/{uuid}/clone (v4.3.0 routes/api.php:340),
            # which replicates enable_ssl/ssl_mode. Today no create path carries them.
            warnings.extend(
                fidelity.database_warnings(
                    full,
                    engine=snapshot.engine,
                    tls_carried=False,
                    version=coolify_version,
                    target_log_drain=fidelity.server_log_drain_enabled(target),
                )
            )
            refusals.extend(fidelity.database_tls_config_refusals(full, engine=snapshot.engine))
            if snapshot.engine is None:
                refusals.append(fidelity.unsupported_engine_refusal(full))
        elif collection == "applications":
            warnings.extend(application_settings_warnings(full))
            origin_wildcard = source_wildcard or (
                server_ref(source_server).wildcard_domain if source_server else None
            )
            warnings.extend(
                fidelity.application_warnings(
                    full, version=coolify_version, source_wildcard=origin_wildcard or None
                )
            )
            refusals.extend(fidelity.application_refusals(full, version=coolify_version))
            # Verbatim labels outlive FINALIZE's release of the custom hosts.
            kept_refusals.extend(
                fidelity.kept_source_label_refusals(full, source_wildcard=origin_wildcard or None)
            )
            kept_warnings.extend(
                fidelity.kept_source_label_warnings(full, source_wildcard=origin_wildcard or None)
            )
            warnings.extend(image_scan_gate_warnings(version=coolify_version))
            if fidelity.unshareable_image_name(full, version=coolify_version):
                if team_applications is None:
                    team_applications = await _team_applications(api)
                refusals.extend(
                    fidelity.shared_image_refusals(full, team_applications, version=coolify_version)
                )
            # Only plain applications POST their storages (resources.copy_storages);
            # compose volumes are materialised by Coolify itself.
            if not compose_backed:
                refusals.extend(fidelity.storage_refusals(api_storages, version=coolify_version))
        else:
            warnings.extend(fidelity.service_warnings(full, version=coolify_version))
            refusals.extend(fidelity.service_refusals(full, version=coolify_version))
            # A part that left the compose (4.7.0+) is not on the target: refused
            # while it holds a custom host or a migrated volume.
            refusals.extend(
                fidelity.removed_part_refusals(
                    full,
                    storages=api_storages,
                    migrating={i.source_name for i in manifest.to_migrate if i.source_name},
                    source_wildcard=source_wildcard
                    or (server_ref(source_server).wildcard_domain if source_server else None)
                    or None,
                )
            )

        # Env keys are validated by the TARGET's API at copy time, and Coolify
        # >=4.2 rejects shapes its own older versions happily stored. Catching that
        # here — reading only — turns a mid-cutover partial write into a line in
        # the plan. Fail-soft for the same reason as api_storages above: an
        # unreadable env list is a worse plan, not a reason to refuse to plan
        # (CREATE reads it again, and fails loudly there).
        envs: list[dict[str, Any]] = []
        try:
            envs = await api.get_envs(collection, snapshot.uuid)
        except Exception as exc:  # diagnostics only, never fatal
            log.debug("planner.envs_unavailable", uuid=snapshot.uuid, error=str(exc)[:120])
        warnings.extend(env_key_warnings(envs))
        served.append((snapshot, full, envs))
        # Locked values are read back from the file Coolify wrote on the source
        # server; the fidelity env rules then judge what is left. Values stay
        # inside review_envs — the plan gets lines and key names only.
        origin = server_ref(source_server) if source_server else None
        review = await env_recovery.review_envs(
            source_host,
            snapshot=snapshot,
            full=full,
            envs=envs,
            version=coolify_version,
            compose=snapshot.docker_compose_raw if compose_backed else None,
            source_ip=origin.ip if origin else None,
            source_wildcard=source_wildcard or (origin.wildcard_domain if origin else None),
        )
        refusals.extend(review.refusals)
        warnings.extend(review.warnings)
        # Schedules (domain/schedules.py): carried at CREATE, reported here.
        more_warnings, more_refusals = await plan_schedules(
            api,
            snapshot,
            source=full,
            source_server=source_server,
            target_server=target,
            version=coolify_version,
        )
        warnings.extend(more_warnings)
        refusals.extend(more_refusals)

        plans.append(
            ResourcePlan(
                snapshot=snapshot,
                strategy=strategy,
                manifest=manifest,
                drift=drift,
                warnings=tuple(warnings),
                refusals=tuple(refusals),
                recover_env_keys=review.recover_env_keys,
                kept_source_refusals=tuple(kept_refusals),
                kept_source_warnings=tuple(kept_warnings),
            )
        )

    if source_server is None:
        raise PreflightError(
            "could not determine the source server from any resource",
            hint="The API did not report a server for these resources.",
        )

    # A server's wildcard is normally read from its settings, but the target may
    # not have one configured — let the caller supply it (or override a wrong one).
    source_ref = server_ref(source_server)
    target_ref = server_ref(target)
    if source_wildcard:
        source_ref = source_ref.model_copy(update={"wildcard_domain": source_wildcard})
    if target_wildcard:
        target_ref = target_ref.model_copy(update={"wildcard_domain": target_wildcard})

    baseline = await dns_baseline(served, source_wildcard=source_ref.wildcard_domain or None)
    notes = list(fidelity.server_warnings(source_server, target))
    if cloudflare := dns_managed.cloudflare_note(baseline, version=coolify_version):
        notes.append(cloudflare)
    if elsewhere := await _elsewhere_note(baseline, source_ref, target_ref):
        notes.append(elsewhere)
    if shared := await shared_hostname_note(
        api, served, source_wildcard=source_ref.wildcard_domain or None
    ):
        notes.append(shared)
    if automatic := await automatic_dns_note(
        api, served, source_wildcard=source_ref.wildcard_domain or None, version=coolify_version
    ):
        notes.append(automatic)
    if records := await target_records_note(
        api, served, target=target_ref, version=coolify_version
    ):
        notes.append(records)
    notes.extend(
        await registry_login_notes(
            api, served, source=source_ref, target=target_ref, version=coolify_version
        )
    )

    return MigrationPlan(
        project=project_name,
        environment=environment,
        source_server=source_ref,
        target_server=target_ref,
        notes=tuple(notes),
        volume_backups_possible=may_hold_volume_backups(coolify_version),
        coolify_version=raw_version or None,
        resources=tuple(plans),
        finalize_policy=finalize_policy,
        transfer_mode=transfer_mode,
        # The matched resource's real name, not the user's input, so the label
        # filter slugifies exactly what Coolify wrote onto the containers.
        selected_resources=tuple(p.snapshot.name for p in plans) if only_resource else (),
        dns_baseline=baseline,
    )


async def _elsewhere_note(
    baseline: tuple[HostBaseline, ...], source: ServerRef, target: ServerRef
) -> str | None:
    """Warn of hosts the DNS gate will find on neither server (dns/gate.elsewhere_note).

    Fail-soft: servers whose addresses cannot be resolved give no warning.
    """
    if not any(entry.addresses for entry in baseline):
        return None
    try:
        source_ips = await dns_resolve.server_addresses(source.ip, source.wildcard_domain or None)
        target_ips = await dns_resolve.server_addresses(target.ip, target.wildcard_domain or None)
    except Exception as exc:  # diagnostics only, never fatal
        log.debug("planner.server_addresses_unavailable", error=str(exc)[:120])
        return None
    return dns_gate.elsewhere_note(
        {entry.host: entry.addresses for entry in baseline},
        source_ips=source_ips,
        target_ips=target_ips,
    )


def _custom_host_owners(
    served: list[tuple[ResourceSnapshot, dict[str, Any], list[dict[str, Any]]]],
    *,
    source_wildcard: str | None,
) -> dict[str, str]:
    """``{custom host: name of the source serving it}`` across the plan."""
    owners: dict[str, str] = {}
    for snapshot, full, envs in served:
        for host in domains.source_custom_hosts(
            snapshot, full, envs, source_wildcard=source_wildcard
        ):
            owners.setdefault(host, snapshot.name)
    return owners


async def shared_hostname_note(
    api: CoolifyClient,
    served: list[tuple[ResourceSnapshot, dict[str, Any], list[dict[str, Any]]]],
    *,
    source_wildcard: str | None,
) -> str | None:
    """Warn when a resource OUTSIDE the plan holds one of its custom hostnames.

    REST only: ``GET /applications`` carries every application's ``fqdn`` and
    compose domains; ``GET /services`` does not load sub-applications
    (ServicesController v4.5.0:270-283), so each other service is read once.
    Fail-soft: a team the token cannot list is a plan without the warning.
    """
    owners = _custom_host_owners(served, source_wildcard=source_wildcard)
    if not owners:
        return None
    own = {snapshot.uuid for snapshot, _, _ in served}
    resources: list[tuple[str, dict[str, Any]]] = []
    try:
        for app in await api.get("/applications") or []:
            if isinstance(app, dict):
                resources.append(("application", app))
        for service in await api.get("/services") or []:
            uuid = service.get("uuid") if isinstance(service, dict) else None
            if not uuid or uuid in own:
                continue
            resources.append(("service", await api.get_resource("services", str(uuid))))
    except Exception as exc:  # diagnostics only, never fatal
        log.debug("planner.shared_hostnames_unavailable", error=str(exc)[:120])
        return None
    return domains.outside_holder_note(
        domains.outside_holders(owners, resources, exclude=own)
    )


async def _team_applications(api: CoolifyClient) -> list[Any]:
    """Every application of the team. Fail-soft: unreadable is a plan without the check."""
    try:
        applications = await api.get("/applications")
    except Exception as exc:  # diagnostics only, never fatal
        log.debug("planner.team_applications_unavailable", error=str(exc)[:120])
        return []
    return applications if isinstance(applications, list) else []


async def automatic_dns_note(
    api: CoolifyClient,
    served: list[tuple[ResourceSnapshot, dict[str, Any], list[dict[str, Any]]]],
    *,
    source_wildcard: str | None,
    version: Version | None,
) -> str | None:
    """Warn when the target's domain claim can make Coolify create a DNS record.

    Reads ``GET /{collection}/{uuid}/dns`` (4.6.0+, database only) of each source
    with a custom host. Only for a version KNOWN to have the route. Fail-soft, like
    the other notes: the end of CREATE still re-checks where each host points.
    """
    if not supports("automatic_dns_on_api_save", version, if_unknown=False):
        return None
    owners = _custom_host_owners(served, source_wildcard=source_wildcard)
    statuses: list[Any] = []
    try:
        for snapshot, _, _ in served:
            if snapshot.name in owners.values() and snapshot.collection != "databases":
                statuses.append(await api.get(f"/{snapshot.collection}/{snapshot.uuid}/dns"))
    except Exception as exc:  # diagnostics only, never fatal
        log.debug("planner.dns_status_unavailable", error=str(exc)[:120])
        return None
    return dns_managed.automatic_dns_note(statuses, hosts=owners)


async def target_records_note(
    api: CoolifyClient,
    served: list[tuple[ResourceSnapshot, dict[str, Any], list[dict[str, Any]]]],
    *,
    target: ServerRef,
    version: Version | None,
) -> str | None:
    """Warn when CREATE can make Coolify create DNS records for the target's own hosts.

    Independent of custom hosts, unlike :func:`automatic_dns_note`: these are the
    hosts under the TARGET's wildcard. Reads ``GET /dns/zones`` (4.6.0+) once, only
    when dns/managed.target_hosts_get_records says a create can queue one and the
    target has a wildcard. Fail-soft: an unreadable list is a plan without it.
    """
    collections = {snapshot.collection for snapshot, _, _ in served}
    if not dns_managed.target_hosts_get_records(collections, version=version):
        return None
    if not dns_wildcard.wildcard_base(target.wildcard_domain or None):
        return None
    try:
        zones = await api.get("/dns/zones")
    except Exception as exc:  # diagnostics only, never fatal
        log.debug("planner.dns_zones_unavailable", error=str(exc)[:120])
        return None
    return dns_managed.target_records_note(zones, target_wildcard=target.wildcard_domain or None)


def _logins(
    result: tuple[frozenset[str], str | None] | None, *, server: str
) -> frozenset[str] | None:
    """A server's registry logins, or ``None`` when Coolify could not tell."""
    if result is None:
        return None
    names, error = result
    if error is not None:
        log.debug("planner.registry_logins_unreadable", server=server, error=error[:120])
        return None
    return names


async def registry_login_notes(
    api: CoolifyClient,
    served: list[tuple[ResourceSnapshot, dict[str, Any], list[dict[str, Any]]]],
    *,
    source: ServerRef,
    target: ServerRef,
    version: Version | None,
) -> list[str]:
    """Warn of images whose registry the source server is logged in to and the target is not.

    Reads ``GET /servers/{uuid}/registries`` (4.4.0+; tried on an unknown release,
    where an older one answers 404) for the source, then the target. Fail-soft: an
    absent route, a failed read or a server whose config Coolify could not read is
    no warning, never a refusal (domain/fidelity.registry_login_warnings). Keyed by
    name, merged: Coolify lets an application and a database share one.
    """
    if not supports("server_registry_logins", version):
        return []
    images: dict[str, tuple[str, ...]] = {}
    pushes: dict[str, tuple[str, ...]] = {}
    for snapshot, full, _ in served:
        images[snapshot.name] = images.get(snapshot.name, ()) + pulled_images(snapshot, full)
        if pushed := fidelity.pushed_image_name(full, version=version):
            pushes[snapshot.name] = (*pushes.get(snapshot.name, ()), pushed)
    if not any(images.values()) and not pushes:
        return []
    try:
        source_logins = _logins(await api.get_server_registries(source.uuid), server=source.name)
        if not source_logins:
            return []
        target_logins = _logins(await api.get_server_registries(target.uuid), server=target.name)
    except Exception as exc:  # diagnostics only, never fatal
        log.debug("planner.registry_logins_unavailable", error=str(exc)[:120])
        return []
    return fidelity.registry_login_warnings(
        images, source_logins, target_logins, target_server=target.name, pushes=pushes
    )


async def dns_baseline(
    served: list[tuple[ResourceSnapshot, dict[str, Any], list[dict[str, Any]]]],
    *,
    source_wildcard: str | None,
) -> tuple[HostBaseline, ...]:
    """Resolve every custom host the sources serve, as DNS answers NOW. Reads only.

    The baseline later checks compare against (dns/gate.py VANISHED, CREATE's
    re-check while the source serves, the rollback's flip-back list). Through
    the zone's authoritative nameservers, whose names it keeps: they tell a
    Cloudflare zone apart for the plan warning. Fail-soft — no baseline is a
    plan without those checks, never a refusal.
    """
    owners = _custom_host_owners(served, source_wildcard=source_wildcard)
    if not owners:
        return ()
    try:
        resolved = await dns_resolve.resolve_hosts(sorted(owners))
    except Exception as exc:  # diagnostics only, never fatal
        log.warning("planner.dns_baseline_unavailable", error=str(exc)[:120])
        return ()
    return tuple(
        HostBaseline(
            host=host,
            resource=owners[host],
            addresses=resolution.addresses,
            ttl=resolution.ttl,
            nameservers=resolution.nameservers,
            cname_chain=resolution.cname_chain,
        )
        for host, resolution in sorted(resolved.items())
    )


async def project_environments(api: CoolifyClient, name_or_uuid: str) -> tuple[str, list[str]]:
    """A project's real name and its environment names. Reads only.

    Returns ``(project_name, [environment_name, ...])`` — the enumerator a
    whole-project migration expands over. Falls back to ``["production"]`` for a
    project whose environments the API does not report, so the default path still
    works.
    """
    project_data = await find_project(api, name_or_uuid)
    detail = await api.get_project(str(project_data["uuid"]))
    environments = [
        str(env["name"])
        for env in detail.get("environments") or []
        if isinstance(env, dict) and env.get("name")
    ]
    return str(project_data.get("name", name_or_uuid)), environments or ["production"]


def stack_labels(plan: MigrationPlan) -> dict[str, str]:
    """Label filter identifying the whole project/environment on the daemon.

    Coolify slugifies both when it writes the labels, so we must too.
    """
    return {
        LABEL_PROJECT: slugify(plan.project),
        LABEL_ENVIRONMENT: slugify(plan.environment),
    }


def observed_labels(plan: MigrationPlan) -> dict[str, str]:
    """Label filter the quiesce/health gates must watch for this plan.

    A resource-scoped run watches ONLY its resource: the siblings it deliberately
    leaves running would otherwise make the stop gate wait out its timeout, or trip
    the mid-copy restart check. A whole-environment run watches the stack, exactly
    as before — so nothing about an unscoped migration changes.
    """
    if plan.selected_resources:
        return resource_labels(
            project=plan.project,
            environment=plan.environment,
            name=plan.selected_resources[0],
        )
    return stack_labels(plan)
