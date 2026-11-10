"""Bind mounts: what moves, where it lands, and what a rollback may delete (pure).

The silent loss this suite pins down: the manifest classed every bind mount
outside ``/data/coolify`` as MIGRATE, DISCOVER journalled it path-to-path, and
neither COPY nor VERIFY ever looked at it — while every bind INSIDE
``/data/coolify`` (a service's ``./data:/data``) was skipped as "regenerated",
though Coolify only ever ``mkdir -p``s such a directory and never refills it.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import (
    BindAction,
    BindPair,
    BindPairingError,
    HostBind,
    StorageCarry,
    StorageRow,
)
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import ApiStorage, Decision, DockerMount, reconcile
from bg_coolify_migrate.domain.statemachine import State, owed_states

SVC = "/data/coolify/services/u1"
TGT = "/data/coolify/services/t1"


def _classify(
    source: str,
    *,
    destination: str = "/data",
    read_only: bool = False,
    uuid: str | None = "u1",
    rows: list[StorageRow] | None = None,
    carry: StorageCarry = StorageCarry.COMPOSE,
) -> tuple[BindAction, str]:
    return binds.classify(
        source=source,
        destination=destination,
        read_only=read_only,
        uuid=uuid,
        rows=rows,
        carry=carry,
    )


class TestPaths:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("/a//b/", "/a/b"),
            ("/", "/"),
            ("relative/x", None),
            ("/a/../b", None),
            ("/a/./b", None),
            (None, None),
        ],
    )
    def test_normalise(self, raw: object, expected: str | None) -> None:
        assert binds.normalise(raw) == expected

    @pytest.mark.parametrize(
        ("path", "uuid", "system"),
        [
            ("/etc/nginx", None, True),
            ("/", None, True),
            ("/home", None, True),
            ("/home/karl/data", None, False),
            ("/home/karl/.ssh", None, True),
            ("/root/backup", None, True),
            ("/opt", None, True),
            ("/opt/app/data", None, False),
            ("/var/lib/docker/volumes/x/_data", None, True),
            ("/var/run/foo.sock", None, True),
            ("/data/coolify", None, True),
            ("/data/coolify/proxy/dynamic", None, True),
            (f"{SVC}/data", "u1", False),
            (f"{SVC}/data", "other", True),
            (SVC, "u1", True),  # the directory itself is Coolify's
            ("/srv/uploads", None, False),
        ],
    )
    def test_is_system_path(self, path: str, uuid: str | None, system: bool) -> None:
        assert binds.is_system_path(path, uuid=uuid) is system

    @pytest.mark.parametrize(
        ("fs_path", "expected"),
        [
            ("./data", f"{SVC}/data"),
            ("data/x", f"{SVC}/data/x"),
            (".", SVC),
            ("/srv/abs", "/srv/abs"),
            ("~/x", None),
            ("", None),
            (None, None),
        ],
    )
    def test_resolve_fs_path_as_coolify_mounts_it(
        self, fs_path: object, expected: str | None
    ) -> None:
        # LocalFileVolume::resolvedFsPath (v4.7.2:574-586).
        assert binds.resolve_fs_path(fs_path, SVC) == expected

    def test_ancestors_and_overlap(self) -> None:
        assert binds.ancestors("/a/b/c") == ["/a", "/a/b"]
        assert binds.overlaps("/a/b", "/a") and binds.overlaps("/a", "/a/b")
        assert not binds.overlaps("/a/bc", "/a/b")

    def test_own_dirs_cover_every_root(self) -> None:
        # A service on compose parsing version 3 resolves ./ under applications/.
        assert binds.own_dirs("u1") == (
            "/data/coolify/applications/u1",
            "/data/coolify/services/u1",
            "/data/coolify/databases/u1",
        )
        assert binds.own_dirs(None) == ()

    def test_workdir_by_kind(self) -> None:
        assert binds.workdir(ResourceKind.SERVICE_COMPOSE, "u1") == SVC
        assert binds.workdir(ResourceKind.DATABASE, "u1") == "/data/coolify/databases/u1"
        assert binds.workdir(None, "u1") == "/data/coolify/applications/u1"


class TestClassify:
    def test_docker_socket_under_both_names_is_passthrough(self) -> None:
        for sock in ("/var/run/docker.sock", "/run/docker.sock"):
            assert _classify(sock)[0] is BindAction.PASSTHROUGH

    def test_read_only_system_file_is_supplied_by_the_target_host(self) -> None:
        action, reason = _classify("/etc/localtime", read_only=True)
        assert action is BindAction.PASSTHROUGH
        assert "target server supplies its own" in reason

    def test_writable_system_path_is_refused(self) -> None:
        action, reason = _classify("/etc/myapp", read_only=False)
        assert action is BindAction.REFUSE
        assert "/etc/myapp" in reason

    def test_writable_bind_of_another_resources_directory_is_refused(self) -> None:
        action, _ = _classify("/data/coolify/services/someoneelse/data")
        assert action is BindAction.REFUSE

    def test_the_whole_resource_directory_read_only_is_regenerated(self) -> None:
        assert _classify(SVC, read_only=True)[0] is BindAction.SKIP

    def test_the_whole_resource_directory_writable_is_refused(self) -> None:
        action, reason = _classify(SVC)
        assert action is BindAction.REFUSE
        assert "subdirectory" in reason

    def test_a_relative_data_directory_is_migrated_not_skipped(self) -> None:
        # THE data-loss case: ./data:/data in a service lands here, and Coolify
        # only ever mkdir -p's it (LocalFileVolume v4.7.2:333-405).
        rows = [StorageRow("/data", f"{SVC}/data")]
        action, reason = _classify(f"{SVC}/data", rows=rows)
        assert action is BindAction.MIGRATE
        assert "never regenerates" in reason

    def test_a_file_coolify_writes_from_its_row_is_skipped(self) -> None:
        rows = [StorageRow("/etc/app.conf", f"{SVC}/app.conf", writes_content=True)]
        action, reason = _classify(f"{SVC}/app.conf", destination="/etc/app.conf", rows=rows)
        assert action is BindAction.SKIP
        assert "content" in reason

    def test_a_bind_coolify_keeps_no_row_for_is_refused(self) -> None:
        action, reason = _classify("/srv/data", rows=[])
        assert action is BindAction.REFUSE
        assert "declared in no Coolify storage" in reason

    def test_unknown_rows_leave_the_proof_to_the_read_back(self) -> None:
        assert _classify("/srv/data", rows=None)[0] is BindAction.MIGRATE
        assert _classify(f"{SVC}/data", rows=None)[0] is BindAction.MIGRATE

    def test_an_absolute_host_path_with_its_row_is_mirrored(self) -> None:
        rows = [StorageRow("/data", "/srv/data")]
        action, reason = _classify("/srv/data/", rows=rows)
        assert action is BindAction.MIGRATE
        assert "path-to-path" in reason

    def test_a_database_s_generated_files_are_skipped(self) -> None:
        # custom-postgres.conf, init scripts: written from the database's
        # columns at every start (StartPostgresql v4.7.2:163-190), no row.
        action, _ = _classify(
            "/data/coolify/databases/u1/custom-postgres.conf",
            destination="/etc/postgresql/postgresql.conf",
            rows=[],
            carry=StorageCarry.NONE,
        )
        assert action is BindAction.SKIP

    def test_a_database_s_own_storages_are_refused_they_are_not_carried(self) -> None:
        rows = [StorageRow("/backup", "/srv/pg-backup")]
        action, reason = _classify("/srv/pg-backup", destination="/backup", rows=rows, carry=StorageCarry.NONE)
        assert action is BindAction.REFUSE
        assert "not carried" in reason

    def test_a_placeholder_file_of_a_plain_app_is_refused(self) -> None:
        # copy_storages skips it, so the target would not even mount it.
        rows = [StorageRow("/blob", "/data/coolify/applications/u1/blob", placeholder=True)]
        action, _ = _classify(
            "/data/coolify/applications/u1/blob", destination="/blob", rows=rows, carry=StorageCarry.API
        )
        assert action is BindAction.REFUSE

    def test_a_placeholder_file_of_a_compose_resource_is_mirrored(self) -> None:
        rows = [StorageRow("/blob", f"{SVC}/blob", placeholder=True)]
        action, reason = _classify(f"{SVC}/blob", destination="/blob", rows=rows)
        assert action is BindAction.MIGRATE
        assert "rsync" in reason

    def test_a_non_absolute_source_is_refused(self) -> None:
        assert _classify("relative/x")[0] is BindAction.REFUSE

    def test_carry_by_kind(self) -> None:
        assert binds.carry_for(ResourceKind.DATABASE) is StorageCarry.NONE
        assert binds.carry_for(ResourceKind.SERVICE_TEMPLATE) is StorageCarry.COMPOSE
        assert binds.carry_for(ResourceKind.APP_GIT_COMPOSE) is StorageCarry.COMPOSE
        assert binds.carry_for(ResourceKind.APP_DOCKERIMAGE) is StorageCarry.API
        assert binds.carry_for(None) is StorageCarry.API


class TestStorageRows:
    def test_parses_file_and_host_path_rows(self) -> None:
        rows = binds.storage_rows(
            {
                "file_storages": [
                    {"mount_path": "/data", "fs_path": "./data", "is_directory": True},
                    {"mount_path": "/etc/a.conf", "fs_path": f"{SVC}/a.conf", "content": "x=1"},
                    {"mount_path": "/blob", "fs_path": f"{SVC}/blob", "content": "[binary file]"},
                    {"mount_path": "/etc/tz", "fs_path": "/etc/timezone", "is_host_file": True},
                    {"mount_path": "/home", "fs_path": "~/x"},  # unresolvable: skipped
                    "junk",
                ],
                "persistent_storages": [
                    {"name": "u1-data", "mount_path": "/vol"},  # a named volume: not a bind
                    {"name": "u1-hp", "mount_path": "/hp", "host_path": "/srv/hp/"},
                ],
            },
            base=SVC,
        )
        assert rows == [
            StorageRow("/data", f"{SVC}/data"),
            StorageRow("/etc/a.conf", f"{SVC}/a.conf", writes_content=True),
            StorageRow("/blob", f"{SVC}/blob", placeholder=True),
            StorageRow("/etc/tz", "/etc/timezone"),
            StorageRow("/hp", "/srv/hp"),
        ]

    def test_nothing_gives_nothing(self) -> None:
        assert binds.storage_rows(None, base=SVC) == []
        assert binds.storage_rows({}, base=SVC) == []


class TestPairing:
    def test_a_relative_bind_pairs_with_the_targets_own_directory(self) -> None:
        # Read back from the target's row, never the source path with u1 -> t1.
        pairs = binds.pair_binds(
            [(f"{SVC}/data", "/data")],
            [StorageRow("/data", f"{TGT}/data")],
            source_uuid="u1",
            target_uuid="t1",
        )
        assert pairs == [BindPair(f"{SVC}/data", f"{TGT}/data", "/data")]

    def test_an_absolute_bind_pairs_with_the_same_path(self) -> None:
        pairs = binds.pair_binds(
            [("/srv/x", "/x")], [StorageRow("/x", "/srv/x")], source_uuid="u1", target_uuid="t1"
        )
        assert pairs[0].target_path == "/srv/x"

    def test_two_parts_at_one_mount_path_pair_by_place(self) -> None:
        rows = [StorageRow("/data", f"{TGT}/a"), StorageRow("/data", f"{TGT}/b")]
        pairs = binds.pair_binds(
            [(f"{SVC}/b", "/data")], rows, source_uuid="u1", target_uuid="t1"
        )
        assert pairs[0].target_path == f"{TGT}/b"

    def test_ambiguity_is_refused(self) -> None:
        rows = [StorageRow("/data", "/srv/a"), StorageRow("/data", "/srv/b")]
        with pytest.raises(BindPairingError, match="several"):
            binds.pair_binds([("/srv/c", "/data")], rows, source_uuid="u1", target_uuid="t1")

    def test_a_bind_with_no_target_row_is_refused(self) -> None:
        with pytest.raises(BindPairingError, match="no counterpart"):
            binds.pair_binds(
                [("/srv/x", "/x")], [StorageRow("/y", "/srv/y")], source_uuid="u1", target_uuid="t1"
            )

    def test_a_non_absolute_source_cannot_pair(self) -> None:
        with pytest.raises(BindPairingError):
            binds.pair_binds([("x", "/x")], [], source_uuid="u1", target_uuid="t1")


class TestMerge:
    def test_a_path_two_resources_bind_is_one_payload(self) -> None:
        shared = BindPair("/srv/shared", "/srv/shared", "/s")
        assert binds.merge_pairs({"a": [shared], "b": [shared]}) == [shared]

    def test_one_source_two_targets_is_refused(self) -> None:
        with pytest.raises(BindPairingError, match="both"):
            binds.merge_pairs(
                {"a": [BindPair("/srv/s", "/x", "/s")], "b": [BindPair("/srv/s", "/y", "/s")]}
            )

    def test_two_sources_one_target_is_refused(self) -> None:
        with pytest.raises(BindPairingError, match="both be copied"):
            binds.merge_pairs({"a": [BindPair("/s1", "/t", "/m"), BindPair("/s2", "/t", "/m")]})

    def test_a_nested_target_must_mirror_a_nested_source(self) -> None:
        ok = [BindPair("/srv/app", "/srv/app", "/a"), BindPair("/srv/app/db", "/srv/app/db", "/b")]
        assert len(binds.merge_pairs({"a": ok})) == 2
        bad = [BindPair("/srv/one", "/t", "/a"), BindPair("/srv/two", "/t/sub", "/b")]
        with pytest.raises(BindPairingError, match="delete the other"):
            binds.merge_pairs({"a": bad})


class TestHostStates:
    @pytest.mark.parametrize(
        ("state", "kind"),
        [
            (binds.STATE_DIR, binds.KIND_DIR),
            (binds.STATE_EMPTY_DIR, binds.KIND_DIR),
            (binds.STATE_FILE, binds.KIND_FILE),
            (binds.STATE_EMPTY_FILE, binds.KIND_FILE),
            (binds.STATE_SYMLINK, None),
            (binds.STATE_OTHER, None),
            (binds.STATE_MISSING, None),
        ],
    )
    def test_source_kind(self, state: str, kind: str | None) -> None:
        assert binds.source_kind(state) == kind
        assert (binds.source_state_problem("/p", state) is None) is (kind is not None)

    @pytest.mark.parametrize(
        ("state", "kind", "ok"),
        [
            (binds.STATE_MISSING, binds.KIND_DIR, True),
            (binds.STATE_EMPTY_DIR, binds.KIND_DIR, True),
            (binds.STATE_EMPTY_DIR, binds.KIND_FILE, True),  # Coolify's default directory
            (binds.STATE_EMPTY_FILE, binds.KIND_FILE, True),
            (binds.STATE_EMPTY_FILE, binds.KIND_DIR, False),
            (binds.STATE_DIR, binds.KIND_DIR, False),  # someone's data
            (binds.STATE_FILE, binds.KIND_FILE, False),
            (binds.STATE_SYMLINK, binds.KIND_DIR, False),
            (binds.STATE_OTHER, binds.KIND_FILE, False),
        ],
    )
    def test_target_state(self, state: str, kind: str, ok: bool) -> None:
        assert (binds.target_state_problem("/t", state, kind=kind) is None) is ok

    @pytest.mark.parametrize(
        ("state", "kind", "ok"),
        [
            (binds.STATE_DIR, binds.KIND_DIR, True),  # this run's own partial copy
            (binds.STATE_FILE, binds.KIND_FILE, True),
            (binds.STATE_EMPTY_DIR, binds.KIND_FILE, True),
            (binds.STATE_FILE, binds.KIND_DIR, False),
            (binds.STATE_SYMLINK, binds.KIND_DIR, False),
        ],
    )
    def test_resumed_target_state(self, state: str, kind: str, ok: bool) -> None:
        assert (binds.resumed_target_problem("/t", state, kind=kind) is None) is ok

    def test_a_symlinked_or_file_parent_forbids_the_write(self) -> None:
        assert binds.ancestor_problem("/srv/a/b", {"/srv": binds.STATE_SYMLINK})
        assert binds.ancestor_problem("/srv/a/b", {"/srv": binds.STATE_DIR, "/srv/a": binds.STATE_FILE})
        assert binds.ancestor_problem("/srv/a/b", {"/srv": binds.STATE_DIR}) is None

    def test_missing_paths_are_what_the_copy_creates(self) -> None:
        states = {"/srv": binds.STATE_DIR, "/srv/a": binds.STATE_MISSING, "/srv/a/b": binds.STATE_MISSING}
        assert binds.missing_paths("/srv/a/b", states) == ["/srv/a", "/srv/a/b"]
        assert binds.missing_paths("/srv/a/b", {**states, "/srv/a/b": binds.STATE_EMPTY_DIR}) == ["/srv/a"]


class TestSharing:
    def test_the_same_or_a_nested_path_shares_the_bytes(self) -> None:
        found = binds.shared_with(
            "/srv/app",
            [
                HostBind("other", "/srv/app/", rw=False),
                HostBind("deep", "/srv/app/uploads"),
                HostBind("elsewhere", "/srv/apple"),
            ],
        )
        assert [b.container for b in found] == ["other", "deep"]

    def test_only_a_writable_parent_counts_and_never_coolify_s_roots(self) -> None:
        found = binds.shared_with(
            "/data/coolify/services/u1/data",
            [
                HostBind("cadvisor", "/", rw=False),
                HostBind("coolify", "/data/coolify/services"),
                HostBind("writer", "/data"),
            ],
        )
        assert [b.container for b in found] == ["writer"]

    def test_the_plan_s_own_containers(self) -> None:
        mine = HostBind("store-u1", "/srv/x", labels={})
        renamed = HostBind("x", "/srv/x", labels={"coolify.name": "store-u1"})
        by_label = HostBind("y", "/srv/x", labels={"coolify.projectName": "shop", "coolify.resourceName": "db"})
        other = HostBind("z", "/srv/x", labels={"coolify.projectName": "shop"})
        labels = [{"coolify.projectName": "shop", "coolify.resourceName": "db"}]
        for bind in (mine, renamed, by_label):
            assert binds.belongs_to(bind, uuids=["u1"], label_sets=labels)
        assert not binds.belongs_to(other, uuids=["u1"], label_sets=labels)


class TestRollback:
    @pytest.mark.parametrize(
        ("path", "ok"),
        [
            ("/srv/app/data", True),
            ("/opt/app", True),
            ("/opt", False),
            ("/", False),
            ("/srv", False),
            ("/etc/app", False),
            ("/var/lib/docker/volumes/x", False),
            ("/data/coolify/services/t1/data", True),
            ("/data/coolify/services/t1", False),  # the target's directory itself
            ("/data/coolify/services/u1/data", False),  # not a target's
            ("/data/coolify/proxy/x", False),
            ("/srv/app/../etc", False),
            ("/srv/app/", False),  # not canonical
        ],
    )
    def test_removable(self, path: str, ok: bool) -> None:
        assert binds.removable(path, target_uuids=["t1"]) is ok

    def test_owed_targets_follow_the_journal_order(self) -> None:
        created = {
            binds.BIND_TARGET: {"path": "/srv/a", "prior": "missing", "created": ["/srv/a"]}
        }
        records = [
            ("step_started", created),
            ("step_started", {binds.BIND_TARGET: {"path": "/srv/b", "prior": "empty-directory"}}),
            ("rollback_step", {binds.BINDS_REMOVED: ["/srv/a"]}),
            ("step_started", {binds.BIND_TARGET: "junk"}),
        ]
        owed = binds.owed_bind_targets(records)
        assert set(owed) == {"/srv/b"}
        # A later attempt that fills /srv/a again owes it again.
        owed = binds.owed_bind_targets([*records, ("step_started", created)])
        assert owed["/srv/a"].created == ("/srv/a",)

    def test_copy_is_owed_once_it_journalled_a_bind_target(self) -> None:
        # A bind is copied OUTSIDE the target resource, which no target delete
        # removes: a COPY that failed after writing one still owes its undo.
        markers = {State.COPY.value: {binds.BIND_TARGET: {"path": "/srv/a", "prior": "missing"}}}
        assert State.COPY.value in owed_states([State.CREATE_TARGET.value], markers=markers)
        assert State.COPY.value not in owed_states([State.CREATE_TARGET.value], markers={})


class TestManifest:
    """The classification as the plan shows it."""

    def _mount(self, source: str, destination: str = "/data", *, rw: bool = True) -> DockerMount:
        return DockerMount(container="c1", type="bind", source=source, destination=destination, rw=rw)

    def test_a_service_data_directory_is_migrated(self) -> None:
        manifest = reconcile(
            docker_mounts=[self._mount(f"{SVC}/data")],
            api_storages=[ApiStorage(kind="file", mount_path="/data", fs_path=f"{SVC}/data", is_directory=True)],
            resource_uuid="u1",
            kind=ResourceKind.SERVICE_COMPOSE,
        )
        (item,) = manifest.to_migrate
        assert item.mount_class is MountClass.BIND
        assert item.source_path == f"{SVC}/data"

    def _content_manifest(self, kind: ResourceKind) -> Any:
        return reconcile(
            docker_mounts=[
                self._mount(f"{SVC}/nginx.conf", "/etc/nginx/nginx.conf", rw=False),
                self._mount("/var/run/docker.sock", "/var/run/docker.sock"),
            ],
            api_storages=[
                ApiStorage(
                    kind="file",
                    mount_path="/etc/nginx/nginx.conf",
                    fs_path=f"{SVC}/nginx.conf",
                    has_content=True,
                )
            ],
            resource_uuid="u1",
            kind=kind,
        )

    def test_a_template_s_content_file_is_skipped_and_flagged_for_the_read_back(self) -> None:
        manifest = self._content_manifest(ResourceKind.SERVICE_TEMPLATE)
        assert [i.decision for i in manifest.items] == [Decision.SKIP, Decision.SKIP]
        assert manifest.items[0].content_file and "template" in manifest.items[0].reason
        assert manifest.items[1].mount_class is MountClass.PASSTHROUGH
        assert not manifest.items[1].content_file

    def test_a_raw_compose_service_s_content_file_is_mirrored(self) -> None:
        # Its target is created from the stored compose, kept without content:
        # (parsers.php v4.7.2:3701-3718): the target row is an empty directory.
        manifest = self._content_manifest(ResourceKind.SERVICE_COMPOSE)
        assert [i.decision for i in manifest.items] == [Decision.MIGRATE, Decision.SKIP]
        assert manifest.items[0].mount_class is MountClass.BIND
        assert not manifest.items[0].content_file
        assert "mirrored with rsync" in manifest.items[0].reason

    def test_a_refused_bind_blocks_once(self) -> None:
        manifest = reconcile(
            docker_mounts=[self._mount("/etc/app"), self._mount("/etc/app", "/other")],
            resource_uuid="u1",
        )
        assert manifest.is_blocked and len(manifest.refused) == 1

    def test_an_already_stopped_stack_migrates_its_declared_directory(self) -> None:
        # No container to inspect: Coolify's row is the only record of the bind.
        manifest = reconcile(
            docker_mounts=[],
            api_storages=[
                ApiStorage(kind="file", mount_path="/data", fs_path="./data", is_directory=True),
                ApiStorage(kind="file", mount_path="/etc/x", fs_path="./x", has_content=True),
                ApiStorage(kind="persistent", name="hp", mount_path="/hp", host_path="/etc/hp"),
            ],
            resource_uuid="u1",
            kind=ResourceKind.SERVICE_COMPOSE,
        )
        decisions = {i.mount_path: i.decision for i in manifest.items}
        # /etc/x: a raw-compose service's content file, which its target's row lacks.
        assert decisions == {
            "/data": Decision.MIGRATE,
            "/etc/x": Decision.MIGRATE,
            "/hp": Decision.SKIP,
        }
        assert manifest.to_migrate[0].source_path == f"{SVC}/data"

    def test_an_already_stopped_template_flags_its_content_file(self) -> None:
        manifest = reconcile(
            docker_mounts=[],
            api_storages=[
                ApiStorage(kind="file", mount_path="/etc/x", fs_path="./x", has_content=True)
            ],
            resource_uuid="u1",
            kind=ResourceKind.SERVICE_TEMPLATE,
        )
        (item,) = manifest.items
        assert item.decision is Decision.SKIP and item.content_file

    def test_a_running_stack_s_unmounted_row_is_stale_not_data(self) -> None:
        manifest = reconcile(
            docker_mounts=[self._mount("/srv/live", "/live")],
            api_storages=[ApiStorage(kind="file", mount_path="/old", fs_path="/srv/old")],
            resource_uuid="u1",
            kind=ResourceKind.SERVICE_COMPOSE,
        )
        assert [i.source_path for i in manifest.items] == ["/srv/live"]
        assert any("no container currently mounts" in w for w in manifest.warnings)
