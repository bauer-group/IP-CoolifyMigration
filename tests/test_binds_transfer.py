"""The server side of bind mounts: rsync of one file, its verification, the probes."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import BindTarget, HostBind
from bg_coolify_migrate.errors import TransferError, VerificationError
from bg_coolify_migrate.transfer import binds as transfer_binds
from bg_coolify_migrate.transfer import rsync, verify
from bg_coolify_migrate.transfer.ssh import CommandResult, SshTarget
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
        host.on(r"^sh -c ", exit_status=1, stderr="Cannot connect")
        with pytest.raises(TransferError, match="could not list"):
            await transfer_binds.host_binds(host)  # type: ignore[arg-type]
        assert shlex.split(host.commands[0])[2] == transfer_binds.HOST_BINDS_SCRIPT


def _args(command: str) -> list[str]:
    """The quoted arguments a fixed script received: ``sh -c SCRIPT sh ARGS...``."""
    return shlex.split(command)[4:]


class TestWrites:
    async def test_prepare_passes_the_path_as_an_argument_never_spliced(self) -> None:
        host = FakeHost()
        host.on(r"^sh -c ")
        await transfer_binds.prepare_target(host, "/srv/a", kind=binds.KIND_DIR, prior="missing")  # type: ignore[arg-type]
        await transfer_binds.prepare_target(  # type: ignore[arg-type]
            host, "/srv/b/f.db", kind=binds.KIND_FILE, prior=binds.STATE_EMPTY_DIR
        )
        assert [shlex.split(c)[2] for c in host.commands] == [transfer_binds.PREPARE_SCRIPT] * 2
        assert [_args(c) for c in host.commands] == [
            ["/srv/a", "dir", "missing"],
            ["/srv/b/f.db", "file", "empty-directory"],
        ]

    async def test_remove_only_what_is_allowed_deepest_first(self) -> None:
        host = FakeHost()
        host.on(r"^sh -c ")
        target = BindTarget("/srv/a/b", "missing", ("/srv/a", "/srv/a/b"))
        await transfer_binds.remove_created(host, target, removable={"/srv/a", "/srv/a/b"})  # type: ignore[arg-type]
        assert [shlex.split(c)[2] for c in host.commands] == [transfer_binds.REMOVE_SCRIPT] * 2
        assert [_args(c) for c in host.commands] == [["rm", "/srv/a/b"], ["rmdir", "/srv/a"]]
        host.commands.clear()
        await transfer_binds.remove_created(host, target, removable=set())  # type: ignore[arg-type]
        assert host.commands == []

    async def test_a_redirected_removal_raises_and_names_the_path(self) -> None:
        host = FakeHost()
        host.on(r"^sh -c ", exit_status=transfer_binds.EXIT_REDIRECTED)
        target = BindTarget("/srv/a/b", "missing", ("/srv/a/b",))
        with pytest.raises(TransferError, match=r"/srv/a/b.*symbolic link"):
            await transfer_binds.remove_created(host, target, removable={"/srv/a/b"})  # type: ignore[arg-type]

    async def test_a_redirected_prepare_raises_before_anything_is_copied(self) -> None:
        host = FakeHost()
        host.on(r"^sh -c ", exit_status=transfer_binds.EXIT_REDIRECTED)
        with pytest.raises(TransferError, match="resolves somewhere"):
            await transfer_binds.prepare_target(host, "/srv/a", kind=binds.KIND_DIR, prior="missing")  # type: ignore[arg-type]


def _posix(path: Path) -> str:
    """A local path as the ``sh`` running the scripts spells it, canonical.

    On Windows (Git's sh) ``C:/x`` is ``/c/x``; realpath first, so an 8.3 short
    name or a symlinked temp dir cannot make ``pwd -P`` disagree with it.
    """
    real = os.path.realpath(path)
    if os.name == "nt":
        return "/" + real[0].lower() + real[2:].replace("\\", "/")
    return real


class _LocalSh:
    """Runs the commands in a real local ``sh``: the scripts' own behaviour, not a model."""

    target = SshTarget(host="local")

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(
        self, command: str, *, timeout: float | None = None, input_text: str | None = None
    ) -> CommandResult:
        self.commands.append(command)
        done = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=False)
        return CommandResult(command, done.returncode, done.stdout, done.stderr)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
class TestSymlinkSafety:
    """A parent swapped for a symlink between the check and the root write or delete.

    A user who owns a pre-existing parent (``/home/alice``) can rename the
    directory the copy created and put ``app -> /etc`` in its place; a plain
    ``rm -rf /home/alice/app/data`` would then delete ``/etc/data`` as root.
    """

    @staticmethod
    def _link(link: Path, to: Path) -> None:
        try:
            os.symlink(to, link, target_is_directory=True)
        except OSError as exc:  # Windows without symlink rights
            pytest.skip(f"cannot create symlinks here: {exc}")

    async def test_removes_the_tree_it_created_and_its_empty_parents(self, tmp_path: Path) -> None:
        (tmp_path / "srv" / "a" / "b").mkdir(parents=True)
        (tmp_path / "srv" / "a" / "b" / "f").write_text("x")
        root = _posix(tmp_path)
        target = BindTarget(f"{root}/srv/a/b", "missing", (f"{root}/srv/a", f"{root}/srv/a/b"))
        await transfer_binds.remove_created(  # type: ignore[arg-type]
            _LocalSh(), target, removable=set(target.created)
        )
        assert not (tmp_path / "srv" / "a").exists()
        assert (tmp_path / "srv").is_dir()

    async def test_a_parent_in_use_by_someone_else_is_left(self, tmp_path: Path) -> None:
        (tmp_path / "srv" / "a" / "b").mkdir(parents=True)
        (tmp_path / "srv" / "a" / "other").write_text("theirs")
        root = _posix(tmp_path)
        target = BindTarget(f"{root}/srv/a/b", "missing", (f"{root}/srv/a", f"{root}/srv/a/b"))
        await transfer_binds.remove_created(  # type: ignore[arg-type]
            _LocalSh(), target, removable=set(target.created)
        )
        assert not (tmp_path / "srv" / "a" / "b").exists()
        assert (tmp_path / "srv" / "a" / "other").read_text() == "theirs"

    async def test_a_parent_swapped_for_a_symlink_is_not_followed(self, tmp_path: Path) -> None:
        victim = tmp_path / "etc" / "data"
        victim.mkdir(parents=True)
        (victim / "keep").write_text("system")
        (tmp_path / "home" / "alice").mkdir(parents=True)
        self._link(tmp_path / "home" / "alice" / "app", tmp_path / "etc")
        root = _posix(tmp_path)
        path = f"{root}/home/alice/app/data"
        target = BindTarget(path, "missing", (path,))
        with pytest.raises(TransferError, match="symbolic link"):
            await transfer_binds.remove_created(_LocalSh(), target, removable={path})  # type: ignore[arg-type]
        assert (victim / "keep").read_text() == "system"

    async def test_the_target_itself_swapped_for_a_symlink_is_not_followed(
        self, tmp_path: Path
    ) -> None:
        victim = tmp_path / "etc"
        victim.mkdir()
        (victim / "keep").write_text("system")
        (tmp_path / "srv").mkdir()
        self._link(tmp_path / "srv" / "data", victim)
        root = _posix(tmp_path)
        path = f"{root}/srv/data"
        target = BindTarget(path, "missing", (path,))
        with pytest.raises(TransferError, match="symbolic link"):
            await transfer_binds.remove_created(_LocalSh(), target, removable={path})  # type: ignore[arg-type]
        assert (victim / "keep").read_text() == "system"

    async def test_prepare_makes_a_directory_and_frees_a_file_slot(self, tmp_path: Path) -> None:
        root = _posix(tmp_path)
        await transfer_binds.prepare_target(  # type: ignore[arg-type]
            _LocalSh(), f"{root}/srv/a", kind=binds.KIND_DIR, prior="missing"
        )
        assert (tmp_path / "srv" / "a").is_dir()
        (tmp_path / "srv" / "b" / "f.db").mkdir(parents=True)
        await transfer_binds.prepare_target(  # type: ignore[arg-type]
            _LocalSh(), f"{root}/srv/b/f.db", kind=binds.KIND_FILE, prior=binds.STATE_EMPTY_DIR
        )
        assert (tmp_path / "srv" / "b").is_dir()
        assert not (tmp_path / "srv" / "b" / "f.db").exists()

    async def test_prepare_refuses_a_symlinked_parent(self, tmp_path: Path) -> None:
        (tmp_path / "etc").mkdir()
        (tmp_path / "home" / "alice").mkdir(parents=True)
        self._link(tmp_path / "home" / "alice" / "app", tmp_path / "etc")
        root = _posix(tmp_path)
        with pytest.raises(TransferError, match="resolves somewhere"):
            await transfer_binds.prepare_target(  # type: ignore[arg-type]
                _LocalSh(), f"{root}/home/alice/app/data", kind=binds.KIND_DIR, prior="missing"
            )
