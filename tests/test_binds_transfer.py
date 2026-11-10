"""The server side of bind mounts: rsync of one file, its verification, the probes."""

from __future__ import annotations

import json
import shlex

import pytest

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import BindTarget, HostBind
from bg_coolify_migrate.errors import TransferError, VerificationError
from bg_coolify_migrate.transfer import binds as transfer_binds
from bg_coolify_migrate.transfer import rsync, verify
from tests.conftest import FakeHost


class TestSingleFileRsync:
    def _spec(self) -> rsync.RsyncSpec:
        return rsync.RsyncSpec(
            source_path="/srv/app/db.sqlite",
            target_path="/srv/app/db.sqlite",
            target_host="10.0.0.2",
            single_file=True,
        )

    def test_no_trailing_slash_on_either_side(self) -> None:
        argv = shlex.split(rsync.build_command(self._spec()))
        assert argv[-2] == "/srv/app/db.sqlite"
        assert argv[-1] == "root@10.0.0.2:/srv/app/db.sqlite"

    def test_every_base_flag_is_kept(self) -> None:
        # Invariant 1: numeric ids, never a chown; BASE_FLAGS unchanged.
        argv = shlex.split(rsync.build_command(self._spec()))
        for flag in rsync.BASE_FLAGS:
            assert flag in argv
        assert "StrictHostKeyChecking=no" not in " ".join(argv)


class TestVerifyFile:
    def _host(self, *, meta: str, digest: str) -> FakeHost:
        host = FakeHost()
        host.on(r"find --version", stdout="find (GNU findutils) 4.9.0")
        host.on(r"-maxdepth 0 -printf", stdout=meta)
        host.on(r"sha256sum", stdout=f"{digest}  /srv/app/db.sqlite")
        return host

    async def test_a_file_is_hashed_and_statted_keyed_dot(self) -> None:
        host = self._host(meta="f|644|999|999|", digest="ab" * 32)
        manifest = await verify.collect_file(host, "/srv/app/db.sqlite")  # type: ignore[arg-type]
        assert manifest.content == {".": "ab" * 32}
        assert manifest.metadata == {".": "f|644|999|999|"}

    async def test_a_chowned_copy_fails_verification(self) -> None:
        source = self._host(meta="f|644|999|999|", digest="ab" * 32)
        target = self._host(meta="f|644|0|0|", digest="ab" * 32)
        report = await verify.verify_volume(
            source,  # type: ignore[arg-type]
            target,  # type: ignore[arg-type]
            source_path="/srv/app/db.sqlite",
            target_path="/srv/app/db.sqlite",
            single_file=True,
        )
        assert not report.ok
        assert report.differences[0].kind is verify.DiffKind.METADATA_DIFFERS

    async def test_a_file_that_cannot_be_read_raises_rather_than_passing(self) -> None:
        host = FakeHost()
        host.on(r"find --version", stdout="BusyBox")
        host.on(r"stat -c", stdout="")
        host.on(r"sha256sum", exit_status=1, stdout="")
        with pytest.raises(VerificationError, match="could not hash"):
            await verify.collect_file(host, "/srv/x")  # type: ignore[arg-type]


class TestProbes:
    async def test_path_states_pass_the_paths_as_arguments(self) -> None:
        host = FakeHost()
        host.on(r"^sh -c ", stdout="missing\nempty-directory\n")
        states = await transfer_binds.path_states(host, ["/a b", "/c;rm -rf /", "/a b"])  # type: ignore[arg-type]
        assert states == {"/a b": "missing", "/c;rm -rf /": "empty-directory"}
        argv = shlex.split(host.commands[0])
        # Never spliced into the script: each path is one argument.
        assert argv[4:] == ["/a b", "/c;rm -rf /"]

    async def test_an_incomplete_answer_raises(self) -> None:
        host = FakeHost()
        host.on(r"^sh -c ", stdout="missing\n")
        with pytest.raises(TransferError, match="could not read"):
            await transfer_binds.path_states(host, ["/a", "/b"])  # type: ignore[arg-type]

    async def test_no_paths_ask_nothing(self) -> None:
        host = FakeHost()
        assert await transfer_binds.path_states(host, []) == {}  # type: ignore[arg-type]
        assert host.commands == []

    def test_parse_host_binds_keeps_binds_only(self) -> None:
        line = json.dumps(
            {
                "name": "/web",
                "labels": {"coolify.managed": "true"},
                "mounts": [
                    {"Type": "bind", "Source": "/srv/x", "RW": False},
                    {"Type": "volume", "Name": "v", "Source": "/var/lib/docker/volumes/v/_data"},
                ],
            }
        )
        parsed = transfer_binds.parse_host_binds(f"{line}\nnot json\n\n")
        assert parsed == [
            HostBind("web", "/srv/x", rw=False, labels={"coolify.managed": "true"})
        ]

    async def test_a_daemon_that_will_not_answer_is_no_all_clear(self) -> None:
        host = FakeHost()
        host.on(r"^docker ps -aq", exit_status=1, stderr="Cannot connect")
        with pytest.raises(TransferError, match="could not list"):
            await transfer_binds.host_binds(host)  # type: ignore[arg-type]


class TestWrites:
    async def test_prepare_a_directory_and_a_file(self) -> None:
        host = FakeHost()
        host.on(r"mkdir|rmdir")
        await transfer_binds.prepare_target(host, "/srv/a", kind=binds.KIND_DIR, prior="missing")  # type: ignore[arg-type]
        await transfer_binds.prepare_target(  # type: ignore[arg-type]
            host, "/srv/b/f.db", kind=binds.KIND_FILE, prior=binds.STATE_EMPTY_DIR
        )
        assert host.commands == [
            "mkdir -p -- /srv/a",
            "mkdir -p -- /srv/b && rmdir -- /srv/b/f.db",
        ]

    async def test_remove_only_what_is_allowed_deepest_first(self) -> None:
        host = FakeHost()
        host.on(r"^rm -rf|^rmdir")
        target = BindTarget("/srv/a/b", "missing", ("/srv/a", "/srv/a/b"))
        await transfer_binds.remove_created(host, target, removable={"/srv/a", "/srv/a/b"})  # type: ignore[arg-type]
        assert host.commands == ["rm -rf -- /srv/a/b", "rmdir -- /srv/a 2>/dev/null"]
        host.commands.clear()
        await transfer_binds.remove_created(host, target, removable=set())  # type: ignore[arg-type]
        assert host.commands == []
