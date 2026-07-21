"""Tests for volume naming and old->new pairing.

`pair_by_mount_path` is the single most important function in the codebase. The
tests below encode, as executable specification, the exact bug that makes
coolify-mover silently lose every service volume.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.naming import (
    VolumeEndpoint,
    VolumePairingError,
    application_volume_name,
    compose_volume_separator,
    database_volume_name,
    pair_by_mount_path,
    postgres_mount_path,
    resource_config_dir,
    service_volume_name,
    slugify,
    storage_api_volume_name,
    volume_data_path,
)


class TestSlugify:
    """Regression cases only. The authority is elsewhere, deliberately.

    This class used to claim it matched Laravel's Str::slug and assert
    `dots.and.dots` -> `dots-and-dots`. Laravel returns `dotsanddots`: it strips
    characters that are not the separator, a letter, a number or whitespace, and
    only then collapses runs. The expectations here were transcribed from the
    same misunderstanding as the implementation, so they agreed with it for the
    life of the project and proved nothing.

    Container discovery filters on these slugs, so being wrong here means
    matching no containers, and `docker ps` returns an empty list rather than an
    error — a migration that succeeds and moves nothing.

    The real check is tests/e2e/test_label_contract.py, which asks the running
    Laravel. What is left here is a fast guard on cases it has already settled;
    when the two disagree, the e2e test is right.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("simple", "simple"),
            ("With Spaces", "with-spaces"),
            ("UPPER", "upper"),
            ("under_score", "under-score"),
            ("multiple   spaces", "multiple-spaces"),
            ("--leading-trailing--", "leading-trailing"),
            ("café", "cafe"),
            # NFKD alone drops the eszett rather than expanding it, turning this
            # into "grue". Hence the explicit transliteration table.
            ("Grüße", "grusse"),
            # Stripped, not separated — the case that exposed the whole mistake.
            ("dots.and.dots", "dotsanddots"),
            ("a/b\\c", "abc"),
            ("api.example.com", "apiexamplecom"),
            # Laravel's default dictionary, which we had missed entirely.
            ("me@example.com", "me-at-examplecom"),
        ],
    )
    def test_agrees_with_laravel_on_settled_cases(self, raw: str, expected: str) -> None:
        assert slugify(raw) == expected


class TestVolumeNames:
    def test_database_volume_uses_postgres_not_postgresql(self) -> None:
        assert database_volume_name(DatabaseEngine.POSTGRESQL, "abc123") == "postgres-data-abc123"

    @pytest.mark.parametrize(
        ("engine", "expected"),
        [
            (DatabaseEngine.MYSQL, "mysql-data-u1"),
            (DatabaseEngine.MARIADB, "mariadb-data-u1"),
            (DatabaseEngine.MONGODB, "mongodb-data-u1"),
            (DatabaseEngine.REDIS, "redis-data-u1"),
            (DatabaseEngine.CLICKHOUSE, "clickhouse-data-u1"),
            (DatabaseEngine.DRAGONFLY, "dragonfly-data-u1"),
            (DatabaseEngine.KEYDB, "keydb-data-u1"),
        ],
    )
    def test_all_engines(self, engine: DatabaseEngine, expected: str) -> None:
        assert database_volume_name(engine, "u1") == expected

    def test_service_volume_uses_underscore(self) -> None:
        assert service_volume_name("svc123", "pocketbase data") == "svc123_pocketbase-data"

    def test_application_volume_uses_hyphen(self) -> None:
        assert application_volume_name("app123", "data") == "app123-data"

    def test_separators_genuinely_differ(self) -> None:
        # This asymmetry is why an application must never be converted into a
        # service or vice versa — it would orphan every volume.
        assert service_volume_name("u", "d") == "u_d"
        assert application_volume_name("u", "d") == "u-d"
        assert service_volume_name("u", "d") != application_volume_name("u", "d")

    def test_storage_api_forces_uuid_prefix(self) -> None:
        # Whatever name we POST, upstream stores `{uuid}-{name}`.
        assert storage_api_volume_name("newuuid", "data") == "newuuid-data"

    def test_volume_data_path(self) -> None:
        assert volume_data_path("x") == "/var/lib/docker/volumes/x/_data"

    @pytest.mark.parametrize(
        ("kind", "sep"),
        [
            (ResourceKind.SERVICE_COMPOSE, "_"),
            (ResourceKind.SERVICE_TEMPLATE, "_"),
            (ResourceKind.APP_GIT_COMPOSE, "-"),
            (ResourceKind.DATABASE, ""),
            (ResourceKind.APP_GIT_BUILD, ""),
        ],
    )
    def test_compose_volume_separator(self, kind: ResourceKind, sep: str) -> None:
        assert compose_volume_separator(kind) == sep


class TestPostgresMountPath:
    @pytest.mark.parametrize(
        "image", ["postgres:18", "postgres:18.1", "postgres:19", "postgres:pg18"]
    )
    def test_18_and_above(self, image: str) -> None:
        assert postgres_mount_path(image) == "/var/lib/postgresql"

    @pytest.mark.parametrize("image", ["postgres:16", "postgres:15.4", "postgres:pg13"])
    def test_below_18(self, image: str) -> None:
        assert postgres_mount_path(image) == "/var/lib/postgresql/data"

    @pytest.mark.parametrize("image", ["postgres:latest", "postgres", "custom/pg:edge"])
    def test_unparseable_falls_back_to_legacy_path(self, image: str) -> None:
        # Matches upstream's behaviour when its regex does not match.
        assert postgres_mount_path(image) == "/var/lib/postgresql/data"

    def test_pinning_image_matters(self) -> None:
        # If the target is created without pinning `image`, the model hook can
        # pick the OTHER path and the mirrored bytes land where nothing looks.
        assert postgres_mount_path("postgres:16") != postgres_mount_path("postgres:18")


class TestResourceConfigDir:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (ResourceKind.DATABASE, "/data/coolify/databases/u1"),
            (ResourceKind.SERVICE_COMPOSE, "/data/coolify/services/u1"),
            (ResourceKind.SERVICE_TEMPLATE, "/data/coolify/services/u1"),
            (ResourceKind.APP_GIT_BUILD, "/data/coolify/applications/u1"),
            (ResourceKind.APP_GIT_COMPOSE, "/data/coolify/applications/u1"),
        ],
    )
    def test_paths(self, kind: ResourceKind, expected: str) -> None:
        assert resource_config_dir(kind, "u1") == expected


class TestPairByMountPath:
    def test_pairs_despite_completely_different_names(self) -> None:
        # THE point: names always change (POST /storages forces {new_uuid}-{name},
        # DB hooks force {engine}-data-{new_uuid}), mount paths never do.
        source = [VolumeEndpoint("postgres-data-OLD", "/var/lib/postgresql/data")]
        target = [VolumeEndpoint("postgres-data-NEW", "/var/lib/postgresql/data")]
        (pair,) = pair_by_mount_path(source, target)
        assert pair.source.name == "postgres-data-OLD"
        assert pair.target.name == "postgres-data-NEW"
        assert pair.source_path == "/var/lib/docker/volumes/postgres-data-OLD/_data"
        assert pair.target_path == "/var/lib/docker/volumes/postgres-data-NEW/_data"

    def test_pairs_across_the_service_separator_change(self) -> None:
        # A service volume is {parent_service_uuid}_{slug}. coolify-mover string-
        # replaces the SUB-application's uuid here, matches nothing, and leaves
        # the row pointing at the old name while data lands at the new one.
        # Pairing by mount path is immune to that entire class of bug.
        source = [VolumeEndpoint("OLDSVC_pocketbase-data", "/pb_data")]
        target = [VolumeEndpoint("NEWSVC_pocketbase-data", "/pb_data")]
        (pair,) = pair_by_mount_path(source, target)
        assert pair.target.name == "NEWSVC_pocketbase-data"

    def test_multiple_volumes_pair_by_path_not_order(self) -> None:
        source = [
            VolumeEndpoint("old-b", "/b"),
            VolumeEndpoint("old-a", "/a"),
        ]
        target = [
            VolumeEndpoint("new-a", "/a"),
            VolumeEndpoint("new-b", "/b"),
        ]
        pairs = {p.source.name: p.target.name for p in pair_by_mount_path(source, target)}
        assert pairs == {"old-a": "new-a", "old-b": "new-b"}

    def test_same_mount_path_in_different_services_disambiguated_by_container(self) -> None:
        # A stack can legitimately mount two different volumes at /data in two
        # different services. Pairing on mount_path alone would be ambiguous.
        source = [
            VolumeEndpoint("old-web", "/data", container="web"),
            VolumeEndpoint("old-worker", "/data", container="worker"),
        ]
        target = [
            VolumeEndpoint("new-worker", "/data", container="worker"),
            VolumeEndpoint("new-web", "/data", container="web"),
        ]
        pairs = {p.source.name: p.target.name for p in pair_by_mount_path(source, target)}
        assert pairs == {"old-web": "new-web", "old-worker": "new-worker"}

    def test_ambiguous_source_refused(self) -> None:
        source = [VolumeEndpoint("a", "/data"), VolumeEndpoint("b", "/data")]
        target = [VolumeEndpoint("x", "/data")]
        with pytest.raises(VolumePairingError, match="ambiguous source"):
            pair_by_mount_path(source, target)

    def test_same_volume_shared_by_two_containers_is_not_ambiguous(self) -> None:
        # A WordPress stack mounts one uploads volume into both nginx and php-fpm
        # at the same path; docker inspect reports it once per container. That is a
        # duplicate of one volume, not two volumes fighting over a path — it must
        # collapse to a single pair, not raise.
        source = [
            VolumeEndpoint("uploads", "/var/www/html/wp-content/uploads"),
            VolumeEndpoint("uploads", "/var/www/html/wp-content/uploads"),
        ]
        target = [VolumeEndpoint("new-uploads", "/var/www/html/wp-content/uploads")]
        (pair,) = pair_by_mount_path(source, target)
        assert pair.source.name == "uploads"
        assert pair.target.name == "new-uploads"

    def test_ambiguous_target_refused(self) -> None:
        source = [VolumeEndpoint("a", "/data")]
        target = [VolumeEndpoint("x", "/data"), VolumeEndpoint("y", "/data")]
        with pytest.raises(VolumePairingError, match="ambiguous target"):
            pair_by_mount_path(source, target)

    def test_unpaired_source_refused_because_data_would_be_left_behind(self) -> None:
        source = [VolumeEndpoint("a", "/a"), VolumeEndpoint("b", "/b")]
        target = [VolumeEndpoint("x", "/a")]
        with pytest.raises(VolumePairingError, match="no counterpart on the target"):
            pair_by_mount_path(source, target)

    def test_unpaired_target_refused_because_it_would_start_empty(self) -> None:
        source = [VolumeEndpoint("a", "/a")]
        target = [VolumeEndpoint("x", "/a"), VolumeEndpoint("y", "/b")]
        with pytest.raises(VolumePairingError, match="no counterpart on the source"):
            pair_by_mount_path(source, target)

    def test_empty_both_sides_is_fine(self) -> None:
        # A stateless nixpacks app legitimately has no volumes at all.
        assert pair_by_mount_path([], []) == []

    def test_container_disambiguation_only_when_known_on_both_sides(self) -> None:
        # If only one side carries container names we must fall back to path-only
        # keys, or nothing would ever pair.
        source = [VolumeEndpoint("old", "/data", container="web")]
        target = [VolumeEndpoint("new", "/data")]
        (pair,) = pair_by_mount_path(source, target)
        assert pair.target.name == "new"
