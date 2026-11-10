"""A container that mounts the whole host writable is an agent: warned about, not a sharer.

``shared_with`` counted a writable bind of any parent, so Portainer's agent
(``-v /:/host``, writable by default) or a host backup (``/:/source``) made
every bind migration on its server refuse, at PREFLIGHT on the source and at
the end of CREATE on the target, with a remedy ("migrate the resources that
share it together") that does not fit. Every other parent, the path itself
and a path inside it still refuse.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import HostBind
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import PreflightError
from tests.test_binds_engine import (
    SRC,
    TARGET_ROWS,
    TGT,
    _mock_target_storages,
    _src,
    _tgt,
    bind_context,
)

AGENT = {
    "name": "/portainer_agent",
    "labels": {},
    "mounts": [
        {"Type": "bind", "Source": "/", "RW": True},
        {"Type": "bind", "Source": "/var/run/docker.sock", "RW": True},
    ],
}


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    context = bind_context(tmp_path)
    yield context
    await context.api.aclose()


class TestDomain:
    @pytest.mark.parametrize(
        "path", ["/srv/app", "/data/coolify/services/u1/data", "/home/alice/app"]
    )
    def test_a_writable_root_bind_shares_no_path(self, path: str) -> None:
        assert binds.shared_with(path, [HostBind("portainer_agent", "/")]) == []

    @pytest.mark.parametrize(
        ("other", "path"),
        [
            ("/srv", "/srv/app"),
            ("/data", "/data/coolify/services/u1/data"),
            ("/home", "/home/alice/app"),
            ("/var", "/var/www/app"),
            ("/srv/app", "/srv/app"),
            ("/srv/app/uploads", "/srv/app"),
        ],
    )
    def test_every_other_writer_still_shares(self, other: str, path: str) -> None:
        assert binds.shared_with(path, [HostBind("x", other)]) == [HostBind("x", other)]

    def test_writers_are_writable_root_binds_once_each(self) -> None:
        found = binds.host_wide_writers(
            [
                HostBind("agent", "/"),
                HostBind("agent", "//"),
                HostBind("cadvisor", "/", rw=False),
                HostBind("backup", "/srv"),
            ]
        )
        assert [b.container for b in found] == ["agent"]

    def test_the_notice_names_the_container_the_server_and_the_remedy(self) -> None:
        text = binds.host_wide_notice(HostBind("portainer_agent", "/"), paths=["/srv/a"], server="old")
        assert "portainer_agent on old" in text
        assert "/srv/a" in text and "read-only" in text and "stop the container" in text

    def test_a_long_path_list_is_shortened(self) -> None:
        paths = [f"/srv/{i}" for i in range(8)]
        text = binds.host_wide_notice(HostBind("a", "/"), paths=paths, server="s")
        assert "/srv/4 and 3 more" in text and "/srv/5" not in text


class TestEngine:
    async def test_on_the_source_it_is_a_notice_not_a_refusal(self, ctx: MigrationContext) -> None:
        _src(ctx).containers = [AGENT]
        await steps._assert_source_binds_safe(ctx)
        assert len(ctx.notices) == 1
        assert "portainer_agent on old" in ctx.notices[0]
        assert f"{SRC}/data" in ctx.notices[0] and "/srv/files" in ctx.notices[0]

    async def test_a_real_sharer_beside_it_still_refuses(self, ctx: MigrationContext) -> None:
        _src(ctx).containers = [
            AGENT,
            {"name": "/backup", "labels": {}, "mounts": [{"Type": "bind", "Source": "/srv"}]},
        ]
        with pytest.raises(PreflightError, match="backup") as exc:
            await steps._assert_source_binds_safe(ctx)
        assert "portainer_agent" not in str(exc.value)

    async def test_on_the_target_it_is_a_notice_not_a_refusal(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_target_storages(respx_mock, *TARGET_ROWS)
        _tgt(ctx).containers = [AGENT]
        await steps._assert_target_binds_safe(ctx)
        assert len(ctx.notices) == 1
        assert "portainer_agent on new" in ctx.notices[0] and f"{TGT}/data" in ctx.notices[0]
