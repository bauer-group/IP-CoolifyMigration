"""Routers written into a compose outlive FINALIZE's release of the domains.

Each parser starts a compose service's labels from its own ``labels:`` and merges
the generated ones on top (applicationParser v4.7.2:1520,2170,2303; serviceParser
v4.7.2:2930,3518,3641; the same at v4.1.2 and v4.5.0), whatever the readonly
setting. Releasing a dockercompose application's docker_compose_domains, or a
service's sub-application URLs, therefore stops only the routers Coolify
generates: a ``Host()`` rule the operator wrote into the compose routes the moved
host again on the kept source's next deploy, next to the target.

Where the proxy still serves the router's container — a Caddy site label, or
a Traefik router on a service that sets traefik.enable=true or keeps a domain
(tests/test_compose_router_exposure.py) — the plan refuses keep and rename for
it, like hand-kept custom_labels (what it cannot read off the GET is warned
about: tests/test_compose_router_refusals.py), and FINALIZE asks the same
predicate of the GET its release is built from, so such a source is never
reported as released.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.kinds import BuildPack, ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
WILD = "app.0046-20.cloud.bauer-group.com"
SHOP = "https://shop.example.com"

LIST_COMPOSE = """\
services:
  web:
    image: nginx
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.shop.rule=Host(`shop.example.com`)"
  worker:
    image: busybox
"""

#: ``web`` keeps no domain once finalize releases shop.example.com, so its own
#: (YAML boolean) traefik.enable is what keeps Traefik serving the router.
MAP_COMPOSE = """\
services:
  web:
    image: nginx
    labels:
      traefik.enable: true
      traefik.http.routers.shop.rule: Host(`shop.example.com`) && PathPrefix(`/api`)
"""

#: The usual extra router on top of the generated one: no traefik.enable of its
#: own, so the release stops it (Coolify's Traefik serves only containers
#: labelled traefik.enable=true) - unless the proxy was edited, which the plan
#: cannot see: a warning, not a refusal.
UNEXPOSED_COMPOSE = MAP_COMPOSE.replace("      traefik.enable: true\n", "")

CADDY_COMPOSE = """\
services:
  web:
    image: nginx
    labels:
      caddy: shop.example.com
"""

PLAIN_COMPOSE = """\
services:
  web:
    image: nginx
    environment:
      - RULE=Host(`shop.example.com`)
"""


def _compose_app(compose: str | None = LIST_COMPOSE, **kw: Any) -> dict[str, Any]:
    """A dockercompose application GET with a custom and a server-bound domain."""
    return {
        "uuid": "a1",
        "build_pack": "dockercompose",
        "docker_compose_raw": compose,
        "docker_compose_domains": json.dumps(
            {"web": {"domain": SHOP}, "worker": {"domain": f"https://w.{WILD}"}}
        ),
        **kw,
    }


def _service(compose: str | None = LIST_COMPOSE, **kw: Any) -> dict[str, Any]:
    """A service GET whose sub-application ``web`` holds the custom host."""
    return {
        "uuid": "sv1",
        "docker_compose_raw": compose,
        "applications": [
            {"name": "web", "fqdn": SHOP, "url": SHOP},
            {"name": "worker", "fqdn": f"https://w.{WILD}"},
        ],
        **kw,
    }


def _routed(source: dict[str, Any], collection: str) -> list[str]:
    return fidelity.compose_hosts_routed_after_release(
        source, collection=collection, source_wildcard=WILD
    )


# ── the pure predicate ───────────────────────────────────────────────────────


class TestComposeHostsRoutedAfterRelease:
    @pytest.mark.parametrize("compose", [LIST_COMPOSE, MAP_COMPOSE, CADDY_COMPOSE])
    def test_a_router_in_a_compose_app_s_compose(self, compose: str) -> None:
        assert _routed(_compose_app(compose), "applications") == ["shop.example.com"]

    def test_a_router_in_a_service_s_compose(self) -> None:
        assert _routed(_service(), "services") == ["shop.example.com"]

    def test_a_router_traefik_no_longer_serves_is_a_warning(self) -> None:
        source = _compose_app(UNEXPOSED_COMPOSE)
        assert "traefik.enable" not in UNEXPOSED_COMPOSE
        assert _routed(source, "applications") == []
        assert fidelity.compose_routes_unexposed(
            source, collection="applications", source_wildcard=WILD
        ) == (["shop.example.com"], ["web"])
        assert (
            fidelity.kept_source_compose_label_refusals(
                source, collection="applications", source_wildcard=WILD
            )
            == []
        )
        [line] = fidelity.kept_source_compose_label_warnings(
            source, collection="applications", source_wildcard=WILD
        )
        assert "cannot tell" in line and "compose service web" in line

    def test_a_base64_compose_is_read(self) -> None:
        raw = base64.b64encode(LIST_COMPOSE.encode()).decode()
        assert _routed(_compose_app(raw), "applications") == ["shop.example.com"]

    def test_only_labels_count(self) -> None:
        # A Host() string anywhere else in the compose routes nothing.
        assert _routed(_compose_app(PLAIN_COMPOSE), "applications") == []

    def test_a_host_finalize_does_not_release_is_not_asked_about(self) -> None:
        compose = LIST_COMPOSE.replace("shop.example.com", f"w.{WILD}")
        assert _routed(_compose_app(compose), "applications") == []
        assert _routed(_service(compose), "services") == []

    def test_a_host_regexp_cannot_be_ruled_out(self) -> None:
        compose = LIST_COMPOSE.replace("Host(`shop.example.com`)", "HostRegexp(`^.+$`)")
        [entry] = _routed(_compose_app(compose), "applications")
        assert entry.startswith("HostRegexp(")

    @pytest.mark.parametrize("compose", [None, "", "services: [", "not: a compose"])
    def test_no_readable_compose(self, compose: str | None) -> None:
        assert _routed(_compose_app(compose), "applications") == []

    def test_nothing_to_release(self) -> None:
        source = _compose_app(docker_compose_domains=json.dumps({"web": {"domain": ""}}))
        assert _routed(source, "applications") == []

    def test_only_compose_backed_sources(self) -> None:
        # A non-compose application deploys custom_labels, never this compose;
        # a database has no domains.
        assert _routed(_compose_app(build_pack="nixpacks", fqdn=SHOP), "applications") == []
        assert _routed(_compose_app(), "databases") == []


class TestKeptSourceComposeLabelRefusals:
    def test_a_compose_application_is_fixed_in_git(self) -> None:
        [line] = fidelity.kept_source_compose_label_refusals(
            _compose_app(), collection="applications", source_wildcard=WILD
        )
        assert "routers written into its compose route shop.example.com" in line
        assert "compose file in git" in line and "--finalize delete" in line

    def test_a_service_is_fixed_in_its_compose(self) -> None:
        [line] = fidelity.kept_source_compose_label_refusals(
            _service(), collection="services", source_wildcard=WILD
        )
        assert "Remove those routers from the source service's compose" in line

    def test_nothing_routed_nothing_said(self) -> None:
        assert (
            fidelity.kept_source_compose_label_refusals(
                _compose_app(PLAIN_COMPOSE), collection="applications", source_wildcard=WILD
            )
            == []
        )


# ── the plan refuses it under keep and rename only ───────────────────────────


def _plan(policy: FinalizePolicy) -> MigrationPlan:
    snapshot = ResourceSnapshot(
        uuid="a1", name="shop", collection="applications", kind=ResourceKind.APP_GIT_COMPOSE
    )
    resource = ResourcePlan(
        snapshot=snapshot,
        strategy=Strategy.RECREATE_ONLY,
        kept_source_refusals=("routers written into its compose route shop.example.com",),
    )
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=(resource,),
        finalize_policy=policy,
    )


class TestPlan:
    @pytest.mark.parametrize("policy", [FinalizePolicy.KEEP, FinalizePolicy.RENAME])
    def test_refused_while_the_source_stays(self, policy: FinalizePolicy) -> None:
        plan = _plan(policy)
        assert plan.is_blocked
        assert plan.blocking_reasons == (
            "shop: routers written into its compose route shop.example.com",
        )

    def test_not_under_delete(self) -> None:
        plan = _plan(FinalizePolicy.DELETE)
        assert not plan.is_blocked
        assert not any("compose route" in w for w in plan.warnings)


class TestPlannerWiring:
    async def test_a_compose_application(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_engine_planner import _plan_one_app

        plan = await _plan_one_app(
            monkeypatch,
            _compose_app(),
            version="4.7.2",
            snapshot={
                "collection": "applications",
                "kind": ResourceKind.APP_GIT_COMPOSE,
                "build_pack": BuildPack.DOCKERCOMPOSE,
            },
        )
        [line] = plan.resources[0].kept_source_refusals
        assert "compose file in git" in line
        assert plan.resources[0].refusals == ()
        assert plan.is_blocked  # built with the default policy, rename
        assert not plan.model_copy(update={"finalize_policy": FinalizePolicy.DELETE}).is_blocked

    async def test_a_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_engine_planner import _plan_one_app

        plan = await _plan_one_app(
            monkeypatch,
            _service(),
            version="4.7.2",
            snapshot={"collection": "services", "kind": ResourceKind.SERVICE_COMPOSE},
        )
        assert any(
            "Remove those routers from the source service's compose" in w
            for w in plan.resources[0].kept_source_refusals
        )
        assert plan.resources[0].kept_source_warnings == ()


# ── FINALIZE asks the same predicate at release time ─────────────────────────


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


def _recording(respx_mock: respx.Router, path: str) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def record(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json={})

    respx_mock.patch(f"{BASE}/{path}").mock(side_effect=record)
    return bodies


class TestReleaseOutcome:
    async def test_a_compose_application_is_released_but_not_reported_so(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import release_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=_compose_app())
        )
        bodies = _recording(respx_mock, "applications/a1")
        snapshot = ResourceSnapshot(
            uuid="a1", name="shop", collection="applications", kind=ResourceKind.APP_GIT_COMPOSE
        )
        outcome = await release_application_domains(api, snapshot, source_wildcard=WILD)
        # The domains go all the same: the holder checks read nothing else.
        assert bodies == [
            {
                "docker_compose_domains": [
                    {"name": "web", "domain": ""},
                    {"name": "worker", "domain": f"https://w.{WILD}"},
                ]
            }
        ]
        assert outcome.ok and outcome.compose_routed == ("shop.example.com",)
        assert outcome.still_routed == () and outcome.released is False

    async def test_a_service_is_released_but_not_reported_so(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import release_source_domains

        respx_mock.get(f"{BASE}/services/sv1").mock(
            return_value=httpx.Response(200, json=_service())
        )
        bodies = _recording(respx_mock, "services/sv1")
        snapshot = ResourceSnapshot(
            uuid="sv1", name="svc", collection="services", kind=ResourceKind.SERVICE_COMPOSE
        )
        outcome = await release_source_domains(api, snapshot, source_wildcard=WILD)
        assert bodies == [{"urls": [{"name": "web", "url": ""}]}]
        assert outcome.compose_routed == ("shop.example.com",) and not outcome.released

    async def test_a_service_without_such_routers_is_released(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import release_service_urls

        respx_mock.get(f"{BASE}/services/sv1").mock(
            return_value=httpx.Response(200, json=_service(PLAIN_COMPOSE))
        )
        _recording(respx_mock, "services/sv1")
        outcome = await release_service_urls(api, "sv1", source_wildcard=WILD)
        assert outcome.released is True

    async def test_a_failed_service_release_is_no_release(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import release_service_urls

        respx_mock.get(f"{BASE}/services/sv1").mock(return_value=httpx.Response(500))
        outcome = await release_service_urls(api, "sv1", source_wildcard=WILD)
        assert outcome.ok is False and outcome.released is False


def _finalize_ctx(tmp_path: Path, api: CoolifyClient, snapshot: ResourceSnapshot) -> MigrationContext:
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
        resources=(ResourcePlan(snapshot=snapshot, strategy=Strategy.RECREATE_ONLY),),
        finalize_policy=FinalizePolicy.KEEP,
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
    context.target_uuids[snapshot.uuid] = "tT"
    return context


class TestFinalizeReport:
    async def test_a_compose_application_is_not_listed_released(
        self, tmp_path: Path, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        snapshot = ResourceSnapshot(
            uuid="a1", name="shop", collection="applications", kind=ResourceKind.APP_GIT_COMPOSE
        )
        ctx = _finalize_ctx(tmp_path, api, snapshot)
        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=_compose_app())
        )
        respx_mock.get(f"{BASE}/applications/tT").mock(
            return_value=httpx.Response(
                200, json={"docker_compose_domains": json.dumps({"web": {"domain": SHOP}})}
            )
        )
        _recording(respx_mock, "applications/a1")
        # FINALIZE also arms the target (engine/deploy_triggers.arm_targets).
        respx_mock.patch(f"{BASE}/applications/tT").mock(
            return_value=httpx.Response(200, json={})
        )
        respx_mock.get(f"{BASE}/applications/a1/tags").mock(
            return_value=httpx.Response(200, json=[])
        )

        result = await steps.step_finalize(ctx)

        assert result["domains_released"] == []
        [notice] = ctx.notices
        assert "routers written into its compose still route shop.example.com" in notice
        assert "compose file in git" in notice

    async def test_a_service_is_not_listed_released(
        self, tmp_path: Path, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        snapshot = ResourceSnapshot(
            uuid="sv1", name="svc", collection="services", kind=ResourceKind.SERVICE_COMPOSE
        )
        ctx = _finalize_ctx(tmp_path, api, snapshot)
        respx_mock.get(f"{BASE}/services/sv1").mock(
            return_value=httpx.Response(200, json=_service())
        )
        respx_mock.get(f"{BASE}/services/tT").mock(
            return_value=httpx.Response(200, json={"applications": [{"name": "web", "fqdn": SHOP}]})
        )
        _recording(respx_mock, "services/sv1")

        result = await steps.step_finalize(ctx)

        assert result["domains_released"] == []
        [notice] = ctx.notices
        assert "Remove those routers from the source's compose in Coolify" in notice
