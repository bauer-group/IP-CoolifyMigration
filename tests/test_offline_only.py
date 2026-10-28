"""A stack blocked only by anonymous volumes migrates offline, on the operator's yes.

Coolify removes every container on stop and before every deploy, so an
anonymous volume (an image ``VOLUME`` the compose does not mount) comes back
empty after each restart. Running, the plan sees it and refuses; stopped, the
plan clears. The run therefore offers to stop such a stack up front — and starts
it again if the migration never got as far as stopping the source itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import (
    Decision,
    DockerMount,
    VolumeItem,
    VolumeManifest,
    reconcile,
)
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import Outcome, State
from bg_coolify_migrate.engine.executor import RunResult
from bg_coolify_migrate.settings.base import Settings

ANON = "a09bdb674c95b3e3888c26c355c3fe0e7ff59b5f83821d60bfab257d09f9d312"


def _anonymous(path: str = "/opt/sonarqube/extensions") -> VolumeItem:
    return VolumeItem(
        mount_class=MountClass.ANONYMOUS,
        decision=Decision.REFUSE,
        reason="anonymous volume",
        source_name=ANON,
        source_path=f"/var/lib/docker/volumes/{ANON}/_data",
        mount_path=path,
    )


def _resource(
    *,
    collection: str = "applications",
    items: tuple[VolumeItem, ...] = (),
    refusals: tuple[str, ...] = (),
    uuid: str = "app1",
) -> ResourcePlan:
    kind = ResourceKind.DATABASE if collection == "databases" else ResourceKind.APP_GIT_COMPOSE
    return ResourcePlan(
        snapshot=ResourceSnapshot(uuid=uuid, name="sonar", collection=collection, kind=kind),
        strategy=Strategy.COPY_DATA,
        manifest=VolumeManifest(items=items),
        refusals=refusals,
    )


def _plan(*resources: ResourcePlan) -> MigrationPlan:
    return MigrationPlan(
        project="p",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=resources,
    )


class TestOfflineOnly:
    def test_anonymous_volumes_alone_make_an_app_offline_only(self) -> None:
        resource = _resource(items=(_anonymous(), _anonymous("/opt/sonarqube/temp")))
        assert resource.offline_only
        assert resource.anonymous_mount_paths == (
            "/opt/sonarqube/extensions",
            "/opt/sonarqube/temp",
        )

    def test_another_refusal_keeps_it_blocked_for_good(self) -> None:
        resource = _resource(items=(_anonymous(),), refusals=("multi-server",))
        assert not resource.offline_only

    def test_a_database_is_never_offered(self) -> None:
        """There an anonymous volume can be the data itself."""
        assert not _resource(collection="databases", items=(_anonymous(),)).offline_only

    def test_an_unblocked_resource_is_not_offline_only(self) -> None:
        assert not _resource().offline_only

    def test_the_refusal_tells_how_to_proceed(self) -> None:
        manifest = reconcile(
            docker_mounts=[
                DockerMount(
                    container="c", type="volume", name=ANON, destination="/opt/sonarqube/temp"
                )
            ]
        )
        (item,) = manifest.refused
        assert "stop the stack in Coolify to migrate it offline" in item.reason
        assert "recreates this volume empty on every restart" in item.reason


class _Api:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def stop(self, collection: str, uuid: str) -> bool:
        self.calls.append(("stop", collection, uuid))
        return True

    async def restart(self, collection: str, uuid: str) -> None:
        self.calls.append(("restart", collection, uuid))


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path, stop_timeout=30)  # type: ignore[call-arg]


class TestOfferOffline:
    async def test_yes_stops_and_replans_until_the_stop_landed(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        from bg_coolify_migrate import cli
        from bg_coolify_migrate.ui import wizard

        monkeypatch.setattr(wizard, "confirm_offline", lambda plan, resources: True)
        monkeypatch.setattr(cli.asyncio, "sleep", _no_sleep)
        blocked = _plan(_resource(items=(_anonymous(),)))
        clear = _plan(_resource())
        answers = iter([blocked, clear])  # the first re-plan still sees the container

        async def build(job: tuple[str, str, str | None]) -> MigrationPlan:
            return next(answers)

        api, stopped = _Api(), []  # type: ignore[var-annotated]
        out = await cli._offer_offline(api, settings, [blocked], [("p", "e", None)], build, stopped)

        assert out == [clear]
        assert api.calls == [("stop", "applications", "app1")]
        assert stopped == [("applications", "app1", "sonar")]

    async def test_no_leaves_everything_as_it_was(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        from bg_coolify_migrate import cli
        from bg_coolify_migrate.ui import wizard

        monkeypatch.setattr(wizard, "confirm_offline", lambda plan, resources: False)
        blocked = _plan(_resource(items=(_anonymous(),)))
        api, stopped = _Api(), []  # type: ignore[var-annotated]
        out = await cli._offer_offline(api, settings, [blocked], [("p", "e", None)], _never, stopped)
        assert out == [blocked]
        assert api.calls == [] and stopped == []

    async def test_a_plan_blocked_for_another_reason_is_not_offered(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        from bg_coolify_migrate import cli
        from bg_coolify_migrate.ui import wizard

        def _fail(plan: object, resources: object) -> bool:
            raise AssertionError("must not ask")

        monkeypatch.setattr(wizard, "confirm_offline", _fail)
        plan = _plan(
            _resource(items=(_anonymous(),)),
            _resource(uuid="app2", refusals=("multi-server",)),
        )
        api, stopped = _Api(), []  # type: ignore[var-annotated]
        await cli._offer_offline(api, settings, [plan], [("p", "e", None)], _never, stopped)
        assert api.calls == []


class TestRestartStopped:
    async def test_starts_again_with_restart_not_start(self) -> None:
        from bg_coolify_migrate import cli

        api, stopped = _Api(), [("applications", "app1", "sonar")]
        await cli._restart_stopped(api, stopped)
        assert api.calls == [("restart", "applications", "app1")]
        assert stopped == []

    async def test_a_failed_restart_is_reported_not_raised(self) -> None:
        from bg_coolify_migrate import cli

        class _Broken(_Api):
            async def restart(self, collection: str, uuid: str) -> None:
                raise RuntimeError("boom")

        stopped = [("applications", "app1", "sonar")]
        await cli._restart_stopped(_Broken(), stopped)
        assert stopped == []


class TestExecuteHandsOver:
    @pytest.mark.parametrize(
        ("reached", "kept"),
        [(State.CREATE_TARGET, True), (State.QUIESCE, False), (State.DONE, False)],
    )
    async def test_the_saga_owns_the_source_from_quiesce_on(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings, reached: State, kept: bool
    ) -> None:
        """Before QUIESCE the up-front stop is ours to undo; from it on, the saga
        stops the source itself and its rollback restarts it."""
        from bg_coolify_migrate import cli
        from bg_coolify_migrate.engine import runner

        async def _accept(*args: object, **kwargs: object) -> None:
            return None

        async def _run(*args: object, **kwargs: object) -> RunResult:
            outcome = Outcome.SUCCEEDED if reached is State.DONE else Outcome.FAILED
            return RunResult(outcome=outcome, reached=reached)

        monkeypatch.setattr(cli, "_accept_host_key", _accept)
        monkeypatch.setattr(runner, "run_migration", _run)
        stopped = [("applications", "app1", "sonar")]
        await cli._execute_plans(
            _Api(), settings, [_plan(_resource())], stopped,
            accept_drift=True, accept_dns=True, accept_hosts=frozenset(),
            delete_previews=False, trust_host_key=False,
        )
        assert bool(stopped) is kept


async def _no_sleep(_: float) -> None:
    return None


async def _never(job: tuple[str, str, str | None]) -> MigrationPlan:
    raise AssertionError("must not re-plan")
