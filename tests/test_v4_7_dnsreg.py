"""Coolify 4.7: two plan warnings that need a little plan-time IO.

* From 4.7.0 every API create queues a DNS record for each hostname of the new
  resource (a service's URL PATCH did so on 4.6.0). CREATE carries no custom host,
  so these are the target's OWN hosts. Proxied by the token, they read ELSEWHERE at
  the DNS gate after the stop: the plan warns when the target's wildcard lies in a
  zone of the team (``GET /dns/zones``).

Both are warnings, and both are fail-soft: no data is never a warning.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.dns import managed
from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import ResourceSnapshot, ServerRef
from bg_coolify_migrate.engine import planner

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
