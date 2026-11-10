"""Every bind pair passes this version's host-path rules before COPY writes a byte.

A resumed COPY reloads DISCOVER's pairs from the journal. Up to 2.14.0 DISCOVER
journalled every bind outside ``/data/coolify`` path-to-path — writable
``/etc``, ``/usr`` and ``/var/lib/docker`` paths included — and COPY only
looked at the target's state: an absent ``/etc/foo`` was created and filled,
and the rollback then refused to remove it. And the target's storage row is
read back, never predicted, so it can name a host path the source's does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import BindPair
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import TransferError
from tests.test_binds_engine import (
    SRC,
    TGT,
    _journalled_targets,
    _mock_target_storages,
    _record_copies,
    _src,
    _tgt,
    bind_context,
)


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    context = bind_context(tmp_path)
    yield context
    await context.api.aclose()


def _problem(source: str, target: str, *, target_uuid: str | None = "t1") -> str | None:
    return binds.pair_problem(
        BindPair(source, target, "/m"), source_uuid="u1", target_uuid=target_uuid
    )


class TestPairProblem:
    @pytest.mark.parametrize(
        ("source", "target"),
        [
            # What 2.14.0 journalled path-to-path:
            ("/etc/app", "/etc/app"),
            ("/var/lib/docker/volumes/x/_data", "/var/lib/docker/volumes/x/_data"),
            ("/usr/local/share/app", "/usr/local/share/app"),
            ("/root/backups", "/root/backups"),
            ("/home/alice/.ssh", "/home/alice/.ssh"),
            ("/srv", "/srv"),
            # A source that is not the resource's:
            ("/data/coolify/services/other/data", "/srv/x"),
            ("/data/coolify/services/u1", "/data/coolify/services/t1/data"),
            # A target that is not the target's:
            ("/srv/app", "/etc/app"),
            ("/data/coolify/services/u1/data", "/data/coolify/services/u1/data"),
            ("/data/coolify/services/u1/data", "/data/coolify/services/other/data"),
            ("/data/coolify/services/u1/data", "/data/coolify/proxy/dynamic"),
            ("/data/coolify/services/u1/data", "/data/coolify/services/t1"),
        ],
    )
    def test_refused(self, source: str, target: str) -> None:
        problem = _problem(source, target)
        assert problem is not None
        assert f"{source} -> {target}" in problem

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("/srv/app", "/srv/app"),
            ("/home/alice/app", "/home/alice/app"),
            ("/opt/app/data", "/opt/app/data"),
            ("/data/coolify/services/u1/data", "/data/coolify/services/t1/data"),
            # compose parsing version 3 resolves ./ under the applications root
            ("/data/coolify/applications/u1/data", "/data/coolify/applications/t1/data"),
            ("/data/coolify/services/u1/.ssh/keys", "/data/coolify/services/t1/.ssh/keys"),
        ],
    )
    def test_the_resource_s_own_paths_pass(self, source: str, target: str) -> None:
        assert _problem(source, target) is None

    def test_an_unknown_target_refuses_only_under_coolify(self) -> None:
        own = ("/data/coolify/services/u1/data", "/data/coolify/services/t1/data")
        assert _problem(*own, target_uuid=None)
        assert _problem("/srv/a", "/srv/a", target_uuid=None) is None

    def test_a_path_that_is_not_canonical_is_refused(self) -> None:
        assert "not canonical" in (_problem("/srv/a/", "/srv/a") or "")


class TestCopy:
    async def test_a_journal_of_2_14_refuses_before_anything_is_written(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 2.14.0's DISCOVER: path-to-path, kind-less, /etc included.
        rows = [
            {"source": "/srv/files", "target": "/srv/files", "mount_path": "/files"},
            {"source": "/etc/app", "target": "/etc/app", "mount_path": "/cfg"},
        ]
        ctx.journal.append(
            "step_completed", state="discover", detail={"volume_pairs": {"svc1": rows}}
        )
        ctx.bind_pairs = {}
        copies = _record_copies(monkeypatch)

        with pytest.raises(TransferError, match="never copies") as exc:
            await steps._copy_binds(ctx)

        assert "/etc/app -> /etc/app" in str(exc.value)
        assert "/srv/files ->" not in str(exc.value)
        hint = exc.value.hint or ""
        assert "began under an earlier version" in hint
        assert "coolify-migrate rollback m1" in hint and "plan the migration again" in hint
        # Nothing written, not even the pair that passes; neither server asked.
        assert copies == [] and _journalled_targets(ctx) == []
        assert _tgt(ctx).commands == [] and _src(ctx).commands == []

    async def test_a_target_in_the_source_uuid_s_tree_is_refused(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.bind_pairs = {
            "svc1": [BindPair(f"{SRC}/data", f"{SRC}/data", "/data", kind=binds.KIND_DIR)]
        }
        copies = _record_copies(monkeypatch)
        with pytest.raises(TransferError, match="not the target resource's"):
            await steps._copy_binds(ctx)
        assert copies == []

    async def test_the_target_uuid_is_read_from_create_s_journal(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.bind_pairs = {
            "svc1": [BindPair(f"{SRC}/data", f"{TGT}/data", "/data", kind=binds.KIND_DIR)]
        }
        ctx.target_uuids = {}
        copies = _record_copies(monkeypatch)
        with pytest.raises(TransferError, match="not the target resource's"):
            await steps._copy_binds(ctx)  # whose directory it is cannot be told
        ctx.journal.append(
            "step_completed", state="create_target", detail={"target_uuids": {"svc1": "tgt1"}}
        )
        assert await steps._copy_binds(ctx) == [f"{TGT}/data"]
        assert copies == [("dir", f"{SRC}/data", f"{TGT}/data")]


class TestCreate:
    async def test_a_target_row_on_a_host_path_refuses_before_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The branch HEAD moved: the target's row at /files names /etc/app.
        _mock_target_storages(respx_mock, (f"{TGT}/data", "/data"), ("/etc/app", "/files"))
        with pytest.raises(TransferError, match="not its own") as exc:
            await steps._assert_target_binds_safe(ctx)
        assert "/srv/files -> /etc/app" in str(exc.value)
        assert "never stopped" in (exc.value.hint or "")
        assert _tgt(ctx).commands == []
