"""Service domains at the DNS gate, and the URL rows copy_envs must not carry.

The gate used to read a service's SERVICE_FQDN_*/SERVICE_URL_* env rows — rows
Coolify derives from the sub-applications' fqdn on each parse, and which hold
whatever was copied until then. It blocked on hosts the target would not serve
and missed the ones it would. It now reads what the proxy is configured from.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.resources import build_env_entries, copy_envs
from bg_coolify_migrate.dns import extract
from bg_coolify_migrate.dns.extract import Hostname, HostnameOrigin
from bg_coolify_migrate.dns.gate import Resolution, bound_to_source
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import ResourcePlan, ServerRef, Strategy
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import DnsGateBlocked
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.test_engine_steps import _plan, _snapshot, _source_host, _target_host
from tests.test_service_domains import SRC_WC, TGT_WC, _service_plan
from tests.test_service_domains import _snapshot as _service_snapshot

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SOURCE_IP = "10.0.0.1"
TARGET_IP = "10.0.0.2"


# ── extraction ───────────────────────────────────────────────────────────────


class TestFromServiceApplications:
    def test_url_wins_and_lists_split(self) -> None:
        apps = [
            {
                "name": "web",
                "fqdn": "https://shop.example.com",
                "url": "https://shop.example.com:8443",
            },
            {"name": "api", "fqdn": "https://a.example.com,https://b.example.com/x"},
        ]
        hosts = extract.from_service_applications(apps)
        assert [h.host for h in hosts] == ["shop.example.com", "a.example.com", "b.example.com"]
        assert {h.origin for h in hosts} == {HostnameOrigin.SERVICE_APPLICATION}

    def test_generated_hosts_are_marked(self) -> None:
        (host,) = extract.from_service_applications([{"fqdn": "http://web-x.1.2.3.4.sslip.io"}])
        assert host.is_generated

    @pytest.mark.parametrize("junk", [None, "apps", {"name": "web"}, ["x", 3, {"fqdn": 5}]])
    def test_junk_yields_nothing(self, junk: object) -> None:
        assert extract.from_service_applications(junk) == []


class TestRouteEnv:
    def test_only_route_keys_are_read(self) -> None:
        envs: list[dict[str, object]] = [
            {"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"},
            {"key": "MINIO_BROWSER_REDIRECT_URL", "value": None},
            {"key": "SERVICE_URL_MINIO", "value": "https://other.example.com"},
            {"key": "APP_URL", "value": "https://app.example.com"},
        ]
        hosts = extract.from_service_route_env(envs)
        assert [(h.host, h.origin) for h in hosts] == [
            ("s3.example.com", HostnameOrigin.SERVICE_ROUTE_ENV)
        ]

    def test_route_keys_come_from_the_images_the_stack_runs(self) -> None:
        # Finding 5: Coolify routes from these keys only for a SPECIFIC_SERVICES
        # image (serviceType() compares the image up to its first ':').
        apps = [
            {"name": "s3", "image": "quay.io/minio/minio:RELEASE.2025-04-22"},
            {"name": "auth", "image": "svhd/logto"},
            {"name": "web", "image": "ghcr.io/acme/nextjs-app:1.0"},
            "junk",
            {"name": "x", "image": None},
        ]
        assert extract.route_env_keys(apps) == frozenset(
            {"MINIO_SERVER_URL", "MINIO_BROWSER_REDIRECT_URL", "LOGTO_ENDPOINT", "LOGTO_ADMIN_ENDPOINT"}
        )
        assert extract.route_env_keys([{"image": "dxflrs/garage:v1"}]) == frozenset(
            {"GARAGE_S3_API_URL", "GARAGE_WEB_URL", "GARAGE_ADMIN_URL"}
        )
        assert extract.route_env_keys("junk") == frozenset()

    def test_a_client_setting_on_another_image_is_not_a_route(self) -> None:
        envs: list[dict[str, object]] = [
            {"key": "LOGTO_ENDPOINT", "value": "https://auth.example.com"}
        ]
        assert extract.from_service_route_env(envs, frozenset()) == []

    def test_service_url_keys(self) -> None:
        assert extract.is_service_url_key("SERVICE_URL_WEB_80")
        assert extract.is_service_url_key("service_fqdn_web")
        assert not extract.is_service_url_key("SERVICE_PASSWORD_WEB")


class TestCollectService:
    def test_derived_rows_are_ignored_and_compose_labels_are_read(self) -> None:
        compose = "    labels:\n      - traefik.http.routers.x.rule=Host(`docs.example.com`)\n"
        hosts = extract.collect_service(
            applications=[
                {"name": "web", "url": "https://shop.example.com"},
                {"name": "auth", "image": "svhd/logto:1.27"},
            ],
            envs=[
                {"key": "SERVICE_URL_WEB", "value": "https://stale.example.com"},
                {"key": "LOGTO_ENDPOINT", "value": "https://auth.example.com"},
            ],
            compose=compose,
        )
        assert [h.host for h in hosts] == [
            "auth.example.com",
            "docs.example.com",
            "shop.example.com",
        ]

    def test_a_logto_endpoint_of_a_client_image_is_not_gated(self) -> None:
        # Finding 5: a Next.js app using the Logto SDK points LOGTO_ENDPOINT at a
        # Logto that stays on the source. Gating it would demand a cutover that
        # breaks that Logto.
        hosts = extract.collect_service(
            applications=[{"name": "web", "image": "ghcr.io/acme/nextjs-app:1.0"}],
            envs=[{"key": "LOGTO_ENDPOINT", "value": "https://auth.example.com"}],
        )
        assert hosts == []


class TestBoundToSource:
    def _h(self, host: str, origin: HostnameOrigin = HostnameOrigin.FQDN) -> Hostname:
        return Hostname(host=host, origin=origin, is_generated=False)

    def test_a_routed_host_under_the_source_wildcard_is_bound(self) -> None:
        hosts = [
            self._h(f"api.{SRC_WC}", HostnameOrigin.SERVICE_APPLICATION),
            self._h(f"api.{TGT_WC}", HostnameOrigin.SERVICE_APPLICATION),
            self._h("shop.example.com"),
        ]
        assert bound_to_source(hosts, source_wildcard=SRC_WC, target_wildcard=TGT_WC) == [hosts[0]]

    def test_a_derived_env_row_is_report_only(self) -> None:
        hosts = [self._h(f"api.{SRC_WC}", HostnameOrigin.SERVICE_ENV)]
        assert bound_to_source(hosts, source_wildcard=SRC_WC, target_wildcard=TGT_WC) == []

    def test_a_wildcard_both_servers_share_is_not_bound(self) -> None:
        hosts = [self._h(f"api.{SRC_WC}")]
        assert bound_to_source(hosts, source_wildcard=SRC_WC, target_wildcard=SRC_WC) == []

    def test_no_source_wildcard_binds_nothing(self) -> None:
        hosts = [self._h(f"api.{SRC_WC}")]
        assert bound_to_source(hosts, source_wildcard=None, target_wildcard=TGT_WC) == []


# ── copy_envs: a service's URL rows that name the source ─────────────────────


class TestServiceUrlRows:
    MINIO = frozenset({"MINIO_SERVER_URL", "MINIO_BROWSER_REDIRECT_URL"})

    def _keys(
        self,
        envs: list[dict[str, Any]],
        *,
        collection: str = "services",
        route_keys: frozenset[str] = MINIO,
    ) -> list[str]:
        entries = build_env_entries(
            envs, collection=collection, source_wildcard=SRC_WC, tag="t1", route_keys=route_keys
        )
        return [e["key"] for e in entries]

    def test_a_route_key_of_another_image_is_copied_verbatim(self) -> None:
        # Finding 5: Coolify neither routes nor regenerates it for that image;
        # dropping it would leave the target's client without its endpoint.
        envs = [{"key": "LOGTO_ENDPOINT", "value": f"https://auth.{SRC_WC}"}]
        assert self._keys(envs, route_keys=frozenset()) == ["LOGTO_ENDPOINT"]

    def test_rows_naming_the_source_are_left_to_the_target(self) -> None:
        envs = [
            {"key": "SERVICE_URL_ADMIN", "value": "http://admin-svc1.10.0.0.1.sslip.io"},
            {"key": "SERVICE_FQDN_API", "value": f"api.{SRC_WC}"},
            {"key": "SERVICE_URL_WEB_80", "value": "https://old-t1.shop.example.com"},
            {"key": "MINIO_SERVER_URL", "value": "https://minio-svc1.10.0.0.1.sslip.io"},
        ]
        assert self._keys(envs) == []

    def test_operator_chosen_values_and_other_rows_are_copied(self) -> None:
        envs = [
            # A reference-only row keeps its custom value forever: copy it.
            {"key": "SERVICE_URL_CALLBACK", "value": "https://auth.example.com/cb"},
            {"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"},
            # Magic placeholders resolve on the target.
            {"key": "SERVICE_FQDN_APP", "value": "$SERVICE_FQDN_APP"},
            # Not a URL row, whatever it looks like.
            {"key": "SERVICE_PASSWORD_WEB", "value": "x.sslip.io"},
            {"key": "SERVICE_URL_EMPTY", "value": ""},
            {"key": "SERVICE_URL_NULL", "value": None},
        ]
        assert self._keys(envs) == [
            "SERVICE_URL_CALLBACK",
            "MINIO_SERVER_URL",
            "SERVICE_FQDN_APP",
            "SERVICE_PASSWORD_WEB",
            "SERVICE_URL_EMPTY",
            "SERVICE_URL_NULL",
        ]

    def test_applications_are_unchanged(self) -> None:
        envs = [{"key": "SERVICE_FQDN_API", "value": f"https://api.{SRC_WC}"}]
        assert self._keys(envs, collection="applications") == ["SERVICE_FQDN_API"]

    def test_without_a_tag_only_parked_hosts_are_kept(self) -> None:
        envs = [{"key": "SERVICE_URL_WEB", "value": "https://old-t1.shop.example.com"}]
        entries = build_env_entries(envs, collection="services")
        assert [e["key"] for e in entries] == ["SERVICE_URL_WEB"]

    async def test_copy_envs_applies_the_filter(self, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/services/s1/envs").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"key": "SERVICE_PASSWORD_WEB", "value": "pw"},
                    {"key": "SERVICE_URL_WEB", "value": f"https://web.{SRC_WC}"},
                ],
            )
        )
        bulk = respx_mock.patch(f"{BASE}/services/s2/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            count = await copy_envs(
                api,
                collection="services",
                source_uuid="s1",
                target_uuid="s2",
                source_wildcard=SRC_WC,
                tag="t1",
            )
        assert count == 1
        assert "SERVICE_URL_WEB" not in bulk.calls[0].request.read().decode()


# ── the gate ─────────────────────────────────────────────────────────────────


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
    context.target_uuids["svc1"] = "tgt1"
    yield context
    await client.aclose()


def _mock_target(
    respx_mock: respx.Router,
    *,
    applications: list[dict[str, Any]],
    envs: list[dict[str, Any]] | None = None,
    compose: str | None = None,
) -> respx.Route:
    respx_mock.get(f"{BASE}/services/tgt1").mock(
        return_value=httpx.Response(
            200,
            json={"uuid": "tgt1", "applications": applications, "docker_compose_raw": compose},
        )
    )
    # Fetched — the route keys live there — but the SERVICE_* rows are ignored.
    return respx_mock.get(f"{BASE}/services/tgt1/envs").mock(
        return_value=httpx.Response(200, json=envs or [])
    )


def _resolver(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]) -> list[str]:
    """Resolve each host to its answer; record what the gate asked about."""
    asked: list[str] = []

    async def fake_resolve(
        hostnames: Iterable[Hostname], config: object = None
    ) -> list[Resolution]:
        out = []
        for h in hostnames:
            asked.append(h.host)
            out.append(Resolution(h, (answers[h.host],) if h.host in answers else ()))
        return out

    monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_all", fake_resolve)
    return asked


class TestServiceGate:
    async def test_blocks_on_a_sub_application_host_still_on_the_source(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envs_route = _mock_target(
            respx_mock,
            applications=[{"name": "web", "url": "https://shop.example.com:8443"}],
            envs=[{"key": "SERVICE_URL_WEB", "value": "https://stale.example.com"}],
        )
        asked = _resolver(monkeypatch, {"shop.example.com": SOURCE_IP})
        with pytest.raises(DnsGateBlocked, match=r"shop\.example\.com"):
            await steps.step_dns_gate(ctx)
        assert envs_route.called
        assert asked == ["shop.example.com"], "a derived env row must not be resolved"

    async def test_a_stale_env_host_does_not_block(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The env row still names the source's custom host (copied, or not yet
        # re-parsed); the sub-application routes the target wildcard. Only the
        # latter is served, and it is never resolved.
        _mock_target(
            respx_mock,
            applications=[{"name": "web", "url": f"https://web.{TGT_WC}"}],
            envs=[{"key": "SERVICE_URL_WEB", "value": "https://shop.example.com"}],
        )
        asked = _resolver(monkeypatch, {"shop.example.com": SOURCE_IP, f"web.{TGT_WC}": TARGET_IP})
        result = await steps.step_dns_gate(ctx)
        assert "shop.example.com" not in asked
        assert result["hostnames"] == 1

    async def test_a_minio_route_key_on_the_source_blocks(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_target(
            respx_mock,
            applications=[{"name": "minio", "fqdn": None, "image": "pgsty/silo:1.0"}],
            envs=[{"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"}],
        )
        _resolver(monkeypatch, {"s3.example.com": SOURCE_IP})
        with pytest.raises(DnsGateBlocked, match=r"s3\.example\.com"):
            await steps.step_dns_gate(ctx)

    async def test_a_custom_compose_label_host_is_gated(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_target(
            respx_mock,
            applications=[],
            compose="labels:\n  - traefik.http.routers.d.rule=Host(`docs.example.com`)\n",
        )
        _resolver(monkeypatch, {"docs.example.com": SOURCE_IP})
        with pytest.raises(DnsGateBlocked, match=r"docs\.example\.com"):
            await steps.step_dns_gate(ctx)

    async def test_a_template_service_compose_is_not_read(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.plan = _plan(
            resources=(
                ResourcePlan(
                    snapshot=_service_snapshot(
                        kind=ResourceKind.SERVICE_TEMPLATE,
                        service_type="minio",
                        docker_compose_raw=None,
                    ),
                    strategy=Strategy.COPY_DATA,
                ),
            )
        )
        _mock_target(
            respx_mock,
            applications=[],
            compose="labels:\n  - traefik.http.routers.d.rule=Host(`docs.example.com`)\n",
        )
        result = await steps.step_dns_gate(ctx)
        assert result["hostnames"] == 0

    async def test_a_host_bound_to_the_source_blocks_even_with_accept_dns(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Invariant 7: the wildcard binds it to the source for good. Not a
        # cutover, so --accept-dns (which the interactive wizard always sets)
        # does not wave it through.
        ctx.accept_dns = True
        _mock_target(respx_mock, applications=[{"name": "api", "fqdn": f"http://api.{SRC_WC}"}])
        asked = _resolver(monkeypatch, {})
        with pytest.raises(DnsGateBlocked, match=re.escape(f"api.{SRC_WC}")) as exc_info:
            await steps.step_dns_gate(ctx)
        assert "--accept-dns does not apply" in (exc_info.value.hint or "")
        assert asked == []


class TestBoundToSourceForApplications:
    async def test_an_fqdn_left_on_the_source_wildcard_blocks(
        self, tmp_path: Path, respx_mock: respx.Router
    ) -> None:
        plan = _plan(
            source_server=ServerRef(uuid="s1", name="old", ip=SOURCE_IP, wildcard_domain=SRC_WC),
            target_server=ServerRef(uuid="s2", name="new", ip=TARGET_IP, wildcard_domain=TGT_WC),
            resources=(ResourcePlan(snapshot=_snapshot(), strategy=Strategy.COPY_DATA),),
        )
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1", "fqdn": f"https://pdf.{SRC_WC}"})
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))
        async with CoolifyClient(HOST, "tok", max_retries=0) as client:
            context = MigrationContext(
                api=client,
                settings=Settings(_env_file=None, state_dir=tmp_path),
                plan=plan,
                journal=Journal.create(tmp_path, "m1"),
                migration_id="m1",
                source_host=_source_host(),  # type: ignore[arg-type]
                target_host=_target_host(),  # type: ignore[arg-type]
            )
            context.target_uuids["db1"] = "db1"
            with pytest.raises(DnsGateBlocked, match=re.escape(f"pdf.{SRC_WC}")):
                await steps.step_dns_gate(context)
