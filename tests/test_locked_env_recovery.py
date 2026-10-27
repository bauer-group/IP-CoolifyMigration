"""Locked variables, read back and carried: the IO shell and the wiring.

domain/locked_envs.py decides what a file says (test_domain_locked_envs.py).
These pin everything around it: the file is read with ``run`` and never echoed,
the plan states what it will carry and keeps the values out of plan.json,
CREATE re-reads BEFORE its first write and refuses with nothing changed, and the
bulk upsert sends the recovered value still locked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml
from structlog.testing import capture_logs

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.resources import build_env_entries, copy_envs
from bg_coolify_migrate.discovery.envfiles import read_source_config
from bg_coolify_migrate.domain import locked_envs as le
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.locked_envs import FileKind, Secrets
from bg_coolify_migrate.domain.manifest import VolumeManifest
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.engine import env_recovery, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import CoolifyApiError, EnvRecoveryError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

SENTINEL = "s3ntinel-locked-7Hq"
HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SVC_ENV = "/data/coolify/services/svc1/.env"
DB_COMPOSE = "/data/coolify/databases/db1/docker-compose.yml"


def locked(key: str, **kw: Any) -> dict[str, Any]:
    """A locked row as the API returns it: no value key at all."""
    return {
        "key": key,
        "is_shown_once": True,
        "is_preview": False,
        "is_literal": False,
        "is_multiline": False,
        "is_runtime": True,
        "is_buildtime": True,
        "is_shared": False,
        "version": "4.5.0",
        **kw,
    }


def double_read(text: str) -> str:
    """What `cat P && printf '\\000' && cat P` prints for an unchanging file."""
    return f"{text}\0{text}"


def serving(path: str, text: str) -> FakeHost:
    host = FakeHost()
    host.on(rf"^cat -- {re.escape(path)} ", stdout=double_read(text))
    return host


# ── reading the file ─────────────────────────────────────────────────────────


class TestReadSourceConfig:
    async def test_reads_with_run_twice_in_one_command(self) -> None:
        host = serving(SVC_ENV, f'API_SECRET="{SENTINEL}"')
        cfg = await read_source_config(host, "services", "svc1")  # type: ignore[arg-type]
        assert cfg.text == f'API_SECRET="{SENTINEL}"'
        assert cfg.unavailable is None and cfg.kind is FileKind.DOTENV
        assert host.commands == [f"cat -- {SVC_ENV} && printf '\\000' && cat -- {SVC_ENV}"]
        assert SENTINEL not in repr(cfg)

    async def test_a_failed_read_never_echoes_stdout_or_stderr(self) -> None:
        host = FakeHost()
        host.on(r"^cat -- ", exit_status=1, stdout=f"API_SECRET={SENTINEL}", stderr=SENTINEL)
        cfg = await read_source_config(host, "services", "svc1")  # type: ignore[arg-type]
        assert cfg.text is None
        assert (
            cfg.unavailable == f"{SVC_ENV} is missing or unreadable on the source server (exit 1)"
        )

    async def test_an_exception_becomes_a_value_free_reason(self) -> None:
        class Exploding(FakeHost):
            async def run(self, command: str, **kw: Any) -> Any:  # type: ignore[override]
                raise UnicodeDecodeError("utf-8", SENTINEL.encode(), 0, 1, SENTINEL)

        with capture_logs() as logs:
            cfg = await read_source_config(Exploding(), "services", "svc1")  # type: ignore[arg-type]
        assert cfg.unavailable == (
            f"{SVC_ENV} could not be read from the source server (UnicodeDecodeError)"
        )
        assert SENTINEL not in repr(logs)

    async def test_a_file_rewritten_while_read_is_unavailable(self) -> None:
        host = FakeHost()
        host.on(r"^cat -- ", stdout="A=1\0A=2")
        cfg = await read_source_config(host, "services", "svc1")  # type: ignore[arg-type]
        assert "changed while it was read" in (cfg.unavailable or "")

    async def test_an_unsafe_uuid_runs_nothing(self) -> None:
        host = FakeHost()
        cfg = await read_source_config(host, "services", "../../etc")  # type: ignore[arg-type]
        assert cfg.unavailable and host.commands == []


# ── the bulk upsert ──────────────────────────────────────────────────────────


class TestBuildEnvEntries:
    def test_a_recovered_value_is_sent_still_locked(self) -> None:
        entries = build_env_entries([locked("API_SECRET")], Secrets({"API_SECRET": "v"}))
        assert entries == [
            {
                "key": "API_SECRET",
                "value": "v",
                "is_shown_once": True,
                "is_preview": False,
                "is_literal": False,
                "is_multiline": False,
                "is_runtime": True,
                "is_buildtime": True,
            }
        ]

    def test_without_a_recovery_a_locked_row_is_still_dropped(self) -> None:
        """REGRESSION: an entry without a value nulls the row on applications."""
        assert build_env_entries([locked("API_SECRET")]) == []
        assert build_env_entries([locked("API_SECRET")], Secrets({"OTHER": "x"})) == []

    def test_a_locked_preview_row_is_never_filled(self) -> None:
        envs = [locked("API_SECRET", is_preview=True)]
        assert build_env_entries(envs, Secrets({"API_SECRET": "prod"})) == []

    def test_an_applications_preview_twin_is_locked_too(self) -> None:
        """The created hook copies the row into an UNLOCKED preview twin."""
        entries = build_env_entries(
            [locked("API_SECRET")], Secrets({"API_SECRET": "v"}), lock_preview_twins=True
        )
        assert [(e["key"], e["is_preview"], e["is_shown_once"], e["value"]) for e in entries] == [
            ("API_SECRET", False, True, "v"),
            ("API_SECRET", True, True, "v"),
        ]

    def test_a_readable_source_preview_row_is_left_to_overwrite_the_twin(self) -> None:
        envs = [locked("API_SECRET"), {"key": "API_SECRET", "value": "pv", "is_preview": True}]
        entries = build_env_entries(envs, Secrets({"API_SECRET": "v"}), lock_preview_twins=True)
        assert [(e["is_preview"], e["value"]) for e in entries] == [(False, "v"), (True, "pv")]

    def test_a_locked_unrecoverable_source_preview_still_gets_a_locked_twin(self) -> None:
        envs = [locked("API_SECRET"), locked("API_SECRET", is_preview=True)]
        entries = build_env_entries(envs, Secrets({"API_SECRET": "v"}), lock_preview_twins=True)
        assert [(e["is_preview"], e["is_shown_once"]) for e in entries] == [
            (False, True),
            (True, True),
        ]


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


class TestCopyEnvs:
    async def test_sends_the_recovered_entry_and_logs_no_value(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/a1/envs").mock(
            return_value=httpx.Response(200, json=[locked("API_SECRET"), locked("GONE")])
        )
        route = respx_mock.patch(f"{BASE}/applications/a2/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        with capture_logs() as logs:
            count = await copy_envs(
                api,
                collection="applications",
                source_uuid="a1",
                target_uuid="a2",
                recovered=Secrets({"API_SECRET": SENTINEL}),
            )
        body = json.loads(route.calls[0].request.read())
        assert count == 2
        assert body["data"] == [
            {
                **{
                    k: v
                    for k, v in locked("API_SECRET").items()
                    if k not in ("version", "is_shared")
                },
                "value": SENTINEL,
            },
            {
                **{
                    k: v
                    for k, v in locked("API_SECRET").items()
                    if k not in ("version", "is_shared")
                },
                "value": SENTINEL,
                "is_preview": True,
            },
        ]
        assert SENTINEL not in repr(logs)

    async def test_services_get_no_preview_entries(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """Services upsert by key alone: an is_preview entry would REPLACE the row."""
        respx_mock.get(f"{BASE}/services/s1/envs").mock(
            return_value=httpx.Response(200, json=[locked("SERVICE_PASSWORD_PG")])
        )
        route = respx_mock.patch(f"{BASE}/services/s2/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        await copy_envs(
            api,
            collection="services",
            source_uuid="s1",
            target_uuid="s2",
            recovered=Secrets({"SERVICE_PASSWORD_PG": "pw"}),
        )
        body = json.loads(route.calls[0].request.read())
        assert [e["is_preview"] for e in body["data"]] == [False]


# ── the plan ─────────────────────────────────────────────────────────────────


class _PlanApi:
    def __init__(self, envs: list[dict[str, Any]]) -> None:
        self._envs = envs

    async def get_server(self, uuid: str) -> dict[str, Any]:
        return {"uuid": uuid, "name": uuid, "ip": "10.0.0.2", "settings": {"server_role": "both"}}

    async def version(self) -> str:
        return "4.5.0"

    async def get_storages(self, collection: str, uuid: str) -> dict[str, Any]:
        return {"persistent_storages": [], "file_storages": []}

    async def get_envs(self, collection: str, uuid: str) -> list[dict[str, Any]]:
        return self._envs


_SERVICE = {
    "uuid": "svc1",
    "name": "store",
    "collection": "services",
    "kind": ResourceKind.SERVICE_COMPOSE,
}
_REDIS = {
    "uuid": "db1",
    "name": "cache",
    "collection": "databases",
    "kind": ResourceKind.DATABASE,
    "engine": DatabaseEngine.REDIS,
    "image": "redis:7.2",
}


async def _plan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    envs: list[dict[str, Any]],
    host: Any,
    shape: dict[str, Any] = _SERVICE,
    full: dict[str, Any] | None = None,
) -> MigrationPlan:
    from bg_coolify_migrate.engine import planner

    async def fake_find_project(api: Any, project: str) -> dict[str, Any]:
        return {"uuid": "p1", "name": "shop"}

    async def fake_find_server(api: Any, name: str) -> dict[str, Any]:
        return {"uuid": "s2", "name": name}

    async def fake_env_resources(api: Any, project_uuid: str, environment: str) -> Any:
        return [(shape["collection"], {"uuid": shape["uuid"], "name": shape["name"]})]

    async def fake_snapshot(api: Any, source_host: Any, **kw: Any) -> Any:
        return ResourceSnapshot(**shape), [], full or {}

    async def fake_resolve_server(api: Any, resource: Any) -> dict[str, Any]:
        return {
            "uuid": "s1",
            "name": "old",
            "ip": "10.0.0.1",
            "settings": {"wildcard_domain": "https://*.app.old.example.com"},
        }

    async def fake_manifest(source_host: Any, **kw: Any) -> VolumeManifest:
        return VolumeManifest()

    async def nothing(*args: Any, **kw: Any) -> Any:
        return None

    async def no_mounts(*args: Any, **kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(planner, "find_project", fake_find_project)
    monkeypatch.setattr(planner, "find_server", fake_find_server)
    monkeypatch.setattr(planner, "environment_resources", fake_env_resources)
    monkeypatch.setattr(planner, "snapshot_resource", fake_snapshot)
    monkeypatch.setattr(planner, "resolve_server", fake_resolve_server)
    monkeypatch.setattr(planner, "build_manifest", fake_manifest)
    monkeypatch.setattr(planner, "assess_drift", nothing)
    monkeypatch.setattr(planner, "inspect_all_mounts", no_mounts)
    return await planner.build_plan(
        _PlanApi(envs),  # type: ignore[arg-type]
        host,
        project="shop",
        environment="production",
        target_server="new",
    )


class TestPlan:
    async def test_states_what_it_will_carry_and_keeps_values_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envs = [locked("SERVICE_PASSWORD_PG"), locked("API_SECRET")]
        host = serving(
            SVC_ENV,
            f'SERVICE_PASSWORD_PG="pw{SENTINEL}"\nAPI_SECRET="https://x.app.old.example.com/{SENTINEL}"',
        )
        plan = await _plan(monkeypatch, envs=envs, host=host)
        resource = plan.resources[0]
        assert not plan.is_blocked, resource.blocking_reasons
        assert resource.recover_env_keys == ("API_SECRET", "SERVICE_PASSWORD_PG")
        text = "\n".join(resource.warnings)
        assert f"['API_SECRET', 'SERVICE_PASSWORD_PG'] will be read from {SVC_ENV}" in text
        assert "the recovered value of API_SECRET names the source server's wildcard" in text
        dumped = plan.model_dump_json()
        assert SENTINEL not in dumped
        assert "x.app.old.example.com" not in dumped

    async def test_an_unreadable_file_refuses_a_regenerated_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = FakeHost()
        host.on(r"^cat -- ", exit_status=1, stderr="Permission denied")
        plan = await _plan(monkeypatch, envs=[locked("SERVICE_PASSWORD_PG")], host=host)
        assert plan.is_blocked
        reason = plan.resources[0].refusals[0]
        assert f"{SVC_ENV} is missing or unreadable on the source server (exit 1)" in reason
        assert plan.resources[0].recover_env_keys == ()

    async def test_a_locked_service_fqdn_is_left_to_the_target_not_reported_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = serving(SVC_ENV, 'SERVICE_FQDN_APP="old.example.com"')
        plan = await _plan(monkeypatch, envs=[locked("SERVICE_FQDN_APP")], host=host)
        warnings = plan.resources[0].warnings
        assert any("['SERVICE_FQDN_APP'] are generated by the target" in w for w in warnings)
        assert not any("WITHOUT" in w for w in warnings)
        assert not plan.is_blocked

    async def test_nothing_locked_reads_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host = FakeHost()
        plan = await _plan(monkeypatch, envs=[{"key": "A", "value": "x"}], host=host)
        assert host.commands == []
        assert plan.resources[0].recover_env_keys == ()

    async def test_redis_reads_its_password_from_the_api_without_ssh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        full = {"uuid": "db1", "internal_db_url": f"redis://default:{SENTINEL}@db1:6379/0"}
        plan = await _plan(
            monkeypatch,
            envs=[locked("REDIS_PASSWORD")],
            host=object(),  # an SSH failure must not matter: the URL answers
            shape=_REDIS,
            full=full,
        )
        resource = plan.resources[0]
        assert not plan.is_blocked, resource.blocking_reasons
        assert resource.recover_env_keys == ("REDIS_PASSWORD",)
        assert any("connection URL" in w for w in resource.warnings)
        assert SENTINEL not in plan.model_dump_json()

    async def test_a_crashing_recovery_degrades_to_unrecovered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kw: Any) -> Any:
            raise KeyError(SENTINEL)

        monkeypatch.setattr(le, "recover", boom)
        host = serving(SVC_ENV, "X=1")
        with capture_logs() as logs:
            plan = await _plan(monkeypatch, envs=[locked("SERVICE_PASSWORD_PG")], host=host)
        assert plan.is_blocked
        assert "reading it back failed (KeyError)" in plan.resources[0].refusals[0]
        assert SENTINEL not in plan.model_dump_json() and SENTINEL not in repr(logs)


# ── CREATE ───────────────────────────────────────────────────────────────────


class _CreateApi:
    """The reads the recovery pre-pass makes. Anything else is not expected here."""

    def __init__(self, envs: list[dict[str, Any]], full: dict[str, Any] | None = None) -> None:
        self.envs = envs
        self.full = full or {}
        self.calls: list[str] = []

    async def get_envs(self, collection: str, uuid: str) -> list[dict[str, Any]]:
        self.calls.append(f"get_envs {uuid}")
        return self.envs

    async def get_resource(self, collection: str, uuid: str) -> dict[str, Any]:
        self.calls.append(f"get_resource {uuid}")
        return self.full

    async def version(self) -> str:
        self.calls.append("version")
        return "4.5.0"

    # CREATE also carries schedules (api/schedules.py); none here.
    async def get_scheduled_tasks(self, collection: str, uuid: str) -> list[dict[str, Any]]:
        return []

    async def get_backups(self, uuid: str) -> list[dict[str, Any]]:
        return []


def _db_plan(
    *, promised: tuple[str, ...] = (), engine: DatabaseEngine = DatabaseEngine.POSTGRESQL
) -> MigrationPlan:
    snapshot = ResourceSnapshot(
        uuid="db1",
        name="pg",
        collection="databases",
        kind=ResourceKind.DATABASE,
        engine=engine,
        image="postgres:16" if engine is DatabaseEngine.POSTGRESQL else "redis:7.2",
    )
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=(
            ResourcePlan(snapshot=snapshot, strategy=Strategy.COPY_DATA, recover_env_keys=promised),
        ),
    )


def _db_compose(env: list[str]) -> str:
    return yaml.safe_dump({"services": {"db1": {"environment": env}}})


@pytest.fixture
def order(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every create-side call, in order. None of them may precede the recovery."""
    calls: list[tuple[str, Any]] = []
    api_resources = steps.api_resources

    def recorder(name: str, result: Any = None) -> Any:
        async def fake(*args: Any, **kw: Any) -> Any:
            calls.append((name, kw.get("recovered")))
            return result

        return fake

    monkeypatch.setattr(api_resources, "ensure_project", recorder("ensure_project", "p1"))
    monkeypatch.setattr(api_resources, "ensure_environment", recorder("ensure_environment", "e1"))
    monkeypatch.setattr(api_resources, "resolve_destination", recorder("resolve_destination", "d1"))
    monkeypatch.setattr(api_resources, "park_source_domains", recorder("park", None))
    monkeypatch.setattr(api_resources, "create_resource", recorder("create", "db2"))
    monkeypatch.setattr(api_resources, "copy_envs", recorder("copy_envs", 1))
    monkeypatch.setattr(api_resources, "copy_storages", recorder("copy_storages", 0))
    monkeypatch.setattr(api_resources, "copy_tags", recorder("copy_tags", 0))
    return calls


def _ctx(tmp_path: Path, api: _CreateApi, plan: MigrationPlan, host: FakeHost) -> MigrationContext:
    return MigrationContext(
        api=api,  # type: ignore[arg-type]
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=plan,
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=host,  # type: ignore[arg-type]
        target_host=FakeHost(),  # type: ignore[arg-type]
    )


class TestCreate:
    async def test_copy_envs_receives_the_recovered_values(
        self, tmp_path: Path, order: list[tuple[str, Any]]
    ) -> None:
        api = _CreateApi([locked("POSTGRES_PASSWORD")])
        host = serving(DB_COMPOSE, _db_compose([f"POSTGRES_PASSWORD=pw{SENTINEL}"]))
        ctx = _ctx(tmp_path, api, _db_plan(promised=("POSTGRES_PASSWORD",)), host)

        result = await steps.step_create_target(ctx)

        assert result == {"target_uuids": {"db1": "db2"}}
        assert order[0][0] == "ensure_project", "recovery must not write anything itself"
        recovered = dict(order)["copy_envs"]
        assert isinstance(recovered, Secrets)
        assert recovered.get("POSTGRES_PASSWORD") == f"pw{SENTINEL}"
        assert all(SENTINEL not in json.dumps(r.detail) for r in ctx.journal.read())

    async def test_a_promised_key_now_missing_refuses_before_any_write(
        self, tmp_path: Path, order: list[tuple[str, Any]]
    ) -> None:
        api = _CreateApi([locked("POSTGRES_PASSWORD")])
        host = serving(DB_COMPOSE, _db_compose(["POSTGRES_USER=shop"]))
        ctx = _ctx(tmp_path, api, _db_plan(promised=("POSTGRES_PASSWORD",)), host)

        with pytest.raises(EnvRecoveryError) as exc_info:
            await steps.step_create_target(ctx)

        assert order == [], "nothing may be written before the recovery passes"
        message = str(exc_info.value)
        assert "['POSTGRES_PASSWORD'] can no longer be read back" in message
        assert f"POSTGRES_PASSWORD: not in {DB_COMPOSE}" in message
        assert "Nothing has been changed" in message
        assert exc_info.value.exit_code == 2

    async def test_an_unrecoverable_regenerated_secret_refuses_even_unpromised(
        self, tmp_path: Path, order: list[tuple[str, Any]]
    ) -> None:
        """A plan made before the key was re-locked never promised it."""
        api = _CreateApi([locked("REDIS_PASSWORD")], full={"internal_db_url": None})
        host = FakeHost()
        host.on(r"^cat -- ", exit_status=1)
        ctx = _ctx(tmp_path, api, _db_plan(engine=DatabaseEngine.REDIS), host)

        with pytest.raises(EnvRecoveryError, match="REDIS_PASSWORD"):
            await steps.step_create_target(ctx)
        assert order == []

    async def test_other_unrecoverable_keys_are_dropped_by_name(
        self, tmp_path: Path, order: list[tuple[str, Any]]
    ) -> None:
        api = _CreateApi([locked("FEATURE_TOKEN")])
        host = serving(DB_COMPOSE, _db_compose(["POSTGRES_USER=shop"]))
        ctx = _ctx(tmp_path, api, _db_plan(), host)

        with capture_logs() as logs:
            await steps.step_create_target(ctx)

        dropped = [e for e in logs if e["event"] == "api.envs.locked_dropped"]
        assert dropped and dropped[0]["keys"] == ["FEATURE_TOKEN"]
        assert dict(order)["copy_envs"] is None

    async def test_nothing_locked_reads_neither_files_nor_version(
        self, tmp_path: Path, order: list[tuple[str, Any]]
    ) -> None:
        api = _CreateApi([{"key": "A", "value": "x"}])
        host = FakeHost()
        ctx = _ctx(tmp_path, api, _db_plan(), host)
        await steps.step_create_target(ctx)
        assert host.commands == []
        # The pre-pass reads the env list only; get_resource is CREATE's own read.
        assert api.calls == ["get_envs db1", "get_resource db1"]

    async def test_an_unexpected_error_is_rewrapped_without_its_message(
        self, tmp_path: Path, order: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kw: Any) -> Any:
            raise KeyError(SENTINEL)

        monkeypatch.setattr(le, "recover", boom)
        api = _CreateApi([locked("POSTGRES_PASSWORD")])
        host = serving(DB_COMPOSE, _db_compose([f"POSTGRES_PASSWORD={SENTINEL}"]))
        ctx = _ctx(tmp_path, api, _db_plan(), host)

        with capture_logs() as logs, pytest.raises(EnvRecoveryError) as exc_info:
            await steps.step_create_target(ctx)

        exc = exc_info.value
        assert "failed (KeyError)" in str(exc)
        assert SENTINEL not in str(exc) and SENTINEL not in repr(exc)
        assert exc.__cause__ is None and exc.__context__ is None
        assert SENTINEL not in repr(logs)
        assert order == []

    async def test_an_api_error_passes_through_untouched(
        self, tmp_path: Path, order: list[tuple[str, Any]]
    ) -> None:
        class FailingApi(_CreateApi):
            async def get_envs(self, collection: str, uuid: str) -> list[dict[str, Any]]:
                raise CoolifyApiError("envs read failed", status_code=500)

        ctx = _ctx(tmp_path, FailingApi([]), _db_plan(), FakeHost())
        with pytest.raises(CoolifyApiError, match="envs read failed"):
            await steps.step_create_target(ctx)
        assert order == []

    async def test_an_unreadable_version_applies_every_rule(
        self, tmp_path: Path, order: list[tuple[str, Any]]
    ) -> None:
        class NoVersion(_CreateApi):
            async def version(self) -> str:
                raise CoolifyApiError("version endpoint down", status_code=503)

        api = NoVersion([locked("POSTGRES_PASSWORD")])
        host = serving(DB_COMPOSE, _db_compose(["POSTGRES_PASSWORD=pw"]))
        ctx = _ctx(tmp_path, api, _db_plan(promised=("POSTGRES_PASSWORD",)), host)
        await steps.step_create_target(ctx)
        recovered = dict(order)["copy_envs"]
        assert recovered is not None and recovered.get("POSTGRES_PASSWORD") == "pw"

    async def test_recovery_is_safe_to_rerun(
        self, tmp_path: Path, order: list[tuple[str, Any]]
    ) -> None:
        """A resumed CREATE reads the files again; no value was ever persisted."""
        api = _CreateApi([locked("POSTGRES_PASSWORD")])
        host = serving(DB_COMPOSE, _db_compose(["POSTGRES_PASSWORD=pw"]))
        plan = _db_plan(promised=("POSTGRES_PASSWORD",))
        first = await env_recovery.recover_for_create(api, host, plan.resources)  # type: ignore[arg-type]
        second = await env_recovery.recover_for_create(api, host, plan.resources)  # type: ignore[arg-type]
        assert (
            first["db1"].get("POSTGRES_PASSWORD") == second["db1"].get("POSTGRES_PASSWORD") == "pw"
        )
