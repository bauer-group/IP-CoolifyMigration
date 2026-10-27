"""Plan-time refusals that keep a parked domain from being lost for good.

Two shapes the rollback cannot repair, so the plan refuses them up front:

* a source still on ``old-<tag>.`` hosts from an earlier run of this project —
  the tag is stable, so a new run would park the parked host again and journal
  it as the one to restore;
* a compose app (Coolify >= 4.3.15) whose domain routes to a port only the
  hidden ``domain_port_overrides`` knows — the release at FINALIZE erases it on
  the source too.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from bg_coolify_migrate.domain import domains, fidelity
from bg_coolify_migrate.engine.runner import make_migration_id
from tests.test_engine_planner import _plan_one_app

TAG = domains.parking_tag("shop", "production")


class TestParkingTag:
    def test_is_the_migration_id_digest(self) -> None:
        mid = make_migration_id("shop", "production", datetime(2026, 10, 27, tzinfo=UTC))
        assert mid == f"20261027-000000-{TAG}"

    def test_is_stable_per_project_and_environment(self) -> None:
        assert domains.parking_tag("shop", "production") == TAG
        assert domains.parking_tag("shop", "staging") != TAG
        assert len(TAG) == 6


class TestLeftoverParkedHosts:
    def test_finds_our_prefix_in_the_fqdn(self) -> None:
        fqdn = f"https://old-{TAG}.shop.example.com,https://api.example.com"
        assert domains.leftover_parked_hosts(fqdn, None, TAG) == [f"old-{TAG}.shop.example.com"]

    def test_finds_it_in_compose_domains(self) -> None:
        raw = json.dumps({"web": {"domain": f"https://old-{TAG}.shop.example.com"}})
        assert domains.leftover_parked_hosts(None, raw, TAG) == [f"old-{TAG}.shop.example.com"]

    @pytest.mark.parametrize(
        "fqdn",
        [
            "https://old-shop.example.com",  # somebody's real domain
            "https://old-abcdef.shop.example.com",  # another project's tag
            f"https://shop.old-{TAG}.example.com",  # not a prefix
            "",
            None,
        ],
    )
    def test_ignores_everything_else(self, fqdn: object) -> None:
        assert domains.leftover_parked_hosts(fqdn, None, TAG) == []


class TestLeftoverParkingRefusals:
    def test_an_application_is_refused_with_the_way_back(self) -> None:
        source = {"fqdn": f"https://old-{TAG}.shop.example.com"}
        [line] = domains.leftover_parking_refusals(source, name="shop", tag=TAG)
        assert f"still answers on old-{TAG}.shop.example.com" in line
        assert "Set its domain back to shop.example.com" in line

    def test_a_double_parked_host_points_back_to_the_real_one(self) -> None:
        source = {"fqdn": f"https://old-{TAG}.old-{TAG}.shop.example.com"}
        [line] = domains.leftover_parking_refusals(source, name="shop", tag=TAG)
        assert "Set its domain back to shop.example.com in Coolify" in line

    def test_a_service_application_is_checked_too(self) -> None:
        source = {
            "applications": [
                {"uuid": "sa1", "fqdn": f"https://old-{TAG}.wiki.example.com"},
                "junk",
                {"uuid": "sa2", "fqdn": None},
            ]
        }
        assert domains.leftover_parking_refusals(source, name="wiki", tag=TAG)

    def test_a_clean_source_is_not_refused(self) -> None:
        source = {"fqdn": "https://shop.example.com", "applications": []}
        assert domains.leftover_parking_refusals(source, name="shop", tag=TAG) == []


class TestThePlanRefuses:
    async def test_a_source_parked_by_an_earlier_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_one_app(monkeypatch, {"fqdn": f"https://old-{TAG}.shop.example.com"})
        assert plan.is_blocked
        assert any("parked by an earlier migration" in r for r in plan.resources[0].refusals)

    async def test_a_ported_compose_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bg_coolify_migrate.domain.kinds import GitAuth, ResourceKind

        plan = await _plan_one_app(
            monkeypatch,
            _compose_source(),
            snapshot={
                "collection": "applications",
                "kind": ResourceKind.APP_GIT_COMPOSE,
                "git_repository": "acme/shop",
                "git_branch": "main",
                "git_auth": GitAuth.PUBLIC,
            },
        )
        assert any("compose service domains route" in r for r in plan.resources[0].refusals)


def _compose_source(
    *,
    label_port: int = 3000,
    expose: list[Any] | None = None,
    domain: str = "https://shop.example.com",
    uuid: str = "a1",
    compose: str | None = None,
) -> dict[str, Any]:
    labels = [
        f"traefik.http.routers.https-0-{uuid}-web.rule=Host(`shop.example.com`)",
        f"traefik.http.services.https-0-{uuid}-web.loadbalancer.server.port={label_port}",
    ]
    doc = {"services": {"web": {"image": "x", "expose": expose or ["80"], "labels": labels}}}
    return {
        "uuid": uuid,
        "build_pack": "dockercompose",
        "docker_compose_domains": json.dumps({"web": {"domain": domain}}),
        "docker_compose": compose if compose is not None else json.dumps(doc),
    }


class TestComposeHiddenPortOverrides:
    def test_a_port_the_portless_domain_cannot_express(self) -> None:
        assert fidelity.compose_hidden_port_overrides(_compose_source()) == {"web": [3000]}

    def test_the_first_exposed_port_is_the_fallback(self) -> None:
        source = _compose_source(expose=["3000", "80"])
        assert fidelity.compose_hidden_port_overrides(source) == {}

    def test_a_port_written_in_the_domain_is_expressible(self) -> None:
        source = _compose_source(domain="https://shop.example.com:3000")
        assert fidelity.compose_hidden_port_overrides(source) == {}

    def test_an_operator_written_label_is_not_coolifys(self) -> None:
        # Not named http(s)-<n>-<uuid>: it lives in the compose itself and is
        # carried to the target with it.
        source = _compose_source(uuid="other")
        source["uuid"] = "a1"
        assert fidelity.compose_hidden_port_overrides(source) == {}

    def test_caddy_upstreams_count(self) -> None:
        doc = {
            "services": {
                "web": {
                    "image": "x",
                    "ports": ["8080:80/tcp"],
                    "labels": {"caddy_0.handle_path.0_reverse_proxy": "{{upstreams 3000}}"},
                }
            }
        }
        source = _compose_source(compose=json.dumps(doc))
        assert fidelity.compose_hidden_port_overrides(source) == {"web": [3000]}

    @pytest.mark.parametrize(
        ("expose", "expected"),
        [
            ([{"target": 3000, "protocol": "udp"}, {"target": 3000}], {}),
            (["53/udp", "3000/tcp"], {}),
            (["x", "3000"], {}),
        ],
    )
    def test_the_fallback_mirrors_coolify(self, expose: list[Any], expected: object) -> None:
        assert fidelity.compose_hidden_port_overrides(_compose_source(expose=expose)) == expected

    def test_no_fallback_port_hides_any_label_port(self) -> None:
        doc = {
            "services": {
                "web": {
                    "image": "x",
                    "labels": [
                        "traefik.http.services.https-0-a1-web.loadbalancer.server.port=3000"
                    ],
                }
            }
        }
        source = _compose_source(compose=json.dumps(doc))
        assert fidelity.compose_hidden_port_overrides(source) == {"web": [3000]}

    @pytest.mark.parametrize(
        "source",
        [
            {"build_pack": "dockerimage", "fqdn": "https://a.b"},
            {
                "build_pack": "dockercompose",
                "docker_compose_domains": json.dumps({"web": {"domain": ""}}),
            },
            {"build_pack": "dockercompose"},
        ],
    )
    def test_nothing_to_check(self, source: dict[str, Any]) -> None:
        assert fidelity.compose_hidden_port_overrides(source) == {}

    @pytest.mark.parametrize("compose", ["", "c2VydmljZXM6IFs=", "bm90IHlhbWw6IFs=", "%%%"])
    def test_an_unreadable_compose_is_unknown(self, compose: str) -> None:
        source = _compose_source()
        source["docker_compose"] = compose
        assert fidelity.compose_hidden_port_overrides(source) is None

    def test_a_base64_compose_is_read(self) -> None:
        import base64

        source = _compose_source()
        source["docker_compose"] = base64.b64encode(source["docker_compose"].encode()).decode()
        assert fidelity.compose_hidden_port_overrides(source) == {"web": [3000]}


class TestComposePortRules:
    # A UI save hides the port from 4.3.15, an API save from 4.3.18 — on the 4.3
    # line (4.3.24 shipped after 4.4.0) as much as in 4.4+.
    @pytest.mark.parametrize("version", [(4, 3, 15), (4, 3, 18), (4, 3, 24), (4, 4, 0), None])
    def test_refused_from_4_3_15(self, version: tuple[int, ...] | None) -> None:
        [line] = [
            r
            for r in fidelity.application_refusals(_compose_source(), version=version)
            if "compose service domains" in r
        ]
        assert "web -> [3000]" in line
        assert "rollback" in line and "parking" not in line

    def test_not_refused_before_4_3_15(self) -> None:
        refusals = fidelity.application_refusals(_compose_source(), version=(4, 3, 14))
        assert not [r for r in refusals if "compose service domains" in r]
        warnings = fidelity.application_warnings(_compose_source(compose=""), version=(4, 3, 14))
        assert not any("could not be read" in w for w in warnings)

    def test_an_unreadable_compose_warns_from_4_3_15(self) -> None:
        warnings = fidelity.application_warnings(_compose_source(compose=""), version=(4, 3, 15))
        assert any("could not be read" in w for w in warnings)


class TestComposeFallbackByRelease:
    """Where a portless compose domain routes changed twice on the 4.3 line."""

    def _source(self, expose: list[Any], ports_exposes: str) -> dict[str, Any]:
        source = _compose_source(expose=expose)
        source["ports_exposes"] = ports_exposes
        return source

    def test_4_3_15_routes_to_ports_exposes(self) -> None:
        # The label routes to 3000 = ports_exposes[0]: no override, nothing hidden.
        source = self._source(["80"], "3000")
        assert fidelity.compose_hidden_port_overrides(source, version=(4, 3, 15)) == {}
        assert fidelity.compose_hidden_port_overrides(source, version=(4, 3, 18)) == {
            "web": [3000]
        }

    def test_4_3_16_takes_any_protocol_then_ports_exposes(self) -> None:
        udp_first = self._source(["3000/udp"], "80")
        assert fidelity.compose_hidden_port_overrides(udp_first, version=(4, 3, 16)) == {}
        assert fidelity.compose_hidden_port_overrides(udp_first, version=(4, 3, 18)) == {
            "web": [3000]
        }
        label = "traefik.http.services.https-0-a1-web.loadbalancer.server.port=3000"
        no_port = _compose_source(
            compose=json.dumps({"services": {"web": {"image": "x", "labels": [label]}}})
        )
        no_port["ports_exposes"] = "3000"
        assert fidelity.compose_hidden_port_overrides(no_port, version=(4, 3, 17)) == {}
        assert fidelity.compose_hidden_port_overrides(no_port, version=(4, 3, 18)) == {
            "web": [3000]
        }

    def test_4_3_18_takes_the_first_tcp_port_only(self) -> None:
        source = self._source(["53/udp", "80"], "3000")
        assert fidelity.compose_hidden_port_overrides(source, version=(4, 3, 18)) == {
            "web": [3000]
        }

    def test_an_unreadable_compose_warns_instead(self) -> None:
        source = _compose_source(compose="")
        assert not fidelity.application_refusals(source, version=(4, 5, 0))
        warnings = fidelity.application_warnings(source, version=(4, 5, 0))
        assert any("docker_compose) could not be read" in w for w in warnings)

    def test_no_warning_when_the_compose_is_readable(self) -> None:
        warnings = fidelity.application_warnings(_compose_source(), version=(4, 5, 0))
        assert not any("could not be read" in w for w in warnings)
