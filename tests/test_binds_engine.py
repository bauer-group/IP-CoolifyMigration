"""Bind mounts through the engine: checked before the stop, copied, verified, undone.

Each test pins one promise of the bind path. The host fake below models a
filesystem — path states, mkdir, rm, rmdir — so "COPY journalled the target
before it created it" and "the rollback deleted only what COPY created" are
asserted against state, not against a scripted string.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import BindPair
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.plan import ResourcePlan, Strategy
from bg_coolify_migrate.domain.statemachine import Compensation, Outcome, State
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.context import (
    EphemeralKey,
    MigrationContext,
    deserialise_bind_pairs,
    serialise_bind_pairs,
)
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import PreflightError, TransferError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer import binds as transfer_binds
from bg_coolify_migrate.transfer.ssh import CommandResult, SshTarget
from tests.conftest import FakeHost
from tests.test_engine_steps import BASE, HOST, _plan, _snapshot

SRC = "/data/coolify/services/svc1"
TGT = "/data/coolify/services/tgt1"


class FsHost(FakeHost):
    """A host whose paths have states, and whose mkdir/rm/rmdir change them."""

    def __init__(
        self,
        paths: dict[str, str] | None = None,
        *,
        containers: list[dict[str, Any]] | None = None,
        target: SshTarget | None = None,
    ) -> None:
        super().__init__(target)
        self.paths: dict[str, str] = dict(paths or {})
        self.containers = containers or []
        self.rsyncs: list[str] = []

    def state(self, path: str) -> str:
        if path in self.paths:
            return self.paths[path]
        if any(p.startswith(f"{path}/") for p in self.paths):
            return binds.STATE_DIR
        return binds.STATE_MISSING

    def _children(self, path: str) -> list[str]:
        return [p for p in self.paths if p.startswith(f"{path}/")]

    def _prepare(self, command: str, path: str, kind: str, prior: str) -> CommandResult:
        """transfer_binds.PREPARE_SCRIPT, on this model of the filesystem."""
        ok = CommandResult(command=command, exit_status=0, stdout="", stderr="")
        if kind == binds.KIND_DIR:
            self._mkdir(path)
            return ok
        self._mkdir(path.rsplit("/", 1)[0] or "/")
        if prior == binds.STATE_EMPTY_DIR:
            return self._remove(command, "rmdir", path, strict=True)
        return ok

    def _remove(
        self, command: str, mode: str, path: str, *, strict: bool = False
    ) -> CommandResult:
        """transfer_binds.REMOVE_SCRIPT (``rm`` or ``rmdir``), on this model."""
        ok = CommandResult(command=command, exit_status=0, stdout="", stderr="")
        if mode == "rm":
            for p in [path, *self._children(path)]:
                self.paths.pop(p, None)
            return ok
        if self._children(path) or self.state(path) not in (binds.STATE_EMPTY_DIR, binds.STATE_DIR):
            busy = CommandResult(command=command, exit_status=1, stdout="", stderr="busy")
            return busy if strict else ok  # the script's rmdir of a parent never fails
        self.paths.pop(path, None)
        return ok

    def _mkdir(self, path: str) -> None:
        for p in (*binds.ancestors(path), path):
            if self.state(p) == binds.STATE_MISSING:
                self.paths[p] = binds.STATE_EMPTY_DIR
            elif self.paths.get(p) == binds.STATE_EMPTY_DIR and p != path:
                self.paths[p] = binds.STATE_DIR

    async def run(
        self, command: str, *, timeout: float | None = None, input_text: str | None = None
    ) -> CommandResult:
        self.commands.append(command)
        for pattern, responder in self._routes:
            if pattern.search(command):
                return responder(command)
        ok = CommandResult(command=command, exit_status=0, stdout="", stderr="")
        if command.startswith("sh -c "):
            argv = shlex.split(command)
            script, args = argv[2], argv[4:]
            if script == transfer_binds.PREPARE_SCRIPT:
                return self._prepare(command, *args)
            if script == transfer_binds.REMOVE_SCRIPT:
                return self._remove(command, *args)
            out = "\n".join(self.state(p) for p in args)
            return CommandResult(command=command, exit_status=0, stdout=out, stderr="")
        if command.startswith("docker ps -aq"):
            out = "\n".join(json.dumps(c) for c in self.containers)
            return CommandResult(command=command, exit_status=0, stdout=out, stderr="")
        if command.startswith("docker ps -a "):
            return ok  # no volume pins on this host
        if "docker events" in command:
            return ok  # the containment checkpoints find nothing started

        if command.startswith("rsync"):
            self.rsyncs.append(command)
            return ok
        for part in command.split(" && "):
            argv = shlex.split(part.replace(" 2>/dev/null", ""))
            path = argv[-1]
            if argv[:3] == ["mkdir", "-p", "--"]:
                self._mkdir(path)
            elif argv[:3] == ["rm", "-rf", "--"]:
                for p in [path, *self._children(path)]:
                    self.paths.pop(p, None)
            elif argv[:2] == ["rmdir", "--"]:
                if self._children(path) or self.state(path) not in (
                    binds.STATE_EMPTY_DIR,
                    binds.STATE_DIR,
                ):
                    return CommandResult(command=command, exit_status=1, stdout="", stderr="busy")
                self.paths.pop(path, None)
            else:
                raise AssertionError(f"FsHost has no answer for {command!r}")
        return ok


def _bind(source: str, mount: str = "/data") -> VolumeItem:
    return VolumeItem(
        mount_class=MountClass.BIND,
        decision=Decision.MIGRATE,
        reason="bind",
        source_path=source,
        mount_path=mount,
        bytes=10,
    )


def _service(*items: VolumeItem, uuid: str = "svc1", name: str = "store") -> ResourcePlan:
    return ResourcePlan(
        snapshot=_snapshot(
            uuid=uuid,
            name=name,
            collection="services",
            kind=ResourceKind.SERVICE_COMPOSE,
            engine=None,
            image=None,
        ),
        strategy=Strategy.COPY_DATA,
        manifest=VolumeManifest(items=items),
    )


def _storages(*rows: tuple[str, str]) -> dict[str, Any]:
    return {
        "persistent_storages": [],
        "file_storages": [
            {"mount_path": m, "fs_path": f, "is_directory": True} for f, m in rows
        ],
    }


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(resources=(_service(_bind(f"{SRC}/data"), _bind("/srv/files", "/files")),)),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=FsHost(  # type: ignore[arg-type]
            {f"{SRC}/data": binds.STATE_DIR, "/srv/files": binds.STATE_DIR}
        ),
        target_host=FsHost(target=SshTarget(host="10.0.0.2")),  # type: ignore[arg-type]
    )
    context.target_uuids = {"svc1": "tgt1"}
    yield context
    await api.aclose()


def _src(ctx: MigrationContext) -> FsHost:
    assert isinstance(ctx.source_host, FsHost)
    return ctx.source_host


def _tgt(ctx: MigrationContext) -> FsHost:
    assert isinstance(ctx.target_host, FsHost)
    return ctx.target_host


def _mock_target_storages(respx_mock: respx.Router, *rows: tuple[str, str]) -> None:
    respx_mock.get(f"{BASE}/services/tgt1/storages").mock(
        return_value=httpx.Response(200, json=_storages(*rows))
    )


TARGET_ROWS = ((f"{TGT}/data", "/data"), ("/srv/files", "/files"))


# ── PREFLIGHT: the source side ───────────────────────────────────────────────


class TestSourceCheck:
    async def test_a_plan_without_binds_asks_nothing(self, ctx: MigrationContext) -> None:
        ctx.plan = _plan()
        await steps._assert_source_binds_safe(ctx)
        assert _src(ctx).commands == []

    async def test_passes_when_only_the_plan_mounts_its_binds(self, ctx: MigrationContext) -> None:
        _src(ctx).containers = [
            {
                "name": "/store-svc1",
                "labels": {"coolify.managed": "true"},
                "mounts": [{"Type": "bind", "Source": f"{SRC}/data", "RW": True}],
            },
            {
                "name": "/cadvisor",
                "labels": None,
                "mounts": [{"Type": "bind", "Source": "/", "RW": False}],
            },
        ]
        await steps._assert_source_binds_safe(ctx)

    async def test_a_path_another_container_mounts_is_refused(self, ctx: MigrationContext) -> None:
        _src(ctx).containers = [
            {
                "name": "/backup-agent",
                "labels": {},
                "mounts": [{"Type": "bind", "Source": "/srv/files", "RW": False}],
            }
        ]
        with pytest.raises(PreflightError, match="backup-agent") as exc:
            await steps._assert_source_binds_safe(ctx)
        assert "Nothing has been changed" in (exc.value.hint or "")

    async def test_a_socket_or_a_missing_source_is_refused(self, ctx: MigrationContext) -> None:
        _src(ctx).paths["/srv/files"] = binds.STATE_OTHER
        with pytest.raises(PreflightError, match="not a regular file or directory"):
            await steps._assert_source_binds_safe(ctx)


# ── CREATE: the target side, while the source still serves ──────────────────


class TestTargetCheck:
    async def test_free_targets_pass_and_nothing_is_written(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_target_storages(respx_mock, *TARGET_ROWS)
        _tgt(ctx).paths[f"{TGT}/data"] = binds.STATE_EMPTY_DIR  # Coolify's storage job
        await steps._assert_target_binds_safe(ctx)
        assert not any(c.startswith(("mkdir", "rm", "rsync")) for c in _tgt(ctx).commands)

    async def test_a_target_path_holding_data_is_refused(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_target_storages(respx_mock, *TARGET_ROWS)
        _tgt(ctx).paths["/srv/files/report.pdf"] = binds.STATE_FILE
        with pytest.raises(TransferError, match="already holds data") as exc:
            await steps._assert_target_binds_safe(ctx)
        assert "never stopped" in (exc.value.hint or "")

    async def test_a_target_path_another_container_mounts_is_refused(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_target_storages(respx_mock, *TARGET_ROWS)
        _tgt(ctx).containers = [
            {"name": "/other", "labels": {}, "mounts": [{"Type": "bind", "Source": "/srv"}]}
        ]
        with pytest.raises(TransferError, match="mounted on the target server by other"):
            await steps._assert_target_binds_safe(ctx)

    async def test_a_bind_the_target_declares_no_row_for_is_refused(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_target_storages(respx_mock, (f"{TGT}/data", "/data"))
        with pytest.raises(TransferError, match="no counterpart") as exc:
            await steps._assert_target_binds_safe(ctx)
        assert "uuid replaced" in (exc.value.hint or "")

    async def test_a_symlinked_parent_is_refused(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_target_storages(respx_mock, *TARGET_ROWS)
        _tgt(ctx).paths["/srv"] = binds.STATE_SYMLINK
        with pytest.raises(TransferError, match="symbolic link"):
            await steps._assert_target_binds_safe(ctx)

    async def test_without_binds_it_asks_nothing(self, ctx: MigrationContext) -> None:
        ctx.plan = _plan()
        await steps._assert_target_binds_safe(ctx)
        assert _tgt(ctx).commands == []


# ── DISCOVER: the journalled pairs ───────────────────────────────────────────


class TestDiscoverRecord:
    async def test_pairs_are_read_back_and_journalled_with_their_kind(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_target_storages(respx_mock, *TARGET_ROWS)
        _src(ctx).paths["/srv/files"] = binds.STATE_FILE
        pairs = await steps._read_target_bind_pairs(
            ctx,
            ctx.plan.resources[0].snapshot,
            steps._source_binds(ctx.plan.resources[0].manifest),
            target_uuid="tgt1",
            consequence="x",
        )
        recorded: dict[str, list[dict[str, str]]] = {"svc1": []}
        await steps._record_bind_pairs(ctx, {"svc1": pairs}, recorded)

        assert recorded["svc1"] == [
            {"source": f"{SRC}/data", "target": f"{TGT}/data", "mount_path": "/data", "kind": "dir"},
            {"source": "/srv/files", "target": "/srv/files", "mount_path": "/files", "kind": "file"},
        ]
        # The journal's rows rebuild exactly what this run holds.
        assert deserialise_bind_pairs(recorded) == ctx.bind_pairs

    async def test_a_source_bind_gone_after_the_stop_is_refused(
        self, ctx: MigrationContext
    ) -> None:
        del _src(ctx).paths["/srv/files"]
        with pytest.raises(TransferError, match="does not exist"):
            await steps._record_bind_pairs(
                ctx, {"svc1": [BindPair("/srv/files", "/srv/files", "/files")]}, {}
            )

    async def test_two_resources_one_host_path_two_targets_is_refused(
        self, ctx: MigrationContext
    ) -> None:
        with pytest.raises(TransferError, match="both"):
            await steps._record_bind_pairs(
                ctx,
                {
                    "a": [BindPair("/srv/s", "/srv/s", "/s")],
                    "b": [BindPair("/srv/s", "/srv/t", "/s")],
                },
                {},
            )


# ── COPY ─────────────────────────────────────────────────────────────────────


def _pairs(ctx: MigrationContext) -> None:
    ctx.bind_pairs = {
        "svc1": [
            BindPair(f"{SRC}/data", f"{TGT}/data", "/data", kind=binds.KIND_DIR),
            BindPair("/srv/files/a.db", "/srv/files/a.db", "/files", kind=binds.KIND_FILE),
        ]
    }


def _record_copies(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    copies: list[tuple[str, str, str]] = []

    async def copy_dir(_ctx: MigrationContext, source: str, target: str) -> None:
        copies.append(("dir", source, target))

    async def copy_file(_ctx: MigrationContext, source: str, target: str) -> None:
        copies.append(("file", source, target))

    monkeypatch.setattr(steps, "_copy_one", copy_dir)
    monkeypatch.setattr(steps, "_copy_file", copy_file)
    return copies


def _journalled_targets(ctx: MigrationContext) -> list[dict[str, Any]]:
    return [
        r.detail[binds.BIND_TARGET]
        for r in ctx.journal.read()
        if r.state == "copy" and binds.BIND_TARGET in r.detail
    ]


class TestCopy:
    async def test_each_target_is_journalled_before_it_is_created(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pairs(ctx)
        target = _tgt(ctx)
        target.paths[f"{TGT}/data"] = binds.STATE_EMPTY_DIR
        journalled_at_mkdir: list[int] = []
        original = target.run

        async def spy(command: str, **kw: Any) -> CommandResult:
            if command.startswith("sh -c ") and transfer_binds.PREPARE_SCRIPT in command:
                journalled_at_mkdir.append(len(_journalled_targets(ctx)))
            return await original(command, **kw)

        monkeypatch.setattr(target, "run", spy)
        copies = _record_copies(monkeypatch)

        assert await steps._copy_binds(ctx) == [f"{TGT}/data", "/srv/files/a.db"]

        assert journalled_at_mkdir == [1, 2]
        assert _journalled_targets(ctx) == [
            {"path": f"{TGT}/data", "prior": "empty-directory", "created": []},
            {
                "path": "/srv/files/a.db",
                "prior": "missing",
                "created": ["/srv", "/srv/files", "/srv/files/a.db"],
            },
        ]
        assert copies == [
            ("dir", f"{SRC}/data", f"{TGT}/data"),
            ("file", "/srv/files/a.db", "/srv/files/a.db"),
        ]
        # The file's parent exists for rsync; the file itself is rsync's to create.
        assert target.state("/srv/files") == binds.STATE_EMPTY_DIR

    async def test_coolify_s_empty_default_directory_gives_way_to_a_file(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.bind_pairs = {"svc1": [BindPair(f"{SRC}/a.db", f"{TGT}/a.db", "/a", binds.KIND_FILE)]}
        _tgt(ctx).paths[f"{TGT}/a.db"] = binds.STATE_EMPTY_DIR
        _record_copies(monkeypatch)
        await steps._copy_binds(ctx)
        assert _tgt(ctx).state(f"{TGT}/a.db") == binds.STATE_MISSING  # rmdir'd for rsync

    async def test_data_that_appeared_since_create_is_never_overwritten(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pairs(ctx)
        _tgt(ctx).paths[f"{TGT}/data/x"] = binds.STATE_FILE
        copies = _record_copies(monkeypatch)
        with pytest.raises(TransferError, match="already holds data"):
            await steps._copy_binds(ctx)
        assert copies == [] and _journalled_targets(ctx) == []

    async def test_a_resumed_copy_copies_over_its_own_partial_target(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pairs(ctx)
        ctx.journal.append(
            "step_started",
            state="copy",
            detail={binds.BIND_TARGET: {"path": f"{TGT}/data", "prior": "missing", "created": []}},
        )
        _tgt(ctx).paths[f"{TGT}/data/half-copied"] = binds.STATE_FILE
        copies = _record_copies(monkeypatch)
        await steps._copy_binds(ctx)
        assert copies[0] == ("dir", f"{SRC}/data", f"{TGT}/data")
        # Not journalled twice: the first record (prior: missing) stays the truth.
        assert [t["path"] for t in _journalled_targets(ctx)] == [f"{TGT}/data", "/srv/files/a.db"]

    async def test_one_host_path_two_resources_bind_is_copied_once(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared = BindPair("/srv/shared", "/srv/shared", "/s", binds.KIND_DIR)
        ctx.plan = _plan(resources=(_service(uuid="a", name="a"), _service(uuid="b", name="b")))
        ctx.bind_pairs = {"a": [shared], "b": [shared]}
        copies = _record_copies(monkeypatch)
        await steps._copy_binds(ctx)
        assert copies == [("dir", "/srv/shared", "/srv/shared")]

    async def test_a_binds_only_plan_still_installs_the_key_and_copies(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The early "nothing to copy" return looked at volume pairs only, so a
        # stack whose data lived in bind mounts was "copied" without a byte.
        _pairs(ctx)
        ctx.volume_pairs = {}
        installed: list[str] = []

        async def install(**_kw: Any) -> EphemeralKey:
            installed.append("key")
            return EphemeralKey("priv", "pub", "SHA256:fp", "/root/.coolify-migrate/k")

        async def no_pins(*_a: Any, **_kw: Any) -> list[str]:
            return []

        async def quiet(*_a: Any, **_kw: Any) -> Any:
            return None

        monkeypatch.setattr(steps.keys, "install", install)
        monkeypatch.setattr(steps.pins, "ensure", no_pins)
        monkeypatch.setattr(steps.quiesce, "snapshot", quiet)
        monkeypatch.setattr(steps.quiesce, "assert_still_stopped", quiet)
        copies = _record_copies(monkeypatch)

        await steps.step_copy(ctx)
        assert installed == ["key"]
        assert len(copies) == 2

    def test_bind_pairs_are_reloaded_from_discover_s_journal(self, ctx: MigrationContext) -> None:
        # A resume skips the completed DISCOVER; the context starts empty.
        _pairs(ctx)
        rows = serialise_bind_pairs(ctx.bind_pairs["svc1"])
        ctx.bind_pairs = {}
        ctx.journal.append("step_completed", state="discover", detail={"volume_pairs": {"svc1": rows}})
        assert steps._bind_pairs(ctx)["svc1"][1].kind == binds.KIND_FILE


# ── VERIFY ───────────────────────────────────────────────────────────────────


class TestVerify:
    async def test_binds_are_verified_like_volumes_files_as_files(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.transfer.verify import Manifest, VerificationReport

        _pairs(ctx)
        ctx.volume_pairs = {}
        calls: list[tuple[str, str, bool]] = []

        async def fake_verify(_s: Any, _t: Any, **kw: Any) -> VerificationReport:
            calls.append((kw["source_path"], kw["target_path"], kw["single_file"]))
            return VerificationReport(
                kw["source_path"], kw["target_path"], Manifest(), Manifest(), ()
            )

        monkeypatch.setattr(steps.verify, "verify_volume", fake_verify)
        result = await steps.step_verify(ctx)
        assert calls == [
            (f"{SRC}/data", f"{TGT}/data", False),
            ("/srv/files/a.db", "/srv/files/a.db", True),
        ]
        assert result["volumes_verified"] == [f"{TGT}/data", "/srv/files/a.db"]

    async def test_a_kind_the_journal_lacks_is_looked_up(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.bind_pairs = {"svc1": [BindPair("/srv/files", "/srv/files", "/files")]}
        _src(ctx).paths["/srv/files"] = binds.STATE_FILE
        assert await steps._bind_kinds(ctx, ctx.bind_pairs["svc1"]) == [
            (ctx.bind_pairs["svc1"][0], binds.KIND_FILE)
        ]


# ── ROLLBACK ─────────────────────────────────────────────────────────────────


def _copied(ctx: MigrationContext, path: str, prior: str, created: list[str]) -> None:
    ctx.journal.append(
        "step_started",
        state="copy",
        detail={binds.BIND_TARGET: {"path": path, "prior": prior, "created": created}},
    )


class TestUndo:
    async def test_only_what_copy_created_is_removed(self, ctx: MigrationContext) -> None:
        target = _tgt(ctx)
        target.paths.update(
            {
                "/srv/files/a.db": binds.STATE_FILE,
                "/srv/keep/untouched": binds.STATE_FILE,
                f"{TGT}/data/x": binds.STATE_FILE,
                "/opt/pre/existing": binds.STATE_FILE,
            }
        )
        _copied(ctx, "/srv/files/a.db", "missing", ["/srv/files", "/srv/files/a.db"])
        _copied(ctx, f"{TGT}/data", "empty-directory", [])  # Coolify's, inside the target
        _copied(ctx, "/opt/pre", "empty-directory", [])  # was there, empty

        await compensations.undo_copy(ctx, {})

        assert target.state("/srv/files") == binds.STATE_MISSING  # created parent, rmdir'd
        assert target.state("/srv/keep/untouched") == binds.STATE_FILE
        assert target.state(f"{TGT}/data/x") == binds.STATE_FILE  # the target delete's job
        assert target.state("/opt/pre/existing") == binds.STATE_FILE
        assert any("/opt/pre" in n and "by hand" in n for n in ctx.notices)
        assert not any(TGT in n for n in ctx.notices)

    async def test_a_parent_someone_else_used_meanwhile_stays(self, ctx: MigrationContext) -> None:
        target = _tgt(ctx)
        target.paths.update({"/srv/app/data/x": binds.STATE_FILE, "/srv/app/theirs": binds.STATE_FILE})
        _copied(ctx, "/srv/app/data", "missing", ["/srv/app", "/srv/app/data"])
        await compensations.undo_copy(ctx, {})
        assert target.state("/srv/app/data") == binds.STATE_MISSING
        assert target.state("/srv/app/theirs") == binds.STATE_FILE

    async def test_a_replayed_rollback_removes_nothing_twice(self, ctx: MigrationContext) -> None:
        target = _tgt(ctx)
        target.paths["/srv/files/a.db"] = binds.STATE_FILE
        _copied(ctx, "/srv/files/a.db", "missing", ["/srv/files/a.db"])
        await compensations.undo_copy(ctx, {})
        # Someone creates the path again after the rollback; a second rollback
        # of THIS migration must leave it.
        target.paths["/srv/files/a.db"] = binds.STATE_FILE
        await compensations.undo_copy(ctx, {})
        assert target.state("/srv/files/a.db") == binds.STATE_FILE

    async def test_an_unsafe_path_is_never_removed(self, ctx: MigrationContext) -> None:
        target = _tgt(ctx)
        target.paths["/etc/app/x"] = binds.STATE_FILE
        _copied(ctx, "/etc/app", "missing", ["/etc/app"])
        with pytest.raises(RuntimeError, match="KEPT"):
            await compensations.undo_copy(ctx, {})
        assert target.state("/etc/app/x") == binds.STATE_FILE

    async def test_after_finalize_a_target_whose_source_is_gone_is_kept(
        self, ctx: MigrationContext
    ) -> None:
        target = _tgt(ctx)
        target.paths["/srv/files/x"] = binds.STATE_FILE
        ctx.journal.append(
            "step_completed",
            state="discover",
            detail={"volume_pairs": {"svc1": [{"source": "/srv/files", "target": "/srv/files"}]}},
        )
        _copied(ctx, "/srv/files", "missing", ["/srv/files"])
        ctx.journal.append("step_started", state="finalize", detail={"policy": "keep"})
        del _src(ctx).paths["/srv/files"]  # deleted by hand after the cutover
        with pytest.raises(RuntimeError, match="only copy"):
            await compensations.undo_copy(ctx, {})
        assert target.state("/srv/files/x") == binds.STATE_FILE

    async def test_after_finalize_a_target_without_a_known_source_is_kept(
        self, ctx: MigrationContext
    ) -> None:
        # No DISCOVER record says where it came from: nothing proves the source
        # still holds it, so the target's copy is not deleted on a guess.
        target = _tgt(ctx)
        target.paths["/srv/files/x"] = binds.STATE_FILE
        _copied(ctx, "/srv/files", "missing", ["/srv/files"])
        ctx.journal.append("step_started", state="finalize", detail={"policy": "rename"})
        with pytest.raises(RuntimeError, match="KEPT"):
            await compensations.undo_copy(ctx, {})
        assert target.state("/srv/files/x") == binds.STATE_FILE

    async def test_a_copy_that_failed_after_a_bind_still_owes_its_undo(
        self, ctx: MigrationContext
    ) -> None:
        """A live rollback owes only completed states — COPY's bind marker makes it owed."""
        target = _tgt(ctx)
        ctx.journal.append("step_completed", state="create_target", detail={"target_uuids": {}})

        async def failing_copy(c: MigrationContext) -> dict[str, Any]:
            target.paths["/srv/files/a.db"] = binds.STATE_FILE
            _copied(c, "/srv/files/a.db", "missing", ["/srv/files/a.db"])
            raise TransferError("the source restarted mid-copy")

        ran: list[str] = []

        async def note(name: str) -> Any:
            async def compensation(c: MigrationContext, info: dict[str, Any]) -> None:
                ran.append(name)

            return compensation

        comps = {
            Compensation.DROP_TARGET_VOLUMES: compensations.undo_copy,
            Compensation.REVOKE_EPHEMERAL_KEY: await note("revoke"),
            Compensation.DELETE_TARGET_RESOURCE: await note("delete"),
            Compensation.RESTORE_SOURCE_FQDN: await note("fqdn"),
            Compensation.RELEASE_SOURCE_PINS: await note("pins"),
        }
        saga = Saga(
            journal=ctx.journal,
            context=ctx,
            steps={State.COPY: failing_copy},
            compensations=comps,
        )
        result = await saga.run(start_from=State.COPY)
        assert result.outcome is Outcome.ROLLED_BACK
        assert target.state("/srv/files/a.db") == binds.STATE_MISSING
        assert "revoke" in ran
