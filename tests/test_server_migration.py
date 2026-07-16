"""Tests for F2: whole-instance migration.

The APP_KEY tests are the important ones. It decrypts every credential Coolify
holds, and it survives only because of an ordering that Geczy's script gets right
by accident and never mentions.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.domain.statemachine import Compensation, rollback_plan_for
from bg_coolify_migrate.errors import QuiesceError
from bg_coolify_migrate.server import fencing
from bg_coolify_migrate.server.appkey import (
    COOLIFY_ENV_PATH,
    AppKeyError,
    ProbeResult,
    assert_survived,
    decrypt_probe,
    extract_app_key,
    extract_db_password,
    fingerprint,
    read,
)
from bg_coolify_migrate.server.inventory import take
from bg_coolify_migrate.server.statemachine import (
    COMPENSATION,
    ORDER,
    ServerState,
    transfer_precedes_install,
)
from tests.conftest import FakeHost

ENV_TEXT = """APP_NAME=Coolify
APP_ID=abc123
APP_KEY=base64:zSyKF1cWYVNQ0PmA7wNJvKTGxu2vHmT3nRoXqPqEBFI=
DB_USERNAME=coolify
DB_PASSWORD=s3cr3tpassword
REDIS_PASSWORD=redispass
"""


class TestOrderingInvariant:
    def test_transfer_precedes_install(self) -> None:
        # THE invariant of the whole feature. install.sh merges the .env with
        # existing values winning and only fills EMPTY vars, so a .env that is
        # already in place keeps its APP_KEY. Reverse these two and it still
        # appears to work — until extraction fails and every secret becomes
        # permanently undecryptable.
        assert transfer_precedes_install()
        assert ORDER.index(ServerState.TRANSFER) < ORDER.index(ServerState.INSTALL_COOLIFY)

    def test_app_key_is_read_before_anything_moves(self) -> None:
        # You cannot assert a key survived if you never saw it.
        assert ORDER.index(ServerState.READ_APP_KEY) < ORDER.index(ServerState.STOP_SOURCE)
        assert ORDER.index(ServerState.READ_APP_KEY) < ORDER.index(ServerState.TRANSFER)

    def test_assert_follows_install(self) -> None:
        assert ORDER.index(ServerState.INSTALL_COOLIFY) < ORDER.index(ServerState.ASSERT_APP_KEY)

    def test_verify_precedes_install(self) -> None:
        # No point installing Coolify onto data we have not proven arrived.
        assert ORDER.index(ServerState.VERIFY) < ORDER.index(ServerState.INSTALL_COOLIFY)

    def test_fence_is_last(self) -> None:
        # Never silence the old instance before the new one is proven working.
        assert ORDER[-2] is ServerState.FENCE_SOURCE

    def test_every_state_appears_once(self) -> None:
        assert len(ORDER) == len(set(ORDER)) == len(ServerState)


class TestCompensations:
    def test_stopping_docker_is_compensated_by_starting_it(self) -> None:
        # F2's outage lasts the whole transfer, so this is THE compensation.
        assert COMPENSATION[ServerState.STOP_SOURCE] == (Compensation.START_SOURCE_DOCKER,)

    def test_read_only_states_have_no_compensation(self) -> None:
        for state in (
            ServerState.INIT,
            ServerState.PREFLIGHT,
            ServerState.INVENTORY,
            ServerState.READ_APP_KEY,
            ServerState.VERIFY,
            ServerState.RECONCILE,
        ):
            assert state not in COMPENSATION

    def test_install_has_no_compensation(self) -> None:
        # Uninstalling Coolify from a box we were told was empty would be a
        # bigger intervention than leaving it there, inert.
        assert ServerState.INSTALL_COOLIFY not in COMPENSATION

    def test_rollback_after_transfer_restarts_docker_and_wipes(self) -> None:
        plan = rollback_plan_for(
            [ServerState.STOP_SOURCE.value, ServerState.TRANSFER.value],
            order=[s.value for s in ORDER],
            compensation_map={k.value: v for k, v in COMPENSATION.items()},
        )
        comps = [s.compensation for s in plan]
        assert comps == [
            Compensation.WIPE_TARGET_DATA,
            Compensation.REVOKE_EPHEMERAL_KEY,
            Compensation.START_SOURCE_DOCKER,
        ]

    def test_rollback_after_fence_unfences_first(self) -> None:
        plan = rollback_plan_for(
            [ServerState.STOP_SOURCE.value, ServerState.FENCE_SOURCE.value],
            order=[s.value for s in ORDER],
            compensation_map={k.value: v for k, v in COMPENSATION.items()},
        )
        comps = [s.compensation for s in plan]
        assert comps.index(Compensation.UNFENCE_SOURCE) < comps.index(
            Compensation.START_SOURCE_DOCKER
        )


class TestAppKeyExtraction:
    def test_extracts_app_key(self) -> None:
        assert extract_app_key(ENV_TEXT) == "base64:zSyKF1cWYVNQ0PmA7wNJvKTGxu2vHmT3nRoXqPqEBFI="

    def test_extracts_db_password(self) -> None:
        # Matters as much as APP_KEY: the copied coolify-db volume holds the OLD
        # password hash, so a regenerated one locks Coolify out of its own DB.
        assert extract_db_password(ENV_TEXT) == "s3cr3tpassword"

    def test_missing_key_returns_none(self) -> None:
        assert extract_app_key("APP_NAME=Coolify\n") is None

    def test_empty_key_returns_none(self) -> None:
        assert extract_app_key("APP_KEY=\n") is None

    def test_strips_quotes(self) -> None:
        assert extract_app_key('APP_KEY="base64:abc"') == "base64:abc"

    def test_ignores_similar_keys(self) -> None:
        assert extract_app_key("MY_APP_KEY=nope\nAPP_KEY=yes\n") == "yes"


class TestFingerprint:
    def test_is_stable(self) -> None:
        assert fingerprint("base64:abc") == fingerprint("base64:abc")

    def test_differs_for_different_keys(self) -> None:
        assert fingerprint("a") != fingerprint("b")

    def test_does_not_leak_the_key(self) -> None:
        # We journal and log THIS, never the key.
        secret = "base64:zSyKF1cWYVNQ0PmA7wNJvKTGxu2vHmT3nRoXqPqEBFI="
        assert secret not in fingerprint(secret)
        assert fingerprint(secret).startswith("sha256:")

    def test_ignores_surrounding_whitespace(self) -> None:
        assert fingerprint(" abc \n") == fingerprint("abc")


class TestAppKeyRead:
    async def test_reads_key_and_password(self, fake_host: FakeHost) -> None:
        fake_host.on(r"test -e", exit_status=0)
        fake_host.on(r"cat", stdout=ENV_TEXT)
        key, password = await read(fake_host)  # type: ignore[arg-type]
        assert key.startswith("base64:")
        assert password == "s3cr3tpassword"

    async def test_missing_env_file_is_fatal(self, fake_host: FakeHost) -> None:
        # Refusing here — before anything is stopped — is the whole point.
        fake_host.on(r"test -e", exit_status=1)
        with pytest.raises(AppKeyError, match="does not exist"):
            await read(fake_host)  # type: ignore[arg-type]

    async def test_missing_key_is_fatal(self, fake_host: FakeHost) -> None:
        fake_host.on(r"test -e", exit_status=0)
        fake_host.on(r"cat", stdout="APP_NAME=Coolify\n")
        with pytest.raises(AppKeyError, match="APP_KEY not found"):
            await read(fake_host)  # type: ignore[arg-type]

    async def test_error_explains_the_stakes(self, fake_host: FakeHost) -> None:
        fake_host.on(r"test -e", exit_status=0)
        fake_host.on(r"cat", stdout="APP_NAME=x\n")
        with pytest.raises(AppKeyError) as exc:
            await read(fake_host)  # type: ignore[arg-type]
        assert "locked vault" in str(exc.value)


class TestAppKeySurvival:
    async def test_identical_key_passes(self, fake_host: FakeHost) -> None:
        fake_host.on(r"cat", stdout=ENV_TEXT)
        key = extract_app_key(ENV_TEXT)
        assert key
        await assert_survived(fake_host, expected=key)  # type: ignore[arg-type]

    async def test_regenerated_key_is_fatal(self, fake_host: FakeHost) -> None:
        # install.sh regenerated it -> the archive was NOT in place when it ran.
        fake_host.on(r"cat", stdout="APP_KEY=base64:DIFFERENTKEYENTIRELY=\n")
        with pytest.raises(AppKeyError, match="does not match"):
            await assert_survived(fake_host, expected="base64:original")  # type: ignore[arg-type]

    async def test_mismatch_explains_recovery(self, fake_host: FakeHost) -> None:
        fake_host.on(r"cat", stdout="APP_KEY=base64:OTHER=\n")
        with pytest.raises(AppKeyError) as exc:
            await assert_survived(fake_host, expected="base64:original")  # type: ignore[arg-type]
        assert "APP_PREVIOUS_KEYS" in str(exc.value)
        assert "undecryptable" in str(exc.value)

    async def test_mismatch_does_not_leak_either_key(self, fake_host: FakeHost) -> None:
        fake_host.on(r"cat", stdout="APP_KEY=base64:LEAKYTARGET=\n")
        with pytest.raises(AppKeyError) as exc:
            await assert_survived(fake_host, expected="base64:LEAKYSOURCE=")  # type: ignore[arg-type]
        assert "LEAKYSOURCE" not in str(exc.value)
        assert "LEAKYTARGET" not in str(exc.value)

    async def test_vanished_key_is_fatal(self, fake_host: FakeHost) -> None:
        fake_host.on(r"cat", stdout="APP_NAME=x\n")
        with pytest.raises(AppKeyError, match="vanished"):
            await assert_survived(fake_host, expected="base64:original")  # type: ignore[arg-type]

    async def test_db_password_mismatch_is_fatal(self, fake_host: FakeHost) -> None:
        # The copied coolify-db volume holds the OLD hash.
        fake_host.on(r"cat", stdout="APP_KEY=base64:same\nDB_PASSWORD=regenerated\n")
        with pytest.raises(AppKeyError, match="DB_PASSWORD"):
            await assert_survived(
                fake_host,  # type: ignore[arg-type]
                expected="base64:same",
                expected_db_password="original",
            )


class TestDecryptProbe:
    async def test_ok(self, fake_host: FakeHost) -> None:
        fake_host.on(r"artisan tinker", stdout="DECRYPT_OK\n")
        assert await decrypt_probe(fake_host) is ProbeResult.OK  # type: ignore[arg-type]

    async def test_empty_instance_is_ok(self, fake_host: FakeHost) -> None:
        fake_host.on(r"artisan tinker", stdout="NO_DATA\n")
        assert await decrypt_probe(fake_host) is ProbeResult.OK  # type: ignore[arg-type]

    async def test_decrypt_exception_is_terminal(self, fake_host: FakeHost) -> None:
        # Artisan RAN and still could not decrypt — the key and data disagree.
        fake_host.on(r"artisan tinker", stdout="DecryptException: MAC is invalid\n")
        assert await decrypt_probe(fake_host) is ProbeResult.DECRYPT_FAILED  # type: ignore[arg-type]

    async def test_command_failure_is_not_ready_not_a_failure(self, fake_host: FakeHost) -> None:
        """tinker could not run — the app is still booting. Transient, not corrupt.

        This is the distinction that F2 got wrong: it treated "cannot answer yet"
        as "the data is unreadable" and rolled back a successful migration one
        second after the container reported running. Now NOT_READY, which the
        caller polls on rather than aborting.
        """
        fake_host.on(r"artisan tinker", exit_status=1, stderr="no such container")
        assert await decrypt_probe(fake_host) is ProbeResult.NOT_READY  # type: ignore[arg-type]


class TestFencing:
    async def test_stop_docker_stops_containers_before_the_daemon(
        self, fake_host: FakeHost
    ) -> None:
        """The containers must be stopped FIRST, then the daemon.

        `systemctl stop docker` stops dockerd, but docker.service ships
        KillMode=process, so the containers keep running under containerd. Stop
        only the daemon and Postgres is still writing when the copy starts — a
        torn snapshot of Coolify's own database. The e2e F2 migration produced
        exactly that (pg_filenode.map missing, the target DB refusing to boot).
        """
        fake_host.on_sequence(
            r"docker ps -q", [{"stdout": "abc123\ndef456\n"}, {"stdout": ""}]
        )
        fake_host.on(r"docker stop -t 60", exit_status=0)
        fake_host.on(r"systemctl stop docker\.socket", exit_status=0)
        fake_host.on(r"systemctl stop docker", exit_status=0)
        fake_host.on(r"systemctl is-active", stdout="inactive\n")
        await fencing.stop_docker(fake_host)  # type: ignore[arg-type]
        assert any("docker stop -t 60" in c for c in fake_host.commands), (
            "did not stop the containers before the daemon"
        )

    async def test_containers_still_running_after_stop_is_fatal(
        self, fake_host: FakeHost
    ) -> None:
        """If a container survives the stop, copying now catches a live database."""
        fake_host.on_sequence(
            r"docker ps -q", [{"stdout": "abc123\n"}, {"stdout": "abc123\n"}]
        )  # STILL running after stop
        fake_host.on(r"docker stop -t 60", exit_status=0)
        with pytest.raises(QuiesceError, match="still running"):
            await fencing.stop_docker(fake_host)  # type: ignore[arg-type]

    async def test_docker_still_active_is_fatal(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps -q", stdout="")
        fake_host.on(r"systemctl stop docker\.socket", exit_status=0)
        fake_host.on(r"systemctl stop docker", exit_status=0)
        fake_host.on(r"systemctl is-active", stdout="active\n")
        with pytest.raises(QuiesceError, match="still active"):
            await fencing.stop_docker(fake_host)  # type: ignore[arg-type]

    async def test_stop_failure_is_fatal(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps -q", stdout="")
        fake_host.on(r"systemctl stop docker\.socket", exit_status=0)
        fake_host.on(r"systemctl stop docker", exit_status=1, stderr="unit not found")
        with pytest.raises(QuiesceError, match="could not stop Docker"):
            await fencing.stop_docker(fake_host)  # type: ignore[arg-type]

    async def test_fence_stops_only_coolifys_own_containers(self, fake_host: FakeHost) -> None:
        # Not the workloads: they are already down, and leaving them means an
        # unfence restores the box exactly.
        fake_host.on(r"docker stop", exit_status=0)
        fake_host.on(r"docker update", exit_status=0)
        fake_host.on(r"printf", exit_status=0)
        result = await fencing.fence(fake_host, target_host="new-host")  # type: ignore[arg-type]
        assert set(result["stopped"]) == set(fencing.COOLIFY_CONTAINERS)

    async def test_fence_disables_restart_policy(self, fake_host: FakeHost) -> None:
        # Otherwise the daemon brings the old brain back on the next boot.
        fake_host.on(r"docker stop", exit_status=0)
        fake_host.on(r"docker update", exit_status=0)
        fake_host.on(r"printf", exit_status=0)
        await fencing.fence(fake_host, target_host="new-host")  # type: ignore[arg-type]
        assert any("--restart=no" in c for c in fake_host.commands)

    async def test_unfence_restores(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker update", exit_status=0)
        fake_host.on(r"docker start", exit_status=0)
        fake_host.on(r"rm -f", exit_status=0)
        await fencing.unfence(fake_host)  # type: ignore[arg-type]
        assert any("--restart=unless-stopped" in c for c in fake_host.commands)
        assert any("docker start" in c for c in fake_host.commands)


class TestInventory:
    def _source(self) -> FakeHost:
        import json

        host = FakeHost()
        # Order matters: FakeHost matches in insertion order, and the .env probe
        # must not fall through to the generic `test -e` stub below.
        host.on(r"test -e /data/coolify/source/\.env", exit_status=0)
        host.on(r"test -e /data/coolify$", exit_status=0)
        host.on(r"docker volume ls", stdout=json.dumps({"Name": "v1", "Driver": "local"}))
        host.on(
            r"docker ps",
            stdout=json.dumps({"ID": "c1", "Names": "app", "State": "running", "Labels": ""}),
        )
        host.on(
            r"docker inspect .*Mounts",
            stdout=json.dumps(
                [
                    {"Type": "volume", "Name": "v1", "Source": "/x", "Destination": "/d"},
                    {"Type": "bind", "Source": "/srv/extra", "Destination": "/e"},
                ]
            ),
        )
        host.on(r"du -sk", stdout="1024\n")
        host.on(r"find .* -type f", stdout="5\n")
        host.on(r"cat", stdout=ENV_TEXT)
        host.on(r"test -e", exit_status=1)  # fence marker absent
        return host

    def _target(self, *, free_kb: str = "999999999", has_coolify: bool = False) -> FakeHost:
        from bg_coolify_migrate.transfer.ssh import SshTarget

        host = FakeHost(SshTarget(host="10.0.0.2"))
        host.on(r"command -v docker", exit_status=0)
        host.on(r"test -e /data/coolify", exit_status=0 if has_coolify else 1)
        host.on(r"df -Pk", stdout=free_kb)
        return host

    async def test_clean_inventory(self) -> None:
        inv = await take(
            self._source(),  # type: ignore[arg-type]
            self._target(),  # type: ignore[arg-type]
            coolify_version="4.0.0",
        )
        assert not inv.is_blocked
        assert "v1" in inv.volumes
        assert inv.app_key_fingerprint.startswith("sha256:")

    async def test_bind_mounts_are_captured(self) -> None:
        # Geczy drops these entirely: `docker inspect .Name` is empty for a bind
        # and its [ -n ] guard filters them out.
        inv = await take(
            self._source(),  # type: ignore[arg-type]
            self._target(),  # type: ignore[arg-type]
            coolify_version="4.0.0",
        )
        assert "/srv/extra" in inv.bind_mounts

    async def test_non_empty_target_blocks(self) -> None:
        # `tar -Pxf - -C /` MERGES two Postgres data dirs. Geczy never checks.
        inv = await take(
            self._source(),  # type: ignore[arg-type]
            self._target(has_coolify=True),  # type: ignore[arg-type]
            coolify_version="4.0.0",
        )
        assert inv.is_blocked
        assert any("MERGES" in r for r in inv.blocking_reasons)

    async def test_force_overwrite_downgrades_to_a_warning(self) -> None:
        inv = await take(
            self._source(),  # type: ignore[arg-type]
            self._target(has_coolify=True),  # type: ignore[arg-type]
            coolify_version="4.0.0",
            force_overwrite=True,
        )
        assert not inv.is_blocked
        assert any("force-overwrite" in w for w in inv.warnings)

    async def test_insufficient_disk_blocks_proportionally(self) -> None:
        # Geczy checks a fixed 1 GB floor and never compares against the total it
        # just computed - hence its 100 GB issue.
        inv = await take(
            self._source(),  # type: ignore[arg-type]
            self._target(free_kb="1\n"),  # type: ignore[arg-type]
            coolify_version="4.0.0",
        )
        assert inv.is_blocked
        assert any("free but needs" in r for r in inv.blocking_reasons)


def test_env_path_is_where_coolify_keeps_it() -> None:
    assert COOLIFY_ENV_PATH == "/data/coolify/source/.env"
