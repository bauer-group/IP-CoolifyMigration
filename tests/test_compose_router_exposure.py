"""A router written into a compose routes after FINALIZE only where the proxy serves it.

Coolify's Traefik runs with ``--providers.docker.exposedbydefault=false``
(proxy.php v4.1.2:351, v4.5.0:809, v4.7.2:809): it serves only containers
labelled ``traefik.enable=true``. The one such label Coolify writes for a
resource comes from fqdnLabelsForTraefik (docker.php v4.1.2:417, v4.7.2:1048),
which the parsers call only for a compose service that has a domain
(applicationParser v4.7.2:2179, serviceParser v4.7.2:3527). So once FINALIZE
releases a service's custom host, a Traefik router written into its compose
routes again only on a service that

* sets ``traefik.enable=true`` itself, or
* keeps a domain: a generated or server-bound host of a compose app's entry, a
  sub-application without a custom host, one Coolify regenerates (a service's
  own SERVICE_URL_*/FQDN_* declaration, serviceParser v4.7.2:2663,2743; a
  compose app's blank entry before 4.3.17, ``compose_blank_domain_regenerated``),
  or a SPECIFIC_SERVICES part, routed from env rows Coolify refills.

That is refused under keep and rename. A Traefik router on any other service is
a warning that says the plan cannot tell (an edited proxy may expose every
container), and one on a service that keeps no domain and sets
``traefik.enable=false`` routes nothing at all. caddy-docker-proxy has no such
switch: a Caddy site label always routes.
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
from bg_coolify_migrate.domain import coolify_versions as cv
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
from bg_coolify_migrate.domain.versions import Version
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
WILD = "app.0046-20.cloud.bauer-group.com"
SHOP = "https://shop.example.com"
RULE = "traefik.http.routers.shop.rule=Host(`shop.example.com`) && PathPrefix(`/api`)"
ENABLE = "traefik.enable=true"


def _service_block(name: str, labels: tuple[str, ...], env: tuple[str, ...]) -> str:
    out = f"  {name}:\n    image: nginx\n"
    if labels:
        out += "    labels:\n" + "".join(f'      - "{label}"\n' for label in labels)
    if env:
        out += "    environment:\n" + "".join(f'      - "{line}"\n' for line in env)
    return out


def _compose(**services: tuple[tuple[str, ...], tuple[str, ...]]) -> str:
    """``name=(labels, environment)`` per compose service."""
    return "services:\n" + "".join(
        _service_block(name, labels, env) for name, (labels, env) in services.items()
    )


def _app(compose: str, domains: dict[str, str] | None = None) -> dict[str, Any]:
    """A dockercompose application GET; ``web`` holds the custom host by default."""
    return {
        "uuid": "a1",
        "build_pack": "dockercompose",
        "docker_compose_raw": compose,
        "docker_compose_domains": json.dumps(
            {k: {"domain": v} for k, v in (domains or {"web": SHOP}).items()}
        ),
    }


def _service(compose: str, parts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A service GET; its sub-application ``web`` holds the custom host by default."""
    return {
        "uuid": "sv1",
        "docker_compose_raw": compose,
        "is_container_label_escape_enabled": True,
        "applications": parts
        if parts is not None
        else [{"name": "web", "fqdn": SHOP, "url": SHOP, "image": "nginx"}],
    }


def _routed(source: dict[str, Any], collection: str, version: Version | None = None) -> list[str]:
    return fidelity.compose_hosts_routed_after_release(
        source, collection=collection, source_wildcard=WILD, version=version
    )


def _unexposed(
    source: dict[str, Any], collection: str, version: Version | None = None
) -> tuple[list[str], list[str]]:
    return fidelity.compose_routes_unexposed(
        source, collection=collection, source_wildcard=WILD, version=version
    )


# ── traefik.enable, read the way Traefik reads it ────────────────────────────


class TestTraefikEnable:
    @pytest.mark.parametrize(
        ("labels", "expected"),
        [
            ("traefik.enable=true", True),
            ("traefik.enable=True", True),  # a YAML boolean, as the compose parser gives it
            ("traefik.enable=1", True),
            (" traefik.enable = TRUE ", True),
            ("traefik.enable=false", False),
            ("traefik.enable=0", False),
            ("traefik.enable=yes", None),  # strconv.ParseBool refuses it
            ("traefik.http.routers.x.rule=Host(`a.example.com`)", None),
            ("", None),
            # The last one wins, as in docker compose's list form.
            ("traefik.enable=true\ntraefik.enable=false", False),
            ("traefik.enable=false\ntraefik.enable=true", True),
        ],
    )
    def test_go_parse_bool(self, labels: str, expected: bool | None) -> None:
        assert fidelity._traefik_enable(labels) is expected

    def test_a_yaml_boolean_in_a_label_map(self) -> None:
        compose = (
            "services:\n  web:\n    image: nginx\n    labels:\n      traefik.enable: true\n"
            f"      traefik.http.routers.shop.rule: {RULE.split('=', 1)[1]}\n"
        )
        assert _routed(_app(compose), "applications") == ["shop.example.com"]


# ── a dockercompose application ──────────────────────────────────────────────


class TestComposeApplication:
    def test_a_router_on_a_service_left_without_a_domain_is_only_a_warning(self) -> None:
        source = _app(_compose(web=((RULE,), ())))
        assert _routed(source, "applications") == []
        assert _unexposed(source, "applications") == (["shop.example.com"], ["web"])

    def test_its_own_traefik_enable_keeps_it_served(self) -> None:
        source = _app(_compose(web=((ENABLE, RULE), ())))
        assert _routed(source, "applications") == ["shop.example.com"]
        assert _unexposed(source, "applications") == ([], [])

    def test_a_server_bound_host_left_on_the_service_keeps_it_served(self) -> None:
        source = _app(_compose(web=((RULE,), ())), {"web": f"{SHOP},https://web.{WILD}"})
        assert _routed(source, "applications") == ["shop.example.com"]

    def test_a_router_on_another_service_that_keeps_its_domain(self) -> None:
        source = _app(
            _compose(web=((), ()), worker=((RULE,), ())),
            {"web": SHOP, "worker": f"https://w.{WILD}"},
        )
        assert _routed(source, "applications") == ["shop.example.com"]

    def test_traefik_enable_false_without_a_domain_routes_nothing(self) -> None:
        source = _app(_compose(web=(("traefik.enable=false", RULE), ())))
        assert _routed(source, "applications") == []
        assert _unexposed(source, "applications") == ([], [])
        assert (
            fidelity.kept_source_compose_label_warnings(
                source, collection="applications", source_wildcard=WILD
            )
            == []
        )

    def test_a_domain_overrides_traefik_enable_false(self) -> None:
        # Coolify's own traefik.enable=true is merged on top of the compose's labels.
        source = _app(
            _compose(web=(("traefik.enable=false", RULE), ())), {"web": f"{SHOP},https://w.{WILD}"}
        )
        assert _routed(source, "applications") == ["shop.example.com"]

    def test_a_caddy_site_always_routes(self) -> None:
        source = _app(_compose(web=(("caddy=shop.example.com",), ())))
        assert _routed(source, "applications") == ["shop.example.com"]

    def test_a_host_regexp_on_an_unserved_service_is_a_warning(self) -> None:
        source = _app(_compose(web=(("traefik.http.routers.r.rule=HostRegexp(`^.+$`)",), ())))
        assert _routed(source, "applications") == []
        hosts, on = _unexposed(source, "applications")
        assert [h[:11] for h in hosts] == ["HostRegexp("] and on == ["web"]


class TestABlankEntryComesBackGenerated:
    """Before 4.3.17 a blanked entry is regenerated while the compose names it."""

    DECLARED = _compose(web=((RULE,), ("SERVICE_URL_WEB_3000",)))

    @pytest.mark.parametrize(
        ("version", "routed"),
        [((4, 3, 16), True), ((4, 1, 2), True), ((4, 3, 17), False), ((4, 7, 2), False)],
    )
    def test_by_release(self, version: Version, routed: bool) -> None:
        source = _app(self.DECLARED)
        assert bool(_routed(source, "applications", version)) is routed
        assert bool(_unexposed(source, "applications", version)[0]) is not routed

    def test_an_unknown_release_counts_as_regenerating(self) -> None:
        assert _routed(_app(self.DECLARED), "applications", None) == ["shop.example.com"]

    def test_the_gate(self) -> None:
        assert cv.supports("compose_blank_domain_regenerated", (4, 3, 16))
        assert not cv.supports("compose_blank_domain_regenerated", (4, 3, 17))
        assert not cv.supports("compose_blank_domain_regenerated", (4, 4, 0))
        assert cv.supports("compose_blank_domain_regenerated", None)

    def test_a_reference_in_another_service_s_value_names_it_too(self) -> None:
        compose = _compose(web=((RULE,), ()), worker=((), ("APP_URL=${SERVICE_FQDN_WEB}",)))
        assert _routed(_app(compose), "applications", (4, 3, 16)) == ["shop.example.com"]

    def test_another_service_s_variable_does_not(self) -> None:
        compose = _compose(web=((RULE,), ("SERVICE_URL_WORKER",)), worker=((), ()))
        assert _routed(_app(compose), "applications", (4, 3, 16)) == []

    def test_a_hyphenated_service_name(self) -> None:
        compose = _compose(**{"my-web": ((RULE,), ("SERVICE_FQDN_MY_WEB",))})
        assert _routed(_app(compose, {"my-web": SHOP}), "applications", (4, 3, 16)) == [
            "shop.example.com"
        ]

    def test_build_args_count(self) -> None:
        compose = (
            "services:\n  web:\n    build:\n      context: .\n      args:\n"
            "        PUBLIC: ${SERVICE_URL_WEB}\n"
            f'    labels:\n      - "{RULE}"\n'
        )
        assert _routed(_app(compose), "applications", (4, 3, 16)) == ["shop.example.com"]

    def test_no_entry_at_all_is_generated_on_every_release(self) -> None:
        # worker's router names web's released host; worker has no entry yet and
        # declares its own SERVICE_URL, so the next parse gives it a domain.
        compose = _compose(web=((), ()), worker=((RULE,), ("SERVICE_URL_WORKER",)))
        assert _routed(_app(compose), "applications", (4, 7, 2)) == ["shop.example.com"]


# ── a service ────────────────────────────────────────────────────────────────


class TestService:
    def test_a_part_that_declares_its_url_gets_a_generated_one_back(self) -> None:
        source = _service(_compose(web=((RULE,), ("SERVICE_URL_WEB_80",))))
        assert _routed(source, "services") == ["shop.example.com"]

    def test_a_reference_alone_does_not_regenerate_it(self) -> None:
        # The magic-reference path fills a null fqdn only while its env row is
        # missing (serviceParser v4.7.2:2822,2867), and the first parse made it.
        source = _service(_compose(web=((RULE,), ("APP_URL=${SERVICE_URL_WEB}",))))
        assert _routed(source, "services") == []
        assert _unexposed(source, "services") == (["shop.example.com"], ["web"])

    def test_a_part_without_a_custom_host_keeps_its_domain(self) -> None:
        parts = [
            {"name": "web", "fqdn": SHOP, "url": SHOP},
            {"name": "admin", "fqdn": f"https://admin.{WILD}", "url": f"https://admin.{WILD}"},
        ]
        source = _service(_compose(web=((), ()), admin=((RULE,), ())), parts)
        assert _routed(source, "services") == ["shop.example.com"]

    def test_a_specific_services_part_is_always_served(self) -> None:
        parts = [{"name": "web", "fqdn": SHOP, "url": SHOP, "image": "quay.io/minio/minio"}]
        source = _service(_compose(web=((RULE,), ())), parts)
        assert _routed(source, "services") == ["shop.example.com"]

    @pytest.mark.parametrize(("env", "routed"), [(("SERVICE_FQDN_WEB",), True), ((), False)])
    def test_a_resumed_release_finds_the_part_already_blank(
        self, env: tuple[str, ...], routed: bool
    ) -> None:
        # FINALIZE died after blanking web; the held host comes from the journal.
        parts = [{"name": "web", "fqdn": None, "url": None, "image": "nginx"}]
        source = _service(_compose(web=((RULE,), env)), parts)
        found = fidelity.compose_hosts_routed_after_release(
            source, collection="services", source_wildcard=WILD, held=["shop.example.com"]
        )
        assert bool(found) is routed

    @pytest.mark.parametrize(("version", "routed"), [((4, 3, 16), True), ((4, 7, 2), False)])
    def test_a_resumed_release_of_a_compose_application(
        self, version: Version, routed: bool
    ) -> None:
        source = _app(TestABlankEntryComesBackGenerated.DECLARED, {"web": ""})
        found = fidelity.compose_hosts_routed_after_release(
            source,
            collection="applications",
            source_wildcard=WILD,
            held=["shop.example.com"],
            version=version,
        )
        assert bool(found) is routed

    def test_a_compose_service_with_no_sub_application(self) -> None:
        # A ServiceDatabase has no fqdn: Coolify generates no router for it.
        source = _service(_compose(web=((), ()), db=((RULE,), ())))
        assert _routed(source, "services") == []
        assert _unexposed(source, "services") == (["shop.example.com"], ["db"])


# ── one host is never both refused and warned about ──────────────────────────


class TestReconciled:
    def test_a_host_served_somewhere_is_only_refused(self) -> None:
        source = _app(
            _compose(web=((RULE,), ()), worker=((RULE.replace("shop.rule", "w.rule"),), ())),
            {"web": SHOP, "worker": f"https://w.{WILD}"},
        )
        [refusal] = fidelity.kept_source_compose_label_refusals(
            source, collection="applications", source_wildcard=WILD
        )
        assert "route shop.example.com" in refusal
        assert _unexposed(source, "applications") == ([], [])
        assert (
            fidelity.kept_source_compose_label_warnings(
                source, collection="applications", source_wildcard=WILD
            )
            == []
        )

    def test_variable_rules_on_a_disabled_service_are_not_warned_about(self) -> None:
        variable = "traefik.http.routers.v.rule=Host(`${DOMAIN}`)"
        disabled = _app(_compose(web=(("traefik.enable=false", variable), ())))
        disabled["settings"] = {"is_container_label_escape_enabled": False}
        assert fidelity.compose_routes_unseen(
            disabled, collection="applications", source_wildcard=WILD
        ) == ([], [])
        served = _app(_compose(web=((ENABLE, variable), ())))
        served["settings"] = {"is_container_label_escape_enabled": False}
        assert fidelity.compose_routes_unseen(
            served, collection="applications", source_wildcard=WILD
        ) == (["Host(`${DOMAIN}`)"], [])


class TestWarningLine:
    def test_it_says_why_and_what_to_do(self) -> None:
        source = _app(_compose(web=((RULE,), ()), api=((RULE.replace("shop.rule", "a.rule"),), ())))
        [line] = fidelity.kept_source_compose_label_warnings(
            source, collection="applications", source_wildcard=WILD
        )
        assert "route shop.example.com" in line
        assert "compose services web, api, which keep no domain" in line
        assert "exposedbydefault=false" in line and "cannot tell" in line
        assert "compose file in git" in line and "--finalize delete" in line

    def test_a_service_names_its_compose(self) -> None:
        [line] = fidelity.kept_source_compose_label_warnings(
            _service(_compose(web=((RULE,), ()))), collection="services", source_wildcard=WILD
        )
        assert "compose service web, which keeps" in line
        assert "the source service's compose in Coolify" in line


# ── the planner asks with the plan's release ─────────────────────────────────


_SNAPSHOT = {
    "collection": "applications",
    "kind": ResourceKind.APP_GIT_COMPOSE,
    "build_pack": BuildPack.DOCKERCOMPOSE,
}


class TestPlannerWiring:
    async def test_an_unserved_router_warns_and_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_engine_planner import _plan_one_app

        plan = await _plan_one_app(
            monkeypatch, _app(_compose(web=((RULE,), ()))), version="4.7.2", snapshot=_SNAPSHOT
        )
        [resource] = plan.resources
        assert resource.kept_source_refusals == ()
        assert any("compose service web" in w for w in resource.kept_source_warnings)
        assert not plan.is_blocked  # the default policy, rename

    @pytest.mark.parametrize(("version", "blocked"), [("4.3.16", True), ("4.7.2", False)])
    async def test_the_release_decides_a_blank_entry(
        self, monkeypatch: pytest.MonkeyPatch, version: str, blocked: bool
    ) -> None:
        from tests.test_engine_planner import _plan_one_app

        plan = await _plan_one_app(
            monkeypatch,
            _app(TestABlankEntryComesBackGenerated.DECLARED),
            version=version,
            snapshot=_SNAPSHOT,
        )
        assert bool(plan.resources[0].kept_source_refusals) is blocked
        assert plan.is_blocked is blocked


# ── FINALIZE asks the same predicate, with the run's release ─────────────────


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


def _snapshot() -> ResourceSnapshot:
    return ResourceSnapshot(
        uuid="a1", name="shop", collection="applications", kind=ResourceKind.APP_GIT_COMPOSE
    )


class TestRelease:
    async def test_an_unserved_router_is_released(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=_app(_compose(web=((RULE,), ()))))
        )
        patch = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={})
        )
        outcome = await api_resources.release_source_domains(
            api, _snapshot(), source_wildcard=WILD, version=(4, 7, 2)
        )
        assert patch.called
        assert outcome.compose_routed == () and outcome.released is True

    @pytest.mark.parametrize(("version", "released"), [((4, 3, 16), False), ((4, 7, 2), True)])
    async def test_the_release_decides_a_blank_entry(
        self,
        api: CoolifyClient,
        respx_mock: respx.Router,
        version: Version,
        released: bool,
    ) -> None:
        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=_app(TestABlankEntryComesBackGenerated.DECLARED))
        )
        respx_mock.patch(f"{BASE}/applications/a1").mock(return_value=httpx.Response(200, json={}))
        outcome = await api_resources.release_source_domains(
            api, _snapshot(), source_wildcard=WILD, version=version
        )
        assert outcome.released is released


def _finalize_ctx(tmp_path: Path, api: CoolifyClient, version: str) -> MigrationContext:
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
        resources=(ResourcePlan(snapshot=_snapshot(), strategy=Strategy.RECREATE_ONLY),),
        finalize_policy=FinalizePolicy.KEEP,
        coolify_version=version,
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
    context.target_uuids["a1"] = "tT"
    return context


class TestFinalize:
    @pytest.mark.parametrize(("version", "released"), [("4.3.16", False), ("4.7.2", True)])
    async def test_step_finalize_asks_with_the_run_s_release(
        self,
        tmp_path: Path,
        api: CoolifyClient,
        respx_mock: respx.Router,
        version: str,
        released: bool,
    ) -> None:
        ctx = _finalize_ctx(tmp_path, api, version)
        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json=_app(TestABlankEntryComesBackGenerated.DECLARED))
        )
        respx_mock.get(f"{BASE}/applications/tT").mock(
            return_value=httpx.Response(
                200, json={"docker_compose_domains": json.dumps({"web": {"domain": SHOP}})}
            )
        )
        respx_mock.patch(f"{BASE}/applications/a1").mock(return_value=httpx.Response(200, json={}))
        # FINALIZE also arms the target (engine/deploy_triggers.arm_targets).
        respx_mock.patch(f"{BASE}/applications/tT").mock(return_value=httpx.Response(200, json={}))
        respx_mock.get(f"{BASE}/applications/a1/tags").mock(
            return_value=httpx.Response(200, json=[])
        )

        result = await steps.step_finalize(ctx)

        assert (result["domains_released"] == ["a1"]) is released
        routed_notices = [n for n in ctx.notices if "routers written into its compose" in n]
        assert bool(routed_notices) is not released
