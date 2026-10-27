"""Service domains: claimed by the target, released by the source at FINALIZE.

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


def _target_get(
    names: tuple[str, ...] = ("web", "api", "admin"), *, urls: dict[str, str] | None = None
) -> httpx.Response:
    apps = [{"uuid": f"t-{n}", "name": n, "url": (urls or {}).get(n)} for n in names]
    return httpx.Response(200, json={"uuid": "tgt1", "applications": apps})


#: The target once it holds the carried URLs - what the read-back must find.
CLAIMED = {"web": "https://shop.example.com:8443", "api": f"http://api.{TGT_WC}/v1"}


def _target_reads(*, claimed: dict[str, str] | None = CLAIMED) -> Any:
    """GET /services/tgt1: as created at first, holding the URLs from then on."""
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _target_get(urls=None if len(calls) == 1 else claimed)

    return handler


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


class TestSubApplicationUuids:
    def test_reads_the_uuids_a_conflict_names(self) -> None:
        assert resources.sub_application_uuids(_source()) == {"sa-web", "sa-api", "sa-admin"}

    def test_tolerates_junk(self) -> None:
        assert resources.sub_application_uuids({"applications": ["x", {"name": "n"}]}) == set()
        assert resources.sub_application_uuids({}) == set()


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


class TestSourceRestoreBodyForServices:
    def test_a_service_body_holds_its_custom_sub_application_urls(self) -> None:
        restore = resources.source_restore_body(_snapshot(), _source(), source_wildcard=SRC_WC)
        assert restore == {"urls": [{"name": "web", "url": "https://shop.example.com:8443"}]}


class TestFinishServiceTarget:
    async def _finish(self, api: CoolifyClient, source: dict[str, Any] | None = None) -> set[str]:
        return await resources.finish_service_target(
            api, _snapshot(), _placement(), source or _source(), "tgt1"
        )

    async def test_one_patch_carries_urls_and_the_network_flag(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(side_effect=_target_reads())
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={"uuid": "tgt1"})
        )
        own = await self._finish(api, _source(connect_to_docker_network=True))
        # The target's sub-applications join the caller's own-holder set.
        assert own == {"t-web", "t-api", "t-admin"}
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
        respx_mock.get(f"{BASE}/services/tgt1").mock(side_effect=_target_reads())
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
        assert await self._finish(api, source) == {"t-web", "t-api", "t-admin"}
        assert not patch.called

    async def test_a_conflict_held_only_by_the_source_is_forced(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The source keeps its hosts until FINALIZE, so the target's claim 409s
        # against the source's own sub-application — a holder we may force.
        respx_mock.get(f"{BASE}/services/tgt1").mock(side_effect=_target_reads())
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
            {"resource_uuid": "sa-web", "domain": "shop.example.com", "resource_name": "shop"},
            {**holder, "domain": "shop.example.com", "resource_name": "X"},
        ]
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(409, json={"conflicts": conflicts})
        )
        with pytest.raises(CoolifyApiError, match="held elsewhere") as exc_info:
            await self._finish(api)
        message = exc_info.value.args[0]  # the message itself, not the echoed body
        # Only the holder that is NOT ours is named — by uuid when it has one, so
        # an orphan carrying the source's name cannot be mistaken for it.
        assert "sa-web" not in message
        uuid = holder["resource_uuid"]
        assert (f"(uuid {uuid})" if uuid else "the Coolify instance itself") in message
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

    async def test_the_target_must_hold_what_it_claimed(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Make-before-break: the source may only let go once the target HOLDS it.
        respx_mock.get(f"{BASE}/services/tgt1").mock(side_effect=_target_reads(claimed={}))
        respx_mock.patch(f"{BASE}/services/tgt1").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(UnsupportedResource, match=r"does not hold \['shop\.example\.com', 'api\."):
            await self._finish(api)

    async def test_an_orphan_of_ours_is_forced_too(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The caller's own set: a target this run already made holds the host.
        respx_mock.get(f"{BASE}/services/tgt1").mock(side_effect=_target_reads())
        conflict = {"conflicts": [{"resource_uuid": "t-earlier", "domain": "shop.example.com"}]}
        patch = respx_mock.patch(f"{BASE}/services/tgt1").mock(
            side_effect=[httpx.Response(409, json=conflict), httpx.Response(200, json={})]
        )
        await resources.finish_service_target(
            api, _snapshot(), _placement(), _source(), "tgt1", own=frozenset({"t-earlier"})
        )
        assert patch.call_count == 2

    async def test_a_target_without_a_wildcard_keeps_its_generated_url(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/tgt1").mock(side_effect=_target_reads())
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
    respx_mock.get(f"{BASE}/services/tgt1").mock(side_effect=_target_reads())
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
                409,
                json={
                    "conflicts": [
                        {
                            "resource_uuid": "x",
                            "domain": "shop.example.com",
                            "resource_name": "X",
                            "resource_type": "application",
                        }
                    ]
                },
            )
        )
        delete = respx_mock.delete(url__startswith=f"{BASE}/services/tgt1").mock(
            return_value=httpx.Response(200, json={})
        )
        source_writes = respx_mock.patch(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json={})
        )

        with pytest.raises(CoolifyApiError, match=r"application 'X' \(uuid x\)"):
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
        # The holder is the target's own sub-application: forced (and only then).
        respx_mock.get(f"{BASE}/services/svcT").mock(
            return_value=httpx.Response(200, json={"applications": [{"uuid": "sa-webT"}]})
        )
        patch = respx_mock.patch(f"{BASE}/services/svc1").mock(
            side_effect=[
                httpx.Response(
                    409,
                    json={
                        "message": "Domain conflicts detected.",
                        "conflicts": [{"domain": "shop.example.com", "resource_uuid": "sa-webT"}],
                    },
                ),
                httpx.Response(200, json={}),
            ]
        )
        restore = {"urls": [{"name": "web", "url": "https://shop.example.com:8443"}]}
        await compensations.undo_restore_source_fqdn(
            ctx, {"target_uuids": {"svc1": "svcT"}, "parked_domains": {"svc1": restore}}
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
        await compensations.undo_restore_source_fqdn(
            ctx, {"target_uuids": {}, "parked_domains": {"svc1": restore}}
        )
        assert [c.request.method for c in respx_mock.calls] == ["GET"]


class TestRollbackAfterFinalize:
    """Findings 1/3/6: the released URLs must be back BEFORE the restart re-parses.

    /restart -> StartService -> parse() rebuilds a blank sub-application fqdn as
    a generated host and bakes the labels from it (parsers.php v4.5.0:2626-2675);
    the urls PATCH only writes the database. So the restore must come first, and
    the target that holds the host goes last.
    """

    async def test_released_urls_are_restored_before_the_source_restarts(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.engine.compensations import build_compensations
        from bg_coolify_migrate.engine.executor import Saga

        ctx.plan = _service_plan(finalize_policy=FinalizePolicy.RENAME)
        restore = {"urls": [{"name": "web", "url": "https://shop.example.com:8443"}]}
        ctx.journal.append(
            "step_completed",
            state="create_target",
            detail={"target_uuids": {"svc1": "tgt1"}, "parked_domains": {"svc1": restore}},
        )
        ctx.journal.append("step_completed", state="quiesce", detail={})
        ctx.journal.append("step_completed", state="finalize", detail={"policy": "rename"})
        released = _source()
        released["applications"][0] = {"uuid": "sa-web", "name": "web", "fqdn": None}
        state = {"source": released}
        order: list[str] = []

        def get_source(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=state["source"])

        def patch_source(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.read())
            if "name" in body:
                order.append("rename back")
            else:
                order.append("restore urls")
                state["source"] = _source()
            return httpx.Response(200, json={})

        def mark(label: str) -> Any:
            def handler(_request: httpx.Request) -> httpx.Response:
                order.append(label)
                return httpx.Response(200, json={})

            return handler

        respx_mock.get(f"{BASE}/services/svc1").mock(side_effect=get_source)
        respx_mock.get(f"{BASE}/services/tgt1").mock(return_value=_target_get(urls=CLAIMED))
        respx_mock.patch(f"{BASE}/services/svc1").mock(side_effect=patch_source)
        respx_mock.post(f"{BASE}/services/svc1/restart").mock(side_effect=mark("restart"))
        respx_mock.delete(url__startswith=f"{BASE}/services/tgt1").mock(
            side_effect=mark("delete target")
        )

        result = await Saga(
            journal=ctx.journal, context=ctx, steps={}, compensations=build_compensations()
        ).rollback()

        assert order == ["rename back", "restore urls", "restart", "delete target"]
        assert result.compensations_failed == []


# ── finding 7: the env filter's wiring at engine level ──────────────────────


class TestCreateTargetEnvFilterWiring:
    async def test_source_wildcard_tag_and_route_keys_reach_the_filter(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """Drop any kwarg from step_create_target's copy_envs call and this fails."""
        from bg_coolify_migrate.domain import domains

        tag = domains.parking_tag(ctx.plan.project, ctx.plan.environment)
        minio = _source()
        minio["applications"][0] = {
            **minio["applications"][0],
            "image": "quay.io/minio/minio:RELEASE.2025-04-22",
        }
        _mock_create(respx_mock, source=minio)
        respx_mock.get(f"{BASE}/services/svc1/envs").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"key": "SERVICE_URL_API", "value": f"https://api.{SRC_WC}"},  # wildcard
                    {"key": "SERVICE_URL_OLD", "value": f"https://old-{tag}.shop.example.com"},
                    {"key": "MINIO_SERVER_URL", "value": "http://s3-svc1.10.0.0.1.sslip.io"},
                    {"key": "SERVICE_URL_CALLBACK", "value": "https://auth.example.com/cb"},
                ],
            )
        )
        bulk = respx_mock.patch(f"{BASE}/services/tgt1/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        respx_mock.patch(f"{BASE}/services/tgt1").mock(return_value=httpx.Response(200, json={}))

        await steps.step_create_target(ctx)

        sent = [e["key"] for e in _body(bulk)["data"]]
        assert sent == ["SERVICE_URL_CALLBACK"]


# ── finding 8: a SPECIFIC_SERVICES image's route keys at FINALIZE ───────────

MINIO_KEYS = frozenset({"MINIO_SERVER_URL", "MINIO_BROWSER_REDIRECT_URL"})


def _minio_source() -> dict[str, Any]:
    """A silo/minio service: routed from MINIO_SERVER_URL, its fqdn null."""
    return {
        "uuid": "svc1",
        "applications": [
            {"uuid": "sa-minio", "name": "minio", "fqdn": None, "image": "pgsty/silo:1.2"}
        ],
    }


class TestServiceRouteUrls:
    def test_only_route_rows_holding_a_custom_host(self) -> None:
        envs = [
            {"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"},
            {"key": "MINIO_BROWSER_REDIRECT_URL", "value": f"https://c.{SRC_WC}"},  # bound
            {"key": "LOGTO_ENDPOINT", "value": "https://auth.example.com"},  # not this image
            {"key": "MINIO_SERVER_URL", "is_preview": True, "value": "https://p.example.com"},
        ]
        assert resources.service_route_urls(
            envs, route_keys=MINIO_KEYS, source_wildcard=SRC_WC
        ) == [{"key": "MINIO_SERVER_URL", "url": "https://s3.example.com"}]

    def test_locked_and_credential_bearing_rows_are_left_alone(self) -> None:
        envs = [
            {"key": "MINIO_SERVER_URL"},  # locked: no value in any read
            {"key": "MINIO_BROWSER_REDIRECT_URL", "value": "https://u:p@s3.example.com"},
        ]
        assert resources.service_route_urls(envs, route_keys=MINIO_KEYS, source_wildcard=None) == []

    def test_the_restore_body_carries_them_under_url(self) -> None:
        # The journal refuses a `value` key; a URL is not a secret.
        envs = [{"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"}]
        body = resources.service_restore_body(
            _minio_source(), source_wildcard=SRC_WC, source_envs=envs
        )
        assert body == {"route_envs": [{"key": "MINIO_SERVER_URL", "url": "https://s3.example.com"}]}
        from bg_coolify_migrate.domain import domains

        assert domains.restore_summary(body) == "MINIO_SERVER_URL=https://s3.example.com"
        # Not a holder Coolify's DNS cleanup counts: never blocks a target delete.
        assert domains.body_hosts(body) == []


class TestRouteEnvReleaseAndRestore:
    async def test_finalize_blanks_a_custom_route_row(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/svc1").mock(
            return_value=httpx.Response(200, json=_minio_source())
        )
        respx_mock.get(f"{BASE}/services/svc1/envs").mock(
            return_value=httpx.Response(
                200, json=[{"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"}]
            )
        )
        bulk = respx_mock.patch(f"{BASE}/services/svc1/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        assert await resources.release_service_urls(api, "svc1", source_wildcard=SRC_WC)
        assert _body(bulk) == {"data": [{"key": "MINIO_SERVER_URL", "value": ""}]}

    async def test_a_stack_without_a_specific_image_reads_no_envs(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        source = _source(applications=_source()["applications"][1:])
        respx_mock.get(f"{BASE}/services/svc1").mock(return_value=httpx.Response(200, json=source))
        assert await resources.release_service_urls(api, "svc1", source_wildcard=SRC_WC)
        assert [c.request.url.path for c in respx_mock.calls] == ["/api/v1/services/svc1"]

    async def test_the_restore_puts_the_row_back_before_anything_else(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/svc1/envs").mock(
            return_value=httpx.Response(200, json=[{"key": "MINIO_SERVER_URL", "value": None}])
        )
        bulk = respx_mock.patch(f"{BASE}/services/svc1/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        body = {"route_envs": [{"key": "MINIO_SERVER_URL", "url": "https://s3.example.com"}]}
        assert await resources.restore_service_urls(api, "svc1", body) is True
        assert _body(bulk) == {"data": [{"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"}]}

    async def test_an_unchanged_row_is_not_written(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/svc1/envs").mock(
            return_value=httpx.Response(
                200, json=[{"key": "MINIO_SERVER_URL", "value": "https://S3.example.com/"}]
            )
        )
        body = {"route_envs": [{"key": "MINIO_SERVER_URL", "url": "https://s3.example.com"}, "x"]}
        assert await resources.restore_service_urls(api, "svc1", body) is False
        assert [c.request.method for c in respx_mock.calls] == ["GET"]


# ── finding 2: the port of a legacy URL on 4.3.15-4.3.17 ────────────────────


class TestRoutedUrlPerPart:
    def test_a_legacy_inline_port_beats_a_port_stripped_url(self) -> None:
        # 4.3.15-4.3.17: saved before 4.3.15, no override entry -> the url
        # accessor strips the port the proxy still routes to.
        source = {
            "applications": [
                {
                    "name": "web",
                    "fqdn": "https://shop.example.com:8443",
                    "url": "https://shop.example.com",
                }
            ]
        }
        assert resources.service_app_urls(source) == {"web": "https://shop.example.com:8443"}

    def test_a_hidden_override_comes_from_url(self) -> None:
        # 4.3.15+ with an override: fqdn portless, url re-composed with the port.
        assert resources.service_app_urls(_source())["web"] == "https://shop.example.com:8443"

    def test_parts_pair_by_index(self) -> None:
        source = {
            "applications": [
                {
                    "name": "web",
                    "fqdn": "https://a.example.com:81,https://b.example.com",
                    "url": "https://a.example.com,https://b.example.com:82",
                }
            ]
        }
        assert resources.service_app_urls(source) == {
            "web": "https://a.example.com:81,https://b.example.com:82"
        }

    def test_lists_that_do_not_pair_fall_back_to_url(self) -> None:
        source = {
            "applications": [
                {"name": "web", "fqdn": "https://a.example.com:81", "url": "https://a.b,https://c.d"}
            ]
        }
        assert resources.service_app_urls(source) == {"web": "https://a.b,https://c.d"}

    def test_a_malformed_port_is_no_port(self) -> None:
        source = {
            "applications": [
                {"name": "web", "fqdn": "https://a.example.com:99999", "url": "https://a.example.com"}
            ]
        }
        assert resources.service_app_urls(source) == {"web": "https://a.example.com"}

    def test_the_restore_body_keeps_the_legacy_port(self) -> None:
        source = {
            "applications": [
                {
                    "name": "web",
                    "fqdn": "https://shop.example.com:8443",
                    "url": "https://shop.example.com",
                }
            ]
        }
        assert resources.service_restore_body(source, source_wildcard=SRC_WC) == {
            "urls": [{"name": "web", "url": "https://shop.example.com:8443"}]
        }
