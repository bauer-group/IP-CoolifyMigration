"""Coolify 4.7 platform changes: the token throttle, the authorized_keys lock.

Each answers one finding of the v4.6.0 -> v4.7.x release-tag audit:

* ThrottleFailedApiRequests (v4.7.0) blocks a whole TOKEN for 1/5/15 minutes
  after 30 refused requests in a minute, from any client of it. The client must
  say so out loud and keep waiting, never abort a rollback early.
* AuthorizedKeys (v4.7.0) edits the SSH user's authorized_keys under a mkdir
  lock. The ephemeral-key append and the sed revoke take the same lock.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from bg_coolify_migrate.api import client as client_mod
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.engine import keys
from bg_coolify_migrate.errors import CoolifyApiError, TransferError
from bg_coolify_migrate.transfer.ssh import SshTarget
from tests.conftest import FakeHost

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


# ── Coolify's authorized_keys lock ───────────────────────────────────────────


def _target() -> FakeHost:
    target = FakeHost(SshTarget(host="10.0.0.2"))
    target.on(r"mkdir -p ~/\.ssh", exit_status=0)
    return target


class TestAuthorizedKeysLock:
    def test_the_prelude_is_coolifys_runlocked(self) -> None:
        """AuthorizedKeys::runLocked v4.7.0:164-182, line for line."""
        command = keys._locked("EDIT")
        assert 'lock="$HOME/.ssh/.coolify-authorized-keys.lock"' in command
        assert 'find "$lock" -maxdepth 0 -type d -mmin +10 -exec rmdir {} \\;' in command
        assert 'until mkdir "$lock" 2>/dev/null; do' in command
        assert '[ "$attempt" -ge 30 ]' in command
        assert "sleep 1; done;" in command
        assert "trap 'rmdir \"$lock\"' EXIT" in command
        assert "trap 'exit 1' HUP INT TERM" in command

    def test_the_edit_runs_only_once_the_lock_is_held_and_released(self) -> None:
        command = keys._locked("EDIT")
        assert command.endswith("EDIT")
        assert command.index('until mkdir "$lock"') < command.index("trap 'rmdir")
        assert command.index("trap 'rmdir") < command.index("EDIT")

    async def test_install_appends_under_the_lock(self) -> None:
        target = _target()
        target.on(r">> ~/\.ssh/authorized_keys", exit_status=0)
        source = FakeHost()
        source.on(r"mkdir -p /root/\.coolify-migrate", exit_status=0)

        await keys.install(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]

        append = next(c for c in target.commands if ">> ~/.ssh/authorized_keys" in c)
        assert append.startswith(keys._locked("")[:60])
        assert append.index('until mkdir "$lock"') < append.index(">> ~/.ssh/authorized_keys")

    async def test_revoke_rewrites_under_the_lock(self) -> None:
        """sed -i renames a copy over the file: unlocked, it can undo Coolify's
        removal of a rotated-away key (removePublicKey v4.7.0:127-148)."""
        target = _target()
        target.on(r"sed -i", exit_status=0)
        source = FakeHost()
        source.on(r"rm -rf", exit_status=0)

        await keys.revoke(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]

        sed = next(c for c in target.commands if "sed -i" in c)
        assert sed.index("trap 'rmdir") < sed.index("sed -i")
        assert "bg-coolify-migrate-m1" in sed

    async def test_a_held_lock_fails_install_clearly_before_the_private_key_moves(
        self,
    ) -> None:
        target = _target()
        target.on(r">> ~/\.ssh/authorized_keys", exit_status=keys.LOCK_BUSY_EXIT)
        source = FakeHost()  # no stubs: any command on it would raise

        with pytest.raises(TransferError, match="stayed locked") as info:
            await keys.install(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]
        assert ".coolify-authorized-keys.lock" in (info.value.hint or "")
        assert source.commands == []

    async def test_a_held_lock_on_revoke_is_logged_and_the_source_still_cleaned(
        self,
    ) -> None:
        target = _target()
        target.on(r"sed -i", exit_status=keys.LOCK_BUSY_EXIT)
        source = FakeHost()
        source.on(r"rm -rf", exit_status=0)

        with capture_logs() as logs:
            await keys.revoke(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]

        events = [e["event"] for e in logs]
        assert "keys.revoke_lock_busy" in events
        assert "keys.revoke_failed" in events
        assert any("rm -rf" in c for c in source.commands)


_SH = shutil.which("sh")


@pytest.mark.skipif(_SH is None, reason="needs a POSIX sh")
class TestLockInARealShell:
    """The prelude is shell text; only a shell can say it takes and frees the lock."""

    def _run(self, home: Path, command: str) -> subprocess.CompletedProcess[str]:
        assert _SH is not None
        env = {**os.environ, "HOME": home.as_posix()}
        return subprocess.run(
            [_SH, "-c", command], env=env, capture_output=True, text=True, timeout=60
        )

    def _lock(self, home: Path) -> Path:
        return home / ".ssh" / ".coolify-authorized-keys.lock"

    def test_the_edit_runs_and_the_lock_is_released(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, keys._locked("echo line >> ~/.ssh/authorized_keys"))
        assert result.returncode == 0, result.stderr
        assert (tmp_path / ".ssh" / "authorized_keys").read_text() == "line\n"
        assert not self._lock(tmp_path).exists()

    def test_the_edits_exit_status_survives_the_release(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, keys._locked("exit 7"))
        assert result.returncode == 7
        assert not self._lock(tmp_path).exists()

    def test_a_fresh_held_lock_waits_then_fails_busy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(keys, "LOCK_ATTEMPTS", 2)
        self._lock(tmp_path).mkdir(parents=True)
        result = self._run(tmp_path, keys._locked("echo line >> ~/.ssh/authorized_keys"))
        assert result.returncode == keys.LOCK_BUSY_EXIT
        assert "is held by another edit" in result.stderr
        # Not ours to remove, and the edit never ran.
        assert self._lock(tmp_path).exists()
        assert not (tmp_path / ".ssh" / "authorized_keys").exists()

    def test_a_stale_lock_is_taken_over(self, tmp_path: Path) -> None:
        lock = self._lock(tmp_path)
        lock.mkdir(parents=True)
        old = time.time() - 20 * 60
        os.utime(lock, (old, old))
        result = self._run(tmp_path, keys._locked("echo line >> ~/.ssh/authorized_keys"))
        assert result.returncode == 0, result.stderr
        assert not lock.exists()
