"""Coolify 4.7 platform changes: the token throttle.

Each answers one finding of the v4.6.0 -> v4.7.x release-tag audit:

* ThrottleFailedApiRequests (v4.7.0) blocks a whole TOKEN for 1/5/15 minutes
  after 30 refused requests in a minute, from any client of it. The client must
  say so out loud and keep waiting, never abort a rollback early.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from bg_coolify_migrate.api import client as client_mod
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.errors import CoolifyApiError

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
TOKEN = "tok-secret-123"

#: The exact 429 body of ThrottleFailedApiRequests v4.7.0:47-49.
BLOCKED = {"message": "Too many failed API requests. Retry after 297 seconds."}


@pytest.fixture
async def api() -> AsyncIterator[CoolifyClient]:
    client = CoolifyClient(HOST, TOKEN, max_retries=2)
    yield client
    await client.aclose()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every backoff instead of sleeping it: a real block is 900 s."""
    recorded: list[float] = []

    async def _sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(client_mod.asyncio, "sleep", _sleep)
    return recorded


# ── the failed-request throttle ──────────────────────────────────────────────


class TestTokenBlocked:
    async def test_a_block_is_warned_about_and_waited_out(
        self, api: CoolifyClient, respx_mock: respx.Router, sleeps: list[float]
    ) -> None:
        route = respx_mock.get(f"{BASE}/servers").mock(
            side_effect=[
                httpx.Response(429, json=BLOCKED, headers={"Retry-After": "297"}),
                httpx.Response(200, json=[{"uuid": "s1"}]),
            ]
        )
        with capture_logs() as logs:
            assert await api.list_servers() == [{"uuid": "s1"}]

        assert route.call_count == 2
        # Retry-After is the floor, not a hint: retrying earlier only gets
        # another 429, and the remaining block is exactly what Coolify said.
        assert sleeps == [297.0]
        blocked = [e for e in logs if e["event"] == "api.token_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["log_level"] == "warning"
        assert blocked[0]["method"] == "GET"
        assert blocked[0]["path"] == "/servers"
        assert blocked[0]["retry_after"] == 297.0
        assert blocked[0]["attempt"] == 1

    async def test_the_warning_never_carries_the_token(
        self, api: CoolifyClient, respx_mock: respx.Router, sleeps: list[float]
    ) -> None:
        respx_mock.get(f"{BASE}/servers").mock(
            side_effect=[
                httpx.Response(429, json=BLOCKED, headers={"Retry-After": "1"}),
                httpx.Response(200, json=[]),
            ]
        )
        with capture_logs() as logs:
            await api.list_servers()
        assert TOKEN not in repr(logs)
        assert "Authorization" not in repr(logs)

    async def test_a_create_that_must_not_double_still_waits(
        self, api: CoolifyClient, respx_mock: respx.Router, sleeps: list[float]
    ) -> None:
        """The block answers before the controller runs (:45-50): _THROTTLED holds."""
        route = respx_mock.post(f"{BASE}/databases/d1/backups").mock(
            side_effect=[
                httpx.Response(429, json=BLOCKED, headers={"Retry-After": "60"}),
                httpx.Response(201, json={"uuid": "b1"}),
            ]
        )
        assert await api.create_backup("d1", {"frequency": "daily"}) == "b1"
        assert route.call_count == 2

    async def test_exhausted_retries_name_the_block_and_the_shared_token(
        self, api: CoolifyClient, respx_mock: respx.Router, sleeps: list[float]
    ) -> None:
        route = respx_mock.get(f"{BASE}/servers").mock(
            return_value=httpx.Response(429, json=BLOCKED, headers={"Retry-After": "900"})
        )
        with pytest.raises(CoolifyApiError) as info:
            await api.list_servers()

        assert route.call_count == 3  # never aborted early: every retry waited
        assert sleeps == [900.0, 900.0]
        assert info.value.status_code == 429
        hint = info.value.hint or ""
        assert "1, then 5, then 15 minutes" in hint
        assert "ANY client" in hint
        assert "token of its own" in hint
        assert "`delete`" in hint

    async def test_an_ordinary_429_gets_neither_warning_nor_hint(
        self, api: CoolifyClient, respx_mock: respx.Router, sleeps: list[float]
    ) -> None:
        """The ordinary API rate limit (config/api.php v4.7.0:4) is routine and says
        something else."""
        respx_mock.get(f"{BASE}/servers").mock(
            return_value=httpx.Response(
                429, json={"message": "Too Many Attempts."}, headers={"Retry-After": "0"}
            )
        )
        with capture_logs() as logs, pytest.raises(CoolifyApiError) as info:
            await api.list_servers()
        assert info.value.hint is None
        assert not [e for e in logs if e["event"] == "api.token_blocked"]

    async def test_a_non_json_429_is_not_mistaken_for_the_block(
        self, api: CoolifyClient, respx_mock: respx.Router, sleeps: list[float]
    ) -> None:
        respx_mock.get(f"{BASE}/servers").mock(
            return_value=httpx.Response(429, text="Too many failed API requests")
        )
        with capture_logs() as logs, pytest.raises(CoolifyApiError) as info:
            await api.list_servers()
        assert info.value.hint is None
        assert not [e for e in logs if e["event"] == "api.token_blocked"]
