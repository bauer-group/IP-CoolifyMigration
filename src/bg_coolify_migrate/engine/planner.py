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
from bg_coolify_migrate.api.fields import database_health_check_warnings
from bg_coolify_migrate.discovery import docker
from bg_coolify_migrate.discovery.docker import (
    LABEL_ENVIRONMENT,
    LABEL_PROJECT,
    LABEL_RESOURCE,
)
from bg_coolify_migrate.domain import compose as compose_mod
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
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    TransferMode,
    select_strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.errors import PreflightError
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
    return ServerRef(
        uuid=str(server.get("uuid", "")),
        name=str(server.get("name", "?")),
        ip=str(server.get("ip", "")),
        user=str(server.get("user", "root")),
        port=int(server.get("port", 22) or 22),
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
        hint="Run `coolify-migrate doctor` to see visible projects.",
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
            github_app_uuid=full.get("github_app_uuid"),
            private_key_uuid=full.get("private_key_uuid"),
        ),
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
    target will resolve — possibly to a different image than the source runs.
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
    finalize_policy: FinalizePolicy = FinalizePolicy.RENAME,
    transfer_mode: TransferMode = TransferMode.AUTO,
    measure: bool = True,
) -> MigrationPlan:
    """Produce the complete plan. Reads only."""
    project_data = await find_project(api, project)
    project_uuid = str(project_data["uuid"])
    target = await find_server(api, target_server)

    resources = await environment_resources(api, project_uuid, environment)
    if not resources:
        raise PreflightError(
            f"no resources in {project}/{environment}",
            hint="Check the environment name (default: production).",
        )

    plans: list[ResourcePlan] = []
    source_server: dict[str, Any] | None = None

    for collection, resource in resources:
        snapshot, containers, full = await snapshot_resource(
            api,
            source_host,
            collection=collection,
            resource=resource,
            project=project,
            environment=environment,
        )

        if source_server is None:
            source_server = await resolve_server(api, full)

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
        if snapshot.kind is ResourceKind.SERVICE_COMPOSE:
            warnings.append(
                "Coolify re-dumps compose through Yaml::dump(Yaml::parse(...)); "
                "comments and formatting will be lost on the target"
            )

        if collection == "databases":
            warnings.extend(database_health_check_warnings(full))

        plans.append(
            ResourcePlan(
                snapshot=snapshot,
                strategy=strategy,
                manifest=manifest,
                drift=drift,
                warnings=tuple(warnings),
            )
        )

    if source_server is None:
        raise PreflightError(
            "could not determine the source server from any resource",
            hint="The API did not report a server for these resources.",
        )

    return MigrationPlan(
        project=str(project_data.get("name", project)),
        environment=environment,
        source_server=server_ref(source_server),
        target_server=server_ref(target),
        resources=tuple(plans),
        finalize_policy=finalize_policy,
        transfer_mode=transfer_mode,
    )


def stack_labels(plan: MigrationPlan) -> dict[str, str]:
    """Label filter identifying the whole project/environment on the daemon.

    Coolify slugifies both when it writes the labels, so we must too.
    """
    return {
        LABEL_PROJECT: slugify(plan.project),
        LABEL_ENVIRONMENT: slugify(plan.environment),
    }
