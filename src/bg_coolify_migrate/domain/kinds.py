"""Resource taxonomy — what kind of thing are we moving, and does it build?

PURE module: no IO. Every function here is total over a captured snapshot.

The central insight, and the reason this is its own module: **"does it build?" is
not a property of the resource kind.** A Coolify service or a dockercompose
application whose compose declares ``build:`` instead of ``image:`` builds from
source just like a nixpacks app does. Callers must combine :func:`classify` with
:func:`bg_coolify_migrate.domain.compose.build_services` to get the real answer;
:func:`always_builds` only covers the kinds that build unconditionally.

Verified against coollabsio/coolify@main:

* ``app/Enums/BuildPackTypes.php`` — the enum is nixpacks|static|dockerfile|
  dockercompose|railpack. ``dockerimage`` is NOT in it but is a live runtime
  value set by ``POST /v1/applications/dockerimage`` and branched on at
  ``ApplicationDeploymentJob.php:490``. Treat the upstream enum as incomplete.
* ``ApplicationDeploymentJob.php:761-764`` — build_pack=dockercompose runs
  ``docker compose build --pull`` unconditionally.
* ``PrivateKey.php:26,40`` / ``GithubApp.php:10`` — git credentials are
  team-scoped, so a key that works on server A works on server B. Nothing to
  provision on the target.
"""

from __future__ import annotations

from enum import StrEnum


class BuildPack(StrEnum):
    """Coolify's ``applications.build_pack`` values.

    ``DOCKERIMAGE`` is deliberately included even though upstream's
    ``BuildPackTypes`` enum omits it — the deploy job branches on the literal
    string, so it is real whatever the enum says.
    """

    NIXPACKS = "nixpacks"
    STATIC = "static"
    DOCKERFILE = "dockerfile"
    DOCKERCOMPOSE = "dockercompose"
    RAILPACK = "railpack"
    DOCKERIMAGE = "dockerimage"


class ResourceKind(StrEnum):
    """The migration-relevant classification.

    Coarser than ``build_pack`` on purpose: what matters to a migration is which
    API route recreates the resource, whether code is rebuilt, and which volume
    naming convention applies — not which builder Coolify happens to invoke.
    """

    APP_GIT_BUILD = "app_git_build"
    """nixpacks | static | dockerfile | railpack — always rebuilt from git."""

    APP_GIT_COMPOSE = "app_git_compose"
    """build_pack=dockercompose — compose is re-read from git on every deploy."""

    APP_DOCKERIMAGE = "app_dockerimage"
    """Runs a registry image by tag/digest. Never builds, never clones."""

    DATABASE = "database"
    """One of the eight standalone engines. Never builds."""

    SERVICE_TEMPLATE = "service_template"
    """One-click service from the live template catalogue."""

    SERVICE_COMPOSE = "service_compose"
    """Service from raw ``docker_compose_raw``."""


class GitAuth(StrEnum):
    """How Coolify authenticates to the git remote — picks the create route."""

    PUBLIC = "public"
    GITHUB_APP = "private-github-app"
    DEPLOY_KEY = "private-deploy-key"
    NONE = "none"


class DatabaseEngine(StrEnum):
    """The eight standalone engines.

    The value is the **API path segment** (``POST /v1/databases/{value}``), which
    is NOT always the volume prefix — postgresql's volume is ``postgres-data-*``.
    See :data:`ENGINE_VOLUME_PREFIX`.
    """

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    MONGODB = "mongodb"
    REDIS = "redis"
    CLICKHOUSE = "clickhouse"
    DRAGONFLY = "dragonfly"
    KEYDB = "keydb"


#: Engine -> the volume-name prefix Coolify's model ``created`` hook uses.
#: Note ``POSTGRESQL`` maps to ``postgres``, not ``postgresql`` — the API path
#: and the volume prefix genuinely differ, and conflating them silently points a
#: migration at a volume that does not exist.
ENGINE_VOLUME_PREFIX: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRESQL: "postgres",
    DatabaseEngine.MYSQL: "mysql",
    DatabaseEngine.MARIADB: "mariadb",
    DatabaseEngine.MONGODB: "mongodb",
    DatabaseEngine.REDIS: "redis",
    DatabaseEngine.CLICKHOUSE: "clickhouse",
    DatabaseEngine.DRAGONFLY: "dragonfly",
    DatabaseEngine.KEYDB: "keydb",
}

#: Coolify's API collections, as returned in ``GET /v1/resources``' ``type``
#: and used as the URL segment for per-resource calls.
COLLECTION_APPLICATIONS = "applications"
COLLECTION_SERVICES = "services"
COLLECTION_DATABASES = "databases"

#: Kinds that rebuild code no matter what their compose says.
_ALWAYS_BUILDING_PACKS = frozenset(
    {BuildPack.NIXPACKS, BuildPack.STATIC, BuildPack.DOCKERFILE, BuildPack.RAILPACK}
)


def classify(
    collection: str,
    *,
    build_pack: BuildPack | str | None = None,
    service_type: str | None = None,
) -> ResourceKind:
    """Map a Coolify resource onto its :class:`ResourceKind`.

    Total over the inputs Coolify can actually produce; raises rather than
    guessing, because a mis-classified resource is routed to the wrong create
    endpoint and the wrong volume naming convention.

    Args:
        collection: ``applications`` | ``services`` | ``databases``.
        build_pack: Only meaningful for applications.
        service_type: Only meaningful for services; the one-click template key.
            ``None`` means the resource carries raw ``docker_compose_raw``.

    Raises:
        ValueError: On an unknown collection or a missing/unknown build_pack.
    """
    if collection == COLLECTION_DATABASES:
        return ResourceKind.DATABASE

    if collection == COLLECTION_SERVICES:
        # Upstream enforces `type` XOR `docker_compose_raw`; a service with no
        # type is the custom-compose path and leaves service_type NULL.
        return ResourceKind.SERVICE_TEMPLATE if service_type else ResourceKind.SERVICE_COMPOSE

    if collection == COLLECTION_APPLICATIONS:
        if build_pack is None:
            raise ValueError("applications require a build_pack to classify")
        pack = BuildPack(build_pack)
        if pack is BuildPack.DOCKERIMAGE:
            return ResourceKind.APP_DOCKERIMAGE
        if pack is BuildPack.DOCKERCOMPOSE:
            return ResourceKind.APP_GIT_COMPOSE
        return ResourceKind.APP_GIT_BUILD

    raise ValueError(f"unclassifiable collection: {collection!r}")


def git_auth(
    *,
    git_repository: str | None,
    github_app_uuid: str | None = None,
    private_key_uuid: str | None = None,
) -> GitAuth:
    """Determine which application create route a git-backed resource needs.

    Order matters: a GitHub App takes precedence over a deploy key, matching
    ``ApplicationsController``'s own resolution order.
    """
    if github_app_uuid:
        return GitAuth.GITHUB_APP
    if private_key_uuid:
        return GitAuth.DEPLOY_KEY
    return GitAuth.PUBLIC if git_repository else GitAuth.NONE


def create_route(kind: ResourceKind, *, auth: GitAuth = GitAuth.NONE) -> str:
    """The API path used to recreate this kind on the target.

    Encodes the hard walls found in upstream:

    * A raw-YAML compose stack **cannot** become an application — ``build_pack=
      dockercompose`` is only reachable on the three git routes, each of which
      requires ``git_repository`` + ``git_branch``. Its only home is
      ``POST /v1/services``.
    * Never convert an application into a service (or back): the compose volume
      separator differs (``-`` vs ``_``), so a converted resource orphans every
      volume it owns.

    Raises:
        ValueError: If a git-backed kind has no usable auth mode.
    """
    if kind is ResourceKind.DATABASE:
        return "/databases/{engine}"
    if kind in (ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE):
        return "/services"
    if kind is ResourceKind.APP_DOCKERIMAGE:
        return "/applications/dockerimage"
    if kind in (ResourceKind.APP_GIT_BUILD, ResourceKind.APP_GIT_COMPOSE):
        if auth is GitAuth.NONE:
            raise ValueError(f"{kind} requires a git remote; got GitAuth.NONE")
        return f"/applications/{auth.value}"
    raise ValueError(f"no create route for {kind!r}")  # pragma: no cover - exhaustive


def always_builds(kind: ResourceKind, *, build_pack: BuildPack | str | None = None) -> bool:
    """True if this kind rebuilds code regardless of what its compose says.

    NOT the whole story — a :attr:`ResourceKind.APP_GIT_COMPOSE`,
    :attr:`ResourceKind.SERVICE_COMPOSE` or :attr:`ResourceKind.SERVICE_TEMPLATE`
    builds *conditionally*, when its compose declares ``build:``. Combine with
    ``compose.build_services()``; :func:`may_build` states which kinds need that
    extra check.
    """
    if kind is not ResourceKind.APP_GIT_BUILD:
        return False
    if build_pack is None:
        return True
    return BuildPack(build_pack) in _ALWAYS_BUILDING_PACKS


def may_build(kind: ResourceKind) -> bool:
    """True if this kind *can* build — i.e. its compose must be inspected.

    ``DATABASE`` and ``APP_DOCKERIMAGE`` are the only kinds that can never build.
    """
    return kind not in (ResourceKind.DATABASE, ResourceKind.APP_DOCKERIMAGE)


def is_compose_backed(kind: ResourceKind) -> bool:
    """True if the resource's topology is defined by a compose document."""
    return kind in (
        ResourceKind.APP_GIT_COMPOSE,
        ResourceKind.SERVICE_TEMPLATE,
        ResourceKind.SERVICE_COMPOSE,
    )


def label_id_key(kind: ResourceKind) -> str:
    """The ``coolify.*Id`` container label that identifies this kind's containers.

    This is how Coolify itself finds containers (``docker ps -a
    --filter=label=coolify.applicationId={id}``), and therefore how we verify a
    stack is genuinely stopped rather than trusting the stop endpoint.
    """
    if kind is ResourceKind.DATABASE:
        return "coolify.databaseId"
    if kind in (ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE):
        return "coolify.serviceId"
    return "coolify.applicationId"
