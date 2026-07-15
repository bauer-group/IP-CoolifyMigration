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
from bg_coolify_migrate.discovery import docker
from bg_coolify_migrate.discovery.docker import LABEL_ENVIRONMENT, LABEL_PROJECT
from bg_coolify_migrate.domain import compose as compose_mod
from bg_coolify_migrate.domain.drift import RebuildDriftReport, assess_rebuild_drift
from bg_coolify_migrate.domain.kinds import (
    BuildPack,
    DatabaseEngine,
    ResourceKind,
    always_builds,
    classify,
    git_auth,
    label_id_key,
    may_build,
)
from bg_coolify_migrate.domain.manifest import DockerVolume, VolumeManifest, reconcile
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


async def environment_resources(
    api: CoolifyClient, project_uuid: str, environment: str
) -> list[tuple[str, dict[str, Any]]]:
    """``(collection, resource)`` for everything in one environment.

    Tolerates the two shapes Coolify's project endpoint has returned: resources
    grouped under ``applications``/``services``/``databases``, or a flat list
    with a ``type``.
    """
    detail = await api.get(f"/projects/{project_uuid}/{environment}")
    if not isinstance(detail, dict):
        raise PreflightError(f"environment {environment!r} not found in project {project_uuid}")

    out: list[tuple[str, dict[str, Any]]] = []
    for collection in ("applications", "services", "databases"):
        for resource in detail.get(collection) or []:
            if isinstance(resource, dict):
                out.append((collection, resource))

    if out:
        return out

    # Flat shape fallback.
    for resource in detail.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        kind = str(resource.get("type", ""))
        if "database" in kind or kind in {e.value for e in DatabaseEngine}:
            out.append(("databases", resource))
        elif kind == "service":
            out.append(("services", resource))
        else:
            out.append(("applications", resource))
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


async def snapshot_resource(
    api: CoolifyClient,
    source_host: RemoteHost,
    *,
    collection: str,
    resource: dict[str, Any],
) -> tuple[ResourceSnapshot, list[docker.Container]]:
    """Capture everything the planner needs about one resource."""
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

    containers = await docker.list_containers(
        source_host, label_filters={label_id_key(kind): str(full.get("id", ""))}
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
    return snapshot, containers


async def build_manifest(
    source_host: RemoteHost,
    *,
    containers: list[docker.Container],
    api_storages: dict[str, Any] | None,
    uuid: str,
    measure: bool = True,
) -> VolumeManifest:
    """Reconcile the three discovery sources into a manifest.

    docker inspect is the truth, the API is the intent, `volume ls` is the
    residue. Each alone misses something — see domain/manifest.py.
    """
    mounts = []
    for container in containers:
        mounts.extend(await docker.inspect_mounts(source_host, container.id or container.name))

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
        snapshot, containers = await snapshot_resource(
            api, source_host, collection=collection, resource=resource
        )

        if source_server is None:
            full = await api.get_resource(collection, snapshot.uuid)
            server_uuid = full.get("server_uuid") or (full.get("server") or {}).get("uuid")
            if server_uuid:
                source_server = await api.get_server(str(server_uuid))

        api_storages: dict[str, Any] | None = None
        try:
            api_storages = await api.get_storages(collection, snapshot.uuid)
        except Exception as exc:
            log.debug("planner.storages_unavailable", uuid=snapshot.uuid, error=str(exc)[:120])

        manifest = await build_manifest(
            source_host,
            containers=containers,
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
