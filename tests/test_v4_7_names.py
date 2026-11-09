"""Coolify 4.7.0 applies the UI's name and description rules to the API.

Every application, service and database create and update validates ``name``
with ValidationPatterns::nameRules (3-255 characters, NAME_PATTERN) and
``description`` with descriptionRules (at most 255, DESCRIPTION_PATTERN) from
v4.7.0 (commit 32541a32c); up to v4.6.0 the API took any string. CREATE replays
the source's values, so one written through the API before 4.7 422s the whole
target. The name is refused at plan time (the target cannot go by another name:
discovery keys on it), the description is left out with a warning.

The predicates mirror PHP exactly: /u is PCRE2_UTF + PCRE2_UCP, so ``\\s`` and
``\\p{L}``/``\\p{M}``/``\\p{N}`` are Unicode-aware, and TrimStrings is Laravel
12.70.1's Str::trim. Both were compared code point by code point against PHP
8.4.27; the sets pinned below are that output.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.resources import Placement, create_resource
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from tests.test_engine_planner import _PlanApi

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"

V460 = (4, 6, 0)
V470 = (4, 7, 0)


# ── the rules themselves ─────────────────────────────────────────────────────


class TestNameRule:
    @pytest.mark.parametrize(
        "name",
        [
            "n8n",
            "owner/repo:main-abc",
            "Müller & Co (prod)",
            "web #1, v2.0+",
            "a_b.c@d",
            "日本語 アプリ",
            "٣٤٥-api",  # Arabic-Indic digits: \p{Nd}
            "Ⅻ app",  # a letter number: \p{Nl}
            "app ½",  # \p{No}
            "e" + chr(0x0301) + "clair",  # a combining acute: \p{M}
            "my" + chr(0x3000) + "app",  # the ideographic space is \s under UCP
            "a" + chr(0x180E) + "bc",  # \h, although str.isspace() says no
            "x" * 255,
        ],
    )
    def test_accepted(self, name: str) -> None:
        assert fidelity.name_rule_problem(name) is None

    @pytest.mark.parametrize(
        ("name", "says"),
        [
            ("db", "is 2 character(s) long"),
            (" db ", "once Coolify trims its surrounding whitespace"),
            # Livewire skips TrimStrings, so the UI saves 'ab ' — the API trims it.
            ("ab ", "is 2 character(s) long once Coolify trims"),
            # U+3164 HANGUL FILLER is a letter, and Str::trim strips it anyway.
            (chr(0x3164) + "db", "is 2 character(s) long"),
            ("x" * 256, "is 256 character(s) long"),
            ("Karl's app", "\"'\" at position 5"),
            ("deploy!", "'!' at position 7"),
            ("a [b]", "'[' at position 3, ']' at position 5"),
            ("🚀 shop", "U+1F680 ROCKET at position 1"),
            # str.isspace() calls U+001C whitespace; PCRE2's \s does not.
            ("a" + chr(0x1C) + "bc", "U+001C at position 2"),
            # A soft hyphen is trimmed at the edges and refused inside.
            ("ab" + chr(0xAD) + "c", "U+00AD SOFT HYPHEN at position 3"),
            ("   ", "is blank"),
            ("", "is blank"),
            (42, "is not text"),
        ],
    )
    def test_refused(self, name: object, says: str) -> None:
        problem = fidelity.name_rule_problem(name)
        assert problem is not None
        assert says in problem

    def test_length_and_characters_are_both_reported(self) -> None:
        problem = fidelity.name_rule_problem("a!")
        assert problem == "is 2 character(s) long and contains '!' at position 2"

    def test_many_refused_characters_are_counted_not_listed(self) -> None:
        problem = fidelity.name_rule_problem("a!?=*[]{}")
        assert problem is not None
        assert problem.endswith("and 3 more")


class TestPhpSemantics:
    """Pinned from PHP 8.4.27 / PCRE2 10.44 (v4.7.0's base image), every code point."""

    def test_backslash_s_is_pcre2_ucp(self) -> None:
        php = {
            *range(0x09, 0x0E),
            0x20,
            0x85,
            0xA0,
            0x1680,
            0x180E,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
        }
        assert {ord(c) for c in fidelity._PCRE_SPACE} == php
        # Neither direction of str.isspace() matches.
        assert chr(0x1C).isspace() and chr(0x1C) not in fidelity._PCRE_SPACE
        assert not chr(0x180E).isspace() and chr(0x180E) in fidelity._PCRE_SPACE

    def test_the_trim_set_is_str_trim(self) -> None:
        php = [
            (0x0, 0x0), (0x9, 0xD), (0x20, 0x20), (0x85, 0x85), (0xA0, 0xA0),
            (0xAD, 0xAD), (0x34F, 0x34F), (0x61C, 0x61C), (0x115F, 0x1160),
            (0x1680, 0x1680), (0x17B4, 0x17B5), (0x180E, 0x180E), (0x2000, 0x200F),
            (0x2028, 0x2029), (0x202F, 0x202F), (0x205F, 0x2065), (0x206A, 0x206F),
            (0x2800, 0x2800), (0x3000, 0x3000), (0x3164, 0x3164), (0xFEFF, 0xFEFF),
            (0xFFA0, 0xFFA0), (0x1D159, 0x1D159), (0x1D173, 0x1D17A), (0xE0020, 0xE0020),
        ]  # fmt: skip
        expected = {cp for low, high in php for cp in range(low, high + 1)}
        assert {ord(c) for c in fidelity._TRIMMED} == expected

    def test_lengths_count_code_points_like_mb_strlen(self) -> None:
        # PHP's mb_strlen counts a decomposed é (e, then U+0301) as two characters.
        assert fidelity.name_rule_problem("e" + chr(0x0301)) is not None
        assert fidelity.name_rule_problem("e" + chr(0x0301) + "x") is None


class TestDescriptionRule:
    @pytest.mark.parametrize(
        "description",
        [None, "", "   ", "Karl's \"best\" app! (v2) a+b=c * @x / y & z?", "x" * 255],
    )
    def test_accepted(self, description: object) -> None:
        assert fidelity.description_rule_problem(description) is None

    @pytest.mark.parametrize(
        ("description", "says"),
        [
            ("Prod: main site", "':' at position 5"),
            ("see https://example.com", "':' at position 10"),
            ("build #1", "'#' at position 7"),
            ("x" * 256, "is 256 characters long"),
            (["a"], "is not text"),
        ],
    )
    def test_refused(self, description: object, says: str) -> None:
        problem = fidelity.description_rule_problem(description)
        assert problem is not None
        assert says in problem

    def test_the_warning_never_echoes_the_description(self) -> None:
        description = "token: s3cr3t-phrase-xyz"
        warning = fidelity.description_warning({"description": description}, version=V470)
        assert warning is not None
        assert "':' at position 6" in warning
        assert "s3cr3t" not in warning


# ── gated on the release that introduced it ──────────────────────────────────


class TestGate:
    def test_introduced_in_4_7_0(self) -> None:
        assert not supports("api_name_rules", V460)
        assert supports("api_name_rules", V470)
        assert supports("api_name_rules", (4, 7, 1))
        assert supports("api_name_rules", None)

    def test_a_bad_name_refuses_from_4_7_and_on_an_unknown_release(self) -> None:
        assert fidelity.name_refusals("db", version=V460) == []
        for version in (V470, None):
            [refusal] = fidelity.name_refusals("db", version=version)
            assert "is 2 character(s) long" in refusal
            assert "restart or redeploy" in refusal
            assert "plan again" in refusal

    def test_a_good_name_never_refuses(self) -> None:
        assert fidelity.name_refusals("Müller & Co (prod)", version=V470) == []

    def test_a_bad_description_is_unsendable_from_4_7_and_on_an_unknown_release(
        self,
    ) -> None:
        source = {"description": "Prod: main site"}
        assert fidelity.unsendable_description(source, version=V460) is None
        assert fidelity.description_warning(source, version=V460) is None
        for version in (V470, None):
            assert fidelity.unsendable_description(source, version=version)
            assert fidelity.description_warning(source, version=version)


# ── CREATE leaves a refused description out of the body ─────────────────────


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


_PLACEMENT = Placement(project_uuid="proj1", environment_name="production", server_uuid="srv2")

_CREATES: dict[str, tuple[str, ResourceSnapshot]] = {
    "application": (
        "/applications/dockerimage",
        ResourceSnapshot(
            uuid="a1", name="web", collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
        ),
    ),
    "service": (
        "/services",
        ResourceSnapshot(
            uuid="s1",
            name="minio",
            collection="services",
            kind=ResourceKind.SERVICE_COMPOSE,
            docker_compose_raw="services:\n  minio:\n    image: minio/minio",
        ),
    ),
    "database": (
        "/databases/postgresql",
        ResourceSnapshot(
            uuid="db1",
            name="postgres",
            collection="databases",
            kind=ResourceKind.DATABASE,
            engine=DatabaseEngine.POSTGRESQL,
            image="postgres:16",
        ),
    ),
}


class TestCreateBody:
    async def _body(
        self,
        api: CoolifyClient,
        respx_mock: respx.Router,
        kind: str,
        description: str,
        version: tuple[int, ...] | None,
    ) -> dict[str, Any]:
        path, snapshot = _CREATES[kind]
        route = respx_mock.post(f"{BASE}{path}").mock(
            return_value=httpx.Response(201, json={"uuid": "new"})
        )
        source = {
            "description": description,
            "build_pack": "dockerimage",
            "docker_registry_image_name": "nginx",
        }
        await create_resource(api, snapshot, _PLACEMENT, source, version=version)
        body: dict[str, Any] = json.loads(route.calls[0].request.read())
        return body

    @pytest.mark.parametrize("kind", sorted(_CREATES))
    @pytest.mark.parametrize(("version", "carried"), [(V460, True), (V470, False), (None, False)])
    async def test_a_refused_description_is_left_out_from_4_7(
        self,
        api: CoolifyClient,
        respx_mock: respx.Router,
        kind: str,
        version: tuple[int, ...] | None,
        carried: bool,
    ) -> None:
        body = await self._body(api, respx_mock, kind, "Prod: main site", version)
        assert ("description" in body) is carried
        assert body["name"] == _CREATES[kind][1].name

    @pytest.mark.parametrize("kind", sorted(_CREATES))
    async def test_a_description_that_fits_is_carried(
        self, api: CoolifyClient, respx_mock: respx.Router, kind: str
    ) -> None:
        body = await self._body(api, respx_mock, kind, "Karl's shop (prod)", V470)
        assert body["description"] == "Karl's shop (prod)"


# ── the plan refuses the name, warns about the description ───────────────────


_SHAPES: dict[str, dict[str, Any]] = {
    "applications": {"collection": "applications", "kind": ResourceKind.APP_DOCKERIMAGE},
    "services": {
        "collection": "services",
        "kind": ResourceKind.SERVICE_TEMPLATE,
        "service_type": "minio",
    },
    "databases": {
        "collection": "databases",
        "kind": ResourceKind.DATABASE,
        "engine": DatabaseEngine.POSTGRESQL,
        "image": "postgres:16",
    },
}


async def _plan_named(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    collection: str = "applications",
    description: str | None = None,
    version: str | Exception = "4.7.0",
) -> Any:
    """build_plan over one resource called ``name``, everything but the rules stubbed."""
    from bg_coolify_migrate.domain.manifest import VolumeManifest
    from bg_coolify_migrate.engine import planner

    full: dict[str, Any] = {"uuid": "r1", "name": name, "description": description}
    if collection == "databases":
        full["type"] = "standalone-postgresql"

    async def fake_find_project(api: Any, project: str) -> dict[str, Any]:
        return {"uuid": "p1", "name": "shop"}

    async def fake_find_server(api: Any, server: str) -> dict[str, Any]:
        return {"uuid": "s2", "name": server}

    async def fake_env_resources(api: Any, project_uuid: str, environment: str) -> Any:
        return [(collection, {"uuid": "r1", "name": name})]

    async def fake_snapshot(api: Any, host: Any, **kw: Any) -> Any:
        return ResourceSnapshot(uuid="r1", name=name, **_SHAPES[collection]), [], full

    async def fake_resolve_server(api: Any, resource: Any) -> dict[str, Any]:
        return {"uuid": "s1", "name": "old", "ip": "10.0.0.1", "settings": {}}

    async def fake_manifest(host: Any, **kw: Any) -> VolumeManifest:
        return VolumeManifest()

    async def nothing(*args: Any, **kw: Any) -> Any:
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
        _PlanApi(version=version, envs=[]),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        project="shop",
        environment="production",
        target_server="new",
    )


class TestPlan:
    @pytest.mark.parametrize("collection", sorted(_SHAPES))
    async def test_a_bad_name_refuses_every_kind_on_4_7(
        self, monkeypatch: pytest.MonkeyPatch, collection: str
    ) -> None:
        plan = await _plan_named(monkeypatch, "db", collection=collection)
        assert plan.is_blocked
        assert any("Coolify >= 4.7" in r for r in plan.resources[0].refusals)

    async def test_4_6_0_takes_any_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_named(monkeypatch, "Karl's app!", version="4.6.0")
        assert not plan.is_blocked

    async def test_an_unreadable_version_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_named(monkeypatch, "🚀 shop", version=RuntimeError("down"))
        assert plan.is_blocked

    async def test_a_good_name_plans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_named(monkeypatch, "Müller & Co (prod)")
        assert not plan.is_blocked

    @pytest.mark.parametrize("collection", sorted(_SHAPES))
    async def test_a_bad_description_warns_and_never_blocks(
        self, monkeypatch: pytest.MonkeyPatch, collection: str
    ) -> None:
        plan = await _plan_named(
            monkeypatch, "web", collection=collection, description="Prod: main site"
        )
        assert not plan.is_blocked
        [line] = [w for w in plan.warnings if "created without a description" in w]
        assert line.startswith("web: the description contains ':' at position 5")
        assert "main site" not in line

    async def test_4_6_0_carries_the_description_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_named(
            monkeypatch, "web", description="Prod: main site", version="4.6.0"
        )
        assert not any("description" in w for w in plan.warnings)
