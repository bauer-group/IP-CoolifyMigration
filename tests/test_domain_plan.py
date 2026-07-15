"""Tests for strategy selection and plan aggregation."""

from __future__ import annotations

import pytest

from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.drift import DriftAxis, DriftFinding, RebuildDriftReport, Severity
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
    requires_drift_gate,
    select_strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy


def _snap(**kw: object) -> ResourceSnapshot:
    base = {
        "uuid": "u1",
        "name": "app",
        "collection": "applications",
        "kind": ResourceKind.APP_GIT_BUILD,
    }
    return ResourceSnapshot(**{**base, **kw})  # type: ignore[arg-type]


class TestSelectStrategy:
    def test_building_resource_rebuilds(self) -> None:
        assert (
            select_strategy(ResourceKind.APP_GIT_BUILD, builds=True, has_volumes=True)
            is Strategy.REBUILD
        )

    def test_building_stateless_resource_still_rebuilds(self) -> None:
        # "No volumes" does NOT mean trivial: the code still drifts.
        assert (
            select_strategy(ResourceKind.APP_GIT_BUILD, builds=True, has_volumes=False)
            is Strategy.REBUILD
        )

    def test_database_copies_data(self) -> None:
        assert (
            select_strategy(ResourceKind.DATABASE, builds=False, has_volumes=True)
            is Strategy.COPY_DATA
        )

    def test_image_only_compose_service_copies_data(self) -> None:
        # A compose stack with only `image:` never builds -> zero drift.
        assert (
            select_strategy(ResourceKind.SERVICE_COMPOSE, builds=False, has_volumes=True)
            is Strategy.COPY_DATA
        )

    def test_compose_service_that_builds_rebuilds(self) -> None:
        # The user's constraint: "compose stacks can also build when they run
        # from src with a Dockerfile". Same kind, different strategy — which is
        # exactly why `builds` is a parameter rather than derived from `kind`.
        assert (
            select_strategy(ResourceKind.SERVICE_COMPOSE, builds=True, has_volumes=True)
            is Strategy.REBUILD
        )

    def test_dockerimage_app_without_volumes_only_recreates(self) -> None:
        assert (
            select_strategy(ResourceKind.APP_DOCKERIMAGE, builds=False, has_volumes=False)
            is Strategy.RECREATE_ONLY
        )

    def test_same_kind_different_builds_gives_different_strategy(self) -> None:
        a = select_strategy(ResourceKind.APP_GIT_COMPOSE, builds=True, has_volumes=True)
        b = select_strategy(ResourceKind.APP_GIT_COMPOSE, builds=False, has_volumes=True)
        assert a is not b


class TestRequiresDriftGate:
    def test_rebuild_is_gated(self) -> None:
        assert requires_drift_gate(Strategy.REBUILD) is True

    @pytest.mark.parametrize("strategy", [Strategy.COPY_DATA, Strategy.RECREATE_ONLY])
    def test_non_building_strategies_are_not_gated(self, strategy: Strategy) -> None:
        # Nothing builds, so nothing can drift. Databases and image-only compose
        # stacks — the majority of a real estate — pass straight through.
        assert requires_drift_gate(strategy) is False


class TestRunningCommit:
    def test_parsed_from_image_tag(self) -> None:
        # The image tag is `{uuid}:{sha}` by construction, which makes it the
        # only trustworthy record of what is actually running.
        snap = _snap(running_image="k8sgw04ggc8s:a1b2c3d4e5f6")
        assert snap.running_commit == "a1b2c3d4e5f6"

    def test_none_without_an_image(self) -> None:
        assert _snap().running_commit is None

    def test_none_when_tag_is_absent(self) -> None:
        assert _snap(running_image="somerepo").running_commit is None

    def test_handles_registry_host_with_port(self) -> None:
        # rsplit, not split: `ghcr.io:443/org/img:sha` must yield the tag.
        snap = _snap(running_image="ghcr.io:443/org/img:deadbeef")
        assert snap.running_commit == "deadbeef"


class TestResourcePlanBlocking:
    def test_clean_plan_is_not_blocked(self) -> None:
        plan = ResourcePlan(snapshot=_snap(), strategy=Strategy.COPY_DATA)
        assert plan.is_blocked is False
        assert plan.blocking_reasons == ()

    def test_refused_volume_blocks(self) -> None:
        manifest = VolumeManifest(
            items=(
                VolumeItem(
                    mount_class=MountClass.ANONYMOUS,
                    decision=Decision.REFUSE,
                    reason="anonymous volume id cannot be reproduced",
                    source_path="/x",
                    mount_path="/data",
                ),
            )
        )
        plan = ResourcePlan(snapshot=_snap(), strategy=Strategy.COPY_DATA, manifest=manifest)
        assert plan.is_blocked
        assert any("anonymous" in r for r in plan.blocking_reasons)

    def test_drift_blocks(self) -> None:
        drift = RebuildDriftReport(
            resource_name="app",
            builds=True,
            findings=(
                DriftFinding(
                    axis=DriftAxis.CODE, severity=Severity.BLOCK, summary="HEAD moved"
                ),
            ),
        )
        plan = ResourcePlan(snapshot=_snap(), strategy=Strategy.REBUILD, drift=drift)
        assert plan.is_blocked
        assert "HEAD moved" in plan.blocking_reasons

    def test_previews_block(self) -> None:
        # Verified: POST /applications/{uuid}/stop does not stop preview
        # containers, so they keep writing during a "quiesced" copy.
        plan = ResourcePlan(snapshot=_snap(has_previews=True), strategy=Strategy.COPY_DATA)
        assert plan.is_blocked
        assert any("preview" in r for r in plan.blocking_reasons)

    def test_hard_and_drift_reasons_are_separable(self) -> None:
        """Regression: --accept-rebuild-drift must actually work.

        Drift is overridable; a refused volume and running previews are not. If
        the two are conflated, the flag gets accepted and the migration aborts
        two lines later on the generic check — a documented flag that does
        nothing.
        """
        drift = RebuildDriftReport(
            resource_name="app",
            builds=True,
            findings=(
                DriftFinding(axis=DriftAxis.CODE, severity=Severity.BLOCK, summary="HEAD moved"),
            ),
        )
        plan = ResourcePlan(snapshot=_snap(), strategy=Strategy.REBUILD, drift=drift)

        assert plan.drift_blocking_reasons == ("HEAD moved",)
        assert plan.hard_blocking_reasons == ()
        assert plan.is_blocked  # blocked overall...
        # ...but a caller honouring --accept-rebuild-drift finds nothing hard.

    def test_hard_reasons_survive_accepting_drift(self) -> None:
        manifest = VolumeManifest(
            items=(
                VolumeItem(
                    mount_class=MountClass.ANONYMOUS,
                    decision=Decision.REFUSE,
                    reason="anonymous volume",
                    source_path="/x",
                    mount_path="/data",
                ),
            )
        )
        plan = ResourcePlan(
            snapshot=_snap(has_previews=True), strategy=Strategy.COPY_DATA, manifest=manifest
        )
        # Neither of these may ever be waved through by a drift flag.
        assert len(plan.hard_blocking_reasons) == 2
        assert plan.drift_blocking_reasons == ()

    def test_blocking_reasons_is_the_union(self) -> None:
        drift = RebuildDriftReport(
            resource_name="app",
            builds=True,
            findings=(
                DriftFinding(axis=DriftAxis.CODE, severity=Severity.BLOCK, summary="HEAD moved"),
            ),
        )
        plan = ResourcePlan(
            snapshot=_snap(has_previews=True), strategy=Strategy.REBUILD, drift=drift
        )
        assert plan.blocking_reasons == plan.hard_blocking_reasons + plan.drift_blocking_reasons

    def test_warn_level_drift_does_not_block(self) -> None:
        drift = RebuildDriftReport(
            resource_name="app",
            builds=True,
            findings=(
                DriftFinding(
                    axis=DriftAxis.BASE_IMAGE, severity=Severity.WARN, summary="unpinned FROM"
                ),
            ),
        )
        plan = ResourcePlan(snapshot=_snap(), strategy=Strategy.REBUILD, drift=drift)
        assert plan.is_blocked is False


class TestMigrationPlan:
    def _plan(self, *resources: ResourcePlan) -> MigrationPlan:
        return MigrationPlan(
            project="shop",
            environment="production",
            source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
            target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
            resources=resources,
        )

    def test_defaults_to_rename_which_is_non_destructive(self) -> None:
        assert self._plan().finalize_policy is FinalizePolicy.RENAME

    def test_total_bytes_sums_across_resources(self) -> None:
        def sized(n: int) -> ResourcePlan:
            return ResourcePlan(
                snapshot=_snap(),
                strategy=Strategy.COPY_DATA,
                manifest=VolumeManifest(
                    items=(
                        VolumeItem(
                            mount_class=MountClass.NAMED,
                            decision=Decision.MIGRATE,
                            reason="named volume",
                            source_path="/x",
                            mount_path="/d",
                            bytes=n,
                        ),
                    )
                ),
            )

        assert self._plan(sized(100), sized(50)).total_bytes == 150

    def test_one_blocked_resource_blocks_the_whole_project(self) -> None:
        # The migration unit is the project: a half-migrated project is a broken
        # project, because Coolify wires resources by internal DNS name.
        ok = ResourcePlan(snapshot=_snap(name="ok"), strategy=Strategy.COPY_DATA)
        bad = ResourcePlan(snapshot=_snap(name="bad", has_previews=True), strategy=Strategy.COPY_DATA)
        plan = self._plan(ok, bad)
        assert plan.is_blocked
        assert len(plan.blocked_resources) == 1
        assert plan.blocked_resources[0].snapshot.name == "bad"

    def test_warnings_are_prefixed_with_the_resource_name(self) -> None:
        r = ResourcePlan(
            snapshot=_snap(name="minio"),
            strategy=Strategy.COPY_DATA,
            warnings=("compose comments will be lost",),
        )
        assert self._plan(r).warnings == ("minio: compose comments will be lost",)

    def test_empty_plan_is_not_blocked(self) -> None:
        assert self._plan().is_blocked is False
        assert self._plan().total_bytes == 0
