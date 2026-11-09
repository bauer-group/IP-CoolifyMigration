"""Services against Coolify 4.7: the source's own compose.

One finding of the v4.6.0 -> v4.7.0 audit:

* A template service's target was built from the instance's CURRENT catalog,
  which the CDN refreshes for every release: v4.7.0's moved getoutline from
  postgres:12 to 16 on the same volume key and mount, so the copy verified and
  the target's database refused its data after the stop. The target now runs
  the source's own compose, carried by one PATCH right after the create.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api import resources
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.plan import MigrationPlan, ResourcePlan, ResourceSnapshot, Strategy
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import CoolifyApiError, UnsupportedResource
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.test_engine_planner import _plan_one_app
from tests.test_engine_steps import _plan, _source_host, _target_host

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SRC_WC = "app.0046-20.cloud.bauer-group.com"
CREATE = "create_target"

#: getoutline as the source runs it — created before the v4.7.0 catalog moved
#: its postgres to 16 on the same key and mount.
SOURCE_COMPOSE = """\
services:
  outline:
    image: outlinewiki/outline:0.82
    environment:
      - SERVICE_URL_OUTLINE_3000
  postgres:
    image: postgres:12-alpine
    volumes:
      - database-data:/var/lib/postgresql/data
"""


def _decoded(body: dict[str, Any]) -> str:
    return base64.b64decode(body["docker_compose_raw"]).decode()


# ── A: the target runs the source's compose ──────────────────────────────────


def _service_plan(
    *, kind: ResourceKind = ResourceKind.SERVICE_TEMPLATE, compose: str | None = SOURCE_COMPOSE
) -> MigrationPlan:
    item = VolumeItem(
        mount_class=MountClass.NAMED,
        decision=Decision.MIGRATE,
        reason="named volume",
        source_name="svc1_database-data",
        source_path="/var/lib/docker/volumes/svc1_database-data/_data",
        mount_path="/var/lib/postgresql/data",
        bytes=1024,
    )
    return _plan(
        resources=(
            ResourcePlan(
                snapshot=ResourceSnapshot(
                    uuid="svc1",
                    name="wiki",
                    collection="services",
                    kind=kind,
                    service_type="getoutline" if kind is ResourceKind.SERVICE_TEMPLATE else None,
                    docker_compose_raw=compose,
                ),
                strategy=Strategy.COPY_DATA,
                manifest=VolumeManifest(items=(item,)),
            ),
        )
    )


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=client,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_service_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_source_host(),  # type: ignore[arg-type]
        target_host=_target_host(),  # type: ignore[arg-type]
    )
    yield context
    await client.aclose()


#: The target once the source's compose took: the source's parts, and the
#: template's newer ones only where a test adds them.
TARGET_PARTS: dict[str, Any] = {
    "applications": [{"uuid": "t-outline", "name": "outline"}],
    "databases": [{"uuid": "t-pg", "name": "postgres"}],
}


def _mock_create(
    respx_mock: respx.Router,
    *,
    target: str = "tgt1",
    target_get: dict[str, Any] | None = None,
    source_envs: list[dict[str, Any]] | None = None,
    target_envs: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, Any]]:
    """CREATE for the wiki plan. Returns every request as (method, path, body)."""
    seen: list[tuple[str, str, Any]] = []

    def record(response: httpx.Response) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            raw = request.read()
            seen.append((request.method, request.url.path, json.loads(raw) if raw else None))
            return response

        return handler

    respx_mock.get(f"{BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
    )
    respx_mock.get(f"{BASE}/projects/p1").mock(
        return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
    )
    respx_mock.get(f"{BASE}/servers/s2/destinations").mock(
        return_value=httpx.Response(200, json=[{"uuid": "d1", "network": "coolify"}])
    )
    respx_mock.get(f"{BASE}/services/svc1").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": "svc1",
                "name": "wiki",
                "service_type": "getoutline",
                "applications": [
                    {"uuid": "sa-outline", "name": "outline", "fqdn": "http://o.10.0.0.1.sslip.io"}
                ],
                "databases": [{"uuid": "sd-pg", "name": "postgres"}],
            },
        )
    )
    respx_mock.post(f"{BASE}/services").mock(
        side_effect=record(httpx.Response(201, json={"uuid": target}))
    )
    respx_mock.patch(f"{BASE}/services/{target}").mock(
        side_effect=record(httpx.Response(200, json={"uuid": target}))
    )
    respx_mock.get(f"{BASE}/services/{target}").mock(
        side_effect=record(
            httpx.Response(200, json={"uuid": target, **(target_get or TARGET_PARTS)})
        )
    )
    respx_mock.get(f"{BASE}/services/svc1/envs").mock(
        return_value=httpx.Response(200, json=source_envs or [])
    )
    respx_mock.get(f"{BASE}/services/{target}/envs").mock(
        return_value=httpx.Response(200, json=target_envs or [])
    )
    respx_mock.patch(f"{BASE}/services/{target}/envs/bulk").mock(
        side_effect=record(httpx.Response(200, json=[]))
    )
    respx_mock.get(f"{BASE}/services/svc1/tags").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(f"{BASE}/services/svc1/scheduled-tasks").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx_mock.get(f"{BASE}/services/svc1/storages").mock(
        return_value=httpx.Response(
            200,
            json={
                "persistent_storages": [
                    {"name": "svc1_database-data", "mount_path": "/var/lib/postgresql/data"}
                ]
            },
        )
    )
    respx_mock.get(f"{BASE}/services/{target}/storages").mock(
        return_value=httpx.Response(
            200,
            json={
                "persistent_storages": [
                    {"name": f"{target}_database-data", "mount_path": "/var/lib/postgresql/data"}
                ]
            },
        )
    )
    return seen


class TestTemplateTargetRunsTheSourceCompose:
    async def test_one_compose_patch_follows_the_create_and_precedes_every_read(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        seen = _mock_create(respx_mock)

        result = await steps.step_create_target(ctx)

        assert result["target_uuids"] == {"svc1": "tgt1"}
        (post,) = [s for s in seen if s[0] == "POST"]
        # The template identity: still a `type` create, never with a compose.
        assert post[2]["type"] == "getoutline"
        assert "docker_compose_raw" not in post[2]
        patches = [s for s in seen if s[0] == "PATCH"]
        assert len(patches) == 1, "only the compose; the source's URL is generated"
        assert set(patches[0][2]) == {"docker_compose_raw"}
        assert _decoded(patches[0][2]) == SOURCE_COMPOSE
        # Before finish_service_target reads the part names, copy_envs and the
        # pairing rehearsal: the first read of the target comes after it.
        assert seen.index(patches[0]) < next(
            i for i, s in enumerate(seen) if s[0] == "GET" and s[1] == "/api/v1/services/tgt1"
        )

    async def test_the_target_uuid_is_journaled_before_the_patch(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A PATCH that fails must cost a target the rollback knows about.
        _mock_create(respx_mock)
        respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(422, json={"message": "Validation failed."})
        )
        delete = respx_mock.delete(url__startswith=f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )
        source_writes = respx_mock.patch(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(CoolifyApiError):
            await steps.step_create_target(ctx)

        assert delete.called, "the half-made target must not be stranded"
        assert not source_writes.called
        assert ctx.journal.undo_info(CREATE)["target_uuids"] == {"svc1": "tgt1"}

    async def test_a_compose_service_needs_no_patch(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _service_plan(kind=ResourceKind.SERVICE_COMPOSE)
        seen = _mock_create(respx_mock)

        await steps.step_create_target(ctx)

        (post,) = [s for s in seen if s[0] == "POST"]
        assert _decoded(post[2]) == SOURCE_COMPOSE
        assert "type" not in post[2]
        assert not [s for s in seen if s[0] == "PATCH"]

    async def test_a_resumed_create_patches_only_its_own_new_target(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Crashed after the PATCH: the journal names the old target, which the
        # resumed step deletes before it creates (and PATCHes) a fresh one.
        ctx.journal.append("step_started", state=CREATE)
        ctx.journal.append(
            "step_started",
            state=CREATE,
            detail={"target_uuids": {"svc1": "tgtOld"}, "parked_domains": {}},
        )
        ctx.target_uuids["svc1"] = "tgtOld"
        respx_mock.get(f"{BASE}/services/tgtOld").mock(
            return_value=httpx.Response(200, json={"uuid": "tgtOld", "applications": []})
        )
        old_delete = respx_mock.delete(url__startswith=f"{BASE}/services/tgtOld").mock(
            return_value=httpx.Response(200, json={})
        )
        old_patch = respx_mock.patch(f"{BASE}/services/tgtOld")
        seen = _mock_create(respx_mock)

        result = await steps.step_create_target(ctx)

        assert old_delete.called
        assert not old_patch.called
        assert result["target_uuids"] == {"svc1": "tgt1"}
        assert [_decoded(s[2]) for s in seen if s[0] == "PATCH"] == [SOURCE_COMPOSE]

    async def test_a_template_without_its_compose_creates_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _service_plan(compose=None)
        seen = _mock_create(respx_mock)

        with pytest.raises(UnsupportedResource, match="no compose to copy"):
            await steps.step_create_target(ctx)

        assert not [s for s in seen if s[0] in ("POST", "PATCH")]


class TestLeftoverTemplateParts:
    def test_parts_the_source_compose_lacks_and_nothing_flags(self) -> None:
        # Before 4.7.0 the re-parse keeps ente's new `albums` unflagged.
        target = {
            "applications": [{"name": "outline"}, {"name": "albums"}],
            "databases": [{"name": "postgres"}],
        }
        assert fidelity.leftover_template_parts(target, SOURCE_COMPOSE) == ["albums"]

    def test_flagged_parts_are_not_leftovers(self) -> None:
        # From 4.7.0 the re-parse flags them and the status ignores them.
        target = {
            "applications": [
                {"name": "outline"},
                {"name": "albums", "is_removed_from_compose": True},
            ]
        }
        assert fidelity.leftover_template_parts(target, SOURCE_COMPOSE) == []

    def test_an_unparseable_compose_says_nothing(self) -> None:
        assert fidelity.leftover_template_parts({"applications": [{"name": "x"}]}, ":\n- [") == []

    async def test_carry_returns_them_and_never_logs_the_compose(
        self, respx_mock: respx.Router, capsys: pytest.CaptureFixture[str]
    ) -> None:
        compose = SOURCE_COMPOSE + "  # SECRET_IN_COMPOSE=hunter2\n"
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )
        respx_mock.get(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(
                200, json={"applications": [{"name": "outline"}, {"name": "albums"}]}
            )
        )
        respx_mock.get(f"{BASE}/services/tgt1/envs").mock(return_value=httpx.Response(200, json=[]))
        respx_mock.get(f"{BASE}/services/svc1/envs").mock(return_value=httpx.Response(200, json=[]))
        snapshot = ResourceSnapshot(
            uuid="svc1",
            name="wiki",
            collection="services",
            kind=ResourceKind.SERVICE_TEMPLATE,
            service_type="getoutline",
            docker_compose_raw=compose,
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            left = await resources.carry_source_compose(api, snapshot, "tgt1", version=None)

        assert left == ["albums"]
        assert _decoded(json.loads(patch.calls[0].request.read())) == compose
        out = capsys.readouterr()
        assert "hunter2" not in out.out + out.err


#: litellm as the v4.7.0 catalog creates it, beside a source made before that
#: sync: LITELLM_SALT_KEY and its generated password are the template's alone.
_TEMPLATE_ENVS: list[dict[str, Any]] = [
    {"uuid": "e-master", "key": "LITELLM_MASTER_KEY", "value": "sk-target-generated"},
    {"uuid": "e-salt", "key": "LITELLM_SALT_KEY", "value": "${SERVICE_PASSWORD_SALTKEY}"},
    {"uuid": "e-saltpw", "key": "SERVICE_PASSWORD_SALTKEY", "value": "fresh-salt-secret"},
    {"uuid": "e-url", "key": "SERVICE_URL_OUTLINE", "value": "http://o.10.0.0.2.sslip.io"},
]
_SOURCE_ENVS: list[dict[str, Any]] = [
    {"uuid": "s-master", "key": "LITELLM_MASTER_KEY", "value": "sk-source-secret"},
    # A URL row copy_envs leaves to the target: still the source's key.
    {"uuid": "s-url", "key": "SERVICE_URL_OUTLINE", "value": "http://o.10.0.0.1.sslip.io"},
]


class _Logs:
    """Stands in for resources' structlog logger, so assertions see every event.

    capsys is no witness for a presence check: the app caches its loggers on
    first use (test_scheduled_tasks._Recorder says more).
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, level: str) -> Any:
        return lambda event, **kw: self.events.append((event, kw))

    def text(self) -> str:
        return json.dumps(self.events, default=str)

    def named(self, event: str) -> list[dict[str, Any]]:
        return [kw for e, kw in self.events if e == event]


class TestTemplateEnvLeftovers:
    def test_exactly_the_keys_the_source_has_no_row_for(self) -> None:
        assert fidelity.leftover_template_envs(_TEMPLATE_ENVS, _SOURCE_ENVS) == [
            ("LITELLM_SALT_KEY", "e-salt"),
            ("SERVICE_PASSWORD_SALTKEY", "e-saltpw"),
        ]

    def test_a_key_the_source_has_is_never_a_leftover(self) -> None:
        assert fidelity.leftover_template_envs(_SOURCE_ENVS, _TEMPLATE_ENVS) == []
        assert fidelity.leftover_template_envs([{"uuid": "x"}, {"key": ""}], []) == []

    async def test_create_deletes_them_and_touches_no_shared_key(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logs = _Logs()
        monkeypatch.setattr(resources, "log", logs)
        seen = _mock_create(respx_mock, source_envs=_SOURCE_ENVS, target_envs=_TEMPLATE_ENVS)
        deleted = respx_mock.delete(url__startswith=f"{BASE}/services/tgt1/envs/").mock(
            return_value=httpx.Response(200, json={"message": "Environment variable deleted."})
        )
        target_delete = respx_mock.delete(url__startswith=f"{BASE}/services/tgt1?")

        await steps.step_create_target(ctx)

        assert sorted(c.request.url.path for c in deleted.calls) == [
            "/api/v1/services/tgt1/envs/e-salt",
            "/api/v1/services/tgt1/envs/e-saltpw",
        ]
        assert not target_delete.called
        # The source's values still overwrite what both have.
        (bulk,) = [s for s in seen if s[1].endswith("/envs/bulk")]
        assert {e["key"] for e in bulk[2]["data"]} == {"LITELLM_MASTER_KEY"}
        (dropped,) = logs.named("api.service.template_envs_dropped")
        assert dropped["keys"] == ["LITELLM_SALT_KEY", "SERVICE_PASSWORD_SALTKEY"]
        for value in ("fresh-salt-secret", "sk-source-secret", "sk-target-generated"):
            assert value not in logs.text()

    async def test_a_retried_delete_that_committed_is_done(self, respx_mock: respx.Router) -> None:
        respx_mock.delete(f"{BASE}/services/tgt1/envs/e-salt").mock(
            return_value=httpx.Response(404, json={"message": "Environment variable not found."})
        )
        respx_mock.delete(f"{BASE}/services/gone/envs/e-salt").mock(
            return_value=httpx.Response(404, json={"message": "Service not found."})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            assert await api.delete_env("services", "tgt1", "e-salt") is False
            with pytest.raises(CoolifyApiError):
                await api.delete_env("services", "gone", "e-salt")

    async def test_a_row_it_cannot_delete_refuses_before_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create(respx_mock, target_envs=[{"key": "LITELLM_SALT_KEY", "value": "x"}])
        undo = respx_mock.delete(url__startswith=f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(UnsupportedResource, match="LITELLM_SALT_KEY") as exc_info:
            await steps.step_create_target(ctx)

        assert "never stopped" in (exc_info.value.hint or "")
        assert [c.request.url.path for c in undo.calls] == ["/api/v1/services/tgt1"]

    async def test_a_compose_service_is_never_pruned(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Created from the source's compose itself: there is no template to undo.
        ctx.plan = _service_plan(kind=ResourceKind.SERVICE_COMPOSE)
        _mock_create(respx_mock, source_envs=_SOURCE_ENVS, target_envs=_TEMPLATE_ENVS)
        deleted = respx_mock.delete(url__startswith=f"{BASE}/services/tgt1/envs/")

        await steps.step_create_target(ctx)

        assert not deleted.called


class TestLeftoverPartsNotice:
    async def test_the_run_tells_the_operator(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create(
            respx_mock,
            target_get={
                **TARGET_PARTS,
                "applications": [*TARGET_PARTS["applications"], {"name": "albums"}],
            },
        )

        await steps.step_create_target(ctx)

        (notice,) = ctx.notices
        assert "wiki" in notice and "['albums']" in notice
        assert "scheduled tasks" in notice

    async def test_none_without_leftovers(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create(respx_mock)
        await steps.step_create_target(ctx)
        assert ctx.notices == []


#: The planner harness's snapshot shape for a template service.
_TEMPLATE: dict[str, Any] = {
    "collection": "services",
    "kind": ResourceKind.SERVICE_TEMPLATE,
    "service_type": "getoutline",
}


class TestPlanForTemplates:
    async def test_a_template_without_its_compose_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_one_app(monkeypatch, {"applications": []}, snapshot=_TEMPLATE)
        assert plan.is_blocked
        assert any("docker_compose_raw" in r for r in plan.resources[0].refusals)

    async def test_a_template_with_its_compose_plans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_one_app(
            monkeypatch,
            {"applications": []},
            snapshot={**_TEMPLATE, "docker_compose_raw": SOURCE_COMPOSE},
        )
        assert not plan.resources[0].refusals

    async def test_before_4_4_the_template_target_loses_comments_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The PATCH re-dumps the compose before 4.4, like the compose create.
        plan = await _plan_one_app(
            monkeypatch,
            {"applications": []},
            version="4.3.10",
            snapshot={**_TEMPLATE, "docker_compose_raw": SOURCE_COMPOSE},
        )
        assert any("re-dumps compose" in w for w in plan.resources[0].warnings)
