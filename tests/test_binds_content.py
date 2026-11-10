"""Content files: Coolify writes them from the storage row — but only the TARGET's row counts.

The plan skipped every file Coolify writes from the content kept in its
file-storage row as "regenerated", before it asked how the row reaches the
target. True for a plain app (CREATE POSTs the content), a compose app (git
keeps ``content:``) and a template service (the catalog has it) — not for:

* a DATABASE: CREATE carries none of its storage rows, so the target started
  without its pg_hba.conf or my.cnf, without a word;
* a RAW-COMPOSE service: Coolify keeps ``docker_compose_raw`` without
  ``content:`` (parsers.php v4.1.2:2740 .. v4.7.2:3709) and the target is
  created from it, so its row is a contentless directory (v4.7.2:3051-3058)
  and the target's container mounted an empty directory where nginx.conf was.

And where the plan rightly skips one, a template or a repository can still
drop the file: the target's row is read back at the end of CREATE.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import BindAction, StorageCarry, StorageRow
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import (
    MigrationContext,
)
from bg_coolify_migrate.errors import TransferError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import SshTarget
from tests.test_binds_engine import SRC, TGT, FsHost, _bind, _service
from tests.test_engine_steps import BASE, HOST, _plan

SVC = "/data/coolify/services/u1"
CONF = f"{SVC}/nginx.conf"
MOUNT = "/etc/nginx/nginx.conf"


def _classify(
    source: str, carry: StorageCarry, *, read_only: bool = True
) -> tuple[BindAction, str]:
    return binds.classify(
        source=source,
        destination=MOUNT,
        read_only=read_only,
        uuid="u1",
        rows=[StorageRow(MOUNT, source, writes_content=True)],
        carry=carry,
    )


class TestTheCarryDecidesFirst:
    def test_a_raw_compose_service_carries_rows_without_content(self) -> None:
        assert binds.carry_for(ResourceKind.SERVICE_COMPOSE) is StorageCarry.RAW_COMPOSE
        assert binds.carry_for(ResourceKind.SERVICE_TEMPLATE) is StorageCarry.COMPOSE
        assert binds.carry_for(ResourceKind.APP_GIT_COMPOSE) is StorageCarry.COMPOSE

    def test_a_database_s_content_file_is_refused(self) -> None:
        action, reason = _classify("/data/coolify/databases/u1/pg_hba.conf", StorageCarry.NONE)
        assert action is BindAction.REFUSE
        assert "carries no storage row of a database" in reason

    def test_a_raw_compose_service_s_content_file_is_mirrored(self) -> None:
        action, reason = _classify(CONF, StorageCarry.RAW_COMPOSE)
        assert action is BindAction.MIGRATE
        assert "without `content:`" in reason and "mirrored with rsync" in reason

    def test_a_raw_compose_content_file_at_a_system_path_is_refused(self) -> None:
        # Coolify writes it there for the source; the target's row has nothing
        # to write, and a migration never writes the host's own paths.
        action, reason = _classify("/etc/myapp/app.conf", StorageCarry.RAW_COMPOSE)
        assert action is BindAction.REFUSE
        assert "host's own paths" in reason

    @pytest.mark.parametrize("carry", [StorageCarry.COMPOSE, StorageCarry.API])
    def test_where_the_target_s_row_gets_the_content_it_is_skipped(
        self, carry: StorageCarry
    ) -> None:
        action, reason = _classify(CONF, carry)
        assert action is BindAction.SKIP
        assert "reads that row back" in reason

    def test_a_content_file_at_a_system_path_is_the_resource_s_not_the_host_s(self) -> None:
        # Before: "the target server supplies its own" — it does not, Coolify
        # writes it from the target's row.
        action, reason = _classify("/etc/myapp/app.conf", StorageCarry.COMPOSE)
        assert action is BindAction.SKIP and "supplies its own" not in reason

    def test_a_database_s_generated_files_without_a_row_stay_skipped(self) -> None:
        action, _ = binds.classify(
            source="/data/coolify/databases/u1/custom-postgres.conf",
            destination="/etc/postgresql/postgresql.conf",
            read_only=True,
            uuid="u1",
            rows=[],
            carry=StorageCarry.NONE,
        )
        assert action is BindAction.SKIP

    def test_regenerated_content_needs_the_row_at_that_mount(self) -> None:
        rows = [StorageRow(MOUNT, CONF, writes_content=True), StorageRow("/data", f"{SVC}/data")]
        assert binds.regenerated_content(rows, source=CONF, destination=MOUNT)
        assert not binds.regenerated_content(rows, source=f"{SVC}/data", destination="/data")
        assert not binds.regenerated_content(rows, source=CONF, destination="/other")
        assert not binds.regenerated_content(None, source=CONF, destination=MOUNT)
        assert not binds.regenerated_content(rows, source="relative", destination=MOUNT)


class TestContentProblem:
    def _problem(self, *rows: StorageRow) -> str | None:
        return binds.content_problem(
            source=CONF, mount_path=MOUNT, target_rows=rows, source_uuid="u1", target_uuid="t1"
        )

    def test_a_target_row_with_content_passes(self) -> None:
        assert self._problem(StorageRow(MOUNT, f"{TGT}/nginx.conf", writes_content=True)) is None

    def test_a_contentless_row_or_none_is_a_problem(self) -> None:
        empty = self._problem(StorageRow(MOUNT, f"{TGT}/nginx.conf"))
        assert empty is not None and "without content" in empty
        missing = self._problem()
        assert missing is not None and "no storage row" in missing

    def test_two_parts_sharing_the_mount_are_told_apart_by_place(self) -> None:
        tgt = "/data/coolify/services/t1"
        rows = (
            StorageRow(MOUNT, f"{tgt}/nginx.conf"),  # the one in the same place: no content
            StorageRow(MOUNT, f"{tgt}/other/nginx.conf", writes_content=True),
        )
        assert self._problem(*rows) is not None


# ── engine: the read-back at the end of CREATE, and DISCOVER's record ────────


def _content_item(source: str = f"{SRC}/nginx.conf") -> VolumeItem:
    return VolumeItem(
        mount_class=MountClass.BIND,
        decision=Decision.SKIP,
        reason="content",
        source_path=source,
        mount_path=MOUNT,
        content_file=True,
    )


def _storages(*files: dict[str, Any]) -> dict[str, Any]:
    return {"persistent_storages": [], "file_storages": list(files)}


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(resources=(_service(_content_item()),)),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=FsHost(),  # type: ignore[arg-type]
        target_host=FsHost(target=SshTarget(host="10.0.0.2")),  # type: ignore[arg-type]
    )
    context.target_uuids = {"svc1": "tgt1"}
    yield context
    await api.aclose()


class TestTheReadBackAtTheEndOfCreate:
    async def test_a_target_row_holding_the_content_passes_and_touches_no_host(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1/storages").mock(
            return_value=httpx.Response(
                200,
                json=_storages(
                    {
                        "mount_path": MOUNT,
                        "fs_path": "./nginx.conf",
                        "content": "worker_processes 1;",
                    }
                ),
            )
        )
        await steps._assert_target_binds_safe(ctx)
        assert ctx.target_host.commands == [] and ctx.source_host.commands == []  # type: ignore[attr-defined]

    async def test_a_target_whose_row_lost_the_content_is_refused_before_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A template whose current catalog no longer has the file: the target's
        # re-parse of the source's compose makes it a contentless directory.
        respx_mock.get(f"{BASE}/services/tgt1/storages").mock(
            return_value=httpx.Response(
                200,
                json=_storages(
                    {"mount_path": MOUNT, "fs_path": "./nginx.conf", "is_directory": True}
                ),
            )
        )
        with pytest.raises(TransferError, match="would start without") as exc:
            await steps._assert_target_binds_safe(ctx)
        assert "store: " in str(exc.value) and MOUNT in str(exc.value)
        assert "never stopped" in (exc.value.hint or "")

    async def test_it_shares_one_read_with_the_copied_binds(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(resources=(_service(_content_item(), _bind(f"{SRC}/data")),))
        assert isinstance(ctx.source_host, FsHost)
        ctx.source_host.paths[f"{SRC}/data"] = binds.STATE_DIR
        route = respx_mock.get(f"{BASE}/services/tgt1/storages").mock(
            return_value=httpx.Response(
                200,
                json=_storages(
                    {"mount_path": MOUNT, "fs_path": "./nginx.conf", "content": "x"},
                    {"mount_path": "/data", "fs_path": "./data", "is_directory": True},
                ),
            )
        )
        await steps._assert_target_binds_safe(ctx)
        assert route.call_count == 1
