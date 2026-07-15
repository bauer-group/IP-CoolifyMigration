"""Table-driven tests for the resource taxonomy.

These are the tests that matter most: `classify` and `create_route` decide which
API endpoint recreates a resource and which volume naming convention applies. A
bug here routes data to the wrong place, which is exactly how coolify-mover loses
service volumes.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.domain.kinds import (
    ENGINE_VOLUME_PREFIX,
    BuildPack,
    DatabaseEngine,
    GitAuth,
    ResourceKind,
    always_builds,
    classify,
    create_route,
    git_auth,
    is_compose_backed,
    label_id_key,
    may_build,
)


class TestClassify:
    @pytest.mark.parametrize(
        ("build_pack", "expected"),
        [
            ("nixpacks", ResourceKind.APP_GIT_BUILD),
            ("static", ResourceKind.APP_GIT_BUILD),
            ("dockerfile", ResourceKind.APP_GIT_BUILD),
            ("railpack", ResourceKind.APP_GIT_BUILD),
            ("dockercompose", ResourceKind.APP_GIT_COMPOSE),
            ("dockerimage", ResourceKind.APP_DOCKERIMAGE),
        ],
    )
    def test_applications(self, build_pack: str, expected: ResourceKind) -> None:
        assert classify("applications", build_pack=build_pack) is expected

    def test_dockerimage_is_classified_even_though_upstream_enum_omits_it(self) -> None:
        # BuildPackTypes.php does not list `dockerimage`, but
        # ApplicationDeploymentJob.php:490 branches on the literal string.
        assert BuildPack("dockerimage") is BuildPack.DOCKERIMAGE
        assert classify("applications", build_pack="dockerimage") is ResourceKind.APP_DOCKERIMAGE

    def test_service_with_type_is_a_template(self) -> None:
        assert classify("services", service_type="minio") is ResourceKind.SERVICE_TEMPLATE

    @pytest.mark.parametrize("service_type", [None, ""])
    def test_service_without_type_is_custom_compose(self, service_type: str | None) -> None:
        # Upstream enforces `type` XOR `docker_compose_raw`; the custom path
        # leaves service_type NULL.
        assert classify("services", service_type=service_type) is ResourceKind.SERVICE_COMPOSE

    def test_databases(self) -> None:
        assert classify("databases") is ResourceKind.DATABASE

    def test_application_without_build_pack_raises(self) -> None:
        with pytest.raises(ValueError, match="require a build_pack"):
            classify("applications")

    def test_unknown_collection_raises(self) -> None:
        with pytest.raises(ValueError, match="unclassifiable"):
            classify("widgets")

    def test_unknown_build_pack_raises(self) -> None:
        with pytest.raises(ValueError):
            classify("applications", build_pack="cargo-cult")


class TestGitAuth:
    @pytest.mark.parametrize(
        ("repo", "gh_app", "key", "expected"),
        [
            ("https://github.com/x/y", None, None, GitAuth.PUBLIC),
            ("https://github.com/x/y", "gh-uuid", None, GitAuth.GITHUB_APP),
            ("https://github.com/x/y", None, "key-uuid", GitAuth.DEPLOY_KEY),
            # GitHub App wins over a deploy key, matching upstream resolution order.
            ("https://github.com/x/y", "gh-uuid", "key-uuid", GitAuth.GITHUB_APP),
            (None, None, None, GitAuth.NONE),
        ],
    )
    def test_resolution(
        self, repo: str | None, gh_app: str | None, key: str | None, expected: GitAuth
    ) -> None:
        assert git_auth(git_repository=repo, github_app_uuid=gh_app, private_key_uuid=key) is expected


class TestCreateRoute:
    def test_database(self) -> None:
        assert create_route(ResourceKind.DATABASE) == "/databases/{engine}"

    @pytest.mark.parametrize(
        "kind", [ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE]
    )
    def test_services_share_one_route(self, kind: ResourceKind) -> None:
        # A raw-YAML compose stack cannot become an application: build_pack=
        # dockercompose is only reachable on the git routes, which require
        # git_repository. POST /services is its only home.
        assert create_route(kind) == "/services"

    def test_dockerimage(self) -> None:
        assert create_route(ResourceKind.APP_DOCKERIMAGE) == "/applications/dockerimage"

    @pytest.mark.parametrize(
        ("auth", "expected"),
        [
            (GitAuth.PUBLIC, "/applications/public"),
            (GitAuth.GITHUB_APP, "/applications/private-github-app"),
            (GitAuth.DEPLOY_KEY, "/applications/private-deploy-key"),
        ],
    )
    def test_git_backed_apps(self, auth: GitAuth, expected: str) -> None:
        assert create_route(ResourceKind.APP_GIT_BUILD, auth=auth) == expected
        assert create_route(ResourceKind.APP_GIT_COMPOSE, auth=auth) == expected

    def test_git_backed_without_auth_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a git remote"):
            create_route(ResourceKind.APP_GIT_BUILD, auth=GitAuth.NONE)


class TestBuildPredicates:
    @pytest.mark.parametrize(
        "pack", ["nixpacks", "static", "dockerfile", "railpack"]
    )
    def test_git_build_packs_always_build(self, pack: str) -> None:
        assert always_builds(ResourceKind.APP_GIT_BUILD, build_pack=pack) is True

    @pytest.mark.parametrize(
        "kind",
        [
            ResourceKind.DATABASE,
            ResourceKind.APP_DOCKERIMAGE,
            ResourceKind.SERVICE_COMPOSE,
            ResourceKind.SERVICE_TEMPLATE,
            ResourceKind.APP_GIT_COMPOSE,
        ],
    )
    def test_other_kinds_do_not_always_build(self, kind: ResourceKind) -> None:
        # Crucially APP_GIT_COMPOSE and SERVICE_* build only CONDITIONALLY —
        # when their compose declares `build:`. That is compose.build_services()'s
        # job, not this predicate's.
        assert always_builds(kind) is False

    @pytest.mark.parametrize("kind", [ResourceKind.DATABASE, ResourceKind.APP_DOCKERIMAGE])
    def test_kinds_that_can_never_build(self, kind: ResourceKind) -> None:
        assert may_build(kind) is False

    @pytest.mark.parametrize(
        "kind",
        [
            ResourceKind.APP_GIT_BUILD,
            ResourceKind.APP_GIT_COMPOSE,
            ResourceKind.SERVICE_COMPOSE,
            ResourceKind.SERVICE_TEMPLATE,
        ],
    )
    def test_kinds_whose_compose_must_be_inspected(self, kind: ResourceKind) -> None:
        assert may_build(kind) is True


class TestComposeBacked:
    @pytest.mark.parametrize(
        "kind",
        [ResourceKind.APP_GIT_COMPOSE, ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE],
    )
    def test_compose_backed(self, kind: ResourceKind) -> None:
        assert is_compose_backed(kind) is True

    @pytest.mark.parametrize(
        "kind", [ResourceKind.DATABASE, ResourceKind.APP_GIT_BUILD, ResourceKind.APP_DOCKERIMAGE]
    )
    def test_not_compose_backed(self, kind: ResourceKind) -> None:
        assert is_compose_backed(kind) is False


class TestLabelIdKey:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (ResourceKind.DATABASE, "coolify.databaseId"),
            (ResourceKind.SERVICE_COMPOSE, "coolify.serviceId"),
            (ResourceKind.SERVICE_TEMPLATE, "coolify.serviceId"),
            (ResourceKind.APP_GIT_BUILD, "coolify.applicationId"),
            (ResourceKind.APP_GIT_COMPOSE, "coolify.applicationId"),
            (ResourceKind.APP_DOCKERIMAGE, "coolify.applicationId"),
        ],
    )
    def test_matches_coolifys_own_filters(self, kind: ResourceKind, expected: str) -> None:
        # These must match what Coolify itself uses in
        # `docker ps -a --filter=label=coolify.applicationId={id}`.
        assert label_id_key(kind) == expected


class TestEngineVolumePrefix:
    def test_every_engine_has_a_prefix(self) -> None:
        assert set(ENGINE_VOLUME_PREFIX) == set(DatabaseEngine)

    def test_postgresql_prefix_is_postgres_not_postgresql(self) -> None:
        # The API path segment is `postgresql` but the volume is `postgres-data-*`.
        # Conflating them points the migration at a volume that does not exist.
        assert DatabaseEngine.POSTGRESQL.value == "postgresql"
        assert ENGINE_VOLUME_PREFIX[DatabaseEngine.POSTGRESQL] == "postgres"

    @pytest.mark.parametrize(
        ("engine", "prefix"),
        [
            (DatabaseEngine.MYSQL, "mysql"),
            (DatabaseEngine.MARIADB, "mariadb"),
            (DatabaseEngine.MONGODB, "mongodb"),
            (DatabaseEngine.REDIS, "redis"),
            (DatabaseEngine.CLICKHOUSE, "clickhouse"),
            (DatabaseEngine.DRAGONFLY, "dragonfly"),
            (DatabaseEngine.KEYDB, "keydb"),
        ],
    )
    def test_other_engines_match_their_path(self, engine: DatabaseEngine, prefix: str) -> None:
        assert ENGINE_VOLUME_PREFIX[engine] == prefix
