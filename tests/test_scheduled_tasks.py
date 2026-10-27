"""Scheduled tasks: carried to the target at CREATE, reported in the plan.

Until this existed, every migrated application and service lost its scheduled
tasks — the nightly cleanup or report cron simply stopped on the target, and
nothing said so. The tests pin the three things that make the copy faithful
(the exact create whitelist, a disabled task staying disabled, an unreadable
command failing loudly), the two that keep it safe (fatal inside CREATE so the
undo deletes the target; a retried POST cannot leave a doubled cron), and the
one that keeps it quiet: a task's command never reaches a log line, an exception
or a plan line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import tests.test_engine_steps as base_steps
from bg_coolify_migrate.api import schedules as api_schedules
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.fields import (
    SCHEDULED_TASK_COLLECTIONS,
    SCHEDULED_TASK_CREATE,
    SCHEDULED_TASK_REQUIRED,
    filter_body,
    rejected_keys,
)
from bg_coolify_migrate.api.schedules import build_scheduled_task_body, copy_scheduled_tasks
from bg_coolify_migrate.domain import schedules
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import CoolifyApiError, InsufficientTokenScope, MigrationError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"

#: A command shaped like the ones that make this sensitive. Searched for in every
#: log line, exception and plan line the tests can see.
SECRET_COMMAND = "curl -fsS -H 'Authorization: Bearer tok-0123456789' https://hc.example/ping"


def _task(**kw: Any) -> dict[str, Any]:
    """A task exactly as GET /{collection}/{uuid}/scheduled-tasks returns it."""
    base: dict[str, Any] = {
        "uuid": "t-src-1",
        "name": "nightly",
        "command": SECRET_COMMAND,
        "frequency": "0 3 * * *",
        "container": "web",
        "timeout": 600,
        "enabled": True,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }
    return {**base, **kw}


def _saved(*uuids: str) -> list[httpx.Response]:
    """createTask's answers, one per POST: the saved task with its own uuid, 201."""
    return [httpx.Response(201, json={"uuid": u}) for u in uuids]


def _held(*tasks: tuple[str, str]) -> httpx.Response:
    """The target's task list, as ``(uuid, name)`` pairs."""
    return httpx.Response(200, json=[{"uuid": u, "name": n} for u, n in tasks])


class _Recorder:
    """Stands in for a module's structlog logger, so assertions see every event.

    structlog.testing.capture_logs cannot be trusted here: the app configures
    cache_logger_on_first_use, and a logger cached by an earlier test bypasses it.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str) -> Any:
        def emit(event: str, **kw: Any) -> None:
            self.events.append((level, event, kw))

        return emit

    def __getattr__(self, level: str) -> Any:
        return self._record(level)

    def text(self) -> str:
        return json.dumps(self.events, default=str)

    def named(self, event: str) -> list[dict[str, Any]]:
        return [kw for _, e, kw in self.events if e == event]


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


# ── whitelist ────────────────────────────────────────────────────────────────


class TestFields:
    def test_the_create_whitelist_is_the_six_upstream_fields(self) -> None:
        # ScheduledTasksController::createTask, identical v4.1.2:57 .. v4.5.0:81.
        upstream = {"name", "command", "frequency", "container", "timeout", "enabled"}
        assert upstream == SCHEDULED_TASK_CREATE

    def test_required_fields_are_whitelisted(self) -> None:
        assert SCHEDULED_TASK_REQUIRED <= SCHEDULED_TASK_CREATE

    def test_databases_have_no_task_routes(self) -> None:
        assert "databases" not in SCHEDULED_TASK_COLLECTIONS
        assert {"applications", "services"} == SCHEDULED_TASK_COLLECTIONS


# ── client ───────────────────────────────────────────────────────────────────


class TestClient:
    @pytest.mark.parametrize("collection", ["applications", "services"])
    async def test_reads_the_resource_task_list(
        self, api: CoolifyClient, respx_mock: respx.Router, collection: str
    ) -> None:
        route = respx_mock.get(f"{BASE}/{collection}/u1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task()])
        )
        assert await api.get_scheduled_tasks(collection, "u1") == [_task()]
        assert route.called

    async def test_create_posts_the_body_unchanged(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.post(f"{BASE}/services/u1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "t1"})
        )
        body = {"name": "n", "command": "true", "frequency": "daily", "enabled": False}
        assert await api.create_scheduled_task("services", "u1", body) == "t1"
        assert json.loads(route.calls[0].request.read()) == body

    async def test_a_create_without_a_uuid_fails_and_never_echoes_the_command(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The uuid is what CREATE counts; the answer echoes the command, so it is
        # not attached to the error.
        respx_mock.post(f"{BASE}/services/u1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"name": "n", "command": SECRET_COMMAND})
        )
        with pytest.raises(CoolifyApiError, match="returned no uuid") as exc_info:
            await api.create_scheduled_task("services", "u1", {"name": "n"})
        assert SECRET_COMMAND not in str(exc_info.value)
        assert SECRET_COMMAND not in repr(exc_info.value.__dict__)


# ── body ─────────────────────────────────────────────────────────────────────


class TestBuildScheduledTaskBody:
    def test_carries_exactly_the_whitelisted_fields(self) -> None:
        body = build_scheduled_task_body(_task())
        assert body == {
            "name": "nightly",
            "command": SECRET_COMMAND,
            "frequency": "0 3 * * *",
            "container": "web",
            "timeout": 600,
            "enabled": True,
        }
        assert rejected_keys(body, SCHEDULED_TASK_CREATE) == frozenset()

    def test_a_disabled_task_stays_disabled(self) -> None:
        """REGRESSION guard: upstream defaults an ABSENT enabled to TRUE.

        Dropping a False on the way would re-arm a task the operator paused.
        """
        assert build_scheduled_task_body(_task(enabled=False))["enabled"] is False

    @pytest.mark.parametrize("container", ["", None])
    def test_an_empty_container_is_omitted(self, container: str | None) -> None:
        assert "container" not in build_scheduled_task_body(_task(container=container))

    def test_an_absent_timeout_is_left_to_the_server(self) -> None:
        assert "timeout" not in build_scheduled_task_body(_task(timeout=None))

    def test_an_unreadable_command_refuses_and_names_the_task(self) -> None:
        task = _task()
        del task["command"]
        with pytest.raises(InsufficientTokenScope, match="'nightly'") as exc_info:
            build_scheduled_task_body(task)
        assert "read:sensitive" in str(exc_info.value)

    @pytest.mark.parametrize("missing", ["name", "frequency"])
    def test_a_task_without_a_required_field_refuses(self, missing: str) -> None:
        with pytest.raises(MigrationError, match=missing) as exc_info:
            build_scheduled_task_body(_task(**{missing: None}))
        assert SECRET_COMMAND not in str(exc_info.value)

    def test_never_round_trips_the_get(self) -> None:
        # The GET carries uuid and timestamps; the create 422s per unknown field.
        assert rejected_keys(_task(), SCHEDULED_TASK_CREATE) == {"uuid", "created_at", "updated_at"}
        assert (
            filter_body(_task(), SCHEDULED_TASK_CREATE).keys()
            == build_scheduled_task_body(_task()).keys()
        )


# ── copy ─────────────────────────────────────────────────────────────────────


class TestCopyScheduledTasks:
    async def test_copies_every_task_to_the_target(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        source = [_task(), _task(uuid="t-src-2", name="paused", enabled=False, container=None)]
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=source)
        )
        post = respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            side_effect=_saved("t1", "t2")
        )
        respx_mock.get(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=_held(("t1", "nightly"), ("t2", "paused"))
        )

        count = await copy_scheduled_tasks(
            api,
            collection="applications",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.APP_DOCKERIMAGE,
        )

        assert count == 2
        bodies = [json.loads(c.request.read()) for c in post.calls]
        assert bodies == [build_scheduled_task_body(t) for t in source]
        assert bodies[1]["enabled"] is False
        assert logs.named("api.scheduled_tasks.copied") == [
            {"count": 2, "names": ["nightly", "paused"], "target": "tgt1"}
        ]
        assert SECRET_COMMAND not in logs.text()

    async def test_services_use_the_service_routes(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task(container=None)])
        )
        post = respx_mock.post(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        respx_mock.get(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=_held(("x", "nightly"))
        )
        count = await copy_scheduled_tasks(
            api,
            collection="services",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.SERVICE_TEMPLATE,
        )
        assert count == 1
        assert post.call_count == 1

    async def test_databases_make_no_request(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # No route is mocked: any request at all fails the test (assert_all_mocked).
        count = await copy_scheduled_tasks(
            api,
            collection="databases",
            source_uuid="db1",
            target_uuid="db2",
            kind=ResourceKind.DATABASE,
        )
        assert count == 0

    async def test_no_tasks_means_no_writes(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        read = respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[])
        )
        count = await copy_scheduled_tasks(
            api,
            collection="applications",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.APP_GIT_BUILD,
        )
        assert count == 0
        assert read.call_count == 1

    async def test_a_rejected_create_is_not_swallowed(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """Unlike copy_tags: a lost cron is a regression, and CREATE fails for free."""
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task()])
        )
        respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(
                422, json={"message": "Validation failed.", "errors": {"timeout": ["max"]}}
            )
        )
        with pytest.raises(CoolifyApiError) as exc_info:
            await copy_scheduled_tasks(
                api,
                collection="applications",
                source_uuid="src1",
                target_uuid="tgt1",
                kind=ResourceKind.APP_DOCKERIMAGE,
            )
        assert exc_info.value.status_code == 422
        assert SECRET_COMMAND not in str(exc_info.value)

    async def test_an_unreadable_command_fails_before_any_write(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        hidden = _task(name="second")
        del hidden["command"]
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task(), hidden])
        )
        post = respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        with pytest.raises(InsufficientTokenScope, match="'second'"):
            await copy_scheduled_tasks(
                api,
                collection="applications",
                source_uuid="src1",
                target_uuid="tgt1",
                kind=ResourceKind.APP_DOCKERIMAGE,
            )
        assert not post.called, "the first task must not be created while the second cannot be"

    async def test_a_doubled_task_fails_the_copy(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """A 502 after Coolify committed the row: the client re-POSTs, two rows exist."""
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task()])
        )
        respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        respx_mock.get(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=_held(("lost", "nightly"), ("x", "nightly"))
        )
        with pytest.raises(
            CoolifyApiError,
            match=r"2 on the target, 1 created; not created by this run: \['nightly'\]",
        ) as exc:
            await copy_scheduled_tasks(
                api,
                collection="applications",
                source_uuid="src1",
                target_uuid="tgt1",
                kind=ResourceKind.APP_DOCKERIMAGE,
            )
        assert "twice" in (exc.value.hint or "")
        assert SECRET_COMMAND not in str(exc.value)

    @pytest.mark.parametrize("name", ["nightly backup ", " nightly backup", "nightly backup\t"])
    async def test_a_name_the_api_trims_is_not_a_doubled_task(
        self, api: CoolifyClient, respx_mock: respx.Router, name: str
    ) -> None:
        """REGRESSION: the UI's Add form keeps a stray space, the API trims it.

        The target holds "nightly backup" for a source task "nightly backup ".
        Counted by name, that read as one task missing and another doubled, and
        CREATE failed on every run with a hint to re-run.
        """
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task(name=name)])
        )
        post = respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            side_effect=_saved("t1")
        )
        respx_mock.get(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=_held(("t1", name.strip()))
        )
        count = await copy_scheduled_tasks(
            api,
            collection="applications",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.APP_DOCKERIMAGE,
        )
        assert count == 1
        assert json.loads(post.calls[0].request.read())["name"] == name, "sent as it is"

    async def test_a_task_the_target_does_not_report_fails_the_copy(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(
                200, json=[_task(), _task(uuid="t-src-2", name="weekly", container=None)]
            )
        )
        respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            side_effect=_saved("t1", "t2")
        )
        respx_mock.get(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=_held(("t1", "nightly"))
        )
        with pytest.raises(CoolifyApiError, match=r"1 on the target, 2 created\)") as exc:
            await copy_scheduled_tasks(
                api,
                collection="applications",
                source_uuid="src1",
                target_uuid="tgt1",
                kind=ResourceKind.APP_DOCKERIMAGE,
            )
        assert "not reported back" in (exc.value.hint or "")

    async def test_the_retry_that_doubles_a_task_is_caught(
        self, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through the client's real retry: 502, retried, both saved."""

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("bg_coolify_migrate.api.client.asyncio.sleep", no_sleep)
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task()])
        )
        post = respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            side_effect=[httpx.Response(502), httpx.Response(201, json={"uuid": "x"})]
        )
        respx_mock.get(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=_held(("lost", "nightly"), ("x", "nightly"))
        )
        async with CoolifyClient(HOST, "tok", max_retries=2) as retrying:
            with pytest.raises(CoolifyApiError, match="do not match"):
                await copy_scheduled_tasks(
                    retrying,
                    collection="applications",
                    source_uuid="src1",
                    target_uuid="tgt1",
                    kind=ResourceKind.APP_DOCKERIMAGE,
                )
        assert post.call_count == 2

    async def test_a_container_the_target_does_not_run_warns_and_still_copies(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        """A template renamed its sub-service: the task is kept and the operator told."""
        respx_mock.get(f"{BASE}/services/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task(container="web")])
        )
        post = respx_mock.post(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        respx_mock.get(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=_held(("x", "nightly"))
        )
        respx_mock.get(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "tgt1",
                    "applications": [{"name": "app"}, {"name": "worker"}],
                    "databases": [{"name": "postgres"}],
                },
            )
        )
        assert (
            await copy_scheduled_tasks(
                api,
                collection="services",
                source_uuid="src1",
                target_uuid="tgt1",
                kind=ResourceKind.SERVICE_TEMPLATE,
            )
            == 1
        )
        assert post.called
        [warning] = logs.named("api.scheduled_tasks.container_missing")
        assert warning["names"] == ["nightly"]
        assert warning["available"] == ["app", "postgres", "worker"]
        assert SECRET_COMMAND not in logs.text()

    async def test_a_single_sub_service_never_warns(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        # One container: ScheduledTaskJob ignores the field (v4.5.0:190).
        respx_mock.get(f"{BASE}/services/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task(container="anything")])
        )
        respx_mock.post(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        respx_mock.get(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=_held(("x", "nightly"))
        )
        respx_mock.get(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={"applications": [{"name": "app"}]})
        )
        await copy_scheduled_tasks(
            api,
            collection="services",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.SERVICE_COMPOSE,
        )
        assert logs.named("api.scheduled_tasks.container_missing") == []

    async def test_a_compose_app_checks_against_its_loaded_compose(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(
                200, json=[_task(name="ok", container="web"), _task(name="lost", container="cron")]
            )
        )
        respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            side_effect=_saved("t1", "t2")
        )
        respx_mock.get(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=_held(("t1", "ok"), ("t2", "lost"))
        )
        respx_mock.get(f"{BASE}/applications/tgt1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "docker_compose_raw": "services:\n  web:\n    image: a\n  worker:\n    image: b\n"
                },
            )
        )
        await copy_scheduled_tasks(
            api,
            collection="applications",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.APP_GIT_COMPOSE,
        )
        [warning] = logs.named("api.scheduled_tasks.container_missing")
        assert warning["names"] == ["lost"]

    async def test_a_compose_app_without_a_loaded_compose_is_not_judged(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task(container="cron")])
        )
        respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        respx_mock.get(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=_held(("x", "nightly"))
        )
        respx_mock.get(f"{BASE}/applications/tgt1").mock(
            return_value=httpx.Response(200, json={"docker_compose_raw": None})
        )
        await copy_scheduled_tasks(
            api,
            collection="applications",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.APP_GIT_COMPOSE,
        )
        assert logs.named("api.scheduled_tasks.container_missing") == []

    async def test_a_failed_container_check_costs_only_the_check(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        respx_mock.get(f"{BASE}/services/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task()])
        )
        respx_mock.post(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        respx_mock.get(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=_held(("x", "nightly"))
        )
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=httpx.Response(500))
        count = await copy_scheduled_tasks(
            api,
            collection="services",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.SERVICE_TEMPLATE,
        )
        assert count == 1
        assert logs.named("api.scheduled_tasks.container_check_failed")

    async def test_a_plain_application_needs_no_container_check(
        self, api: CoolifyClient, respx_mock: respx.Router, logs: _Recorder
    ) -> None:
        # No GET /applications/tgt1 is mocked: a plain app runs one container.
        respx_mock.get(f"{BASE}/applications/src1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[_task()])
        )
        respx_mock.post(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        respx_mock.get(f"{BASE}/applications/tgt1/scheduled-tasks").mock(
            return_value=_held(("x", "nightly"))
        )
        await copy_scheduled_tasks(
            api,
            collection="applications",
            source_uuid="src1",
            target_uuid="tgt1",
            kind=ResourceKind.APP_GIT_BUILD,
        )
        assert logs.named("api.scheduled_tasks.container_missing") == []


# ── domain ───────────────────────────────────────────────────────────────────


def _server(tz: str | None = None, *, has_key: bool = True) -> dict[str, Any]:
    settings: dict[str, Any] = {"is_reachable": True}
    if has_key:
        settings["server_timezone"] = tz
    return {"uuid": "s", "settings": settings}


class TestScheduledTaskRefusals:
    @pytest.mark.parametrize("version", [(4, 4, 0), (4, 5, 0), None])
    def test_a_timeout_above_the_create_limit_refuses_and_names_the_task(
        self, version: tuple[int, ...] | None
    ) -> None:
        # None: an unknown version fails toward checking.
        [line] = schedules.scheduled_task_refusals([_task(timeout=36001)], version=version)
        assert "'nightly' (36001s)" in line
        assert SECRET_COMMAND not in line

    def test_silent_before_4_4(self) -> None:
        assert schedules.scheduled_task_refusals([_task(timeout=99999)], version=(4, 3, 24)) == []

    @pytest.mark.parametrize("timeout", [36000, None, "40000", True])
    def test_the_limit_itself_and_non_integers_pass(self, timeout: object) -> None:
        assert schedules.scheduled_task_refusals([_task(timeout=timeout)], version=(4, 5, 0)) == []


class TestScheduledTaskWarnings:
    def _warn(self, tasks: list[dict[str, Any]], **kw: Any) -> list[str]:
        args: dict[str, Any] = {
            "source_uuid": "src-uuid-1",
            "source_server": _server("UTC"),
            "target_server": _server("UTC"),
            "version": (4, 5, 0),
        }
        return schedules.scheduled_task_warnings(tasks, **{**args, **kw})

    def test_a_full_container_name_warns_from_4_5(self) -> None:
        [line] = self._warn([_task(container="store-src-uuid-1")])
        assert "['nightly']" in line
        assert "full name" in line

    def test_a_full_container_name_is_silent_before_4_5(self) -> None:
        assert self._warn([_task(container="store-src-uuid-1")], version=(4, 4, 7)) == []

    def test_a_bare_service_name_is_silent(self) -> None:
        assert self._warn([_task(container="store")]) == []

    def test_a_different_server_clock_warns_for_armed_tasks(self) -> None:
        [line] = self._warn(
            [_task(), _task(name="paused", enabled=False)],
            source_server=_server("Europe/Berlin"),
        )
        assert "['nightly']" in line
        assert "Europe/Berlin" in line and "UTC" in line

    def test_only_disabled_tasks_are_silent(self) -> None:
        assert self._warn([_task(enabled=False)], source_server=_server("Europe/Berlin")) == []

    def test_an_unknown_clock_is_silent(self) -> None:
        assert self._warn([_task()], source_server=_server(has_key=False)) == []
        assert self._warn([_task()], target_server=None) == []

    def test_empty_is_utc(self) -> None:
        # ScheduledJobManager falls back to config('app.timezone') = 'UTC'.
        assert self._warn([_task()], source_server=_server("")) == []

    def test_no_line_ever_carries_the_command(self) -> None:
        lines = self._warn(
            [_task(container="store-src-uuid-1")], source_server=_server("Europe/Berlin")
        )
        assert len(lines) == 2
        assert all(SECRET_COMMAND not in line for line in lines)


class TestTaskContainerMismatches:
    def test_names_tasks_whose_container_is_not_running_there(self) -> None:
        tasks = [_task(name="a", container="web"), _task(name="b", container="cron")]
        assert schedules.task_container_mismatches(tasks, {"web", "db"}) == ["b"]

    def test_no_container_means_no_mismatch(self) -> None:
        tasks = [_task(container=None), _task(container="")]
        assert schedules.task_container_mismatches(tasks, {"web", "db"}) == []

    def test_one_container_makes_the_field_moot(self) -> None:
        assert schedules.task_container_mismatches([_task(container="x")], {"web"}) == []


class TestCopyCountMismatches:
    def test_exact_copies_match(self) -> None:
        assert schedules.copy_count_mismatches(["a", "b", "b"], ["b", "a", "b"]) == {}

    def test_a_doubled_and_a_missing_row_are_both_reported(self) -> None:
        assert schedules.copy_count_mismatches(["a", "b"], ["a", "a"]) == {
            "a": (1, 2),
            "b": (1, 0),
        }


class TestServerTimezone:
    def test_reads_the_settings(self) -> None:
        assert schedules.server_timezone(_server("Europe/Berlin")) == "Europe/Berlin"

    def test_unknown_is_none_not_utc(self) -> None:
        assert schedules.server_timezone(None) is None
        assert schedules.server_timezone({"uuid": "s"}) is None
        assert schedules.server_timezone(_server(has_key=False)) is None

    def test_empty_is_utc(self) -> None:
        assert schedules.server_timezone(_server(None)) == "UTC"


# ── CREATE_TARGET ────────────────────────────────────────────────────────────


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=client,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=base_steps.TestCreateTarget._service_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=base_steps._source_host(),  # type: ignore[arg-type]
        target_host=base_steps._target_host(),  # type: ignore[arg-type]
    )
    yield context
    await client.aclose()


def _service_create(respx_mock: respx.Router, tasks: list[dict[str, Any]]) -> respx.Route:
    """CREATE for the budibase service plan, with ``tasks`` on the source."""
    delete = base_steps.TestCreateTarget._mock_service_create(
        respx_mock,
        target_volumes=[
            {"name": "tgt1_minio-data", "mount_path": "/data"},
            {"name": "tgt1_redis-data", "mount_path": "/data"},
        ],
    )
    respx_mock.get(f"{BASE}/services/svc1/scheduled-tasks").mock(
        return_value=httpx.Response(200, json=tasks)
    )
    return delete


class TestCreateTarget:
    async def test_tasks_are_created_on_the_target_after_it_exists(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        source = [
            _task(container=None),
            _task(uuid="t2", name="paused", enabled=False, container=None),
        ]
        delete = _service_create(respx_mock, source)
        post = respx_mock.post(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            side_effect=_saved("t1", "t2")
        )
        respx_mock.get(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=_held(("t1", "nightly"), ("t2", "paused"))
        )

        result = await steps.step_create_target(ctx)

        assert result["target_uuids"] == {"svc1": "tgt1"}
        assert [json.loads(c.request.read()) for c in post.calls] == [
            build_scheduled_task_body(t) for t in source
        ]
        order = [(c.request.method, c.request.url.path) for c in respx_mock.calls]
        created_at = order.index(("POST", "/api/v1/services"))
        tasks_at = [
            i for i, call in enumerate(order) if call[1] == "/api/v1/services/tgt1/scheduled-tasks"
        ]
        assert tasks_at and min(tasks_at) > created_at
        assert not delete.called

    async def test_a_rejected_task_deletes_the_target_before_any_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        delete = _service_create(respx_mock, [_task(container=None)])
        respx_mock.post(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(403, json={"message": "needs the deploy permission"})
        )
        stop = respx_mock.post(url__regex=r".*/stop$").mock(return_value=httpx.Response(200))

        with pytest.raises(CoolifyApiError):
            await steps.step_create_target(ctx)

        assert delete.called, "the in-step undo must delete the half-made target"
        assert not stop.called, "the source was never stopped"

    async def test_a_doubled_task_deletes_the_target(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        delete = _service_create(respx_mock, [_task(container=None)])
        respx_mock.post(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=httpx.Response(201, json={"uuid": "x"})
        )
        respx_mock.get(f"{BASE}/services/tgt1/scheduled-tasks").mock(
            return_value=_held(("lost", "nightly"), ("x", "nightly"))
        )
        with pytest.raises(CoolifyApiError, match="do not match"):
            await steps.step_create_target(ctx)
        assert delete.called


# ── the plan ─────────────────────────────────────────────────────────────────
# The rules above are pure; these pin the WIRING in build_plan, because a rule
# nobody calls protects nothing and reports nothing.


class _TaskPlanApi:
    def __init__(
        self,
        *,
        tasks: list[dict[str, Any]] | Exception,
        target_timezone: str | None = None,
        version: str = "4.5.0",
    ) -> None:
        self._tasks = tasks
        self._tz = target_timezone
        self._version = version
        self.task_reads: list[tuple[str, str]] = []

    async def get_server(self, uuid: str) -> dict[str, Any]:
        settings: dict[str, Any] = {"server_role": "both"}
        if self._tz is not None:
            settings["server_timezone"] = self._tz
        return {"uuid": uuid, "name": uuid, "ip": "10.0.0.2", "settings": settings}

    async def version(self) -> str:
        return self._version

    async def get_storages(self, collection: str, uuid: str) -> dict[str, Any]:
        return {"persistent_storages": [], "file_storages": []}

    async def get_envs(self, collection: str, uuid: str) -> list[dict[str, Any]]:
        return []

    async def get_scheduled_tasks(self, collection: str, uuid: str) -> list[dict[str, Any]]:
        # RECORDED, not asserted here: the planner's read is fail-soft, so an
        # AssertionError raised in here would be swallowed and prove nothing.
        self.task_reads.append((collection, uuid))
        if isinstance(self._tasks, Exception):
            raise self._tasks
        return self._tasks


async def _plan(
    monkeypatch: pytest.MonkeyPatch,
    api: _TaskPlanApi,
    *,
    snapshot: dict[str, Any] | None = None,
    source_timezone: str | None = None,
    full: dict[str, Any] | None = None,
) -> Any:
    from bg_coolify_migrate.domain.kinds import BuildPack
    from bg_coolify_migrate.domain.manifest import VolumeManifest
    from bg_coolify_migrate.domain.plan import ResourceSnapshot
    from bg_coolify_migrate.engine import planner

    shape = snapshot or {
        "collection": "applications",
        "kind": ResourceKind.APP_DOCKERIMAGE,
        "build_pack": BuildPack.DOCKERIMAGE,
    }

    async def fake_find_project(_api: Any, project: str) -> dict[str, Any]:
        return {"uuid": "p1", "name": "shop"}

    async def fake_find_server(_api: Any, name: str) -> dict[str, Any]:
        return {"uuid": "s2", "name": name}

    async def fake_env_resources(_api: Any, project_uuid: str, environment: str) -> Any:
        return [(shape["collection"], {"uuid": "a1", "name": "api"})]

    async def fake_snapshot(_api: Any, host: Any, **kw: Any) -> Any:
        return ResourceSnapshot(uuid="a1", name="api", **shape), [], full or {}

    async def fake_resolve_server(_api: Any, resource: Any) -> dict[str, Any]:
        settings: dict[str, Any] = {}
        if source_timezone is not None:
            settings["server_timezone"] = source_timezone
        return {"uuid": "s1", "name": "old", "ip": "10.0.0.1", "settings": settings}

    async def fake_manifest(host: Any, **kw: Any) -> VolumeManifest:
        return VolumeManifest()

    async def nothing(*_a: Any, **_kw: Any) -> Any:
        return None

    async def no_mounts(host: Any, containers: Any) -> list[Any]:
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
        api,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        project="shop",
        environment="production",
        target_server="new",
    )


class TestPlan:
    async def test_task_rules_see_the_source_tasks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan(monkeypatch, _TaskPlanApi(tasks=[_task(timeout=40000)]))
        assert plan.is_blocked
        assert any("'nightly' (40000s)" in r for r in plan.resources[0].refusals)
        assert all(SECRET_COMMAND not in line for line in plan.resources[0].blocking_reasons)

    async def test_an_unreadable_task_list_is_fail_soft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _TaskPlanApi(tasks=CoolifyApiError("boom", status_code=500))
        plan = await _plan(monkeypatch, api)
        assert not plan.is_blocked
        assert api.task_reads == [("applications", "a1")]

    async def test_databases_never_read_tasks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bg_coolify_migrate.domain.kinds import DatabaseEngine

        api = _TaskPlanApi(tasks=[_task(timeout=40000)])
        plan = await _plan(
            monkeypatch,
            api,
            snapshot={
                "collection": "databases",
                "kind": ResourceKind.DATABASE,
                "engine": DatabaseEngine.POSTGRESQL,
            },
        )
        assert api.task_reads == []
        assert not any("timeout" in r for r in plan.resources[0].refusals)

    async def test_a_different_target_clock_reaches_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _TaskPlanApi(
            tasks=[_task(), _task(name="paused", enabled=False)], target_timezone="UTC"
        )
        plan = await _plan(monkeypatch, api, source_timezone="Europe/Berlin")
        [line] = [w for w in plan.resources[0].warnings if "server's clock" in w]
        assert "['nightly']" in line
        assert not plan.is_blocked


# ── canary ───────────────────────────────────────────────────────────────────


def _task_create_fields(php: str) -> set[str]:
    """The FIRST ``$allowedFields`` of ScheduledTasksController — createTask's."""
    from tests.test_api_fields import _ALLOWED_FIELDS_RE, _PHP_STRING_RE

    arrays = _ALLOWED_FIELDS_RE.findall(php)
    return set(_PHP_STRING_RE.findall(arrays[0])) if arrays else set()


def test_the_canary_parser_reads_the_create_array() -> None:
    """A silently empty extraction would make the canary pass forever."""
    php = (
        "private function createTask() {\n"
        "  $allowedFields = ['name', 'command', 'frequency', 'container', 'timeout', 'enabled'];\n"
        "}\nprivate function updateTask() { $allowedFields = ['name']; }"
    )
    assert _task_create_fields(php) == set(SCHEDULED_TASK_CREATE)
    assert _task_create_fields("<?php // nothing") == set()


@pytest.mark.integration
async def test_scheduled_task_whitelist_matches_upstream() -> None:
    """SCHEDULED_TASK_CREATE equals createTask's array at the newest release tag."""
    from tests.test_api_fields import released_controllers

    tag, _ = await released_controllers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://raw.githubusercontent.com/coollabsio/coolify/{tag}/"
            "app/Http/Controllers/Api/ScheduledTasksController.php"
        )
        resp.raise_for_status()
    accepted = _task_create_fields(resp.text)
    assert accepted, f"found no $allowedFields in ScheduledTasksController at {tag}"
    assert accepted == set(SCHEDULED_TASK_CREATE), (
        f"Coolify {tag} createTask accepts {sorted(accepted)}; we send "
        f"{sorted(SCHEDULED_TASK_CREATE)}. An unlisted field 422s the task — and CREATE."
    )
