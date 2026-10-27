"""Service domains: carried to the target, released at FINALIZE, never parked.

A service's domains live on its sub-applications (``applications[].fqdn``/``url``),
which is what the target's proxy labels are generated from. Before this, CREATE
carried none of them: the target came up on parse-generated sslip hosts, the
custom domains moved nowhere, and the source kept every host it ever had.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api import resources
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.resources import Placement
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import ResourcePlan, ResourceSnapshot, ServerRef, Strategy
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import CoolifyApiError, UnsupportedResource
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost
from tests.test_engine_steps import _plan, _source_host, _target_host

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SRC_WC = "app.0046-20.cloud.bauer-group.com"
TGT_WC = "app.0047-20.cloud.bauer-group.com"
SSLIP = "http://admin-svc1.10.0.0.1.sslip.io"


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


def _snapshot(**kw: object) -> ResourceSnapshot:
    base: dict[str, Any] = {
        "uuid": "svc1",
        "name": "shop",
        "collection": "services",
        "kind": ResourceKind.SERVICE_COMPOSE,
        "docker_compose_raw": "services:\n  web:\n    image: nginx",
    }
    return ResourceSnapshot(**{**base, **kw})


def _placement() -> Placement:
    return Placement(
        project_uuid="p1",
        environment_name="production",
        server_uuid="s2",
        source_wildcard=SRC_WC,
        target_wildcard=TGT_WC,
    )


def _source(**kw: object) -> dict[str, Any]:
    """web: custom with a port (4.5 shape), api: server-bound, admin: generated."""
    base: dict[str, Any] = {
        "uuid": "svc1",
        "applications": [
            {
                "uuid": "sa-web",
                "name": "web",
                "fqdn": "https://shop.example.com",
                "url": "https://shop.example.com:8443",
            },
            {"uuid": "sa-api", "name": "api", "fqdn": f"http://api.{SRC_WC}/v1"},
            {"uuid": "sa-admin", "name": "admin", "fqdn": SSLIP},
        ],
    }
    return {**base, **kw}


def _target_get(names: tuple[str, ...] = ("web", "api", "admin")) -> httpx.Response:
    return httpx.Response(
        200, json={"uuid": "tgt1", "applications": [{"uuid": f"t-{n}", "name": n} for n in names]}
    )


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.read())


# ── pure helpers ─────────────────────────────────────────────────────────────


class TestServiceAppUrls:
    def test_url_wins_over_fqdn_because_it_keeps_the_hidden_port(self) -> None:
        assert resources.service_app_urls(_source())["web"] == "https://shop.example.com:8443"

    def test_fqdn_is_read_when_url_is_absent_or_null(self) -> None:
        source = {
            "applications": [
                {"name": "a", "fqdn": "https://a.example.com"},
                {"name": "b", "fqdn": "https://b.example.com", "url": None},
            ]
        }
        assert resources.service_app_urls(source) == {
            "a": "https://a.example.com",
            "b": "https://b.example.com",
        }

    def test_junk_and_nameless_entries_are_skipped(self) -> None:
        source = {
            "applications": [
                "junk",
                {"fqdn": "https://nameless.example.com"},
                {"name": "", "fqdn": "https://blank.example.com"},
                {"name": "empty", "fqdn": ""},
                {"name": "numeric", "fqdn": 5},
            ]
        }
        assert resources.service_app_urls(source) == {}

    def test_parts_are_normalised(self) -> None:
        source = {"applications": [{"name": "a", "fqdn": " https://a.com , ,https://b.com "}]}
        assert resources.service_app_urls(source) == {"a": "https://a.com,https://b.com"}

    def test_no_applications_key(self) -> None:
        assert resources.service_app_urls({}) == {}


class TestServiceUrlsBody:
    def _body(self, source: dict[str, Any], **kw: Any) -> Any:
        return resources.service_urls_body(
            source,
            target_names=kw.pop("target_names", {"web", "api", "admin"}),
            source_wildcard=kw.pop("source_wildcard", SRC_WC),
            target_wildcard=kw.pop("target_wildcard", TGT_WC),
        )

    def test_custom_bound_and_generated_hosts(self) -> None:
        entries, missing = self._body(_source())
        assert entries == [
            # custom: verbatim, port included
            {"name": "web", "url": "https://shop.example.com:8443"},
            # server-bound: onto the target's wildcard, scheme and path kept
            {"name": "api", "url": f"http://api.{TGT_WC}/v1"},
            # admin's sslip host names the SOURCE's IP: not carried at all
        ]
        assert missing == []

    def test_server_bound_without_a_target_wildcard_is_not_carried(self) -> None:
        entries, _ = self._body(_source(), target_wildcard=None)
        assert [e["name"] for e in entries] == ["web"]

    def test_a_name_the_target_lacks_is_missing(self) -> None:
        entries, missing = self._body(_source(), target_names={"api"})
        assert missing == ["web"]
        assert entries == [{"name": "api", "url": f"http://api.{TGT_WC}/v1"}]

    def test_a_generated_only_name_is_not_missing(self) -> None:
        # admin carries nothing, so its absence on the target costs nothing.
        _, missing = self._body(_source(), target_names={"web", "api"})
        assert missing == []

    def test_nothing_to_carry(self) -> None:
        assert self._body({"applications": []}) == ([], [])


class TestCustomUrls:
    def test_only_sub_applications_with_a_custom_host(self) -> None:
        assert resources.service_custom_urls(_source(), source_wildcard=SRC_WC) == [
            {"name": "web", "url": "https://shop.example.com:8443"}
        ]

    def test_the_full_original_is_kept_for_a_mixed_list(self) -> None:
        mixed = f"https://shop.example.com,https://x.{SRC_WC}"
        source = {"applications": [{"name": "web", "fqdn": mixed}]}
        assert resources.service_restore_body(source, source_wildcard=SRC_WC) == {
            "urls": [{"name": "web", "url": mixed}]
        }

    def test_bound_and_generated_only_needs_no_restore(self) -> None:
        source = _source(applications=_source()["applications"][1:])
        assert resources.service_restore_body(source, source_wildcard=SRC_WC) is None

    def test_without_a_source_wildcard_a_bound_host_counts_as_custom(self) -> None:
        source = {"applications": [{"name": "api", "fqdn": f"http://api.{SRC_WC}"}]}
        assert resources.service_custom_urls(source, source_wildcard=None) == [
            {"name": "api", "url": f"http://api.{SRC_WC}"}
        ]


class TestOnlyOwnConflicts:
    OWN = frozenset({"sa-web", "sa-api"})

    def test_every_holder_is_the_source(self) -> None:
        conflicts = [{"resource_uuid": "sa-web"}, {"resource_uuid": "sa-api"}]
        assert resources.only_own_conflicts(conflicts, self.OWN)

    def test_a_third_party_holder(self) -> None:
        conflicts = [{"resource_uuid": "sa-web"}, {"resource_uuid": "someone-else"}]
        assert not resources.only_own_conflicts(conflicts, self.OWN)

    def test_the_instance_domain_is_never_own(self) -> None:
        assert not resources.only_own_conflicts([{"resource_uuid": None}], self.OWN)

    def test_no_conflicts_is_not_own(self) -> None:
        assert not resources.only_own_conflicts([], self.OWN)


# ── IO: create, carry, restore, release ──────────────────────────────────────


class TestCreateService:
    async def test_create_is_a_single_post(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # REGRESSION: the connect_to_docker_network PATCH used to follow the POST
        # inside create_service, BEFORE the engine journaled the uuid — a failure
        # there stranded a target no rollback knew about.
        respx_mock.post(f"{BASE}/services").mock(
            return_value=httpx.Response(201, json={"uuid": "t"})
        )
        uuid = await resources.create_resource(
            api, _snapshot(), _placement(), {"connect_to_docker_network": True}
        )
        assert uuid == "t"
        assert len(respx_mock.calls) == 1


class TestParkSourceDomainsForServices:
    async def test_a_service_is_never_parked_but_its_restore_is_returned(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        restore = await resources.park_source_domains(
            api, _snapshot(), _source(), source_wildcard=SRC_WC, tag="t1"
        )
        assert restore == {"urls": [{"name": "web", "url": "https://shop.example.com:8443"}]}
        assert not respx_mock.calls


class TestFinishServiceTarget:
    async def _finish(self, api: CoolifyClient, source: dict[str, Any] | None = None) -> int:
        return await resources.finish_service_target(
            api, _snapshot(), _placement(), source or _source(), "tgt1"
        )

    async def test_one_patch_carries_urls_and_the_network_flag(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={"uuid": "tgt1"})
        )
        count = await self._finish(api, _source(connect_to_docker_network=True))
        assert count == 2
        assert patch.call_count == 1
        assert _body(patch) == {
            "connect_to_docker_network": True,
            "urls": [
                {"name": "web", "url": "https://shop.example.com:8443"},
                {"name": "api", "url": f"http://api.{TGT_WC}/v1"},
            ],
        }

    async def test_never_forces_on_the_first_attempt(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )
        await self._finish(api)
        body = _body(patch)
        assert "force_domain_override" not in body
        assert "connect_to_docker_network" not in body  # only when the source had it

    async def test_a_missing_sub_application_refuses_before_any_write(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get(("api",)))
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(UnsupportedResource, match=r"\['web'\]") as exc_info:
            await self._finish(api, _source(connect_to_docker_network=True))
        assert "template catalog" in (exc_info.value.hint or "")
        assert "never stopped" in (exc_info.value.hint or "")
        assert not patch.called

    async def test_nothing_to_carry_writes_nothing(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )
        source = {"applications": [{"uuid": "sa-admin", "name": "admin", "fqdn": SSLIP}]}
        assert await self._finish(api, source) == 0
        assert not patch.called

    async def test_a_conflict_held_only_by_the_source_is_forced(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The source keeps its hosts until FINALIZE, so the target's claim 409s
        # against the source's own sub-application — the one holder we may force.
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
        conflict = {
            "message": "Domain conflicts detected. Use force_domain_override=true to proceed.",
            "conflicts": [
                {
                    "domain": "https://shop.example.com",
                    "resource_uuid": "sa-web",
                    "resource_type": "service",
                    "message": "Domain https://shop.example.com is already in use by service 'shop'",
                }
            ],
        }
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            side_effect=[httpx.Response(409, json=conflict), httpx.Response(200, json={})]
        )
        await self._finish(api)
        assert patch.call_count == 2
        first, second = _body(patch, 0), _body(patch, 1)
        assert second == {**first, "force_domain_override": True}

    @pytest.mark.parametrize(
        "holder",
        [
            {"resource_uuid": "other-app", "resource_type": "application"},
            {"resource_uuid": None, "resource_type": "instance"},
        ],
    )
    async def test_any_other_holder_is_refused_without_force(
        self, api: CoolifyClient, respx_mock: respx.Router, holder: dict[str, Any]
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
        conflicts = [
            {"resource_uuid": "sa-web", "message": "in use by service 'shop'"},
            {**holder, "message": "Domain https://shop.example.com is already in use by X"},
        ]
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(409, json={"conflicts": conflicts})
        )
        with pytest.raises(CoolifyApiError, match="already in use by X") as exc_info:
            await self._finish(api)
        assert exc_info.value.status_code == 409
        assert "never stopped" in (exc_info.value.hint or "")
        assert patch.call_count == 1

    async def test_other_errors_propagate_unforced(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(422, json={"message": "Validation failed."})
        )
        with pytest.raises(CoolifyApiError) as exc_info:
            await self._finish(api)
        assert exc_info.value.status_code == 422
        assert patch.call_count == 1

    async def test_a_409_naming_no_holder_is_not_forced(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(409, json={"message": "Domain conflicts detected."})
        )
        with pytest.raises(CoolifyApiError, match="named no holder"):
            await self._finish(api)
        assert patch.call_count == 1

    async def test_a_target_without_a_wildcard_keeps_its_generated_url(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )
        placement = _placement()
        placement.target_wildcard = None
        await resources.finish_service_target(api, _snapshot(), placement, _source(), "tgt1")
        # api's server-bound URL cannot be placed: not sent, so the target's own
        # parse-generated URL stays (and a warning names the host).
        assert [e["name"] for e in _body(patch)["urls"]] == ["web"]


class TestRestoreServiceUrls:
    async def test_a_source_that_holds_its_originals_is_not_written(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # 4.5 re-parses the compose and rewrites the env rows on every urls PATCH:
        # a pre-finalize rollback must not touch a live, unchanged source.
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json=_source())
        )
        body = {"urls": [{"name": "web", "url": "https://SHOP.example.com:8443/"}]}
        assert await resources.restore_service_urls(api, "svc1", body) is False
        assert [c.request.method for c in respx_mock.calls] == ["GET"]

    async def test_only_what_differs_is_patched_and_force_is_passed_on(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        released = _source()
        released["applications"][0] = {"uuid": "sa-web", "name": "web", "fqdn": None}
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json=released)
        )
        patch = respx_mock.patch(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json={})
        )
        body = {
            "urls": [
                {"name": "web", "url": "https://shop.example.com:8443"},
                {"name": "admin", "url": SSLIP},
                {"name": "gone", "url": "https://gone.example.com"},
            ],
            "force_domain_override": True,
        }
        assert await resources.restore_service_urls(api, "svc1", body) is True
        assert _body(patch) == {
            "urls": [{"name": "web", "url": "https://shop.example.com:8443"}],
            "force_domain_override": True,
        }

    async def test_an_empty_body_reads_nothing(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        assert await resources.restore_service_urls(api, "svc1", {"urls": []}) is False
        assert not respx_mock.calls


class TestReleaseServiceUrls:
    async def test_blanks_only_the_custom_sub_applications(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json=_source())
        )
        patch = respx_mock.patch(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json={})
        )
        await resources.release_service_urls(api, "svc1", source_wildcard=SRC_WC)
        assert _body(patch) == {"urls": [{"name": "web", "url": ""}]}

    async def test_nothing_custom_writes_nothing(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        source = _source(applications=_source()["applications"][1:])
        respx_mock.get(f"{BASE}/services/svc1").mock(return_value=httpx.Response(200, json=source))
        await resources.release_service_urls(api, "svc1", source_wildcard=SRC_WC)
        assert [c.request.method for c in respx_mock.calls] == ["GET"]

    async def test_a_failure_is_a_warning_not_a_failed_finalize(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json=_source())
        )
        respx_mock.patch(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        await resources.release_service_urls(api, "svc1", source_wildcard=SRC_WC)


# ── engine wiring ────────────────────────────────────────────────────────────


def _service_plan(**kw: object) -> Any:
    return _plan(
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1", wildcard_domain=SRC_WC),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2", wildcard_domain=TGT_WC),
        resources=(ResourcePlan(snapshot=_snapshot(), strategy=Strategy.COPY_DATA),),
        **kw,
    )


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
    yield context
    await client.aclose()


def _mock_create(respx_mock: respx.Router, *, source: dict[str, Any] | None = None) -> None:
    respx_mock.get(f"{BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
    )
    respx_mock.get(f"{BASE}/projects/p1").mock(
        return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
    )
    respx_mock.get(f"{BASE}/servers/s2/destinations").mock(
        return_value=httpx.Response(200, json=[{"uuid": "d1", "network": "coolify"}])
    )
    respx_mock.get(f"{BASE}/services/svc1").mock(
        return_value=httpx.Response(200, json=source or _source())
    )
    respx_mock.post(f"{BASE}/services").mock(
        return_value=httpx.Response(201, json={"uuid": "tgt1"})
    )
    respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get())
    respx_mock.get(f"{BASE}/services/svc1/envs").mock(
        return_value=httpx.Response(200, json=[{"key": "A", "value": "1"}])
    )
    respx_mock.patch(f"{BASE}/services/tgt1/envs/bulk").mock(
        return_value=httpx.Response(200, json={})
    )
    respx_mock.get(f"{BASE}/services/svc1/tags").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(f"{BASE}/services/svc1/scheduled-tasks").mock(
        return_value=httpx.Response(200, json=[])
    )


class TestCreateTarget:
    async def test_urls_are_carried_after_the_uuid_is_journaled_and_before_envs(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create(respx_mock)
        respx_mock.patch(f"{BASE}/services/tgt1").mock(return_value=httpx.Response(200, json={}))

        result = await steps.step_create_target(ctx)

        assert result["target_uuids"] == {"svc1": "tgt1"}
        writes = [
            (c.request.method, c.request.url.path)
            for c in respx_mock.calls
            if c.request.method != "GET"
        ]
        assert writes == [
            ("POST", "/api/v1/services"),
            ("PATCH", "/api/v1/services/tgt1"),
            ("PATCH", "/api/v1/services/tgt1/envs/bulk"),
        ]
        # The source is never written to before FINALIZE.
        assert ("PATCH", "/api/v1/services/svc1") not in writes
        undo = ctx.journal.undo_info("create_target")
        assert undo["parked_domains"] == {
            "svc1": {"urls": [{"name": "web", "url": "https://shop.example.com:8443"}]}
        }

    async def test_the_restore_is_journaled_before_anything_is_written(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create(respx_mock)
        respx_mock.post(f"{BASE}/services").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        with pytest.raises(CoolifyApiError):
            await steps.step_create_target(ctx)
        undo = ctx.journal.undo_info("create_target")
        assert undo["target_uuids"] == {}
        assert undo["parked_domains"]["svc1"]["urls"][0]["name"] == "web"

    async def test_a_third_party_conflict_deletes_the_target_and_leaves_the_source(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create(respx_mock)
        respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(
                409, json={"conflicts": [{"resource_uuid": "x", "message": "in use by app X"}]}
            )
        )
        delete = respx_mock.delete(url__startswith=f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )
        source_writes = respx_mock.patch(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(CoolifyApiError, match="in use by app X"):
            await steps.step_create_target(ctx)

        assert delete.called, "the journaled target must be deleted"
        # The rollback's restore GETs the source, finds its originals, and writes
        # nothing: the live source was never touched.
        assert not source_writes.called


class TestFinalize:
    def _hosts(self, ctx: MigrationContext) -> None:
        source = FakeHost()
        source.on(r"rm -rf", exit_status=0)
        target = FakeHost()
        target.on(r"sed -i", exit_status=0)
        # FINALIZE also releases the volume pins (discovery/pins.py); none here.
        for host in (source, target):
            host.on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        ctx.source_host = source  # type: ignore[assignment]
        ctx.target_host = target  # type: ignore[assignment]

    @pytest.mark.parametrize("policy", [FinalizePolicy.KEEP, FinalizePolicy.RENAME])
    async def test_kept_and_renamed_sources_release_their_custom_hosts(
        self, ctx: MigrationContext, respx_mock: respx.Router, policy: FinalizePolicy
    ) -> None:
        ctx.plan = _service_plan(finalize_policy=policy)
        self._hosts(ctx)
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json=_source())
        )
        patch = respx_mock.patch(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json={})
        )
        await steps.step_finalize(ctx)
        bodies = [_body(patch, i) for i in range(patch.call_count)]
        assert {"urls": [{"name": "web", "url": ""}]} in bodies

    async def test_a_deleted_source_needs_no_release(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _service_plan(finalize_policy=FinalizePolicy.DELETE)
        self._hosts(ctx)
        delete = respx_mock.delete(url__startswith=f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json={})
        )
        await steps.step_finalize(ctx)
        assert delete.called
        assert [c.request.method for c in respx_mock.calls] == ["DELETE"]


class TestRestoreCompensation:
    async def test_a_released_source_gets_its_urls_back_forcing_on_409(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        released = _source()
        released["applications"][0] = {"uuid": "sa-web", "name": "web", "fqdn": None}
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json=released)
        )
        patch = respx_mock.patch(f"{BASE}/services/svc1").mock(
            side_effect=[
                httpx.Response(409, json={"message": "Domain conflicts detected."}),
                httpx.Response(200, json={}),
            ]
        )
        restore = {"urls": [{"name": "web", "url": "https://shop.example.com:8443"}]}
        await compensations.undo_create_target(
            ctx, {"target_uuids": {}, "parked_domains": {"svc1": restore}}
        )
        assert patch.call_count == 2
        assert "force_domain_override" not in _body(patch, 0)
        assert _body(patch, 1) == {**restore, "force_domain_override": True}

    async def test_an_untouched_source_is_not_written(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json=_source())
        )
        restore = {"urls": [{"name": "web", "url": "https://shop.example.com:8443"}]}
        await compensations.undo_create_target(
            ctx, {"target_uuids": {}, "parked_domains": {"svc1": restore}}
        )
        assert [c.request.method for c in respx_mock.calls] == ["GET"]
