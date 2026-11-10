"""FINALIZE never reports a source as released while its labels still route the hosts.

The plan refuses keep and rename for an application whose readonly labels are
off and whose labels route a host finalize moves (tests/test_kept_source_labels.py).
Readonly labels can still be switched off after the plan. FINALIZE therefore
re-reads the source at release time and asks the same predicate
(fidelity.hosts_routed_after_release): its domains are released all the same —
it then no longer holds the hosts for Coolify's conflict check and managed DNS —
but it is not listed as released, and a notice names the hosts and the remedy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import ResourcePlan, ResourceSnapshot, ServerRef, Strategy
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost
from tests.test_kept_source_labels import BASE, HOST, SHOP, WILD, _app, _plan, _snapshot


def _source_routes(respx_mock: respx.Router, source: dict[str, Any]) -> list[dict[str, Any]]:
    """Mock FINALIZE's reads and record the source PATCH bodies."""
    bodies: list[dict[str, Any]] = []

    def patch(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json={})

    respx_mock.get(f"{BASE}/applications/a1").mock(return_value=httpx.Response(200, json=source))
    respx_mock.get(f"{BASE}/applications/aT").mock(
        return_value=httpx.Response(200, json={"uuid": "aT", "fqdn": SHOP})
    )
    respx_mock.patch(f"{BASE}/applications/a1").mock(side_effect=patch)
    # FINALIZE also arms the target (engine/deploy_triggers.arm_targets): its
    # auto-deploy PATCH and the tag copy, neither of which this report is about.
    respx_mock.patch(f"{BASE}/applications/aT").mock(return_value=httpx.Response(200, json={}))
    respx_mock.get(f"{BASE}/applications/a1/tags").mock(return_value=httpx.Response(200, json=[]))
    return bodies


class TestReleaseOutcome:
    @pytest.fixture
    async def api(self):  # type: ignore[no-untyped-def]
        client = CoolifyClient(HOST, "tok", max_retries=0)
        yield client
        await client.aclose()

    async def test_hand_kept_labels_are_not_a_release(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import release_application_domains

        bodies = _source_routes(respx_mock, _app(readonly=False))
        outcome = await release_application_domains(api, _snapshot(), source_wildcard=WILD)
        # The domains go all the same: the source no longer holds the host for
        # Coolify's conflict check and managed DNS.
        assert bodies == [{"domains": f"https://x.{WILD}"}]
        assert outcome.ok is True
        assert outcome.still_routed == ("shop.example.com",)
        assert outcome.released is False

    @pytest.mark.parametrize("readonly", [True, None])
    async def test_managed_or_unreadable_labels_are(
        self, api: CoolifyClient, respx_mock: respx.Router, readonly: bool | None
    ) -> None:
        from bg_coolify_migrate.api.resources import release_application_domains

        _source_routes(respx_mock, _app(readonly=readonly))
        outcome = await release_application_domains(api, _snapshot(), source_wildcard=WILD)
        assert outcome.released is True and outcome.still_routed == ()

    async def test_a_compose_application_is_released_by_its_compose_domains(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Its labels are generated from docker_compose_domains at every deploy,
        # so hand-kept custom_labels never route anything.
        from bg_coolify_migrate.api.resources import release_application_domains

        source = _app(
            readonly=False,
            build_pack="dockercompose",
            docker_compose_domains=json.dumps({"web": {"domain": SHOP}}),
        )
        bodies = _source_routes(respx_mock, source)
        snapshot = _snapshot().model_copy(update={"kind": ResourceKind.APP_GIT_COMPOSE})
        outcome = await release_application_domains(api, snapshot, source_wildcard=WILD)
        assert bodies == [{"docker_compose_domains": [{"name": "web", "domain": ""}]}]
        assert outcome.released is True

    async def test_a_service_release_is_a_release(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import release_source_domains

        respx_mock.get(f"{BASE}/services/sv1").mock(
            return_value=httpx.Response(200, json={"uuid": "sv1", "applications": []})
        )
        snapshot = ResourceSnapshot(
            uuid="sv1", name="svc", collection="services", kind=ResourceKind.SERVICE_TEMPLATE
        )
        outcome = await release_source_domains(api, snapshot, source_wildcard=WILD)
        assert outcome.released is True


@pytest.fixture
async def finalize_ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    host = (
        FakeHost()
        .on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        .on(r"authorized_keys\.bak", exit_status=0)
        .on(r"rm -rf /root/\.coolify-migrate/", exit_status=0)
        # The containment checkpoints (engine/containment.py) find nothing started.
        .quiet_docker()
    )
    plan = _plan(FinalizePolicy.KEEP).model_copy(
        update={
            "source_server": ServerRef(uuid="s1", name="old", ip="10.0.0.1", wildcard_domain=WILD),
            "resources": (ResourcePlan(snapshot=_snapshot(), strategy=Strategy.RECREATE_ONLY),),
        }
    )
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=plan,
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=host,  # type: ignore[arg-type]
        target_host=host,  # type: ignore[arg-type]
    )
    context.target_uuids["a1"] = "aT"
    yield context
    await api.aclose()


class TestFinalizeReport:
    async def test_a_source_switched_to_hand_kept_labels_is_not_listed_released(
        self, finalize_ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        bodies = _source_routes(respx_mock, _app(readonly=False))

        result = await steps.step_finalize(finalize_ctx)

        assert bodies == [{"domains": f"https://x.{WILD}"}]
        assert result["domains_released"] == []
        [notice] = finalize_ctx.notices
        assert "labels still route shop.example.com" in notice
        assert "label management back to Coolify" in notice and "delete it" in notice

    async def test_managed_labels_are_listed_released(
        self, finalize_ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _source_routes(respx_mock, _app(readonly=True))

        result = await steps.step_finalize(finalize_ctx)

        assert result["domains_released"] == ["a1"]
        assert finalize_ctx.notices == []
