"""Two server records, one Docker daemon: refused at PREFLIGHT, before anything is touched.

The plan's same-server refusal compares server uuids, and Coolify gives one SSH
endpoint one record (ServersController v4.7.2:637) — but one machine added twice,
as localhost and as its public IP, or on two SSH ports, is two records and ONE
daemon. Every source-side checkpoint then filters the source's daemon on the
plan's labels and finds the target's containers under the same project,
environment and name: a healthy target read as the source started again. So
PREFLIGHT asks both daemons for their engine ID (a UUID moby writes once into
its data root, daemon/id.go v27.5.1:12-35) and refuses when both answer the same.
An unanswered probe refuses nothing.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import containment as c
from bg_coolify_migrate.engine import containment, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import PreflightError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost
from tests.test_engine_steps import BASE, HOST, _plan, _source_host, _target_host

ENGINE = "0b4d6c1e-8e2a-4f0e-9a51-3c2d7e1f6a90"
#: What a daemon before moby 23 reported: an ID derived from its key — an ID all the same.
KEY_ID = "ABCD:EFGH:IJKL:MNOP:QRST:UVWX:YZ12:3456:7890:ABCD:EFGH:IJKL"


class TestParsing:
    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            (f"{ENGINE}\n", ENGINE),
            (f"\n  {ENGINE}  \n", ENGINE),
            (f"{KEY_ID}\n", KEY_ID),
            ("", None),
            ("<no value>\n", None),
        ],
    )
    def test_the_engine_id(self, stdout: str, expected: str | None) -> None:
        assert c.parse_daemon_id(stdout) == expected


class TestTheRefusal:
    def test_one_daemon_is_refused_naming_both_servers(self) -> None:
        refusal = c.shared_daemon_refusal(ENGINE, ENGINE, source="old", target="new")
        assert refusal is not None
        assert "'old'" in refusal and "'new'" in refusal and ENGINE in refusal
        assert "Pick a target server on another machine" in refusal

    @pytest.mark.parametrize(
        ("source", "target"), [(ENGINE, "other"), (None, ENGINE), (ENGINE, None), (None, None)]
    )
    def test_two_daemons_or_no_answer_refuse_nothing(
        self, source: str | None, target: str | None
    ) -> None:
        assert c.shared_daemon_refusal(source, target, source="old", target="new") is None

    def test_the_hint_covers_a_cloned_machine(self) -> None:
        assert "engine-id" in c.SHARED_DAEMON_HINT
        assert "Nothing has been changed" in c.SHARED_DAEMON_HINT


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_source_host(),  # type: ignore[arg-type]
        target_host=_target_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


def _host(ctx: MigrationContext, side: str) -> FakeHost:
    host = ctx.source_host if side == "source" else ctx.target_host
    assert isinstance(host, FakeHost)
    return host


class TestTheCheck:
    async def test_two_daemons_pass(self, ctx: MigrationContext) -> None:
        await containment.assert_separate_daemons(ctx)
        for side in ("source", "target"):
            assert containment.DAEMON_ID_COMMAND in _host(ctx, side).commands

    async def test_one_daemon_is_refused(self, ctx: MigrationContext) -> None:
        for side in ("source", "target"):
            _host(ctx, side).on(r"^docker info", stdout=f"{ENGINE}\n")
        with pytest.raises(PreflightError, match="are one Docker daemon") as excinfo:
            await containment.assert_separate_daemons(ctx)
        assert excinfo.value.exit_code == 2, "nothing touched, nothing to roll back"
        assert "engine-id" in (excinfo.value.hint or "")

    @pytest.mark.parametrize("side", ["source", "target"])
    async def test_a_daemon_that_does_not_answer_refuses_nothing(
        self, ctx: MigrationContext, side: str
    ) -> None:
        other = "target" if side == "source" else "source"
        _host(ctx, side).on(r"^docker info", stderr="permission denied", exit_status=1)
        _host(ctx, other).on(r"^docker info", stdout=f"{ENGINE}\n")
        await containment.assert_separate_daemons(ctx)


class TestPreflight:
    @pytest.fixture(autouse=True)
    def _instance(self, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/version").mock(return_value=httpx.Response(200, text="4.1.2"))
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )

    async def test_refuses_before_it_looks_at_the_stack(self, ctx: MigrationContext) -> None:
        for side in ("source", "target"):
            _host(ctx, side).on(r"^docker info", stdout=f"{ENGINE}\n")
        with pytest.raises(PreflightError, match="are one Docker daemon"):
            await steps.step_preflight(ctx)
        # The first container query is the discoverability check, after this one.
        assert not any(cmd.startswith("docker ps") for cmd in _host(ctx, "source").commands)

    async def test_passes_two_daemons(self, ctx: MigrationContext) -> None:
        await steps.step_preflight(ctx)
        assert containment.DAEMON_ID_COMMAND in _host(ctx, "target").commands
