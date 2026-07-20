"""Contract tests for the Coolify API client.

The capability-probe tests are the important ones: they encode the single most
dangerous Coolify behaviour — a token without `read:sensitive` gets HTTP 200 with
the secret keys silently ABSENT.

NOTE: these use the `respx_mock` FIXTURE, not `@respx.mock` as a class decorator.
The class decorator silently prevents pytest from collecting the methods at all,
which produces a test file that reports green while running nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.errors import CoolifyApiError, InsufficientTokenScope

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"


@pytest.fixture
async def api() -> AsyncIterator[CoolifyClient]:
    client = CoolifyClient(HOST, "tok", max_retries=2)
    yield client
    await client.aclose()


class TestBaseUrl:
    def test_api_v1_is_appended(self) -> None:
        client = CoolifyClient(HOST, "t")
        assert str(client._client.base_url).rstrip("/") == BASE

    def test_api_v1_is_not_doubled(self) -> None:
        client = CoolifyClient(f"{HOST}/api/v1", "t")
        assert str(client._client.base_url).rstrip("/") == BASE

    def test_trailing_slash_tolerated(self) -> None:
        client = CoolifyClient(f"{HOST}/", "t")
        assert str(client._client.base_url).rstrip("/") == BASE

    def test_token_is_sent_as_bearer(self) -> None:
        client = CoolifyClient(HOST, "sekrit")
        assert client._client.headers["Authorization"] == "Bearer sekrit"


class TestCapabilityProbe:
    async def test_private_key_present_means_sensitive_readable(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"uuid": "k1", "private_key": "-----BEGIN..."}])
        )
        assert await api.can_read_sensitive() is True
        await api.assert_can_read_sensitive()  # must not raise

    async def test_private_key_absent_means_not_readable(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # THE trap: HTTP 200, no error, the key is simply gone.
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"uuid": "k1", "name": "default"}])
        )
        assert await api.can_read_sensitive() is False

    async def test_assert_raises_with_an_actionable_hint(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"uuid": "k1"}])
        )
        with pytest.raises(InsufficientTokenScope) as exc:
            await api.assert_can_read_sensitive()
        assert "read:sensitive" in str(exc.value)
        assert "silently OMITS" in str(exc.value)

    async def test_indeterminate_probe_fails_closed(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # No keys to inspect: we cannot prove the capability, so we must not
        # assume it. An indeterminate probe is not a pass.
        respx_mock.get(f"{BASE}/security/keys").mock(return_value=httpx.Response(200, json=[]))
        assert await api.can_read_sensitive() is False

    async def test_probe_is_cached(self, api: CoolifyClient, respx_mock: respx.Router) -> None:
        route = respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        await api.can_read_sensitive()
        await api.can_read_sensitive()
        assert route.call_count == 1


class TestRetry:
    async def test_429_is_retried(self, api: CoolifyClient, respx_mock: respx.Router) -> None:
        # Deploy endpoints rate-limit under normal operation; 429 is expected,
        # not exceptional.
        route = respx_mock.get(f"{BASE}/servers").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json=[{"uuid": "s1"}]),
            ]
        )
        assert await api.list_servers() == [{"uuid": "s1"}]
        assert route.call_count == 2

    @pytest.mark.parametrize("status", [502, 503, 504])
    async def test_transient_5xx_is_retried(
        self, api: CoolifyClient, respx_mock: respx.Router, status: int
    ) -> None:
        route = respx_mock.get(f"{BASE}/servers").mock(
            side_effect=[httpx.Response(status), httpx.Response(200, json=[])]
        )
        await api.list_servers()
        assert route.call_count == 2

    async def test_retries_are_bounded(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get(f"{BASE}/servers").mock(return_value=httpx.Response(503))
        with pytest.raises(CoolifyApiError):
            await api.list_servers()
        assert route.call_count == 3  # initial + max_retries(2)

    async def test_post_is_not_retried_on_timeout(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # A retried create can produce TWO resources. Cleaning that up by hand is
        # exactly the half-state this tool exists to prevent.
        route = respx_mock.post(f"{BASE}/services").mock(side_effect=httpx.TimeoutException("boom"))
        with pytest.raises(CoolifyApiError, match="timed out"):
            await api.post("/services", {"name": "x"})
        assert route.call_count == 1

    async def test_get_is_retried_on_timeout(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get(f"{BASE}/servers").mock(
            side_effect=[httpx.TimeoutException("boom"), httpx.Response(200, json=[])]
        )
        await api.list_servers()
        assert route.call_count == 2

    async def test_400_is_not_retried(self, api: CoolifyClient, respx_mock: respx.Router) -> None:
        route = respx_mock.get(f"{BASE}/servers").mock(return_value=httpx.Response(400))
        with pytest.raises(CoolifyApiError):
            await api.list_servers()
        assert route.call_count == 1


class TestErrorMapping:
    async def test_422_hint_points_at_our_whitelist(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Coolify names the offending field; that is the most useful signal when
        # api/fields.py has drifted from upstream.
        respx_mock.post(f"{BASE}/services").mock(
            return_value=httpx.Response(422, json={"bogus_field": ["This field is not allowed."]})
        )
        with pytest.raises(CoolifyApiError) as exc:
            await api.post("/services", {"bogus_field": 1})
        assert exc.value.status_code == 422
        assert "api/fields.py" in str(exc.value)
        assert "bogus_field" in str(exc.value)

    async def test_401_hint(self, api: CoolifyClient, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/servers").mock(return_value=httpx.Response(401))
        with pytest.raises(CoolifyApiError, match="COOLIFY_TOKEN"):
            await api.list_servers()

    async def test_403_hint_mentions_abilities(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers").mock(return_value=httpx.Response(403))
        with pytest.raises(CoolifyApiError, match="write/deploy"):
            await api.list_servers()

    async def test_404_hint_mentions_api_disabled(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers").mock(return_value=httpx.Response(404))
        with pytest.raises(CoolifyApiError, match="API is disabled"):
            await api.list_servers()

    async def test_non_json_body_is_reported_clearly(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers").mock(return_value=httpx.Response(200, text="<html>nope"))
        with pytest.raises(CoolifyApiError, match="non-JSON"):
            await api.list_servers()

    async def test_wrong_json_shape_is_reported(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers").mock(
            return_value=httpx.Response(200, json={"not": "a list"})
        )
        with pytest.raises(CoolifyApiError, match="expected a JSON array"):
            await api.list_servers()


class TestEndpoints:
    async def test_bulk_env_body_uses_the_data_key(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The body key is literally `data` ($request->get('data')); anything else
        # is a 400 "Bulk data is required.".
        route = respx_mock.patch(f"{BASE}/services/u1/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        await api.set_envs_bulk("services", "u1", [{"key": "A", "value": "1"}])
        assert route.calls[0].request.content == b'{"data":[{"key":"A","value":"1"}]}'

    async def test_storages_returns_the_two_key_object(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/u1/storages").mock(
            return_value=httpx.Response(
                200, json={"persistent_storages": [{"name": "v"}], "file_storages": []}
            )
        )
        result = await api.get_storages("applications", "u1")
        assert set(result) == {"persistent_storages", "file_storages"}

    async def test_tag_names_projects_to_names(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Upstream returns full Tag objects; only `name` survives a move, because
        # the target instance mints its own tag rows per team.
        respx_mock.get(f"{BASE}/services/u1/tags").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"uuid": "t1", "name": "prod", "created_at": "x", "updated_at": "y"},
                    {"uuid": "t2", "name": "billing", "created_at": "x", "updated_at": "y"},
                ],
            )
        )
        assert await api.get_tag_names("services", "u1") == ["prod", "billing"]

    async def test_tag_names_drops_nameless_entries(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # A null name forwarded into a create body 422s the WHOLE resource, not
        # just the tag — upstream validates `tags.*` as string|min:2.
        respx_mock.get(f"{BASE}/services/u1/tags").mock(
            return_value=httpx.Response(
                200, json=[{"uuid": "t1", "name": None}, {"uuid": "t2", "name": "keep"}, {}]
            )
        )
        assert await api.get_tag_names("services", "u1") == ["keep"]

    async def test_no_tags_is_an_empty_list_not_an_error(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/databases/u1/tags").mock(
            return_value=httpx.Response(200, json=[])
        )
        assert await api.get_tag_names("databases", "u1") == []

    async def test_delete_passes_delete_volumes_flag(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.delete(f"{BASE}/databases/u1").mock(
            return_value=httpx.Response(200, json={})
        )
        await api.delete_resource("databases", "u1", delete_volumes=True)
        assert route.calls[0].request.url.params["deleteVolumes"] == "true"

    async def test_delete_defaults_to_keeping_volumes(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Never destroy data as a side effect of a default.
        route = respx_mock.delete(f"{BASE}/databases/u1").mock(
            return_value=httpx.Response(200, json={})
        )
        await api.delete_resource("databases", "u1")
        assert route.calls[0].request.url.params["deleteVolumes"] == "false"

    async def test_empty_body_returns_none(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.post(f"{BASE}/applications/u1/start").mock(return_value=httpx.Response(204))
        assert await api.start("applications", "u1") is None

    async def test_version(self, api: CoolifyClient, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/version").mock(
            return_value=httpx.Response(200, json={"version": "4.0.0-beta.400"})
        )
        assert await api.version() == "4.0.0-beta.400"


async def test_context_manager_closes(respx_mock: respx.Router) -> None:
    respx_mock.get(f"{BASE}/version").mock(
        return_value=httpx.Response(200, json={"version": "4.0.0"})
    )
    async with CoolifyClient(HOST, "t") as api:
        assert await api.version() == "4.0.0"
    assert api._client.is_closed
