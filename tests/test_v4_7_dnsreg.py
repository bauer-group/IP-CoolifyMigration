"""Coolify 4.7: two plan warnings that need a little plan-time IO.

* From 4.7.0 every API create queues a DNS record for each hostname of the new
  resource (a service's URL PATCH did so on 4.6.0). CREATE carries no custom host,
  so these are the target's OWN hosts. Proxied by the token, they read ELSEWHERE at
  the DNS gate after the stop: the plan warns when the target's wildcard lies in a
  zone of the team (``GET /dns/zones``).
* Registry logins live only in each server's Docker config, and nothing pulls an
  image before START. From 4.4.0 ``GET /servers/{uuid}/registries`` lists them,
  so the plan warns about an image whose registry the source server is logged in
  to and the target is not (a gap older than 4.7).

Both are warnings, and both are fail-soft: no data is never a warning.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.dns import managed
from bg_coolify_migrate.domain import fidelity, images
from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import ResourceSnapshot, ServerRef
from bg_coolify_migrate.engine import planner
from bg_coolify_migrate.errors import CoolifyApiError

TGT_WC = "https://*.app.tgt.example.com"
ZONES = [
    {
        "zone": "example.com",
        "account_name": "acme",
        "integration_token_uuid": "tok1",
        "integration_token_name": "main",
        "managed_records_count": 3,
    }
]


def _snapshot(collection: str = "applications", name: str = "api") -> ResourceSnapshot:
    kinds = {
        "applications": ResourceKind.APP_DOCKERIMAGE,
        "services": ResourceKind.SERVICE_TEMPLATE,
        "databases": ResourceKind.DATABASE,
    }
    return ResourceSnapshot(
        uuid=f"{name}-uuid", name=name, collection=collection, kind=kinds[collection]
    )


def _served(*collections: str) -> list[tuple[ResourceSnapshot, dict[str, Any], list[Any]]]:
    return [(_snapshot(c, f"r{i}"), {}, []) for i, c in enumerate(collections)]


def _target(wildcard: str = TGT_WC) -> ServerRef:
    return ServerRef(uuid="s2", name="new", ip="10.0.0.2", wildcard_domain=wildcard)


class TestFeatureGates:
    @pytest.mark.parametrize(
        ("version", "expected"), [((4, 6, 0), False), ((4, 7, 0), True), ((4, 7, 1), True)]
    )
    def test_api_create_queues_records_from_4_7_0(
        self, version: tuple[int, int, int], expected: bool
    ) -> None:
        assert supports("automatic_dns_on_api_create", version) is expected

    def test_an_unknown_version_does_not_count_as_4_7(self) -> None:
        assert supports("automatic_dns_on_api_create", None, if_unknown=False) is False

    @pytest.mark.parametrize(
        ("version", "expected"),
        [((4, 3, 24), False), ((4, 4, 0), True), ((4, 7, 1), True), (None, True)],
    )
    def test_the_registries_route_from_4_4_0(
        self, version: tuple[int, int, int] | None, expected: bool
    ) -> None:
        # Unknown fails toward checking; an older release then answers 404.
        assert supports("server_registry_logins", version) is expected


# ── 1. records for the target's own hosts ─────────────────────────────────────


class TestWhenCreateQueuesRecords:
    @pytest.mark.parametrize(
        ("collections", "version", "expected"),
        [
            ({"applications"}, (4, 7, 0), True),
            ({"services"}, (4, 7, 0), True),
            ({"databases"}, (4, 7, 0), False),
            ({"applications", "databases"}, (4, 7, 1), True),
            # 4.6.0: an application's create queues nothing, and its server-bound
            # hosts are already in place at the claim PATCH...
            ({"applications"}, (4, 6, 0), False),
            # ...but a service's URL PATCH adds the rewritten hosts.
            ({"services"}, (4, 6, 0), True),
            ({"services"}, (4, 5, 2), False),
            # Only a release KNOWN to do it: the note reads a 4.6.0 route.
            ({"applications", "services"}, None, False),
        ],
    )
    def test_by_release_and_kind(
        self, collections: set[str], version: tuple[int, int, int] | None, expected: bool
    ) -> None:
        assert managed.target_hosts_get_records(collections, version=version) is expected


class TestZonesHolding:
    def test_the_longest_parent_zone_wins(self) -> None:
        zones = [{"zone": "example.com"}, {"zone": "tgt.example.com"}, {"zone": "other.com"}]
        held = managed.zones_holding("app.tgt.example.com", zones)
        assert [row["zone"] for row in held] == ["tgt.example.com"]

    def test_the_zone_apex_itself_and_case(self) -> None:
        assert managed.zones_holding("Example.COM.", [{"zone": "example.com"}])

    def test_a_name_that_only_ends_alike_is_no_parent(self) -> None:
        assert managed.zones_holding("app.notexample.com", [{"zone": "example.com"}]) == []

    @pytest.mark.parametrize("zones", [None, "junk", [None, 3, {"zone": 5}, {"name": "x"}]])
    def test_junk_holds_nothing(self, zones: object) -> None:
        assert managed.zones_holding("app.example.com", zones) == []


class TestTargetRecordsNote:
    def test_a_wildcard_in_a_team_zone_is_named_with_its_remedy(self) -> None:
        note = managed.target_records_note(ZONES, target_wildcard=TGT_WC)
        assert note is not None
        assert "app.tgt.example.com" in note and "example.com" in note
        assert "'main'" in note
        assert "--accept-dns-host" in note and "proxied DNS off" in note

    @pytest.mark.parametrize(
        "wildcard", ["app.tgt.example.com", "*.app.tgt.example.com", "https://app.tgt.example.com"]
    )
    def test_every_stored_wildcard_shape(self, wildcard: str) -> None:
        assert managed.target_records_note(ZONES, target_wildcard=wildcard) is not None

    @pytest.mark.parametrize("wildcard", [None, ""])
    def test_no_wildcard_no_note(self, wildcard: str | None) -> None:
        assert managed.target_records_note(ZONES, target_wildcard=wildcard) is None

    def test_a_wildcard_in_no_zone_of_the_team(self) -> None:
        assert managed.target_records_note(ZONES, target_wildcard="*.app.tgt.other.net") is None

    def test_two_tokens_on_one_zone_queue_nothing(self) -> None:
        # findZones returns both rows; queueAutomaticRecords needs exactly one.
        zones = [*ZONES, {**ZONES[0], "integration_token_uuid": "tok2"}]
        assert managed.target_records_note(zones, target_wildcard=TGT_WC) is None

    def test_a_token_without_a_name(self) -> None:
        note = managed.target_records_note([{"zone": "example.com"}], target_wildcard=TGT_WC)
        assert note is not None and "integration token" not in note

    def test_no_secret_shaped_field_reaches_the_note(self) -> None:
        note = managed.target_records_note(ZONES, target_wildcard=TGT_WC)
        assert note is not None and "tok1" not in note and "acme" not in note


class _ZonesApi:
    def __init__(self, zones: object = None, *, fail: bool = False) -> None:
        self.zones = ZONES if zones is None else zones
        self.fail = fail
        self.read: list[str] = []

    async def get(self, path: str, **_params: object) -> object:
        self.read.append(path)
        if self.fail:
            raise RuntimeError("403")
        assert path == "/dns/zones"
        return self.zones


class TestPlannerTargetRecords:
    async def test_reads_the_zones_once_for_a_4_7_create(self) -> None:
        api = _ZonesApi()
        note = await planner.target_records_note(
            api,  # type: ignore[arg-type]
            _served("applications", "services"),
            target=_target(),
            version=(4, 7, 0),
        )
        assert note is not None and "app.tgt.example.com" in note
        assert api.read == ["/dns/zones"]

    async def test_a_4_6_service_is_read_too(self) -> None:
        api = _ZonesApi()
        note = await planner.target_records_note(
            api,  # type: ignore[arg-type]
            _served("services"),
            target=_target(),
            version=(4, 6, 0),
        )
        assert note is not None

    @pytest.mark.parametrize(
        ("collections", "version", "wildcard"),
        [
            (("applications",), (4, 6, 0), TGT_WC),
            (("applications",), None, TGT_WC),
            (("databases",), (4, 7, 0), TGT_WC),
            (("applications",), (4, 7, 0), ""),
        ],
    )
    async def test_no_read_where_no_record_can_be_queued(
        self,
        collections: tuple[str, ...],
        version: tuple[int, int, int] | None,
        wildcard: str,
    ) -> None:
        api = _ZonesApi()
        note = await planner.target_records_note(
            api,  # type: ignore[arg-type]
            _served(*collections),
            target=_target(wildcard),
            version=version,
        )
        assert note is None and api.read == []

    async def test_an_unreadable_zone_list_is_no_warning(self) -> None:
        note = await planner.target_records_note(
            _ZonesApi(fail=True),  # type: ignore[arg-type]
            _served("applications"),
            target=_target(),
            version=(4, 7, 0),
        )
        assert note is None


class TestBuildPlanWiring:
    async def test_the_note_reaches_the_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests import test_engine_planner as harness

        class Api(harness._PlanApi):
            async def get(self, path: str, **params: object) -> Any:
                if path == "/dns/zones":
                    return ZONES
                return await super().get(path, **params)

            async def get_server(self, uuid: str) -> dict[str, Any]:
                server = await super().get_server(uuid)
                server["settings"]["wildcard_domain"] = TGT_WC
                return server

        monkeypatch.setattr(harness, "_PlanApi", Api)
        plan = await harness._plan_one_app(monkeypatch, {}, version="4.7.0")
        assert any("app.tgt.example.com, the target server's wildcard" in n for n in plan.notes)
        assert not plan.is_blocked


# ── 2. registry logins the target lacks ──────────────────────────────────────

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SECRET_USER = "octocat-deploy"


class TestRegistryNames:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            # The key `docker login` writes for Docker Hub, and its aliases.
            ("https://index.docker.io/v1/", "docker.io"),
            ("registry-1.docker.io", "docker.io"),
            ("registry.hub.docker.com", "docker.io"),
            (" GHCR.io ", "ghcr.io"),
            ("https://registry.example.com:5000/v2/", "registry.example.com:5000"),
        ],
    )
    def test_normalised_like_normalize_registry(self, key: str, expected: str) -> None:
        assert images.normalise_registry(key) == expected

    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            ("ghcr.io/acme/api:1.2", "ghcr.io"),
            ("GHCR.IO/acme/api", "ghcr.io"),
            ("registry.example.com:5000/team/app@sha256:abc", "registry.example.com:5000"),
            ("localhost:5000/app", "localhost:5000"),
            ("acme/api:1", "docker.io"),
            ("postgres:16", "docker.io"),
            ("index.docker.io/acme/api", "docker.io"),
        ],
    )
    def test_the_registry_of_an_image(self, image: str, expected: str) -> None:
        assert images.registry_of(image) == expected

    @pytest.mark.parametrize(
        ("image", "official"),
        [
            ("postgres:16", True),
            ("redis", True),
            ("docker.io/library/redis:7", True),
            ("index.docker.io/library/redis", True),
            ("acme/api:1", False),
            ("ghcr.io/postgres", False),
            ("localhost/postgres", False),
        ],
    )
    def test_docker_official_images(self, image: str, official: bool) -> None:
        assert images.is_docker_official(image) is official


class TestRegistryLoginWarnings:
    def test_a_login_only_the_source_has_is_named_with_its_remedy(self) -> None:
        [line] = fidelity.registry_login_warnings(
            {"api": ["ghcr.io/acme/api:1.2"]}, {"ghcr.io"}, frozenset(), target_server="new"
        )
        assert line.startswith("api: pulls ghcr.io/acme/api:1.2 from ghcr.io")
        assert "'new'" in line and "Servers > new > Registries" in line
        assert "START" in line and "private" in line

    def test_the_remedy_never_calls_root_or_a_credential_store_enough(self) -> None:
        # An application's deploy pulls in a helper that mounts only the SSH user's
        # config.json (ApplicationDeploymentJob v4.7.0:3392-3418), so root's login
        # never reaches it: proceeding on one fails START after the stop.
        [line] = fidelity.registry_login_warnings(
            {"api": ["ghcr.io/acme/api"]}, {"ghcr.io"}, set(), target_server="new"
        )
        assert "as its SSH user" in line and "without sudo" in line
        assert "root's, does not count" in line
        assert "works too" not in line and "credsStore" not in line

    @pytest.mark.parametrize(
        ("ref", "checked"),
        [
            ("ghcr.io/acme/api:${TAG:-latest}", True),
            ("ghcr.io/${ORG}/api", True),
            ("${REGISTRY}/acme/api", False),
            ("$REGISTRY/acme/api", False),
            ("${IMAGE}", False),
        ],
    )
    def test_only_a_variable_in_the_registry_part_skips(self, ref: str, checked: bool) -> None:
        warnings = fidelity.registry_login_warnings(
            {"api": [ref]}, {"ghcr.io", "docker.io"}, set(), target_server="new"
        )
        assert bool(warnings) is checked

    def test_a_pushed_name_is_worded_as_a_push(self) -> None:
        [line] = fidelity.registry_login_warnings(
            {},
            {"ghcr.io"},
            set(),
            target_server="new",
            pushes={"api": ["ghcr.io/acme/api"]},
        )
        assert line.startswith("api: builds and pushes ghcr.io/acme/api to ghcr.io")
        assert "a failed push fails that deploy" in line and "without sudo" in line

    def test_a_target_logged_in_too_is_clean(self) -> None:
        assert (
            fidelity.registry_login_warnings(
                {"api": ["ghcr.io/acme/api"]},
                {"ghcr.io"},
                {"https://GHCR.IO"},
                target_server="new",
            )
            == []
        )

    @pytest.mark.parametrize(
        ("source", "target"), [(None, frozenset()), ({"ghcr.io"}, None), (frozenset(), set())]
    )
    def test_unknown_or_no_logins_are_no_warning(
        self, source: set[str] | None, target: set[str] | None
    ) -> None:
        warnings = fidelity.registry_login_warnings(
            {"api": ["ghcr.io/acme/api"]}, source, target, target_server="new"
        )
        assert warnings == []

    def test_docker_hub_checks_only_what_can_be_private(self) -> None:
        warnings = fidelity.registry_login_warnings(
            {"db": ["postgres:16"], "api": ["acme/private:3"], "env": ["${IMAGE}"]},
            {"https://index.docker.io/v1/"},
            set(),
            target_server="new",
        )
        [line] = warnings
        assert line.startswith("api: pulls acme/private:3 from docker.io")

    def test_one_line_per_resource_and_registry(self) -> None:
        warnings = fidelity.registry_login_warnings(
            {
                "stack": [
                    "ghcr.io/acme/web",
                    "ghcr.io/acme/worker",
                    "quay.io/acme/proxy",
                    "ghcr.io/acme/web",
                ],
                "other": ["registry.gitlab.com/x/y"],
            },
            {"ghcr.io", "quay.io", "registry.gitlab.com"},
            {"registry.gitlab.com"},
            target_server="new",
        )
        assert [w.split(" from ")[0] for w in warnings] == [
            "stack: pulls ghcr.io/acme/web, ghcr.io/acme/worker",
            "stack: pulls quay.io/acme/proxy",
        ]


@pytest.fixture
async def api() -> AsyncIterator[CoolifyClient]:
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


class TestClientRegistries:
    async def test_only_the_names_of_logins_leave_the_client(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/s1/registries").mock(
            return_value=httpx.Response(
                200,
                json={
                    "registries": [
                        {
                            "registry": "ghcr.io",
                            "logged_in": True,
                            "source": "auths",
                            "username": SECRET_USER,
                            "used_by": [{"type": "Application", "name": "api", "link": None}],
                        },
                        # Used by a resource, but no login holds it.
                        {"registry": "quay.io", "logged_in": False, "used_by": []},
                        {"registry": "Docker.IO", "logged_in": True, "source": "credHelpers"},
                        "junk",
                    ],
                    "error": None,
                },
            )
        )
        assert await api.get_server_registries("s1") == (frozenset({"ghcr.io", "docker.io"}), None)

    async def test_coolify_could_not_read_the_config(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/s1/registries").mock(
            return_value=httpx.Response(
                200,
                json={
                    "registries": [],
                    "error": "Could not read the Docker configuration on this server.",
                },
            )
        )
        assert await api.get_server_registries("s1") == (
            frozenset(),
            "Could not read the Docker configuration on this server.",
        )

    async def test_an_older_release_has_no_route(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/s1/registries").mock(return_value=httpx.Response(404))
        assert await api.get_server_registries("s1") is None

    async def test_a_body_without_the_list_is_an_error(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/s1/registries").mock(
            return_value=httpx.Response(200, json={"error": None})
        )
        with pytest.raises(CoolifyApiError, match="no registries list"):
            await api.get_server_registries("s1")


class TestPulledImages:
    def test_a_dockerimage_application_adds_its_image_name(self) -> None:
        snapshot = _snapshot("applications")
        full = {"docker_registry_image_name": " ghcr.io/acme/api "}
        assert planner.pulled_images(snapshot, full) == ("ghcr.io/acme/api",)

    def test_a_building_application_pushes_that_name_and_pulls_nothing(self) -> None:
        snapshot = _snapshot("applications").model_copy(update={"kind": ResourceKind.APP_GIT_BUILD})
        assert planner.pulled_images(snapshot, {"docker_registry_image_name": "x/y"}) == ()

    def test_a_database_and_a_compose_keep_resource_images(self) -> None:
        db = _snapshot("databases").model_copy(update={"image": "ghcr.io/acme/pg:16"})
        assert planner.pulled_images(db, {}) == ("ghcr.io/acme/pg:16",)
        compose = _snapshot("services").model_copy(
            update={
                "docker_compose_raw": (
                    "services:\n"
                    "  web:\n    image: ghcr.io/acme/web:1\n"
                    "  job:\n    image: ghcr.io/acme/web:1\n"
                )
            }
        )
        assert planner.pulled_images(compose, {}) == ("ghcr.io/acme/web:1",)


_BUILD = {"build_pack": "nixpacks", "docker_registry_image_name": "ghcr.io/acme/api"}


class TestPushedImageName:
    @pytest.mark.parametrize(
        ("version", "pushed"),
        [
            # CREATE carries the name, and the target's build pushes to it.
            ((4, 4, 0), "ghcr.io/acme/api"),
            ((4, 5, 2), "ghcr.io/acme/api"),
            # From 4.6 CREATE leaves it out; unknown counts as the newest.
            ((4, 6, 0), None),
            ((4, 7, 1), None),
            (None, None),
        ],
    )
    def test_carried_only_before_4_6(
        self, version: tuple[int, int, int] | None, pushed: str | None
    ) -> None:
        assert fidelity.pushed_image_name(_BUILD, version=version) == pushed

    @pytest.mark.parametrize("build_pack", ["dockerimage", "dockercompose"])
    def test_packs_that_push_nothing(self, build_pack: str) -> None:
        app = {**_BUILD, "build_pack": build_pack}
        assert fidelity.pushed_image_name(app, version=(4, 5, 2)) is None


class _RegistriesApi:
    def __init__(self, **by_server: object) -> None:
        self.by_server = by_server
        self.read: list[str] = []

    async def get_server_registries(self, uuid: str) -> object:
        self.read.append(uuid)
        answer = self.by_server.get(uuid)
        if isinstance(answer, Exception):
            raise answer
        return answer


_SOURCE = ServerRef(uuid="s1", name="old", ip="10.0.0.1")
_DOCKERIMAGE = [(_snapshot("applications"), {"docker_registry_image_name": "ghcr.io/acme/api"}, [])]


async def _notes(api: _RegistriesApi, version: tuple[int, int, int] | None = (4, 7, 0)) -> Any:
    return await planner.registry_login_notes(
        api,  # type: ignore[arg-type]
        _DOCKERIMAGE,  # type: ignore[arg-type]
        source=_SOURCE,
        target=_target(),
        version=version,
    )


class TestPlannerRegistryLogins:
    async def test_reads_both_servers_and_warns(self) -> None:
        api = _RegistriesApi(s1=(frozenset({"ghcr.io"}), None), s2=(frozenset(), None))
        [note] = await _notes(api)
        assert note.startswith("api: pulls ghcr.io/acme/api from ghcr.io")
        assert api.read == ["s1", "s2"]

    async def test_an_unknown_release_is_checked(self) -> None:
        api = _RegistriesApi(s1=(frozenset({"ghcr.io"}), None), s2=(frozenset(), None))
        assert len(await _notes(api, version=None)) == 1

    async def test_no_read_before_4_4(self) -> None:
        api = _RegistriesApi()
        assert await _notes(api, version=(4, 3, 24)) == []
        assert api.read == []

    async def test_a_source_without_logins_needs_no_target_read(self) -> None:
        api = _RegistriesApi(s1=(frozenset(), None))
        assert await _notes(api) == []
        assert api.read == ["s1"]

    @pytest.mark.parametrize(
        "target",
        [
            None,  # no route
            (frozenset(), "The server is not reachable. Validate the server to read its..."),
            RuntimeError("403"),
        ],
    )
    async def test_an_unreadable_target_is_no_warning(self, target: object) -> None:
        api = _RegistriesApi(s1=(frozenset({"ghcr.io"}), None), s2=target)
        assert await _notes(api) == []

    async def test_an_unreadable_source_is_no_warning(self) -> None:
        api = _RegistriesApi(s1=(frozenset({"ghcr.io"}), "Could not read the Docker config"))
        assert await _notes(api) == []
        assert api.read == ["s1"]

    async def test_nothing_pulled_reads_nothing(self) -> None:
        api = _RegistriesApi()
        build = _snapshot("applications").model_copy(update={"kind": ResourceKind.APP_GIT_BUILD})
        notes = await planner.registry_login_notes(
            api,  # type: ignore[arg-type]
            [(build, {}, [])],
            source=_SOURCE,
            target=_target(),
            version=(4, 7, 0),
        )
        assert notes == [] and api.read == []

    @pytest.mark.parametrize(("version", "warned"), [((4, 5, 2), True), ((4, 6, 0), False)])
    async def test_a_build_that_pushes_up_to_4_5(
        self, version: tuple[int, int, int], warned: bool
    ) -> None:
        api = _RegistriesApi(s1=(frozenset({"ghcr.io"}), None), s2=(frozenset(), None))
        build = _snapshot("applications").model_copy(update={"kind": ResourceKind.APP_GIT_BUILD})
        full = {"build_pack": "nixpacks", "docker_registry_image_name": "ghcr.io/acme/api"}
        notes = await planner.registry_login_notes(
            api,  # type: ignore[arg-type]
            [(build, full, [])],
            source=_SOURCE,
            target=_target(),
            version=version,
        )
        assert [n.split(" to ")[0] for n in notes] == (
            ["api: builds and pushes ghcr.io/acme/api"] if warned else []
        )

    async def test_two_resources_of_one_name_are_both_checked(self) -> None:
        api = _RegistriesApi(s1=(frozenset({"ghcr.io", "quay.io"}), None), s2=(frozenset(), None))
        app = _snapshot("applications")
        db = _snapshot("databases").model_copy(update={"image": "quay.io/acme/pg:16"})
        notes = await planner.registry_login_notes(
            api,  # type: ignore[arg-type]
            [(app, {"docker_registry_image_name": "ghcr.io/acme/api"}, []), (db, {}, [])],
            source=_SOURCE,
            target=_target(),
            version=(4, 7, 0),
        )
        assert [n.split(" from ")[0] for n in notes] == [
            "api: pulls ghcr.io/acme/api",
            "api: pulls quay.io/acme/pg:16",
        ]


class TestBuildPlanRegistryWiring:
    @pytest.mark.parametrize(("version", "warned"), [("4.7.0", True), ("4.3.24", False)])
    async def test_the_warning_reaches_the_plan(
        self, monkeypatch: pytest.MonkeyPatch, version: str, warned: bool
    ) -> None:
        from tests import test_engine_planner as harness

        read: list[str] = []

        class Api(harness._PlanApi):
            async def get_server_registries(self, uuid: str) -> object:
                read.append(uuid)
                return (frozenset({"ghcr.io"}) if uuid == "s1" else frozenset(), None)

        monkeypatch.setattr(harness, "_PlanApi", Api)
        plan = await harness._plan_one_app(
            monkeypatch, {"docker_registry_image_name": "ghcr.io/acme/api"}, version=version
        )
        assert any(n.startswith("api: pulls ghcr.io/acme/api") for n in plan.notes) is warned
        assert read == (["s1", "s2"] if warned else [])
        assert not plan.is_blocked
