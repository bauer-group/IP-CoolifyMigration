"""Tests for the volume manifest reconciliation.

Each test below corresponds to a concrete failure of one of the two tools this
one replaces. If a test here regresses, we have reintroduced a known data-loss
bug.
"""

from __future__ import annotations

from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.manifest import (
    ApiStorage,
    Decision,
    DiscoverySource,
    DockerMount,
    DockerVolume,
    reconcile,
)


def _m(**kw: object) -> DockerMount:
    base = {"container": "c1", "type": "volume", "destination": "/data"}
    return DockerMount(**{**base, **kw})  # type: ignore[arg-type]


class TestNamedVolumes:
    def test_named_volume_is_migrated(self) -> None:
        manifest = reconcile(docker_mounts=[_m(name="postgres-data-u1")])
        (item,) = manifest.items
        assert item.decision is Decision.MIGRATE
        assert item.mount_class is MountClass.NAMED
        assert item.source_name == "postgres-data-u1"
        assert item.source_path == "/var/lib/docker/volumes/postgres-data-u1/_data"
        assert item.mount_path == "/data"
        assert DiscoverySource.DOCKER_INSPECT in item.discovered_from

    def test_stopped_container_volumes_are_included(self) -> None:
        # Geczy discovers volumes from `docker ps` (running only), so a stopped
        # container's volume is silently skipped and never even reported.
        # We take mounts from `docker ps -a`, so state is irrelevant here —
        # this test documents that the manifest layer makes no such distinction.
        manifest = reconcile(docker_mounts=[_m(name="v1"), _m(name="v2", container="stopped")])
        assert len(manifest.to_migrate) == 2


class TestBindMounts:
    def test_bind_mount_is_migrated(self) -> None:
        # coolify-mover copies the DB row for a bind mount but never the data:
        # its rsync only ever touches /var/lib/docker/volumes/{name}/_data.
        manifest = reconcile(
            docker_mounts=[_m(type="bind", source="/srv/app-data", destination="/data", name=None)]
        )
        (item,) = manifest.items
        assert item.decision is Decision.MIGRATE
        assert item.mount_class is MountClass.BIND
        assert item.source_path == "/srv/app-data"

    def test_docker_sock_is_skipped(self) -> None:
        manifest = reconcile(
            docker_mounts=[
                _m(
                    type="bind",
                    source="/var/run/docker.sock",
                    destination="/var/run/docker.sock",
                    name=None,
                )
            ]
        )
        (item,) = manifest.items
        assert item.decision is Decision.SKIP
        assert item.mount_class is MountClass.PASSTHROUGH

    def test_coolify_config_dir_is_skipped(self) -> None:
        # /data/coolify/applications/{uuid}/ is derived state: regenerated on
        # every deploy and embedding the OLD uuid. Copying it plants stale
        # container names and labels on the target.
        manifest = reconcile(
            docker_mounts=[
                _m(
                    type="bind",
                    source="/data/coolify/applications/olduuid/docker-compose.yml",
                    destination="/app/docker-compose.yml",
                    name=None,
                )
            ]
        )
        (item,) = manifest.items
        assert item.decision is Decision.SKIP
        assert "regenerates" in item.reason


class TestAnonymousVolumes:
    def test_volume_without_name_is_refused(self) -> None:
        manifest = reconcile(docker_mounts=[_m(name=None)])
        (item,) = manifest.items
        assert item.decision is Decision.REFUSE
        assert item.mount_class is MountClass.ANONYMOUS
        assert manifest.is_blocked

    def test_64_hex_name_is_recognised_as_anonymous(self) -> None:
        # Docker reports a Name for anonymous volumes too — a random 64-hex id
        # that cannot be reproduced on the target.
        manifest = reconcile(docker_mounts=[_m(name="a" * 64)])
        (item,) = manifest.items
        assert item.decision is Decision.REFUSE
        assert item.mount_class is MountClass.ANONYMOUS

    def test_64_char_non_hex_name_is_a_normal_volume(self) -> None:
        manifest = reconcile(docker_mounts=[_m(name="z" * 64)])
        (item,) = manifest.items
        assert item.decision is Decision.MIGRATE
        assert item.mount_class is MountClass.NAMED

    def test_refusal_blocks_the_manifest(self) -> None:
        manifest = reconcile(docker_mounts=[_m(name="good"), _m(name=None, destination="/anon")])
        assert manifest.is_blocked
        assert len(manifest.refused) == 1
        assert len(manifest.to_migrate) == 1


class TestTmpfs:
    def test_tmpfs_is_skipped(self) -> None:
        manifest = reconcile(docker_mounts=[_m(type="tmpfs", name=None, destination="/run")])
        (item,) = manifest.items
        assert item.decision is Decision.SKIP
        assert item.mount_class is MountClass.TMPFS


class TestApiCrossCheck:
    def test_declared_storage_with_no_live_mount_warns(self) -> None:
        manifest = reconcile(
            docker_mounts=[_m(name="v1", destination="/data")],
            api_storages=[ApiStorage(kind="persistent", name="ghost", mount_path="/ghost")],
        )
        assert any("no container currently mounts" in w for w in manifest.warnings)

    def test_placeholder_content_warns_so_it_is_rsynced_instead(self) -> None:
        # LocalFileVolume.content caps at 5 MiB and returns '[binary file]' /
        # '[file too large to display]'. Such a file cannot be recreated through
        # the API, so it must be mirrored instead.
        manifest = reconcile(
            docker_mounts=[_m(name="v1")],
            api_storages=[
                ApiStorage(
                    kind="file",
                    mount_path="/app/blob.bin",
                    content_is_placeholder=True,
                )
            ],
        )
        assert any("5 MiB content cap" in w for w in manifest.warnings)

    def test_matching_storage_produces_no_warning(self) -> None:
        manifest = reconcile(
            docker_mounts=[_m(name="v1", destination="/data")],
            api_storages=[ApiStorage(kind="persistent", name="v1", mount_path="/data")],
        )
        assert manifest.warnings == ()

    def test_declared_storage_with_an_existing_volume_is_migrated_not_skipped(self) -> None:
        # The GlobaLeaks data-loss case: Coolify declares a persistent storage and
        # its docker volume exists, but no (running) container mounts it — the
        # stack's containers are gone. The API is the intent and the volume is the
        # data, so it MUST be migrated, not warned-and-skipped.
        manifest = reconcile(
            docker_mounts=[],
            api_storages=[
                ApiStorage(kind="persistent", name="u1_data", mount_path="/var/globaleaks")
            ],
            docker_volumes=[DockerVolume(name="u1_data")],
            uuid_prefixes=frozenset({"u1"}),
        )
        assert len(manifest.to_migrate) == 1
        item = manifest.to_migrate[0]
        assert item.source_name == "u1_data"
        assert item.mount_path == "/var/globaleaks"
        assert item.source_path == "/var/lib/docker/volumes/u1_data/_data"
        # and it is NOT also reported as an orphan
        assert manifest.warnings == ()

    def test_declared_persistent_storage_without_a_volume_still_warns(self) -> None:
        # No volume on disk means nothing to migrate — keep the surfacing warning.
        manifest = reconcile(
            docker_mounts=[],
            api_storages=[ApiStorage(kind="persistent", name="ghost", mount_path="/ghost")],
            docker_volumes=[],
        )
        assert manifest.to_migrate == ()
        assert any("no container currently mounts" in w for w in manifest.warnings)

    def test_file_storage_is_not_fabricated_into_a_volume_migration(self) -> None:
        # A file storage is not a docker volume; the safety net must only fire for
        # persistent volumes, never turn a like-named file mount into a volume copy.
        manifest = reconcile(
            docker_mounts=[],
            api_storages=[ApiStorage(kind="file", name="cfg", mount_path="/etc/x.conf")],
            docker_volumes=[DockerVolume(name="cfg")],
        )
        assert manifest.to_migrate == ()


class TestOrphanDetection:
    def test_unattached_coolify_volume_warns(self) -> None:
        manifest = reconcile(
            docker_mounts=[_m(name="u1-data")],
            docker_volumes=[
                DockerVolume(name="u1-data"),
                DockerVolume(name="u1-orphan"),
            ],
            uuid_prefixes=frozenset({"u1"}),
        )
        assert any("u1-orphan" in w and "no container mounts it" in w for w in manifest.warnings)

    def test_unrelated_volume_is_ignored(self) -> None:
        manifest = reconcile(
            docker_mounts=[_m(name="u1-data")],
            docker_volumes=[DockerVolume(name="somebody-elses-volume")],
            uuid_prefixes=frozenset({"u1"}),
        )
        assert manifest.warnings == ()

    def test_orphan_detected_via_coolify_managed_label(self) -> None:
        manifest = reconcile(
            docker_mounts=[],
            docker_volumes=[DockerVolume(name="mystery", labels={"coolify.managed": "true"})],
        )
        assert any("mystery" in w for w in manifest.warnings)

    def test_attached_volume_is_not_reported_as_orphan(self) -> None:
        manifest = reconcile(
            docker_mounts=[_m(name="u1-data")],
            docker_volumes=[DockerVolume(name="u1-data", labels={"coolify.managed": "true"})],
            uuid_prefixes=frozenset({"u1"}),
        )
        assert manifest.warnings == ()


class TestManifestAggregates:
    def test_totals(self) -> None:
        manifest = reconcile(
            docker_mounts=[
                _m(name="a", destination="/a"),
                _m(name="b", destination="/b"),
                _m(type="tmpfs", name=None, destination="/t"),
            ]
        )
        assert len(manifest.to_migrate) == 2
        assert len(manifest.skipped) == 1
        assert manifest.is_blocked is False

    def test_total_bytes_only_counts_migrated(self) -> None:
        from bg_coolify_migrate.domain.manifest import VolumeItem

        manifest = reconcile(docker_mounts=[])
        assert manifest.total_bytes == 0

        item_a = VolumeItem(
            mount_class=MountClass.NAMED,
            decision=Decision.MIGRATE,
            reason="x",
            source_path="/a",
            mount_path="/a",
            bytes=100,
        )
        item_b = VolumeItem(
            mount_class=MountClass.TMPFS,
            decision=Decision.SKIP,
            reason="x",
            source_path="/b",
            mount_path="/b",
            bytes=999,
        )
        from bg_coolify_migrate.domain.manifest import VolumeManifest

        m = VolumeManifest(items=(item_a, item_b))
        assert m.total_bytes == 100

    def test_every_item_carries_a_reason(self) -> None:
        # A report must be able to explain every line without the reader
        # re-deriving the logic.
        manifest = reconcile(
            docker_mounts=[
                _m(name="a"),
                _m(name=None, destination="/anon"),
                _m(type="tmpfs", name=None, destination="/t"),
                _m(type="bind", source="/var/run/docker.sock", destination="/s", name=None),
            ]
        )
        assert all(item.reason for item in manifest.items)
