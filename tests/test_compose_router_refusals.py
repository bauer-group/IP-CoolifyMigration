"""Routers written into a compose: refused where the plan can read the host.

A router the operator wrote into a compose outlives FINALIZE's release of the
domains (tests/test_compose_router_labels.py). The plan refuses keep and rename
for it, like hand-kept custom_labels, when it names a host finalize releases.
What the plan cannot read off the GET stays a warning that says so:

* a ``Host()`` built from a variable — docker compose fills it in at every
  deploy unless Coolify escapes ``$`` in the labels (escapeDollarSign,
  applicationParser v4.7.2:2172-2175, serviceParser v4.7.2:3520-3523; on by
  default, migration 2024_05_15_151236);
* a SPECIFIC_SERVICES part (minio, logto, garage, silo), routed from env rows
  the GET does not carry (generateServiceSpecificFqdns, docker.php
  v4.7.2:757-855).

The plan never shows one host as both.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.kinds import BuildPack, ResourceKind
from bg_coolify_migrate.domain.statemachine import FinalizePolicy

WILD = "app.0046-20.cloud.bauer-group.com"
SHOP = "https://shop.example.com"

LITERAL = "traefik.http.routers.shop.rule=Host(`shop.example.com`)"
VARIABLE = "traefik.http.routers.var.rule=Host(`${DOMAIN}`)"


def _compose(*labels: str, image: str = "nginx") -> str:
    lines = "".join(f'      - "{label}"\n' for label in labels)
    return f"services:\n  web:\n    image: {image}\n    labels:\n{lines}"


def _compose_app(*labels: str, escape: bool | None = None, **kw: Any) -> dict[str, Any]:
    """A dockercompose application GET whose ``web`` holds the custom host."""
    out: dict[str, Any] = {
        "uuid": "a1",
        "build_pack": "dockercompose",
        "docker_compose_raw": _compose(*labels),
        "docker_compose_domains": json.dumps(
            {"web": {"domain": SHOP}, "worker": {"domain": f"https://w.{WILD}"}}
        ),
    }
    if escape is not None:
        out["settings"] = {"is_container_label_escape_enabled": escape}
    return {**out, **kw}


def _service(
    *labels: str, escape: bool | None = True, image: str = "nginx", fqdn: str = SHOP
) -> dict[str, Any]:
    """A service GET; its sub-application ``web`` holds ``fqdn``."""
    out: dict[str, Any] = {
        "uuid": "sv1",
        "docker_compose_raw": _compose(*labels, image=image),
        "applications": [{"name": "web", "fqdn": fqdn, "url": fqdn, "image": image}],
    }
    if escape is not None:
        out["is_container_label_escape_enabled"] = escape
    return out


def _unseen(source: dict[str, Any], collection: str) -> tuple[list[str], list[str]]:
    return fidelity.compose_routes_unseen(source, collection=collection, source_wildcard=WILD)


# ── label escaping, read by key ──────────────────────────────────────────────


class TestLabelEscaping:
    @pytest.mark.parametrize(
        ("source", "collection", "expected"),
        [
            ({"is_container_label_escape_enabled": True}, "services", True),
            ({"is_container_label_escape_enabled": False}, "services", False),
            ({}, "services", None),
            ({"settings": {"is_container_label_escape_enabled": False}}, "applications", False),
            ({"settings": {"is_container_label_escape_enabled": True}}, "applications", True),
            # 4.1.x: no settings in the application GET; a top-level key is not its.
            ({"is_container_label_escape_enabled": True}, "applications", None),
            ({"settings": {"is_container_label_escape_enabled": 1}}, "applications", None),
        ],
    )
    def test_read_by_key_never_guessed(
        self, source: dict[str, Any], collection: str, expected: bool | None
    ) -> None:
        assert fidelity.label_escaping(source, collection=collection) is expected


# ── what the plan cannot read ────────────────────────────────────────────────


class TestComposeRoutesUnseen:
    @pytest.mark.parametrize("escape", [False, None])
    def test_a_variable_host_when_compose_fills_it_in(self, escape: bool | None) -> None:
        assert _unseen(_compose_app(VARIABLE, escape=escape), "applications") == (
            ["Host(`${DOMAIN}`)"],
            [],
        )

    def test_escaped_labels_route_no_variable(self) -> None:
        # `$` becomes `$$`: the proxy reads a literal `${DOMAIN}` and routes nothing.
        assert _unseen(_compose_app(VARIABLE, escape=True), "applications") == ([], [])

    def test_host_sni_and_caddy_site_variables(self) -> None:
        source = _service(
            "traefik.tcp.routers.t.rule=HostSNI(`$HOST`)",
            "caddy_0=https://app.${BASE}",
            escape=False,
        )
        variables, _ = _unseen(source, "services")
        assert variables == ["HostSNI(`$HOST`)", "caddy https://app.${BASE}"]

    def test_nothing_to_release_nothing_to_tell(self) -> None:
        source = _compose_app(
            VARIABLE, escape=False, docker_compose_domains=json.dumps({"web": {"domain": ""}})
        )
        assert _unseen(source, "applications") == ([], [])

    def test_only_compose_backed_sources(self) -> None:
        assert _unseen(_compose_app(VARIABLE, build_pack="nixpacks"), "applications") == ([], [])
        assert _unseen(_compose_app(VARIABLE), "databases") == ([], [])

    def test_a_route_env_part_s_other_hosts_cannot_be_matched(self) -> None:
        # minio is routed from MINIO_SERVER_URL / MINIO_BROWSER_REDIRECT_URL rows,
        # which finalize blanks and the GET does not carry.
        source = _service(
            LITERAL,
            "traefik.http.routers.s3.rule=Host(`s3.example.com`)",
            f"traefik.http.routers.own.rule=Host(`own.{WILD}`)",
            image="quay.io/minio/minio:latest",
        )
        # shop.example.com is a released domain: the refusal names it, not this.
        assert _unseen(source, "services") == ([], ["s3.example.com"])

    def test_a_route_env_part_s_host_regexp_with_no_domain_to_release(self) -> None:
        source = _service(
            "traefik.http.routers.r.rule=HostRegexp(`^.+$`)",
            image="minio/minio",
            fqdn=f"https://m.{WILD}",
        )
        variables, unmatched = _unseen(source, "services")
        assert variables == [] and [u[:11] for u in unmatched] == ["HostRegexp("]

    def test_a_plain_service_has_no_route_rows_to_wonder_about(self) -> None:
        source = _service(LITERAL, "traefik.http.routers.o.rule=Host(`other.example.com`)")
        assert _unseen(source, "services") == ([], [])


class TestOneHostNeverBothRefusedAndWarned:
    def test_the_refusal_and_the_warnings_name_different_things(self) -> None:
        source = _service(
            LITERAL,
            VARIABLE,
            "traefik.http.routers.s3.rule=Host(`s3.example.com`)",
            "traefik.http.routers.r.rule=HostRegexp(`^.+$`)",
            escape=False,
            image="quay.io/minio/minio",
        )
        [refusal] = fidelity.kept_source_compose_label_refusals(
            source, collection="services", source_wildcard=WILD
        )
        warnings = fidelity.kept_source_compose_label_warnings(
            source, collection="services", source_wildcard=WILD
        )
        assert "route shop.example.com, HostRegexp(" in refusal
        assert len(warnings) == 2
        joined = " ".join(warnings)
        assert "shop.example.com" not in joined and "HostRegexp" not in joined
        assert "${DOMAIN}" in joined and "s3.example.com" in joined
        assert "${DOMAIN}" not in refusal and "s3.example.com" not in refusal


class TestWarningLines:
    def test_a_variable_says_it_cannot_tell(self) -> None:
        [line] = fidelity.kept_source_compose_label_warnings(
            _compose_app(VARIABLE, escape=False), collection="applications", source_wildcard=WILD
        )
        assert "build their hosts from variables (Host(`${DOMAIN}`))" in line
        assert "its label escaping is off" in line
        assert "cannot tell" in line
        assert "compose file in git" in line and "--finalize delete" in line

    def test_an_unreadable_escape_setting_says_so(self) -> None:
        [line] = fidelity.kept_source_compose_label_warnings(
            _compose_app(VARIABLE), collection="applications", source_wildcard=WILD
        )
        assert "does not show whether its label escaping is on (Coolify 4.1" in line
        [line] = fidelity.kept_source_compose_label_warnings(
            _service(VARIABLE, escape=None), collection="services", source_wildcard=WILD
        )
        assert "does not show whether its label escaping is on" in line
        assert "Coolify 4.1" not in line

    def test_a_route_env_part_names_its_rows(self) -> None:
        source = _service(
            "traefik.http.routers.s3.rule=Host(`s3.example.com`)", image="quay.io/minio/minio"
        )
        [line] = fidelity.kept_source_compose_label_warnings(
            source, collection="services", source_wildcard=WILD
        )
        assert "route s3.example.com" in line
        assert "(MINIO_BROWSER_REDIRECT_URL, MINIO_SERVER_URL)" in line
        assert "cannot tell" in line and "source service's compose in Coolify" in line
        assert (
            fidelity.kept_source_compose_label_refusals(
                source, collection="services", source_wildcard=WILD
            )
            == []
        )


# ── the planner: refusals and warnings, both kept-source only ────────────────


class TestPlannerWiring:
    async def test_a_literal_router_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_engine_planner import _plan_one_app

        plan = await _plan_one_app(
            monkeypatch,
            _compose_app(LITERAL, escape=True),
            version="4.7.2",
            snapshot={
                "collection": "applications",
                "kind": ResourceKind.APP_GIT_COMPOSE,
                "build_pack": BuildPack.DOCKERCOMPOSE,
            },
        )
        [resource] = plan.resources
        assert len(resource.kept_source_refusals) == 1
        assert resource.kept_source_warnings == ()
        assert plan.is_blocked
        assert not plan.model_copy(update={"finalize_policy": FinalizePolicy.DELETE}).is_blocked

    async def test_a_variable_router_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_engine_planner import _plan_one_app

        plan = await _plan_one_app(
            monkeypatch,
            _compose_app(VARIABLE, escape=False),
            version="4.7.2",
            snapshot={
                "collection": "applications",
                "kind": ResourceKind.APP_GIT_COMPOSE,
                "build_pack": BuildPack.DOCKERCOMPOSE,
            },
        )
        [resource] = plan.resources
        assert resource.kept_source_refusals == ()
        [line] = resource.kept_source_warnings
        assert "cannot tell" in line
        assert not plan.is_blocked
        assert any("cannot tell" in w for w in plan.warnings)
        deleting = plan.model_copy(update={"finalize_policy": FinalizePolicy.DELETE})
        assert not any("cannot tell" in w for w in deleting.warnings)

    async def test_a_minio_service_warns_about_its_route_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_engine_planner import _plan_one_app

        plan = await _plan_one_app(
            monkeypatch,
            _service(
                "traefik.http.routers.s3.rule=Host(`s3.example.com`)", image="quay.io/minio/minio"
            ),
            version="4.7.2",
            snapshot={"collection": "services", "kind": ResourceKind.SERVICE_COMPOSE},
        )
        [resource] = plan.resources
        assert resource.kept_source_refusals == ()
        assert any("MINIO_SERVER_URL" in w for w in resource.kept_source_warnings)
