"""Coolify 4.7 platform changes: the token throttle, the authorized_keys lock, the F2 host-key pin.

Each answers one finding of the v4.6.0 -> v4.7.x release-tag audit:

* ThrottleFailedApiRequests (v4.7.0) blocks a whole TOKEN for 1/5/15 minutes
  after 30 refused requests in a minute, from any client of it. The client must
  say so out loud and keep waiting, never abort a rollback early.
* AuthorizedKeys (v4.7.0) edits the SSH user's authorized_keys under a mkdir
  lock. The ephemeral-key append and the sed revoke take the same lock.
* Coolify (4.7.0) pins its own host's SSH key; F2 moves the pin and not the key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from bg_coolify_migrate.api import client as client_mod
from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import coolify_versions as cv
from bg_coolify_migrate.domain import host_keys
from bg_coolify_migrate.domain.statemachine import Outcome
from bg_coolify_migrate.engine import keys
from bg_coolify_migrate.engine.executor import RunResult
from bg_coolify_migrate.errors import CoolifyApiError, TransferError
from bg_coolify_migrate.server import runner as server_runner
from bg_coolify_migrate.server.inventory import ServerInventory, take
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


# ── F2: Coolify's pin of its own host key ────────────────────────────────────


def _localhost(**settings: Any) -> dict[str, Any]:
    pinned: dict[str, Any] = {
        "ssh_host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOld",
        "ssh_host_key_fingerprints": ["SHA256:old (ssh-ed25519)"],
        "is_ssh_host_key_check_disabled": False,
        "is_ssh_host_key_check_enforced": False,
    }
    pinned.update(settings)
    return {
        "uuid": "srv-local",
        "name": "localhost",
        "ip": "host.docker.internal",
        "settings": pinned,
    }


V47 = (4, 7, 0)


class TestFeatureRow:
    def test_pinning_starts_in_4_7_0(self) -> None:
        assert not cv.supports("ssh_host_key_pinning", (4, 6, 0))
        assert cv.supports("ssh_host_key_pinning", V47)
        assert cv.supports("ssh_host_key_pinning", (4, 7, 1))


class TestInstanceCheckingMode:
    @pytest.mark.parametrize(
        ("text", "mode"),
        [
            ("APP_KEY=x\n", "warn"),  # absent: Coolify's default
            ("SSH_HOST_KEY_CHECKING=enforce\n", "enforce"),
            ('SSH_HOST_KEY_CHECKING="Enforce "\n', "enforce"),  # strtolower(trim())
            ("SSH_HOST_KEY_CHECKING='off'\n", "off"),
            ("export SSH_HOST_KEY_CHECKING=off # no checks\n", "off"),
            ("SSH_HOST_KEY_CHECKING=strict\n", "warn"),  # unknown: Coolify uses warn
            ("SSH_HOST_KEY_CHECKING=\n", "warn"),
            ("SSH_HOST_KEY_CHECKING=off\nSSH_HOST_KEY_CHECKING=enforce\n", "enforce"),
            ("SSH_HOST_KEY_CHECKING=off\nSSH_HOST_KEY_CHECKING=warn\n", "warn"),
            ("#SSH_HOST_KEY_CHECKING=off\n", "warn"),
        ],
    )
    def test_read_as_coolify_reads_it(self, text: str, mode: str) -> None:
        assert host_keys.instance_checking_mode(text) == mode

    def test_an_unread_file_is_unknown(self) -> None:
        assert host_keys.instance_checking_mode(None) is None


class TestSelfHostKeyWarning:
    def test_silent_before_4_7(self) -> None:
        assert (
            host_keys.self_host_key_warning(_localhost(), version=(4, 6, 0), instance_mode="warn")
            is None
        )

    def test_warn_mode_says_it_still_connects_and_how_to_trust(self) -> None:
        warning = host_keys.self_host_key_warning(_localhost(), version=V47, instance_mode="warn")
        assert warning is not None
        assert "'localhost'" in warning
        assert "still connects" in warning
        assert "ENFORCED" not in warning
        assert "Servers > localhost > Security" in warning
        assert "/api/v1/servers/srv-local/host-key/reset" in warning
        assert "ssh-keygen -lf" in warning
        assert "Never send it empty" in warning

    @pytest.mark.parametrize(
        ("instance_mode", "flag"), [("enforce", False), ("warn", True), (None, True)]
    )
    def test_enforce_says_the_host_is_unreachable_after_the_fence(
        self, instance_mode: str | None, flag: bool
    ) -> None:
        warning = host_keys.self_host_key_warning(
            _localhost(is_ssh_host_key_check_enforced=flag),
            version=V47,
            instance_mode=instance_mode,
        )
        assert warning is not None
        assert "ENFORCED" in warning
        assert "cannot reach its own host" in warning
        assert "after the source is fenced" in warning

    def test_an_unread_mode_names_both_outcomes(self) -> None:
        warning = host_keys.self_host_key_warning(_localhost(), version=V47, instance_mode=None)
        assert warning is not None
        assert "warn mode (the default)" in warning
        assert "under enforce" in warning

    @pytest.mark.parametrize(
        ("server", "instance_mode"),
        [
            (_localhost(is_ssh_host_key_check_disabled=True), "enforce"),
            (_localhost(is_ssh_host_key_check_enforced=True), "off"),  # never weaker than off
            (_localhost(ssh_host_key=None, ssh_host_key_fingerprints=[]), "enforce"),
        ],
    )
    def test_silent_when_nothing_is_checked_or_nothing_pinned(
        self, server: dict[str, Any], instance_mode: str
    ) -> None:
        """No pin: Coolify pins whatever the new host presents on first use."""
        assert (
            host_keys.self_host_key_warning(server, version=V47, instance_mode=instance_mode)
            is None
        )

    def test_a_record_that_does_not_say_warns(self) -> None:
        server = {"uuid": "srv-local", "name": "localhost", "settings": {}}
        assert host_keys.self_host_key_warning(server, version=V47, instance_mode="warn")

    def test_an_unknown_version_fails_toward_the_warning(self) -> None:
        assert host_keys.self_host_key_warning(_localhost(), version=None, instance_mode="warn")


ENV_TEXT = """APP_KEY=base64:zSyKF1cWYVNQ0PmA7wNJvKTGxu2vHmT3nRoXqPqEBFI=
DB_PASSWORD=s3cr3tpassword
SSH_HOST_KEY_CHECKING=enforce
"""


def _source(*, second_read_fails: bool = False) -> FakeHost:
    host = FakeHost()
    host.on(r"test -e /data/coolify/source/\.env", exit_status=0)
    host.on(r"test -e /data/coolify$", exit_status=0)
    host.on(r"docker volume ls", stdout=json.dumps({"Name": "v1", "Driver": "local"}))
    host.on(r"docker ps", stdout="")
    host.on(r"du -sk", stdout="1024\n")
    host.on(r"find .* -type f", stdout="5\n")
    reads: list[dict[str, object]] = [{"stdout": ENV_TEXT}]
    reads.append({"exit_status": 1} if second_read_fails else {"stdout": ENV_TEXT})
    host.on_sequence(r"cat", reads)
    host.on(r"test -e", exit_status=1)  # fence marker absent
    return host


def _target_host() -> FakeHost:
    host = FakeHost(SshTarget(host="10.0.0.2"))
    host.on(r"command -v docker", exit_status=0)
    host.on(r"test -e /data/coolify", exit_status=1)
    host.on(r"df -Pk", stdout="999999999")
    return host


class TestInventoryPlumbing:
    async def test_the_plan_warns_with_the_mode_from_the_copied_env(self) -> None:
        inv = await take(
            _source(),  # type: ignore[arg-type]
            _target_host(),  # type: ignore[arg-type]
            coolify_version="4.7.0",
            self_server=_localhost(),
        )
        assert not inv.is_blocked  # a warning, never a refusal
        assert "ENFORCED" in inv.self_host_key_warning
        assert inv.self_host_key_warning in inv.warnings

    async def test_no_warning_before_4_7(self) -> None:
        inv = await take(
            _source(),  # type: ignore[arg-type]
            _target_host(),  # type: ignore[arg-type]
            coolify_version="4.6.0",
            self_server=_localhost(),
        )
        assert inv.self_host_key_warning == ""
        assert not any("host key" in w for w in inv.warnings)

    async def test_an_unreadable_env_leaves_the_mode_unknown(self) -> None:
        inv = await take(
            _source(second_read_fails=True),  # type: ignore[arg-type]
            _target_host(),  # type: ignore[arg-type]
            coolify_version="4.7.0",
            self_server=_localhost(),
        )
        assert "under enforce" in inv.self_host_key_warning

    async def test_the_env_text_never_reaches_the_inventory(self) -> None:
        inv = await take(
            _source(),  # type: ignore[arg-type]
            _target_host(),  # type: ignore[arg-type]
            coolify_version="4.7.0",
            self_server=_localhost(),
        )
        dumped = inv.model_dump_json()
        assert "s3cr3tpassword" not in dumped
        assert "zSyKF1cW" not in dumped


def _inventory(warning: str) -> ServerInventory:
    return ServerInventory(
        source_host="10.0.0.1",
        target_host="10.0.0.2",
        coolify_version="4.7.0",
        self_host_key_warning=warning,
    )


class TestRunNotice:
    def test_a_success_repeats_the_warning_as_a_notice(self) -> None:
        result = RunResult(outcome=Outcome.SUCCEEDED, reached=None)
        server_runner._note_self_host_key(result, _inventory("trust the new key"))
        assert result.notices == ["trust the new key"]

    def test_a_rolled_back_run_does_not(self) -> None:
        """The rollback starts the source, whose pin matches its own key."""
        result = RunResult(outcome=Outcome.ROLLED_BACK, reached=None)
        server_runner._note_self_host_key(result, _inventory("trust the new key"))
        assert result.notices == []

    def test_nothing_to_repeat(self) -> None:
        result = RunResult(outcome=Outcome.SUCCEEDED, reached=None)
        server_runner._note_self_host_key(result, _inventory(""))
        assert result.notices == []


class _Api:
    """Just what _source_ssh reads: the server list, the record, the keys."""

    def __init__(self, servers: list[dict[str, Any]]) -> None:
        self._servers = servers

    async def list_servers(self) -> list[dict[str, Any]]:
        return self._servers

    async def get_server(self, uuid: str) -> dict[str, Any]:
        return next(s for s in self._servers if s["uuid"] == uuid)

    async def get(self, path: str) -> list[dict[str, Any]]:
        return []


class TestSourceRecord:
    async def test_the_listed_record_comes_back_with_its_settings(self) -> None:
        own = _localhost()
        other = {"uuid": "srv-2", "name": "edge", "ip": "10.0.0.9", "settings": {}}
        target, record = await server_runner._source_ssh(_Api([other, own]), None)  # type: ignore[arg-type]
        assert record is own
        assert target.host == "host.docker.internal"
        assert record["settings"]["ssh_host_key_fingerprints"]
