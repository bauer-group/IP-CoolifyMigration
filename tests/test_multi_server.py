"""Applications that also deploy to additional servers are refused, on every release.

The target is created on one server, so the additional deployments would be
dropped; QUIESCE polls only the source server's daemon (invariant 9), while
Coolify's fan-out stop returns early on a non-functional server up to 4.3.x.
None of that is the operator's judgement, so it is a hard refusal — at plan time,
and again at CREATE for a server attached after the plan was made. Only the
remedy's wording depends on the release.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.kinds import BuildPack, ResourceKind
from bg_coolify_migrate.domain.manifest import VolumeManifest
from bg_coolify_migrate.domain.plan import ResourcePlan, Strategy
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import PreflightError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.test_engine_planner import _plan_one_app
from tests.test_engine_steps import BASE, HOST, _plan, _snapshot, _source_host, _target_host

V45 = (4, 5, 0)

_TWO = {"additional_servers_count": 2, "additional_networks_count": 2}


def _refusal(source: dict[str, Any], version: tuple[int, ...] | None = V45) -> str:
    line = fidelity.multi_server_refusal(source, version=version)
    assert line is not None
    return line


# ── the rule ─────────────────────────────────────────────────────────────────


class TestMultiServerApplications:
    def test_an_application_on_additional_servers_refuses(self) -> None:
        out = fidelity.application_refusals(_TWO, version=V45)
        assert len(out) == 1
        assert "2 additional server(s)" in out[0]

    def test_it_is_the_first_refusal(self) -> None:
        out = fidelity.application_refusals(
            {**_TWO, "manual_webhook_secret_github": "short"}, version=V45
        )
        assert len(out) == 2
        assert "additional server" in out[0]

    @pytest.mark.parametrize(
        "source",
        [
            {"additional_servers_count": 0, "additional_networks_count": 0},
            {},
            {"additional_servers": []},
        ],
    )
    def test_a_single_server_application_is_silent(self, source: dict[str, Any]) -> None:
        assert fidelity.application_refusals(source, version=V45) == []
        assert fidelity.multi_server_refusal(source, version=V45) is None

    @pytest.mark.parametrize(
        "version", [(4, 1, 2), (4, 2, 1), (4, 3, 10), (4, 4, 0), (4, 5, 0), None]
    )
    def test_the_refusal_is_not_version_gated(self, version: tuple[int, ...] | None) -> None:
        assert fidelity.application_refusals(_TWO, version=version)

    def test_the_api_reattach_route_is_named_only_from_4_3(self) -> None:
        assert "POST /applications" not in _refusal(_TWO, (4, 2, 1))
        assert "POST /applications/{uuid}/destinations" in _refusal(_TWO, (4, 3, 0))

    def test_the_4_4_restrictions_are_named_from_4_4(self) -> None:
        assert "Coolify >= 4.4" not in _refusal(_TWO, (4, 3, 24))
        assert "Coolify >= 4.4" in _refusal(_TWO, (4, 4, 0))
        assert "Coolify >= 4.4" in _refusal(_TWO, None)  # unknown fails toward checking

    def test_removal_is_one_way_from_4_4(self) -> None:
        assert "one-way" not in _refusal(_TWO, (4, 3, 24))
        assert "one-way" in _refusal(_TWO, (4, 4, 0))
        assert "one-way" in _refusal(_TWO, None)

    def test_it_warns_against_the_api_detach(self) -> None:
        # The one remediation that would orphan running containers.
        assert "only detaches" in _refusal(_TWO)

    def test_the_target_server_is_not_re_added(self) -> None:
        # Coolify 422s an additional destination on the primary's own server.
        assert "except the migration's target server" in _refusal(_TWO)

    def test_an_unreachable_server_is_not_stopped_by_the_ui(self) -> None:
        # StopApplicationOneServer returns early; removeServer detaches anyway.
        assert "while that server is reachable" in _refusal(_TWO)

    def test_storage_copies_are_named(self) -> None:
        assert "its OWN copy" in _refusal(_TWO, (4, 1, 2))

    def test_server_names_are_listed_without_reading_anything_else(self) -> None:
        line = _refusal(
            {
                "additional_servers": [
                    {"name": "edge-2", "uuid": "u2", "proxy": {"secret": "XTOKENX"}},
                    {"name": "edge-2", "uuid": "u2"},  # a pre-4.5 duplicate pivot row
                    {"uuid": "u3", "settings": {"wildcard_domain": "Y"}},
                    {"proxy": {"last_saved_proxy_configuration": "Z"}},
                    "not-a-row",
                ]
            }
        )
        assert "(edge-2, u3)" in line
        assert "XTOKENX" not in line
        assert "None" not in line

    @pytest.mark.parametrize(
        ("source", "count"),
        [
            ({"additional_servers_count": True}, 0),
            ({"additional_servers_count": "3"}, 3),
            ({"additional_servers_count": None}, 0),
            ({"additional_servers_count": -2}, 0),
            ({"additional_servers_count": "x"}, 0),
            ({"additional_networks_count": 1}, 1),
            ({"additional_servers": [{"name": "a"}, {"name": "b"}]}, 2),
            (
                {
                    "additional_servers_count": 1,
                    "additional_networks_count": 2,
                    "additional_servers": [{}, {}, {}],
                },
                3,
            ),
        ],
    )
    def test_the_count_parse_is_defensive(self, source: dict[str, Any], count: int) -> None:
        assert fidelity.additional_servers(source)[0] == count


# ── the planner wiring ───────────────────────────────────────────────────────


class TestPlannerWiring:
    async def test_a_multi_server_application_blocks_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The floor release: the GET field flows through build_plan there too.
        plan = await _plan_one_app(monkeypatch, {"additional_servers_count": 1}, version="4.1.2")
        assert plan.is_blocked
        assert "additional server" in plan.resources[0].blocking_reasons[0]

    async def test_a_multi_server_compose_application_blocks_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rule must not sit under build_plan's `not compose_backed` branch."""
        plan = await _plan_one_app(
            monkeypatch,
            {"additional_servers_count": 1, "build_pack": "dockercompose"},
            snapshot={
                "collection": "applications",
                "kind": ResourceKind.APP_GIT_COMPOSE,
                "build_pack": BuildPack.DOCKERCOMPOSE,
            },
            version="4.1.2",
        )
        assert plan.is_blocked
        assert any("additional server" in r for r in plan.resources[0].blocking_reasons)


# ── the CREATE re-check ──────────────────────────────────────────────────────


def _app_resource() -> ResourcePlan:
    return ResourcePlan(
        snapshot=_snapshot(
            uuid="app1",
            name="shop",
            collection="applications",
            kind=ResourceKind.APP_DOCKERIMAGE,
            engine=None,
            image=None,
        ),
        strategy=Strategy.COPY_DATA,
        manifest=VolumeManifest(),
    )


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(resources=(_app_resource(),)),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_source_host(),  # type: ignore[arg-type]
        target_host=_target_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


def _mock_placement(respx_mock: respx.Router) -> None:
    respx_mock.get(f"{BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
    )
    respx_mock.get(f"{BASE}/projects/p1").mock(
        return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
    )
    respx_mock.get(f"{BASE}/servers/s2/destinations").mock(
        return_value=httpx.Response(200, json=[{"uuid": "d1", "network": "coolify"}])
    )


def _mock_multi_server_app(respx_mock: respx.Router) -> tuple[respx.Route, respx.Route]:
    """The source GET at CREATE: a server was attached after the plan. Returns
    (any application create, any PATCH of the source)."""
    respx_mock.get(f"{BASE}/applications/app1").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": "app1",
                "fqdn": "https://shop.example.com",
                "additional_servers_count": 1,
                "additional_servers": [{"name": "edge-2", "uuid": "s3"}],
            },
        )
    )
    create = respx_mock.post(url__startswith=f"{BASE}/applications").mock(
        return_value=httpx.Response(201, json={"uuid": "tgt1"})
    )
    park = respx_mock.patch(url__startswith=f"{BASE}/applications/app1").mock(
        return_value=httpx.Response(200, json={})
    )
    return create, park


class TestCreateRecheck:
    async def test_a_server_attached_after_the_plan_refuses_before_anything_is_touched(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_placement(respx_mock)
        respx_mock.get(f"{BASE}/version").mock(return_value=httpx.Response(200, text="4.2.1"))
        create, park = _mock_multi_server_app(respx_mock)

        with pytest.raises(PreflightError, match=r"1 additional server\(s\) \(edge-2\)") as exc:
            await steps.step_create_target(ctx)

        assert not create.called, "nothing may be created for a refused resource"
        assert not park.called, "the source's domains must not be parked"
        assert "never stopped" in (exc.value.hint or "")
        # The remedy is worded for the instance's own release (no route before 4.3).
        assert "POST /applications" not in str(exc.value)
        assert ctx.target_uuids == {}

    async def test_resources_created_earlier_in_the_run_are_deleted(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """A database made first is undone; the application is never made."""
        ctx.plan = _plan(
            resources=(
                ResourcePlan(snapshot=_snapshot(), strategy=Strategy.COPY_DATA),
                _app_resource(),
            )
        )
        _mock_placement(respx_mock)
        respx_mock.get(f"{BASE}/version").mock(return_value=httpx.Response(200, text="4.5.0"))
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1"})
        )
        respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))
        respx_mock.get(f"{BASE}/databases/db1/tags").mock(return_value=httpx.Response(200, json=[]))
        delete = respx_mock.delete(url__startswith=f"{BASE}/databases/db2").mock(
            return_value=httpx.Response(200, json={"message": "queued"})
        )
        create, park = _mock_multi_server_app(respx_mock)

        with pytest.raises(PreflightError, match="additional server"):
            await steps.step_create_target(ctx)

        assert delete.called, "the database created before the refusal must not be stranded"
        assert not create.called
        assert not park.called

    async def test_an_unreadable_version_still_refuses_with_the_newest_wording(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_placement(respx_mock)
        respx_mock.get(f"{BASE}/version").mock(return_value=httpx.Response(500, text="boom"))
        create, _ = _mock_multi_server_app(respx_mock)

        with pytest.raises(PreflightError, match="one-way"):
            await steps.step_create_target(ctx)
        assert not create.called

    async def test_a_single_server_application_proceeds_without_a_version_read(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """The common path costs no extra API call."""
        _mock_placement(respx_mock)
        version = respx_mock.get(f"{BASE}/version").mock(
            return_value=httpx.Response(200, text="4.5.0")
        )
        respx_mock.get(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "app1",
                    "build_pack": "dockerimage",
                    "docker_registry_image_name": "nginx",
                    "additional_servers_count": 0,
                },
            )
        )
        respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(201, json={"uuid": "tgt1"})
        )
        respx_mock.get(f"{BASE}/applications/app1/scheduled-tasks").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx_mock.get(f"{BASE}/applications/app1/envs").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx_mock.get(f"{BASE}/applications/app1/storages").mock(
            return_value=httpx.Response(200, json={"persistent_storages": [], "file_storages": []})
        )
        respx_mock.get(f"{BASE}/applications/app1/tags").mock(
            return_value=httpx.Response(200, json=[])
        )

        result = await steps.step_create_target(ctx)
        assert result["target_uuids"] == {"app1": "tgt1"}
        assert not version.called
