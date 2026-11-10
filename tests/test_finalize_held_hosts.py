"""FINALIZE asks the labels about the hosts the source held BEFORE the release.

FINALIZE releases a kept source's custom hosts with one domain write, then
checks whether anything the source deploys still routes them: hand-kept
custom_labels (readonly labels off) or a router written into its compose
(tests/test_finalize_label_report.py, tests/test_compose_router_labels.py). If
FINALIZE dies after that write, ``resume`` runs the release again — and the
source's GET no longer names the hosts, so a check of its CURRENT domains finds
nothing and lists it as released, while its labels still route them.

The hosts are therefore read off the restore body CREATE journalled before any
write (journal key ``parked_domains``; ``domains.body_hosts`` plus a service's
``route_envs``), also when nothing is left to release.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api import resources as api_resources
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, State
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
WILD = "app.0046-20.cloud.bauer-group.com"
SHOP = "https://shop.example.com"
LITERAL = "traefik.http.routers.shop.rule=Host(`shop.example.com`)"

#: An application whose FINALIZE domain PATCH went through: no custom host in
#: its fqdn any more, and its hand-kept labels (readonly off) still route one.
RELEASED_APP = {
    "uuid": "a1",
    "build_pack": "dockerimage",
    "fqdn": f"https://x.{WILD}",
    "custom_labels": "traefik.x.rule=Host(`shop.example.com`)",
    "settings": {"is_container_label_readonly_enabled": False},
}


def _compose(*labels: str, image: str = "nginx") -> str:
    lines = "".join(f'      - "{label}"\n' for label in labels)
    return f"services:\n  web:\n    image: {image}\n    labels:\n{lines}"


def _released_compose_app() -> dict[str, Any]:
    return {
        "uuid": "a1",
        "build_pack": "dockercompose",
        "docker_compose_raw": _compose(LITERAL),
        "docker_compose_domains": json.dumps({"web": {"domain": ""}}),
    }


def _service(*labels: str, image: str = "nginx", fqdn: str = SHOP) -> dict[str, Any]:
    return {
        "uuid": "sv1",
        "docker_compose_raw": _compose(*labels, image=image),
        "applications": [{"name": "web", "fqdn": fqdn, "url": fqdn, "image": image}],
        "is_container_label_escape_enabled": True,
    }


def _app_snapshot(kind: ResourceKind = ResourceKind.APP_DOCKERIMAGE) -> ResourceSnapshot:
    return ResourceSnapshot(uuid="a1", name="shop", collection="applications", kind=kind)


# ── the pure parts ───────────────────────────────────────────────────────────


class TestHeldBeforeRelease:
    def test_every_shape_of_restore_body(self) -> None:
        assert api_resources.held_before_release(
            {"domains": f"{SHOP},https://x.{WILD}", "noindex_domains": ["x"]}
        ) == ("shop.example.com", f"x.{WILD}")
        assert api_resources.held_before_release(
            {"docker_compose_domains": [{"name": "web", "domain": SHOP}]}
        ) == ("shop.example.com",)
        assert api_resources.held_before_release(
            {
                "urls": [{"name": "web", "url": SHOP}],
                "route_envs": [{"key": "MINIO_SERVER_URL", "url": "https://s3.example.com:9000"}],
            }
        ) == ("shop.example.com", "s3.example.com")

    @pytest.mark.parametrize("before", [None, {}, {"route_envs": "nope"}])
    def test_nothing_journalled(self, before: dict[str, Any] | None) -> None:
        assert api_resources.held_before_release(before) == ()


class TestPredicatesTakeTheHeldHosts:
    def test_hand_kept_labels_of_an_already_released_source(self) -> None:
        assert fidelity.hosts_routed_after_release(RELEASED_APP, source_wildcard=WILD) == []
        assert fidelity.hosts_routed_after_release(
            RELEASED_APP, source_wildcard=WILD, held=["shop.example.com", f"x.{WILD}"]
        ) == ["shop.example.com"]

    def test_managed_labels_never_route_a_held_host(self) -> None:
        # Readonly labels on: the release regenerated them without the host.
        source = {**RELEASED_APP, "settings": {"is_container_label_readonly_enabled": True}}
        assert (
            fidelity.hosts_routed_after_release(
                source, source_wildcard=WILD, held=["shop.example.com"]
            )
            == []
        )

    def test_compose_routers_of_an_already_released_source(self) -> None:
        source = _released_compose_app()
        assert (
            fidelity.compose_hosts_routed_after_release(
                source, collection="applications", source_wildcard=WILD
            )
            == []
        )
        assert fidelity.compose_hosts_routed_after_release(
            source, collection="applications", source_wildcard=WILD, held=["shop.example.com"]
        ) == ["shop.example.com"]

    def test_a_held_host_that_is_not_custom_is_never_asked_about(self) -> None:
        source = {
            **_released_compose_app(),
            "docker_compose_raw": _compose(f"traefik.http.routers.w.rule=Host(`w.{WILD}`)"),
        }
        assert (
            fidelity.compose_hosts_routed_after_release(
                source,
                collection="applications",
                source_wildcard=WILD,
                held=[f"w.{WILD}", "a1.10.0.0.1.sslip.io"],
            )
            == []
        )


# ── the release functions ────────────────────────────────────────────────────


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


def _patches(respx_mock: respx.Router, path: str) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def record(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json={})

    respx_mock.patch(f"{BASE}/{path}").mock(side_effect=record)
    return bodies


class TestAResumedRelease:
    async def test_hand_kept_labels_are_still_reported(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=RELEASED_APP)
        )
        bodies = _patches(respx_mock, "applications/a1")
        outcome = await api_resources.release_source_domains(
            api,
            _app_snapshot(),
            source_wildcard=WILD,
            before={"domains": f"{SHOP},https://x.{WILD}"},
        )
        assert bodies == []  # the PATCH went through before the crash
        assert outcome.ok and outcome.still_routed == ("shop.example.com",)
        assert outcome.released is False

    async def test_without_the_journal_it_could_not_tell(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Why the restore body is passed: the current fqdn no longer names the host.
        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=RELEASED_APP)
        )
        outcome = await api_resources.release_source_domains(
            api, _app_snapshot(), source_wildcard=WILD
        )
        assert outcome.released is True

    async def test_a_compose_application_s_routers_are_still_reported(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=_released_compose_app())
        )
        bodies = _patches(respx_mock, "applications/a1")
        outcome = await api_resources.release_source_domains(
            api,
            _app_snapshot(ResourceKind.APP_GIT_COMPOSE),
            source_wildcard=WILD,
            before={"docker_compose_domains": [{"name": "web", "domain": SHOP}]},
        )
        assert bodies == []
        assert outcome.compose_routed == ("shop.example.com",) and not outcome.released

    async def test_a_service_s_routers_are_still_reported(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/sv1").mock(
            return_value=httpx.Response(200, json=_service(LITERAL, fqdn=f"https://w.{WILD}"))
        )
        bodies = _patches(respx_mock, "services/sv1")
        snapshot = ResourceSnapshot(
            uuid="sv1", name="svc", collection="services", kind=ResourceKind.SERVICE_COMPOSE
        )
        outcome = await api_resources.release_source_domains(
            api, snapshot, source_wildcard=WILD, before={"urls": [{"name": "web", "url": SHOP}]}
        )
        assert bodies == []
        assert outcome.compose_routed == ("shop.example.com",) and not outcome.released


class TestRouteRowsAtRelease:
    async def test_a_router_naming_a_route_row_s_host_is_reported(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The plan only warns here (it reads no env rows); FINALIZE does read them.
        source = _service(
            "traefik.http.routers.s3.rule=Host(`s3.example.com`)",
            image="quay.io/minio/minio",
            fqdn=f"https://m.{WILD}",
        )
        respx_mock.get(f"{BASE}/services/sv1").mock(return_value=httpx.Response(200, json=source))
        respx_mock.get(f"{BASE}/services/sv1/envs").mock(
            return_value=httpx.Response(
                200, json=[{"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"}]
            )
        )
        bulk = respx_mock.patch(f"{BASE}/services/sv1/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        outcome = await api_resources.release_service_urls(api, "sv1", source_wildcard=WILD)
        assert bulk.called
        assert outcome.compose_routed == ("s3.example.com",) and not outcome.released


# ── the step reads the journalled restore body ───────────────────────────────


@pytest.fixture
async def finalize_ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    host = (
        FakeHost()
        .on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        .on(r"authorized_keys\.bak", exit_status=0)
        .on(r"rm -rf /root/\.coolify-migrate/", exit_status=0)
        # The containment checkpoints (engine/containment.py) find nothing started.
        .quiet_docker()
    )
    plan = MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1", wildcard_domain=WILD),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=(ResourcePlan(snapshot=_app_snapshot(), strategy=Strategy.RECREATE_ONLY),),
        finalize_policy=FinalizePolicy.KEEP,
    )
    context = MigrationContext(
        api=client,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=plan,
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=host,  # type: ignore[arg-type]
        target_host=host,  # type: ignore[arg-type]
    )
    context.target_uuids["a1"] = "aT"
    yield context
    await client.aclose()


class TestResumedFinalize:
    async def test_a_hand_labelled_source_is_not_listed_released(
        self, finalize_ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # CREATE journalled the source's original domains; the crash came after
        # FINALIZE's domain PATCH, so the source GET holds no custom host now.
        finalize_ctx.journal.append(
            "step_started",
            state=State.CREATE_TARGET.value,
            detail={
                "target_uuids": {"a1": "aT"},
                "parked_domains": {"a1": {"domains": f"{SHOP},https://x.{WILD}"}},
            },
        )
        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=RELEASED_APP)
        )
        respx_mock.get(f"{BASE}/applications/aT").mock(
            return_value=httpx.Response(200, json={"uuid": "aT", "fqdn": SHOP})
        )
        source_patches = _patches(respx_mock, "applications/a1")
        # FINALIZE also arms the target (engine/deploy_triggers.arm_targets).
        respx_mock.patch(f"{BASE}/applications/aT").mock(return_value=httpx.Response(200, json={}))
        respx_mock.get(f"{BASE}/applications/a1/tags").mock(
            return_value=httpx.Response(200, json=[])
        )

        result = await steps.step_finalize(finalize_ctx)

        assert source_patches == []
        assert result["domains_released"] == []
        [notice] = finalize_ctx.notices
        assert "labels still route shop.example.com" in notice
