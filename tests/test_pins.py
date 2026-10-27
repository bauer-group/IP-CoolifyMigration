"""Volume pins: never-started containers that keep Coolify's cleanup off our data.

Coolify's Docker cleanup runs `docker volume prune -af` on a server with
"Delete unused volumes" on (v4.1.2:CleanupDocker.php:61-63, v4.5.0:166-168),
and only ever removes containers labelled `coolify.managed=true`
(v4.1.2:CleanupDocker.php:51, v4.5.0:211-216). A created container without
that label therefore keeps a volume alive through both.
"""

from __future__ import annotations

import shlex

import pytest

from bg_coolify_migrate.discovery import pins
from bg_coolify_migrate.discovery.docker import Container
from bg_coolify_migrate.errors import MigrationError
from tests.fake_daemon import FakeDaemon

MID = "20261027-101500-abc123"
OURS = {pins.LABEL_ROLE: pins.ROLE_PIN, pins.LABEL_MIGRATION: MID, pins.LABEL_SIDE: pins.SOURCE}
ANON = "a" * 64


def _cmd(volumes: tuple[str, ...] = ("postgres-data-db1", "app1_uploads")) -> str:
    return pins.create_command(name="p", migration_id=MID, side=pins.SOURCE, volumes=volumes)


class TestCreateCommand:
    def test_mounts_every_volume_readonly_without_copy(self) -> None:
        argv = shlex.split(_cmd().split(" && ", 1)[1])
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        assert mounts == [
            "type=volume,source=app1_uploads,target=/pin/app1_uploads,readonly,volume-nocopy",
            "type=volume,source=postgres-data-db1,target=/pin/postgres-data-db1,readonly,"
            "volume-nocopy",
        ]
        assert argv[-2:] == [pins.PIN_IMAGE, "/pin"]
        assert "--network" in argv and argv[argv.index("--network") + 1] == "none"

    def test_carries_no_coolify_label(self) -> None:
        # Coolify's container cleanup only touches label coolify.managed=true.
        argv = shlex.split(_cmd().split(" && ", 1)[1])
        labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
        assert labels == [
            f"{pins.LABEL_ROLE}=pin",
            f"{pins.LABEL_MIGRATION}={MID}",
            f"{pins.LABEL_SIDE}=source",
        ]
        assert "coolify." not in _cmd()

    def test_never_runs_starts_removes_volumes_or_chowns(self) -> None:
        cmd = _cmd()
        for forbidden in ("docker run", "docker start", " -v ", "chown", "docker rm"):
            assert forbidden not in cmd

    def test_never_pulls(self) -> None:
        # Without it, a missing image resolves to docker.io/bg-coolify-migrate/pin.
        assert "docker create --pull never " in _cmd()

    def test_image_is_imported_only_when_absent(self) -> None:
        image = shlex.quote(pins.PIN_IMAGE)
        assert _cmd().startswith(
            f"(docker image inspect {image} >/dev/null 2>&1 || "
            f"head -c 10240 /dev/zero | docker import - {image} >/dev/null) && "
        )

    @pytest.mark.parametrize("bad", ["a,readonly=false", "../x", "", "-v"])
    def test_refuses_a_name_that_could_bend_the_mount_spec(self, bad: str) -> None:
        with pytest.raises(ValueError):
            pins.create_command(name="p", migration_id=MID, side=pins.SOURCE, volumes=[bad])

    def test_an_idle_pin_says_so_in_a_label(self) -> None:
        idle = pins.create_command(
            name="p", migration_id=MID, side=pins.SOURCE, volumes=["v1"], idle=True
        )
        argv = shlex.split(idle.split(" && ", 1)[1])
        labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
        assert labels[-1] == f"{pins.LABEL_HOLD}={pins.HOLD_IDLE}"
        assert pins.LABEL_HOLD not in _cmd()

    def test_refuses_no_volumes_and_a_bad_side(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            pins.create_command(name="p", migration_id=MID, side=pins.SOURCE, volumes=[])
        with pytest.raises(ValueError, match="side"):
            pins.create_command(name="p", migration_id=MID, side="both", volumes=["v1"])
        with pytest.raises(ValueError, match="migration id"):
            pins.create_command(name="p", migration_id="a b", side=pins.SOURCE, volumes=["v1"])


class TestPinName:
    def test_is_deterministic_docker_safe_and_side_specific(self) -> None:
        a = pins.pin_name(MID, pins.SOURCE, ["v2", "v1", "v1"])
        assert a == pins.pin_name(MID, pins.SOURCE, ["v1", "v2"])
        assert a != pins.pin_name(MID, pins.TARGET, ["v1", "v2"])
        assert a != pins.pin_name(MID, pins.SOURCE, ["v1"])
        assert a.startswith(f"bg-coolify-migrate-pin-{MID}-source-")

    @pytest.mark.parametrize("bad", ["a/b", "a b", "-x", ""])
    def test_rejects_an_unsafe_migration_id(self, bad: str) -> None:
        with pytest.raises(ValueError):
            pins.pin_name(bad, pins.SOURCE, ["v1"])


def _c(state: str, labels: dict[str, str]) -> Container:
    return Container(id="c", name="c", state=state, labels=labels)


class TestPredicates:
    def test_a_stopped_container_with_our_role_is_a_pin(self) -> None:
        assert pins.is_pin(_c("created", OURS))
        assert pins.is_pin(_c("created", OURS), migration_id=MID, side=pins.SOURCE)

    def test_a_running_container_is_never_a_pin(self) -> None:
        assert not pins.is_pin(_c("running", OURS))

    def test_another_migration_or_side_is_not_ours(self) -> None:
        assert not pins.is_pin(_c("created", OURS), migration_id="other")
        assert not pins.is_pin(_c("created", OURS), side=pins.TARGET)

    def test_a_coolify_container_is_not_a_pin(self) -> None:
        assert not pins.is_pin(_c("exited", {"coolify.managed": "true"}))

    def test_anonymous_volumes_are_never_awaited(self) -> None:
        assert pins.is_anonymous(ANON)
        assert not pins.is_anonymous("postgres-data-db1")
        held = [pins.Pin("p1", frozenset({"v1", ANON})), pins.Pin("p2", frozenset({"v2"}))]
        assert pins.awaited(held) == {"v1", "v2"}

    def test_an_idle_pins_volumes_are_never_awaited(self) -> None:
        # Nothing mounted them before the migration; nothing will after it.
        held = [pins.Pin("p1", frozenset({"v1"})), pins.Pin("p2", frozenset({"v2"}), idle=True)]
        assert pins.awaited(held) == {"v1"}

    def test_only_a_running_non_pin_is_a_reference(self) -> None:
        assert pins.is_reference(_c("running", {"coolify.managed": "true"}))
        for state in ("created", "exited", "restarting", "dead", "removing"):
            assert not pins.is_reference(_c(state, {"coolify.managed": "true"}))
        assert not pins.is_reference(_c("created", OURS))


class TestEnsure:
    async def test_creates_one_pin_for_all_volumes(self) -> None:
        daemon = FakeDaemon(volumes=("v1", "v2"))
        names = await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["v2", "v1"])
        assert len(names) == 1 and len(daemon.created) == 1
        assert daemon.containers[names[0]].volumes == ["v1", "v2"]
        assert daemon.containers[names[0]].state == "created"

    async def test_is_idempotent_when_an_existing_pin_covers_the_set(self) -> None:
        daemon = FakeDaemon(volumes=("v1", "v2"))
        first = await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["v1", "v2"])
        again = await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["v1"])
        assert again == first
        assert len(daemon.created) == 1

    async def test_adds_a_pin_for_the_uncovered_volumes_only(self) -> None:
        daemon = FakeDaemon(volumes=("v1", "v2", "v3"))
        await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["v1"])
        names = await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["v1", "v3"])
        assert len(names) == 2
        assert daemon.containers[names[1]].volumes == ["v3"]

    async def test_idle_volumes_get_a_labelled_pin_of_their_own(self) -> None:
        daemon = FakeDaemon(volumes=("v1", "v2", "v3"))
        names = await pins.ensure(
            daemon, migration_id=MID, side=pins.SOURCE, volumes=["v1"], idle=["v1", "v2", "v3"]
        )
        assert len(names) == 2
        mounted, idle = (daemon.containers[n] for n in names)
        assert mounted.volumes == ["v1"] and pins.LABEL_HOLD not in mounted.labels
        assert idle.volumes == ["v2", "v3"] and idle.labels[pins.LABEL_HOLD] == pins.HOLD_IDLE
        listed = {p.name: p.idle for p in await pins.list_pins(daemon, migration_id=MID, side="source")}
        assert listed == {mounted.name: False, idle.name: True}

    async def test_never_creates_a_volume_absent_from_volume_ls(self) -> None:
        # A --mount of a missing volume CREATES it; a pin must never do that.
        daemon = FakeDaemon(volumes=("v1",))
        await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["v1", "ghost"])
        assert daemon.volumes == {"v1"}
        assert "ghost" not in daemon.created[0]

    async def test_with_only_absent_volumes_creates_nothing(self) -> None:
        daemon = FakeDaemon()
        assert (
            await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["ghost"]) == []
        )
        assert daemon.created == []

    async def test_with_no_volumes_runs_no_command(self) -> None:
        daemon = FakeDaemon()
        assert await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["", ""]) == []
        assert daemon.commands == []

    async def test_retries_once_when_the_image_was_pruned_under_it(self) -> None:
        # Coolify's image prune can remove the just-imported, still-unused image.
        daemon = FakeDaemon(volumes=("v1",))
        daemon.create_failures = [
            (125, "Unable to find image: No such image: bg-coolify-migrate/pin:1")
        ]
        names = await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["v1"])
        assert len(daemon.created) == 2 and names[0] in daemon.containers

    async def test_any_other_create_failure_raises(self) -> None:
        daemon = FakeDaemon(volumes=("v1",))
        daemon.create_failures = [(1, "Cannot connect to the Docker daemon")]
        with pytest.raises(MigrationError, match="remote command failed"):
            await pins.ensure(daemon, migration_id=MID, side=pins.SOURCE, volumes=["v1"])
        assert len(daemon.created) == 1


class TestRelease:
    async def test_removes_only_our_stopped_pins_without_force_or_volumes(self) -> None:
        daemon = FakeDaemon(volumes=("v1",))
        daemon.add(
            "postgres-db1", state="exited", labels={"coolify.managed": "true"}, volumes=("v1",)
        )
        daemon.add("ours", state="created", labels=OURS, volumes=("v1",))
        daemon.add("theirs", state="created", labels={**OURS, pins.LABEL_MIGRATION: "other"})
        daemon.add("ours-target", state="created", labels={**OURS, pins.LABEL_SIDE: pins.TARGET})
        daemon.add("impostor", state="running", labels=OURS)

        assert await pins.release(daemon, migration_id=MID, side=pins.SOURCE) == ["ours"]
        assert set(daemon.containers) == {"postgres-db1", "theirs", "ours-target", "impostor"}
        removals = [c for c in daemon.commands if c.startswith("docker rm")]
        assert removals == ["docker rm ours"]
        assert daemon.volumes == {"v1"}

    async def test_a_pin_already_gone_counts_as_released(self) -> None:
        daemon = FakeDaemon()
        daemon.add("ours", state="created", labels=OURS)
        daemon.rm_failures = [(1, "Error response from daemon: No such container: ours")]
        assert await pins.release(daemon, migration_id=MID, side=pins.SOURCE) == ["ours"]

    async def test_any_other_rm_failure_raises(self) -> None:
        daemon = FakeDaemon()
        daemon.add("ours", state="created", labels=OURS)
        daemon.rm_failures = [(1, "Cannot connect to the Docker daemon")]
        with pytest.raises(MigrationError):
            await pins.release(daemon, migration_id=MID, side=pins.SOURCE)

    async def test_nothing_to_release_does_nothing(self) -> None:
        assert await pins.release(FakeDaemon(), migration_id=MID, side=pins.SOURCE) == []


class TestReleaseWhenReferenced:
    async def test_releases_once_a_non_pin_mounts_the_volume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeDaemon(volumes=("v1",))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1",))
        # Another migration's pin is no reference: it may be released any time.
        daemon.add(
            "theirs", state="created", labels={**OURS, pins.LABEL_MIGRATION: "x"}, volumes=("v1",)
        )

        async def restarted(_seconds: float) -> None:
            daemon.add("postgres-db1", state="running", volumes=("v1",))

        monkeypatch.setattr(pins.asyncio, "sleep", restarted)
        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=60
        )
        assert outcome == pins.Release(released=("ours",))
        assert "ours" not in daemon.containers and "theirs" in daemon.containers

    async def test_keeps_the_pin_on_timeout_and_says_which_volume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeDaemon(volumes=("v1", "v2"))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1", "v2"))
        daemon.add("postgres-db1", state="running", volumes=("v1",))
        sleeps: list[float] = []

        async def no_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(pins.asyncio, "sleep", no_sleep)
        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=10, interval=5
        )
        assert outcome == pins.Release(kept=("ours",), unmounted=("v2",))
        assert sleeps == [5, 5]
        assert "ours" in daemon.containers

    async def test_timeout_zero_asks_exactly_once(self) -> None:
        daemon = FakeDaemon(volumes=("v1",))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1",))
        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=0
        )
        assert outcome.kept == ("ours",)
        assert len([c for c in daemon.commands if "--filter volume=" in c]) == 1

    async def test_an_anonymous_volume_is_never_waited_for(self) -> None:
        # A recreated container gets a NEW anonymous volume, never the old one.
        daemon = FakeDaemon(volumes=(ANON,))
        daemon.add("ours", state="created", labels=OURS, volumes=(ANON,))
        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=0
        )
        assert outcome.released == ("ours",)

    async def test_waits_for_every_pinned_volume_whatever_the_capture_said(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The requirement comes from the pins themselves: a resumed QUIESCE
        # overwrites the journalled capture with an empty one.
        daemon = FakeDaemon(volumes=("v1", "v2"))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1", "v2"))
        daemon.add("app", state="running", volumes=("v1",))
        mounted: list[str] = []

        async def remount(_seconds: float) -> None:
            mounted.append("db")
            daemon.add("db", state="running", volumes=("v2",))

        monkeypatch.setattr(pins.asyncio, "sleep", remount)
        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=60
        )
        assert mounted == ["db"] and outcome.released == ("ours",)

    async def test_without_pins_does_nothing(self) -> None:
        daemon = FakeDaemon()
        assert (
            await pins.release_when_referenced(
                daemon, migration_id=MID, side=pins.SOURCE, timeout=60
            )
            == pins.Release()
        )

    async def test_a_container_seen_once_does_not_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Coolify's rolling update: the restarted container is up during its
        # health check, then removed when the check fails - and QUIESCE already
        # removed the old one. Released at that first sighting, the volume was
        # left unreferenced and unpinned.
        daemon = FakeDaemon(volumes=("v1",))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1",))
        looks: list[float] = []

        async def health_check_fails(seconds: float) -> None:
            looks.append(seconds)
            if len(looks) == 1:
                daemon.add("app-new", state="running", volumes=("v1",))
            else:
                daemon.containers.pop("app-new", None)

        monkeypatch.setattr(pins.asyncio, "sleep", health_check_fails)
        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=10, settle=10, interval=5
        )
        assert outcome == pins.Release(kept=("ours",), unmounted=("v1",))
        assert "ours" in daemon.containers

    async def test_releases_once_the_mount_held_across_the_settle_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeDaemon(volumes=("v1",))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1",))
        sleeps: list[float] = []

        async def restarted(seconds: float) -> None:
            sleeps.append(seconds)
            daemon.add("db", state="running", volumes=("v1",))

        monkeypatch.setattr(pins.asyncio, "sleep", restarted)
        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=5, settle=10, interval=5
        )
        # Seen after the first wait, then held for two more.
        assert outcome.released == ("ours",) and sleeps == [5, 5, 5]

    async def test_an_idle_pin_is_released_without_waiting_for_a_mount(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A volume nothing mounted before the migration - a stale storage row's,
        # a profile-gated compose volume - is never mounted by the restarted
        # source. Awaiting it kept every pin of the migration, for good.
        daemon = FakeDaemon(volumes=("v1", "stale"))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1",))
        daemon.add(
            "ours-idle",
            state="created",
            labels={**OURS, pins.LABEL_HOLD: pins.HOLD_IDLE},
            volumes=("stale",),
        )
        daemon.add("db", state="running", volumes=("v1",))
        _no_sleep(monkeypatch)

        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=0
        )
        assert set(outcome.released) == {"ours", "ours-idle"} and not outcome.kept

    async def test_an_idle_pin_goes_even_when_the_others_are_kept(self) -> None:
        daemon = FakeDaemon(volumes=("v1", "stale"))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1",))
        daemon.add(
            "ours-idle",
            state="created",
            labels={**OURS, pins.LABEL_HOLD: pins.HOLD_IDLE},
            volumes=("stale",),
        )
        outcome = await pins.release_when_referenced(
            daemon, migration_id=MID, side=pins.SOURCE, timeout=0
        )
        assert outcome == pins.Release(released=("ours-idle",), kept=("ours",), unmounted=("v1",))


class TestReleaseIdle:
    async def test_keeps_every_awaiting_pin_even_while_mounted(self) -> None:
        # A container mounting the volume now may be the next one a queued
        # stop removes: no look counts.
        daemon = FakeDaemon(volumes=("v1", "stale"))
        daemon.add("ours", state="created", labels=OURS, volumes=("v1",))
        daemon.add(
            "ours-idle",
            state="created",
            labels={**OURS, pins.LABEL_HOLD: pins.HOLD_IDLE},
            volumes=("stale",),
        )
        daemon.add("db", state="running", volumes=("v1",))
        outcome = await pins.release_idle(daemon, migration_id=MID, side=pins.SOURCE)
        assert outcome == pins.Release(released=("ours-idle",), kept=("ours",), unmounted=("v1",))
        assert "ours" in daemon.containers


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(_seconds: float) -> None:
        raise AssertionError("waited, but nothing could have changed")

    monkeypatch.setattr(pins.asyncio, "sleep", forbidden)


class TestUnreferenced:
    async def test_a_failed_probe_counts_as_unreferenced(self) -> None:
        # Falls toward keeping the pin, never toward losing the data.
        daemon = FakeDaemon()
        daemon.on(r"--filter volume=v1", exit_status=1, stderr="daemon unreachable")
        assert await pins.unreferenced(daemon, ["v1"]) == {"v1"}

    async def test_a_stopped_coolify_container_is_no_reference(self) -> None:
        # Before 4.4.4 Coolify's cleanup prunes stopped resource containers
        # (`label!=` filters are ANDed) right before `docker volume prune -af`
        # (v4.4.3:CleanupDocker.php:164,173): the container goes, then the data.
        daemon = FakeDaemon(volumes=("v1",))
        daemon.add(
            "postgres-db1", state="exited", labels={"coolify.managed": "true"}, volumes=("v1",)
        )
        assert await pins.unreferenced(daemon, ["v1"]) == {"v1"}

    async def test_a_running_coolify_container_is_a_reference(self) -> None:
        daemon = FakeDaemon(volumes=("v1",))
        daemon.add("postgres-db1", labels={"coolify.managed": "true"}, volumes=("v1",))
        assert await pins.unreferenced(daemon, ["v1"]) == set()
