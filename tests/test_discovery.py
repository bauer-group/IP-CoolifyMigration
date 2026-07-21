"""Tests for docker introspection and the quiesce gate.

The quiesce gate is the correctness foundation of the whole tool: everything else
rests on the claim that nothing is writing while we copy. If that claim is false,
byte-exact verification is meaningless — we would have verified a torn snapshot
faithfully.
"""

from __future__ import annotations

import json

import pytest

from bg_coolify_migrate.discovery.docker import (
    STATE_GONE,
    Container,
    _parse_labels,
    container_labels,
    image_of,
    inspect_mounts,
    inspect_state,
    list_containers,
    list_volumes,
    path_size,
    volume_exists,
)
from bg_coolify_migrate.discovery.quiesce import (
    assert_previews_absent,
    assert_still_stopped,
    killed_since,
    snapshot,
    wait_until_stopped,
)
from bg_coolify_migrate.errors import QuiesceError
from bg_coolify_migrate.transfer.ssh import SshError
from tests.conftest import FakeHost


def _ps_line(name: str, state: str, labels: str = "") -> str:
    return json.dumps({"ID": f"id-{name}", "Names": name, "State": state, "Labels": labels})


LABELS = {"coolify.projectName": "shop", "coolify.environmentName": "production"}


class TestParseLabels:
    def test_simple(self) -> None:
        assert _parse_labels("a=1,b=2") == {"a": "1", "b": "2"}

    def test_value_containing_equals(self) -> None:
        # Traefik rules contain '='; splitting on every '=' would corrupt them.
        parsed = _parse_labels("traefik.http.routers.r.rule=Host(`a.com`),x=1")
        assert parsed["traefik.http.routers.r.rule"] == "Host(`a.com`)"
        assert parsed["x"] == "1"

    def test_empty(self) -> None:
        assert _parse_labels("") == {}


class TestListContainers:
    async def test_uses_dash_a_to_include_stopped(self, fake_host: FakeHost) -> None:
        # Geczy uses bare `docker ps`, so a stopped container's volume is silently
        # skipped and never even reported. Coolify's own code uses -a; so do we.
        fake_host.on(r"docker ps", stdout=_ps_line("web", "running"))
        await list_containers(fake_host, label_filters=LABELS)  # type: ignore[arg-type]
        assert "docker ps -a" in fake_host.commands[0]

    async def test_applies_every_label_filter(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps", stdout="")
        await list_containers(fake_host, label_filters=LABELS)  # type: ignore[arg-type]
        cmd = fake_host.commands[0]
        assert "coolify.projectName=shop" in cmd
        assert "coolify.environmentName=production" in cmd

    async def test_parses_containers(self, fake_host: FakeHost) -> None:
        fake_host.on(
            r"docker ps",
            stdout="\n".join(
                [
                    _ps_line("web", "running", "coolify.managed=true"),
                    _ps_line("db", "exited", "coolify.managed=true"),
                ]
            ),
        )
        containers = await list_containers(fake_host, label_filters=LABELS)  # type: ignore[arg-type]
        assert [c.name for c in containers] == ["web", "db"]
        assert containers[1].is_stopped

    async def test_unparseable_line_is_skipped_not_fatal(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps", stdout="{not json}\n" + _ps_line("web", "running"))
        containers = await list_containers(fake_host, label_filters=LABELS)  # type: ignore[arg-type]
        assert len(containers) == 1


class TestContainerProperties:
    @pytest.mark.parametrize("state", ["exited", "created", "dead", STATE_GONE])
    def test_stopped_states(self, state: str) -> None:
        # `gone` included: docker rm -f kills before it deletes, so a container
        # that no longer exists stopped first — and is certainly not writing.
        assert Container(id="i", name="n", state=state, labels={}).is_stopped

    @pytest.mark.parametrize("state", ["running", "restarting", "paused"])
    def test_running_states(self, state: str) -> None:
        assert not Container(id="i", name="n", state=state, labels={}).is_stopped

    def test_preview_detection(self) -> None:
        base = Container(id="i", name="n", state="running", labels={"coolify.pullRequestId": "0"})
        preview = Container(
            id="i", name="n", state="running", labels={"coolify.pullRequestId": "7"}
        )
        assert base.is_preview is False
        assert preview.is_preview is True

    def test_missing_pr_label_means_base_deploy(self) -> None:
        assert Container(id="i", name="n", state="running", labels={}).is_preview is False

    def test_malformed_pr_label_means_base_deploy(self) -> None:
        c = Container(id="i", name="n", state="running", labels={"coolify.pullRequestId": "x"})
        assert c.is_preview is False

    def test_sigkill_detection(self) -> None:
        # 137 = SIGKILL: the stop timeout was hit. A killed database has not
        # flushed, so its volume is a torn snapshot.
        killed = Container(id="i", name="n", state="exited", labels={}, exit_code=137)
        clean = Container(id="i", name="n", state="exited", labels={}, exit_code=0)
        assert killed.was_killed is True
        assert clean.was_killed is False


class TestInspect:
    async def test_state_and_exit_code(self, fake_host: FakeHost) -> None:
        fake_host.on(
            r"docker inspect .*State", stdout=json.dumps({"Status": "exited", "ExitCode": 0})
        )
        assert await inspect_state(fake_host, "c1") == ("exited", 0)  # type: ignore[arg-type]

    async def test_state_unparseable(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker inspect .*State", stdout="nonsense")
        assert await inspect_state(fake_host, "c1") == ("unknown", None)  # type: ignore[arg-type]

    async def test_removed_container_reports_gone(self, fake_host: FakeHost) -> None:
        """A container deleted between `docker ps -a` and the inspect.

        The normal case, not an edge case: Coolify's stop is `docker stop` then
        `docker rm -f` in one invocation, so a container listed as exited one
        round-trip ago is routinely already deleted. Raising here aborted the
        migration at the quiesce gate because the stop we asked for had worked.
        """
        fake_host.on(
            r"docker inspect .*State",
            stderr="Error: No such object: 92a6dca14b5f",
            exit_status=1,
        )
        assert await inspect_state(fake_host, "92a6dca14b5f") == (STATE_GONE, None)  # type: ignore[arg-type]

    async def test_daemon_failure_still_raises(self, fake_host: FakeHost) -> None:
        """A vanished container and an unreachable daemon must not look alike."""
        fake_host.on(
            r"docker inspect .*State",
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
            exit_status=1,
        )
        with pytest.raises(SshError):
            await inspect_state(fake_host, "c1")  # type: ignore[arg-type]

    async def test_mounts(self, fake_host: FakeHost) -> None:
        fake_host.on(
            r"docker inspect .*Mounts",
            stdout=json.dumps(
                [
                    {
                        "Type": "volume",
                        "Name": "pg-data",
                        "Source": "/var/lib/docker/volumes/pg-data/_data",
                        "Destination": "/var/lib/postgresql/data",
                        "RW": True,
                    },
                    {
                        "Type": "bind",
                        "Source": "/srv/config",
                        "Destination": "/etc/app",
                        "RW": False,
                    },
                ]
            ),
        )
        mounts = await inspect_mounts(fake_host, "c1")  # type: ignore[arg-type]
        assert len(mounts) == 2
        assert mounts[0].name == "pg-data"
        assert mounts[1].type == "bind"

    async def test_mounts_unparseable_returns_empty(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker inspect .*Mounts", stdout="{{bad")
        assert await inspect_mounts(fake_host, "c1") == []  # type: ignore[arg-type]

    async def test_image_of_gives_the_deployed_commit(self, fake_host: FakeHost) -> None:
        # The image tag IS the commit by construction, which makes it the only
        # trustworthy record of what is actually running.
        fake_host.on(r"docker inspect .*Config.Image", stdout="k8sgw04ggc8s:a1b2c3d\n")
        assert await image_of(fake_host, "c1") == "k8sgw04ggc8s:a1b2c3d"  # type: ignore[arg-type]

    async def test_image_of_failure_returns_none(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker inspect", exit_status=1)
        assert await image_of(fake_host, "c1") is None  # type: ignore[arg-type]

    async def test_container_labels(self, fake_host: FakeHost) -> None:
        # How we recover settings the API refuses to return.
        fake_host.on(
            r"docker inspect .*Config.Labels",
            stdout=json.dumps({"traefik.http.routers.r.rule": "Host(`a.com`)"}),
        )
        labels = await container_labels(fake_host, "c1")  # type: ignore[arg-type]
        assert labels["traefik.http.routers.r.rule"] == "Host(`a.com`)"


class TestVolumes:
    async def test_list_volumes(self, fake_host: FakeHost) -> None:
        fake_host.on(
            r"docker volume ls",
            stdout=json.dumps({"Name": "v1", "Driver": "local", "Labels": "coolify.managed=true"}),
        )
        volumes = await list_volumes(fake_host)  # type: ignore[arg-type]
        assert volumes[0].name == "v1"
        assert volumes[0].labels["coolify.managed"] == "true"

    async def test_volume_exists(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker volume inspect", exit_status=0)
        assert await volume_exists(fake_host, "v1") is True  # type: ignore[arg-type]

    async def test_volume_missing(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker volume inspect", exit_status=1)
        assert await volume_exists(fake_host, "v1") is False  # type: ignore[arg-type]


class TestPathSize:
    async def test_reports_bytes_and_count(self, fake_host: FakeHost) -> None:
        # Sizes drive a PROPORTIONAL disk check. Geczy's fixed 1 GB floor never
        # compares against the total it just computed.
        fake_host.on(r"du -sk", stdout="2048\n")
        fake_host.on(r"find .* -type f", stdout="17\n")
        assert await path_size(fake_host, "/vol") == (2048 * 1024, 17)  # type: ignore[arg-type]

    async def test_missing_path_is_zero_not_a_crash(self, fake_host: FakeHost) -> None:
        fake_host.on(r"du -sk", exit_status=1)
        fake_host.on(r"find", exit_status=1)
        assert await path_size(fake_host, "/nope") == (0, 0)  # type: ignore[arg-type]


class TestQuiesceSnapshot:
    async def test_resolves_exit_codes_for_stopped_containers(self, fake_host: FakeHost) -> None:
        # `docker ps` does not report exit codes, and we must distinguish a clean
        # stop from a SIGKILL at the timeout.
        fake_host.on(r"docker ps", stdout=_ps_line("db", "exited"))
        fake_host.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 0}))
        report = await snapshot(fake_host, label_filters=LABELS)  # type: ignore[arg-type]
        assert report.is_quiesced
        assert report.containers[0].exit_code == 0

    async def test_running_container_is_not_quiesced(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps", stdout=_ps_line("web", "running"))
        report = await snapshot(fake_host, label_filters=LABELS)  # type: ignore[arg-type]
        assert report.is_quiesced is False
        assert len(report.running) == 1


class TestPreviewGate:
    async def test_previews_block(self, fake_host: FakeHost) -> None:
        # Verified: POST /applications/{uuid}/stop does NOT stop previews
        # (StopApplication filters pullRequestId=0), so they keep writing.
        fake_host.on(
            r"docker ps", stdout=_ps_line("web-pr-7", "running", "coolify.pullRequestId=7")
        )
        with pytest.raises(QuiesceError, match="preview deployment"):
            await assert_previews_absent(fake_host, label_filters=LABELS)  # type: ignore[arg-type]

    async def test_error_explains_why_and_how_to_fix(self, fake_host: FakeHost) -> None:
        fake_host.on(
            r"docker ps", stdout=_ps_line("web-pr-7", "running", "coolify.pullRequestId=7")
        )
        with pytest.raises(QuiesceError) as exc:
            await assert_previews_absent(fake_host, label_filters=LABELS)  # type: ignore[arg-type]
        assert "pullRequestId=0" in str(exc.value)
        assert "--delete-previews" in str(exc.value)

    async def test_no_previews_passes(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps", stdout=_ps_line("web", "running", "coolify.pullRequestId=0"))
        fake_host.on(r"docker inspect", stdout=json.dumps({"Status": "running", "ExitCode": None}))
        await assert_previews_absent(fake_host, label_filters=LABELS)  # type: ignore[arg-type]


class TestWaitUntilStopped:
    async def test_already_stopped_returns_immediately(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps", stdout=_ps_line("db", "exited"))
        fake_host.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 0}))
        report = await wait_until_stopped(fake_host, label_filters=LABELS, timeout=1)  # type: ignore[arg-type]
        assert report.is_quiesced

    async def test_sigkill_is_fatal_not_a_warning(self, fake_host: FakeHost) -> None:
        # Mirroring an unflushed data directory byte-exactly just gives you a
        # faithful copy of corruption.
        fake_host.on(r"docker ps", stdout=_ps_line("db", "exited"))
        fake_host.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 137}))
        with pytest.raises(QuiesceError, match="SIGKILLed"):
            await wait_until_stopped(fake_host, label_filters=LABELS, timeout=1)  # type: ignore[arg-type]

    async def test_sigkill_error_explains_the_consequence(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps", stdout=_ps_line("db", "exited"))
        fake_host.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 137}))
        with pytest.raises(QuiesceError) as exc:
            await wait_until_stopped(fake_host, label_filters=LABELS, timeout=1)  # type: ignore[arg-type]
        assert "has not flushed" in str(exc.value)

    async def test_timeout_raises_naming_the_stragglers(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps", stdout=_ps_line("stubborn", "running"))
        with pytest.raises(QuiesceError, match="stubborn"):
            await wait_until_stopped(
                fake_host,  # type: ignore[arg-type]
                label_filters=LABELS,
                timeout=0.01,
                poll_interval=0.001,
            )

    async def test_empty_stack_is_quiesced(self, fake_host: FakeHost) -> None:
        fake_host.on(r"docker ps", stdout="")
        report = await wait_until_stopped(fake_host, label_filters=LABELS, timeout=1)  # type: ignore[arg-type]
        assert report.is_quiesced

    async def test_container_removed_mid_snapshot_is_quiesced(self, fake_host: FakeHost) -> None:
        """The failure that rolled back a real migration.

        `docker ps -a` lists the stack, and Coolify's `docker rm -f` lands before
        the per-container inspect. The gate must read that as "the stop worked",
        not abort — the exit codes it costs us are recovered from the event log
        by killed_since().
        """
        fake_host.on(r"docker ps", stdout=_ps_line("db", "exited"))
        fake_host.on(
            r"docker inspect",
            stderr="Error: No such object: id-db",
            exit_status=1,
        )
        report = await wait_until_stopped(fake_host, label_filters=LABELS, timeout=1)  # type: ignore[arg-type]
        assert report.is_quiesced
        assert not report.killed


class TestAssertStillStopped:
    async def test_restart_during_copy_is_detected(self, fake_host: FakeHost) -> None:
        # A container with restart: unless-stopped can be brought back by the
        # daemon mid-transfer. Neither predecessor checks; nothing would raise.
        before = FakeHost()
        before.on(r"docker ps", stdout=_ps_line("db", "exited"))
        before.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 0}))
        since = await snapshot(before, label_filters=LABELS)  # type: ignore[arg-type]

        fake_host.on(r"docker ps", stdout=_ps_line("db", "running"))
        with pytest.raises(QuiesceError, match="restarted during the copy"):
            await assert_still_stopped(fake_host, label_filters=LABELS, since=since)  # type: ignore[arg-type]

    async def test_new_container_is_detected(self, fake_host: FakeHost) -> None:
        before = FakeHost()
        before.on(r"docker ps", stdout=_ps_line("db", "exited"))
        before.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 0}))
        since = await snapshot(before, label_filters=LABELS)  # type: ignore[arg-type]

        fake_host.on(r"docker ps", stdout=_ps_line("something-else", "exited"))
        fake_host.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 0}))
        with pytest.raises(QuiesceError, match="appeared during the copy"):
            await assert_still_stopped(fake_host, label_filters=LABELS, since=since)  # type: ignore[arg-type]

    async def test_disappearance_is_tolerated(self, fake_host: FakeHost) -> None:
        """The copy's `before` snapshot can land inside Coolify's rm -f sweep.

        Demanding set equality failed finished 2.5 GB transfers because containers
        that were already dead finished being deleted. A container that is gone
        cannot have written to a volume we were mirroring.
        """
        before = FakeHost()
        before.on(
            r"docker ps", stdout="\n".join([_ps_line("db", "exited"), _ps_line("app", "exited")])
        )
        before.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 0}))
        since = await snapshot(before, label_filters=LABELS)  # type: ignore[arg-type]

        fake_host.on(r"docker ps", stdout="")
        await assert_still_stopped(fake_host, label_filters=LABELS, since=since)  # type: ignore[arg-type]

    async def test_unchanged_passes(self, fake_host: FakeHost) -> None:
        before = FakeHost()
        before.on(r"docker ps", stdout=_ps_line("db", "exited"))
        before.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 0}))
        since = await snapshot(before, label_filters=LABELS)  # type: ignore[arg-type]

        fake_host.on(r"docker ps", stdout=_ps_line("db", "exited"))
        fake_host.on(r"docker inspect", stdout=json.dumps({"Status": "exited", "ExitCode": 0}))
        await assert_still_stopped(fake_host, label_filters=LABELS, since=since)  # type: ignore[arg-type]


class TestKilledSince:
    """The SIGKILL check, which for a long while could not fire at all.

    Exit code 137 lives on a container, and Coolify's stop is `docker stop`
    followed by `docker rm -f` in one SSH invocation — so by the time anything
    polls, the record is gone. The event log outlives the container, and that is
    what this reads. Without it, invariant 9's "and not SIGKILLed" is a comment:
    a database killed mid-write gets mirrored byte-exactly as a torn snapshot.
    """

    async def test_reports_a_killed_container(self) -> None:
        host = (
            FakeHost()
            .on(r"date \+%s", stdout="1700000100\n")
            .on(r"docker events", stdout="pg-1 137\n")
        )
        assert await killed_since(host, since=1700000000, label_filters=LABELS) == [("pg-1", 137)]

    async def test_ignores_a_clean_exit(self) -> None:
        """Exit 0 is the whole point of asking politely."""
        host = (
            FakeHost()
            .on(r"date \+%s", stdout="1700000100\n")
            .on(r"docker events", stdout="pg-1 0\n")
        )
        assert await killed_since(host, since=1700000000, label_filters=LABELS) == []

    async def test_picks_the_killed_one_out_of_a_stack(self) -> None:
        host = (
            FakeHost()
            .on(r"date \+%s", stdout="1700000100\n")
            .on(r"docker events", stdout="app-1 0\nredis-1 0\npg-1 137\nworker-1 143\n")
        )
        # 143 is SIGTERM — a clean stop, not a kill.
        assert await killed_since(host, since=1700000000, label_filters=LABELS) == [("pg-1", 137)]

    async def test_survives_a_container_name_with_spaces(self) -> None:
        """The format is `name exitCode`, so the split has to come from the right."""
        host = (
            FakeHost()
            .on(r"date \+%s", stdout="1700000100\n")
            .on(r"docker events", stdout="odd name 137\n")
        )
        assert await killed_since(host, since=1700000000, label_filters=LABELS) == [
            ("odd name", 137)
        ]

    async def test_quiet_window_is_not_a_kill(self) -> None:
        host = FakeHost().on(r"date \+%s", stdout="1700000100\n").on(r"docker events", stdout="")
        assert await killed_since(host, since=1700000000, label_filters=LABELS) == []

    async def test_refuses_when_the_event_log_cannot_be_read(self) -> None:
        """A stop we cannot vet is a stop we do not trust.

        Returning [] here would read as "nothing was killed" and let the copy
        proceed over a data directory nobody checked.
        """
        host = (
            FakeHost()
            .on(r"date \+%s", stdout="1700000100\n")
            .on(r"docker events", stderr="permission denied", exit_status=1)
        )
        with pytest.raises(QuiesceError, match="event log"):
            await killed_since(host, since=1700000000, label_filters=LABELS)

    async def test_asks_the_source_for_the_time(self) -> None:
        """Our clock would silently narrow or widen the window if it were skewed."""
        host = FakeHost().on(r"date \+%s", stdout="1700000100\n").on(r"docker events", stdout="")
        await killed_since(host, since=1700000000, label_filters=LABELS)
        events = next(c for c in host.commands if c.startswith("docker events"))
        assert "--since 1700000000" in events
        assert "--until 1700000100" in events
        assert "--filter label=coolify.projectName=shop" in events
