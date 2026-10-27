"""Database backup schedules: carried to the target at CREATE, reported in the plan.

Until this existed, a migrated database stopped being backed up: its schedules
are part of no database GET and no create body, and the target came up with
none. The tests pin what makes the copy faithful (the whitelist, `enabled` as
it was, the S3 destinations a 4.5 row discloses), what keeps it honest (the
GET's foreign rows dropped, a pre-4.5 S3 schedule created local-only and SAID
so), and what keeps it safe: fatal inside CREATE, no re-POST on an ambiguous
status, a doubled schedule caught — and a rollback that deletes the rows but
never the dump files, which after a SUCCEEDED run are the last copy of the
post-cutover data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import tests.test_engine_steps as base_steps
from bg_coolify_migrate.api import schedules as api_schedules
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.fields import (
    BACKUP_CREATE,
    BACKUP_S3_FIELDS,
    BACKUP_UNSETTABLE,
    rejected_keys,
)
from bg_coolify_migrate.api.schedules import build_backup_body, copy_backup_schedules
from bg_coolify_migrate.domain import schedules
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import CoolifyApiError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.test_scheduled_tasks import _plan, _Recorder, _TaskPlanApi

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"

PG = "App\\Models\\StandalonePostgresql"
#: An execution message as Coolify < 4.5 returns it: raw dump output. Searched
#: for everywhere the tests can see.
LEAKY_MESSAGE = "pg_dump: connection to postgres://shop:hunter2-pw@db failed"


def _row(**kw: Any) -> dict[str, Any]:
    """A backup schedule exactly as GET /databases/{uuid}/backups returns it on 4.5."""
    base: dict[str, Any] = {
        "id": 7,
        "uuid": "b-src-1",
        "team_id": 0,
        "description": None,
        "enabled": True,
        "save_s3": False,
        "frequency": "0 2 * * *",
        "database_backup_retention_amount_locally": 7,
        "database_backup_retention_days_locally": 0,
        "database_backup_retention_max_storage_locally": 0.0,
        "database_backup_retention_amount_s3": 0,
        "database_backup_retention_days_s3": 0,
        "database_backup_retention_max_storage_s3": 0.0,
        "database_type": PG,
        "database_id": 3,
        "s3_storage_id": None,
        "databases_to_backup": "shopdb",
        "dump_all": False,
        "timeout": 3600,
        "disable_local_backup": False,
        "missing_backup_notification_days": 0,
        "s3_storage_uuid": None,
        "s3_storage_uuids": [],
        "last_execution_at": None,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "executions": [],
    }
    return {**base, **kw}


def _pre_4_5(row: dict[str, Any]) -> dict[str, Any]:
    """The same row as Coolify < 4.5 returns it: no S3 uuids, ever."""
    return {k: v for k, v in row.items() if k not in ("s3_storage_uuid", "s3_storage_uuids")}


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(api_schedules, "log", recorder)
    return recorder


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("bg_coolify_migrate.api.client.asyncio.sleep", instant)


# ── whitelist ────────────────────────────────────────────────────────────────


class TestFields:
    def test_values_only(self) -> None:
        assert not BACKUP_CREATE & BACKUP_S3_FIELDS, "S3 uuids are set, never copied off a GET"
        assert not BACKUP_CREATE & BACKUP_UNSETTABLE
        assert "backup_now" not in BACKUP_CREATE | BACKUP_S3_FIELDS

    def test_the_whole_create_array(self) -> None:
        # $backupConfigFields v4.5.0:1057, minus the S3 uuids.
        upstream = {
            "save_s3",
            "enabled",
            "dump_all",
            "frequency",
            "databases_to_backup",
            "database_backup_retention_amount_locally",
            "database_backup_retention_days_locally",
            "database_backup_retention_max_storage_locally",
            "database_backup_retention_amount_s3",
            "database_backup_retention_days_s3",
            "database_backup_retention_max_storage_s3",
            "timeout",
            "missing_backup_notification_days",
        }
        assert upstream == BACKUP_CREATE


# ── client ───────────────────────────────────────────────────────────────────


class TestClient:
    async def test_execution_messages_never_leave_the_client(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        execution = {
            "uuid": "e1",
            "status": "failed",
            "message": LEAKY_MESSAGE,
            "filename": "/data/coolify/backups/databases/t-0/pg-db1/pg-dump-1.dmp",
            "created_at": "2026-10-02T02:00:00Z",
        }
        respx_mock.get(f"{BASE}/databases/db1/backups").mock(
            return_value=httpx.Response(200, json=[_row(executions=[execution])])
        )
        [row] = await api.get_backups("db1")
        assert row["executions"] == [
            {
                "status": "failed",
                "filename": execution["filename"],
                "created_at": execution["created_at"],
            }
        ]
        assert LEAKY_MESSAGE not in json.dumps(row)

    async def test_create_returns_the_new_uuid(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(201, json={"uuid": "b2", "message": "created"})
        )
        assert await api.create_backup("db2", {"frequency": "daily"}) == "b2"
        assert json.loads(route.calls[0].request.read()) == {"frequency": "daily"}

    async def test_create_without_a_uuid_is_an_error(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(201, json={"message": "created"})
        )
        with pytest.raises(CoolifyApiError, match="returned no uuid"):
            await api.create_backup("db2", {"frequency": "daily"})

    @pytest.mark.parametrize("status", [502, 503, 504])
    async def test_an_ambiguous_status_is_never_re_posted(
        self, respx_mock: respx.Router, no_sleep: None, status: int
    ) -> None:
        """The proxy may answer after Coolify saved the row: a retry would double it."""
        route = respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            side_effect=[httpx.Response(status), httpx.Response(201, json={"uuid": "b2"})]
        )
        async with CoolifyClient(HOST, "tok", max_retries=3) as retrying:
            with pytest.raises(CoolifyApiError):
                await retrying.create_backup("db2", {"frequency": "daily"})
        assert route.call_count == 1

    async def test_the_throttle_is_retried(self, respx_mock: respx.Router, no_sleep: None) -> None:
        # 429 is refused BEFORE the controller runs, so nothing was saved.
        route = respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(201, json={"uuid": "b2"}),
            ]
        )
        async with CoolifyClient(HOST, "tok", max_retries=3) as retrying:
            assert await retrying.create_backup("db2", {"frequency": "daily"}) == "b2"
        assert route.call_count == 2

    async def test_other_posts_keep_their_retries(
        self, respx_mock: respx.Router, no_sleep: None
    ) -> None:
        """The narrowing is per call, not a change to the client's default."""
        route = respx_mock.post(f"{BASE}/projects").mock(
            side_effect=[httpx.Response(502), httpx.Response(201, json={"uuid": "p1"})]
        )
        async with CoolifyClient(HOST, "tok", max_retries=3) as retrying:
            await retrying.post("/projects", {"name": "x"})
        assert route.call_count == 2


# ── domain ───────────────────────────────────────────────────────────────────


class TestOwnSchedules:
    def test_another_engines_rows_with_the_same_numeric_id_are_dropped(self) -> None:
        rows = [
            _row(uuid="mine"),
            _row(uuid="mysql", database_type="App\\Models\\StandaloneMysql"),
            _row(uuid="svc", database_type="App\\Models\\ServiceDatabase"),
        ]
        own = schedules.own_backup_schedules(rows, DatabaseEngine.POSTGRESQL)
        assert [r["uuid"] for r in own] == ["mine"]

    def test_an_unknown_engine_owns_nothing(self) -> None:
        assert schedules.own_backup_schedules([_row()], None) == []

    def test_every_engine_has_an_owner_class(self) -> None:
        """A new engine must fail here, not KeyError at CREATE or lose schedules."""
        assert set(schedules._BACKUP_OWNER) == set(DatabaseEngine)


class TestS3Destinations:
    def test_undisclosed_before_4_5(self) -> None:
        assert schedules.backup_s3_destinations(_pre_4_5(_row(s3_storage_id=4))) is None

    def test_primary_first_and_deduplicated(self) -> None:
        row = _row(s3_storage_uuids=["a", "b", "b", ""], s3_storage_uuid="b")
        assert schedules.backup_s3_destinations(row) == ("b", "a")

    def test_a_primary_outside_the_list_is_ignored(self) -> None:
        row = _row(s3_storage_uuids=["a"], s3_storage_uuid="zzz")
        assert schedules.backup_s3_destinations(row) == ("a",)

    def test_none_disclosed_is_empty_not_unknown(self) -> None:
        assert schedules.backup_s3_destinations(_row(s3_storage_uuids=[])) == ()
        assert schedules.backup_s3_destinations(_row(s3_storage_uuids=None)) == ()


class TestBackupWarnings:
    def _warn(self, rows: list[dict[str, Any]], **kw: Any) -> list[str]:
        args: dict[str, Any] = {
            "engine": DatabaseEngine.POSTGRESQL,
            "source_timezone": "UTC",
            "target_timezone": "UTC",
        }
        return schedules.backup_warnings(rows, **{**args, **kw})

    def test_a_stock_schedule_is_silent(self) -> None:
        assert self._warn([_row()]) == []

    def test_pre_4_5_s3_is_created_local_only_and_says_so(self) -> None:
        [line] = self._warn([_pre_4_5(_row(save_s3=True, s3_storage_id=4))])
        assert "LOCAL-ONLY" in line
        assert "'0 2 * * *'" in line

    def test_a_dangling_4_5_storage_warns(self) -> None:
        [line] = self._warn([_row(save_s3=True, s3_storage_uuids=[])])
        assert "no longer exists" in line

    def test_disclosed_s3_carries_without_a_word(self) -> None:
        row = _row(save_s3=True, s3_storage_uuids=["s3a"], s3_storage_uuid="s3a")
        assert self._warn([row]) == []

    def test_disable_local_backup_warns(self) -> None:
        [line] = self._warn([_row(disable_local_backup=True)])
        assert "disable_local_backup" in line

    def test_a_different_clock_warns_only_for_an_armed_schedule(self) -> None:
        [line] = self._warn([_row()], source_timezone="Europe/Berlin")
        assert "Europe/Berlin" in line and "UTC" in line
        assert self._warn([_row(enabled=False)], source_timezone="Europe/Berlin") == []
        assert self._warn([], source_timezone="Europe/Berlin") == []

    def test_history_names_the_dump_directory_and_only_when_runs_exist(self) -> None:
        runs = [
            {"status": "success", "filename": "/data/x/old/a.dmp", "created_at": "2026-10-01"},
            {"status": "success", "filename": "/data/x/new/b.dmp", "created_at": "2026-10-02"},
        ]
        [line] = self._warn([_row(executions=runs)])
        assert "2 backup run(s)" in line
        assert "/data/x/new/" in line
        assert self._warn([_row(executions=[])]) == []

    def test_history_without_filenames_names_the_root(self) -> None:
        [line] = self._warn([_row(executions=[{"status": "failed", "filename": None}])])
        assert "/data/coolify/backups/databases/" in line

    @pytest.mark.parametrize(
        "engine", [DatabaseEngine.REDIS, DatabaseEngine.KEYDB, DatabaseEngine.DRAGONFLY]
    )
    def test_engines_without_backups_are_reported_not_carried(self, engine: DatabaseEngine) -> None:
        owner = schedules._BACKUP_OWNER[engine]
        [line] = self._warn([_row(database_type=owner)], engine=engine)
        assert "not carried" in line
        assert self._warn([], engine=engine) == []

    def test_an_unrecognised_owner_is_reported(self) -> None:
        [line] = self._warn([_row(database_type="App\\Models\\Something")])
        assert "unrecognised" in line

    def test_known_foreign_owners_are_dropped_silently(self) -> None:
        rows = [
            _row(database_type="App\\Models\\ServiceDatabase", save_s3=True),
            _row(database_type="App\\Models\\StandaloneSqlite", disable_local_backup=True),
        ]
        assert self._warn(rows) == []

    def test_no_line_ever_carries_an_execution_message(self) -> None:
        rows = [
            _pre_4_5(_row(save_s3=True, disable_local_backup=True)),
            _row(executions=[{"status": "failed", "message": LEAKY_MESSAGE}]),
        ]
        lines = self._warn(rows, source_timezone="Europe/Berlin")
        assert len(lines) == 4
        assert all(LEAKY_MESSAGE not in line for line in lines)


class TestServiceBackupWarnings:
    @pytest.mark.parametrize(
        "db",
        [
            {"name": "db", "image": "postgres:16"},
            {"name": "db", "image": "supabase/postgres:15.1"},
            {"name": "db", "image": "timescale/timescaledb:latest-pg16"},
            {"name": "db", "image": "pgvector/pgvector:pg16"},
            {"name": "db", "image": "mariadb:11"},
            {"name": "db", "image": "mongo:7"},
            {"name": "db", "image": "something:1", "custom_type": "mysql"},
        ],
    )
    def test_backup_capable_databases_warn(self, db: dict[str, Any]) -> None:
        [line] = schedules.service_backup_warnings({"databases": [db]})
        assert "['db']" in line

    @pytest.mark.parametrize("image", ["redis:7", "minio/minio:latest", ""])
    def test_others_are_silent(self, image: str) -> None:
        assert (
            schedules.service_backup_warnings({"databases": [{"name": "x", "image": image}]}) == []
        )

    def test_no_databases_is_silent(self) -> None:
        assert schedules.service_backup_warnings({}) == []
        assert schedules.service_backup_warnings({"databases": None}) == []


# ── body ─────────────────────────────────────────────────────────────────────


class TestBuildBackupBody:
    def test_a_get_row_never_leaks_a_rejected_key(self) -> None:
        body = build_backup_body(_row(s3_storage_uuids=["s3a"], s3_storage_uuid="s3a"))
        assert rejected_keys(body, BACKUP_CREATE | BACKUP_S3_FIELDS) == frozenset()
        assert "backup_now" not in body
        assert not set(body) & BACKUP_UNSETTABLE

    def test_values_carry_as_they_are(self) -> None:
        body = build_backup_body(_row(enabled=False, dump_all=True, timeout=1200))
        assert body["enabled"] is False
        assert body["dump_all"] is True
        assert body["timeout"] == 1200
        assert body["frequency"] == "0 2 * * *"

    def test_a_4_5_row_carries_every_destination_primary_first(self) -> None:
        body = build_backup_body(
            _row(save_s3=True, s3_storage_uuids=["s3a", "s3b"], s3_storage_uuid="s3b")
        )
        assert body["save_s3"] is True
        assert body["s3_storage_uuids"] == ["s3b", "s3a"]
        assert body["s3_storage_uuid"] == "s3b"

    def test_a_preselected_storage_is_kept_with_s3_off(self) -> None:
        body = build_backup_body(_row(save_s3=False, s3_storage_uuids=["s3a"]))
        assert body["save_s3"] is False
        assert body["s3_storage_uuids"] == ["s3a"]

    def test_a_pre_4_5_s3_row_is_created_local_only(self) -> None:
        body = build_backup_body(_pre_4_5(_row(save_s3=True, s3_storage_id=4)))
        assert body["save_s3"] is False
        assert not set(body) & BACKUP_S3_FIELDS

    def test_notification_days_only_when_the_row_has_them(self) -> None:
        # The column and the create field ship in one release (4.3.18 / 4.4.0).
        assert (
            build_backup_body(_row(missing_backup_notification_days=3))[
                "missing_backup_notification_days"
            ]
            == 3
        )
        older = {k: v for k, v in _row().items() if k != "missing_backup_notification_days"}
        assert "missing_backup_notification_days" not in build_backup_body(older)

    def test_negative_retention_is_sent_as_zero(self) -> None:
        """The UI stores -1; the API refuses it; retention treats both as 'keep all'."""
        body = build_backup_body(
            _row(
                database_backup_retention_amount_locally=-1,
                database_backup_retention_days_s3=-5,
                database_backup_retention_max_storage_s3=-0.5,
            )
        )
        assert body["database_backup_retention_amount_locally"] == 0
        assert body["database_backup_retention_days_s3"] == 0
        assert body["database_backup_retention_max_storage_s3"] == 0
        assert build_backup_body(_row())["database_backup_retention_amount_locally"] == 7


# ── copy ─────────────────────────────────────────────────────────────────────


async def _copy(
    api: CoolifyClient, engine: DatabaseEngine | None = DatabaseEngine.POSTGRESQL
) -> int:
    return await copy_backup_schedules(
        api, collection="databases", source_uuid="db1", target_uuid="db2", engine=engine
    )


class TestCopyBackupSchedules:
    async def test_one_body_per_own_schedule(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        source = [
            _row(uuid="b1"),
            _row(uuid="b2", frequency="@weekly", enabled=False),
            _row(uuid="foreign", database_type="App\\Models\\StandaloneMysql"),
        ]
        respx_mock.get(f"{BASE}/databases/db1/backups").mock(
            return_value=httpx.Response(200, json=source)
        )
        post = respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            side_effect=[
                httpx.Response(201, json={"uuid": "n1"}),
                httpx.Response(201, json={"uuid": "n2"}),
            ]
        )
        respx_mock.get(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _row(uuid="n1"),
                    _row(uuid="n2"),
                    # Another engine's row sharing the target's numeric id: not ours.
                    _row(uuid="x", database_type="App\\Models\\StandaloneRedis"),
                ],
            )
        )

        assert await _copy(api) == 2
        bodies = [json.loads(c.request.read()) for c in post.calls]
        assert bodies == [build_backup_body(source[0]), build_backup_body(source[1])]
        assert bodies[1]["enabled"] is False
        assert logs.named("api.backups.copied") == [{"count": 2, "target": "db2"}]

    async def test_engines_without_backups_make_no_request(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        assert await _copy(api, DatabaseEngine.REDIS) == 0

    async def test_other_collections_make_no_request(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        count = await copy_backup_schedules(
            api, collection="services", source_uuid="s1", target_uuid="s2", engine=None
        )
        assert count == 0

    async def test_no_schedules_means_no_writes(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/databases/db1/backups").mock(
            return_value=httpx.Response(200, json=[])
        )
        assert await _copy(api) == 0

    async def test_a_rejected_create_is_not_swallowed(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/databases/db1/backups").mock(
            return_value=httpx.Response(200, json=[_row()])
        )
        respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(422, json={"message": "Validation failed."})
        )
        with pytest.raises(CoolifyApiError) as exc_info:
            await _copy(api)
        assert exc_info.value.status_code == 422

    async def test_a_doubled_schedule_fails_the_copy(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/databases/db1/backups").mock(
            return_value=httpx.Response(200, json=[_row()])
        )
        respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(201, json={"uuid": "n1"})
        )
        respx_mock.get(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(200, json=[_row(uuid="n1"), _row(uuid="ghost")])
        )
        with pytest.raises(CoolifyApiError, match="holds 2 backup schedule\\(s\\) where 1"):
            await _copy(api)

    async def test_a_dropped_s3_destination_is_logged(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        """A schedule added after the plan was built was never announced."""
        respx_mock.get(f"{BASE}/databases/db1/backups").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _pre_4_5(
                        _row(
                            save_s3=True,
                            s3_storage_id=4,
                            executions=[{"status": "failed", "message": LEAKY_MESSAGE}],
                        )
                    )
                ],
            )
        )
        respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(201, json={"uuid": "n1"})
        )
        respx_mock.get(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(200, json=[_row(uuid="n1")])
        )
        await _copy(api)
        assert logs.named("api.backups.s3_dropped") == [
            {
                "frequency": "0 2 * * *",
                "target": "db2",
                "hint": "its S3 storage is not disclosed by this Coolify; created local-only",
            }
        ]
        assert LEAKY_MESSAGE not in logs.text()


# ── CREATE_TARGET and its undo ───────────────────────────────────────────────


@pytest.fixture
async def ctx(tmp_path: Path, no_sleep: None):  # type: ignore[no-untyped-def]
    # Retries ON, so a test can prove the backup create does not use them.
    client = CoolifyClient(HOST, "tok", max_retries=3)
    context = MigrationContext(
        api=client,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=base_steps._plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=base_steps._source_host(),  # type: ignore[arg-type]
        target_host=base_steps._target_host(),  # type: ignore[arg-type]
    )
    yield context
    await client.aclose()


def _database_create(respx_mock: respx.Router, rows: list[dict[str, Any]]) -> respx.Route:
    """CREATE for the plain postgres plan (db1 -> db2). Returns the target's DELETE."""
    respx_mock.get(f"{BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
    )
    respx_mock.get(f"{BASE}/projects/p1").mock(
        return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
    )
    respx_mock.get(f"{BASE}/servers/s2/destinations").mock(
        return_value=httpx.Response(200, json=[{"uuid": "d1", "network": "coolify"}])
    )
    respx_mock.get(f"{BASE}/databases/db1").mock(
        return_value=httpx.Response(200, json={"uuid": "db1"})
    )
    respx_mock.post(f"{BASE}/databases/postgresql").mock(
        return_value=httpx.Response(201, json={"uuid": "db2"})
    )
    respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(f"{BASE}/databases/db1/tags").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(f"{BASE}/databases/db1/backups").mock(
        return_value=httpx.Response(200, json=rows)
    )
    return respx_mock.delete(url__startswith=f"{BASE}/databases/db2").mock(
        return_value=httpx.Response(200, json={"message": "queued"})
    )


class TestCreateTarget:
    async def test_schedules_are_created_on_the_new_database(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        delete = _database_create(respx_mock, [_row()])
        post = respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(201, json={"uuid": "n1"})
        )
        respx_mock.get(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(200, json=[_row(uuid="n1")])
        )

        result = await steps.step_create_target(ctx)

        assert result == {"target_uuids": {"db1": "db2"}}, "nothing new to undo, so nothing new"
        assert json.loads(post.calls[0].request.read()) == build_backup_body(_row())
        assert not delete.called
        journal = json.dumps([r.detail for r in ctx.journal.read()])
        assert "n1" not in journal, "backup uuids are not journalled: no compensation needs them"

    async def test_a_refused_schedule_deletes_the_target_with_zero_downtime(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        delete = _database_create(respx_mock, [_row()])
        respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(422, json={"message": "Validation failed."})
        )
        stop = respx_mock.post(url__regex=r".*/stop$").mock(return_value=httpx.Response(200))

        with pytest.raises(CoolifyApiError):
            await steps.step_create_target(ctx)

        assert delete.called
        assert not stop.called

    async def test_a_proxy_error_is_not_re_posted_and_costs_only_the_target(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        delete = _database_create(respx_mock, [_row()])
        post = respx_mock.post(f"{BASE}/databases/db2/backups").mock(
            return_value=httpx.Response(502)
        )
        with pytest.raises(CoolifyApiError):
            await steps.step_create_target(ctx)
        assert post.call_count == 1
        assert delete.called


class TestUndoCreateTarget:
    async def test_deletes_the_database_but_never_its_dump_files(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No backup request is ever sent: delete_s3 would destroy post-cutover dumps.

        The backup routes are mocked to ANSWER and asserted never called. Leaving
        them unmocked proves nothing: undo_create_target is fail-soft, so respx's
        AllMockedAssertionError for a wrapped request is logged away and the test
        stays green while every dump is deleted against a real Coolify.
        """
        recorder = _Recorder()
        monkeypatch.setattr(compensations, "log", recorder)

        def answer(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[_row(uuid="n1")])
            return httpx.Response(200, json={"message": "Backup configuration deleted."})

        backups = respx_mock.route(url__regex=r".*/databases/db2/backups.*").mock(
            side_effect=answer
        )
        delete = respx_mock.delete(url__regex=r".*/databases/db2(\?.*)?$").mock(
            return_value=httpx.Response(200, json={"message": "queued"})
        )

        await compensations.undo_create_target(ctx, {"target_uuids": {"db1": "db2"}})

        assert not backups.called, "no backup schedule or dump may be read or deleted"
        assert [(c.request.method, c.request.url.path) for c in respx_mock.calls] == [
            ("DELETE", "/api/v1/databases/db2")
        ]
        assert delete.call_count == 1
        [kept] = recorder.named("compensate.target_backup_files_kept")
        assert kept["uuid"] == "db2"
        assert "/data/coolify/backups/databases/" in kept["hint"]

    async def test_says_nothing_about_backups_for_an_application(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Recorder()
        monkeypatch.setattr(compensations, "log", recorder)
        ctx.plan = base_steps._compose_plan()
        respx_mock.delete(url__regex=r".*/applications/tgt1(\?.*)?$").mock(
            return_value=httpx.Response(200, json={"message": "queued"})
        )
        await compensations.undo_create_target(ctx, {"target_uuids": {"app1": "tgt1"}})
        assert recorder.named("compensate.target_backup_files_kept") == []


# ── the plan ─────────────────────────────────────────────────────────────────


class _BackupPlanApi(_TaskPlanApi):
    def __init__(self, *, backups: list[dict[str, Any]] | Exception, **kw: Any) -> None:
        super().__init__(tasks=[], **kw)
        self._backups = backups
        self.backup_reads: list[str] = []

    async def get_backups(self, uuid: str) -> list[dict[str, Any]]:
        self.backup_reads.append(uuid)
        if isinstance(self._backups, Exception):
            raise self._backups
        return self._backups


_DATABASE = {
    "collection": "databases",
    "kind": ResourceKind.DATABASE,
    "engine": DatabaseEngine.POSTGRESQL,
}


class TestPlan:
    async def test_a_pre_4_5_s3_schedule_warns_and_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _BackupPlanApi(backups=[_pre_4_5(_row(save_s3=True, s3_storage_id=4))])
        plan = await _plan(monkeypatch, api, snapshot=_DATABASE)
        assert any("LOCAL-ONLY" in w for w in plan.resources[0].warnings)
        assert not plan.is_blocked

    async def test_negative_retention_never_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = _BackupPlanApi(backups=[_row(database_backup_retention_amount_locally=-1)])
        plan = await _plan(monkeypatch, api, snapshot=_DATABASE)
        assert not plan.is_blocked

    async def test_an_unreadable_schedule_list_is_fail_soft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _BackupPlanApi(backups=CoolifyApiError("timed out", status_code=504))
        plan = await _plan(monkeypatch, api, snapshot=_DATABASE)
        assert not plan.is_blocked

    async def test_a_different_target_clock_reaches_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _BackupPlanApi(backups=[_row()], target_timezone="UTC")
        plan = await _plan(monkeypatch, api, snapshot=_DATABASE, source_timezone="Asia/Tokyo")
        assert any("Asia/Tokyo" in w and "dumps" in w for w in plan.resources[0].warnings)

    async def test_a_service_with_a_postgres_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = _BackupPlanApi(backups=[])
        plan = await _plan(
            monkeypatch,
            api,
            snapshot={"collection": "services", "kind": ResourceKind.SERVICE_TEMPLATE},
            full={"databases": [{"name": "postgresql", "image": "postgres:16-alpine"}]},
        )
        assert any("service database(s) ['postgresql']" in w for w in plan.resources[0].warnings)
        assert api.backup_reads == [], "the backup route resolves standalone databases only"


# ── volume backups: unreadable, and destroyed by a DELETE finalize ──────────


def _volume_plan(
    *, possible: bool = True, policy: FinalizePolicy = FinalizePolicy.RENAME
) -> MigrationPlan:
    """A postgres with a named volume and an api without one."""
    volume = VolumeItem(
        mount_class=MountClass.NAMED,
        decision=Decision.MIGRATE,
        reason="named volume",
        source_name="db1-data",
        source_path="/var/lib/docker/volumes/db1-data/_data",
        mount_path="/var/lib/postgresql/data",
    )

    def resource(name: str, *items: VolumeItem) -> ResourcePlan:
        return ResourcePlan(
            snapshot=ResourceSnapshot(
                uuid=name, name=name, collection="databases", kind=ResourceKind.DATABASE
            ),
            strategy=Strategy.COPY_DATA,
            manifest=VolumeManifest(items=items),
        )

    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=(resource("postgres", volume), resource("api")),
        finalize_policy=policy,
        volume_backups_possible=possible,
    )


class TestVolumeBackupNotes:
    """No API reads a volume-backup schedule (4.3.0+), and a resource delete
    destroys its archives, local and S3. Before this the plan said nothing."""

    def test_names_every_resource_with_a_volume(self) -> None:
        [note] = _volume_plan().warnings
        assert "['postgres']" in note and "'api'" not in note
        assert "neither seen nor carried" in note

    def test_a_delete_finalize_says_coolify_deletes_their_archives(self) -> None:
        notes = _volume_plan(policy=FinalizePolicy.DELETE).warnings
        assert len(notes) == 2
        assert "--finalize delete" in notes[1] and "S3" in notes[1]
        assert "['postgres']" in notes[1]

    def test_follows_a_policy_swapped_after_planning(self) -> None:
        # cli.py swaps finalize_policy onto the built plan with model_copy.
        plan = _volume_plan()
        swapped = plan.model_copy(update={"finalize_policy": FinalizePolicy.DELETE})
        assert len(plan.warnings) == 1 and len(swapped.warnings) == 2

    def test_silent_where_volume_backups_do_not_exist(self) -> None:
        plan = _volume_plan(possible=False, policy=FinalizePolicy.DELETE)
        assert plan.warnings == ()
        assert plan.volume_backup_resources == ()

    def test_nothing_with_a_volume_says_nothing(self) -> None:
        assert schedules.volume_backup_notes([], deletes_source=True) == []

    @pytest.mark.parametrize(
        ("version", "possible"),
        [((4, 2, 9), False), ((4, 3, 0), True), ((4, 5, 0), True), (None, True)],
    )
    def test_exists_from_4_3_and_an_unknown_version_counts(
        self, version: tuple[int, ...] | None, possible: bool
    ) -> None:
        assert schedules.may_hold_volume_backups(version) is possible

    def test_a_plan_saved_before_the_flag_still_loads(self) -> None:
        saved = (
            '{"project": "shop", "environment": "production",'
            ' "source_server": {"uuid": "s1", "name": "old", "ip": "10.0.0.1"},'
            ' "target_server": {"uuid": "s2", "name": "new", "ip": "10.0.0.2"}}'
        )
        assert MigrationPlan.model_validate_json(saved).volume_backups_possible is False

    @pytest.mark.parametrize(
        ("version", "possible"),
        [("4.2.9", False), ("4.3.0", True), ("4.5.0", True), ("unknown", True)],
    )
    async def test_the_planner_sets_it_from_the_instance_version(
        self, monkeypatch: pytest.MonkeyPatch, version: str, possible: bool
    ) -> None:
        plan = await _plan(monkeypatch, _TaskPlanApi(tasks=[], version=version))
        assert plan.volume_backups_possible is possible


# ── canary ───────────────────────────────────────────────────────────────────

_BACKUP_FIELDS_RE = re.compile(
    r"function create_backup\b.*?\$backupConfigFields\s*=\s*\[(.*?)\];", re.S
)


def backup_create_fields(php: str) -> set[str]:
    """create_backup's ``$backupConfigFields`` — NOT ``$allowedFields``, which the
    shared create_allowed_fields() parses, so this canary needs its own."""
    from tests.test_api_fields import _PHP_STRING_RE

    match = _BACKUP_FIELDS_RE.search(php)
    return set(_PHP_STRING_RE.findall(match.group(1))) if match else set()


class TestBackupFieldsExtraction:
    def test_reads_create_backups_array_not_update_backups(self) -> None:
        php = (
            "public function update_backup() { $backupConfigFields = ['enabled']; }\n"
            "public function create_backup(Request $request) {\n"
            "  $backupConfigFields = ['save_s3', 'frequency', 's3_storage_uuid'];\n"
            "}\n"
        )
        assert backup_create_fields(php) == {"save_s3", "frequency", "s3_storage_uuid"}

    def test_an_empty_extraction_is_visible(self) -> None:
        assert backup_create_fields("<?php class X {}") == set()


@pytest.mark.integration
async def test_backup_fields_match_released_controller() -> None:
    """BACKUP_CREATE is accepted by, and adjudicates, create_backup at the newest tag."""
    from tests.test_api_fields import released_controllers

    tag, sources = await released_controllers()
    accepted = backup_create_fields(sources["databases"])
    assert accepted, f"found no create_backup $backupConfigFields at {tag}"
    assert accepted >= BACKUP_CREATE, (
        f"Coolify {tag} would 422 {sorted(BACKUP_CREATE - accepted)}, which we still send"
    )
    assert accepted - BACKUP_CREATE - BACKUP_S3_FIELDS == set(), (
        f"Coolify {tag} accepts backup fields we neither send nor adjudicated: "
        f"{sorted(accepted - BACKUP_CREATE - BACKUP_S3_FIELDS)}"
    )
