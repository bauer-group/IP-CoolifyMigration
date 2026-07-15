"""Tests for the drift gates.

The three axes encode verified upstream behaviour:
  code     — git_commit_sha does not pin (ApplicationDeploymentJob.php:2329-2349)
  topology — dockercompose re-reads the compose from git every deploy
  base     — `docker build --pull` is forced, so FROM tags always refresh
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.domain.drift import (
    RECOVERABLE_FROM_DOCKER,
    UNREADABLE_SETTINGS,
    DriftAxis,
    Severity,
    assess_rebuild_drift,
    diff_config,
    normalise,
)


class TestNonBuildingResources:
    def test_database_never_drifts(self) -> None:
        # Databases never build, so there is nothing to drift.
        report = assess_rebuild_drift(resource_name="pg", builds=False)
        assert report.is_blocked is False
        assert report.severity is Severity.OK
        assert report.findings == ()

    def test_non_building_ignores_commit_mismatch(self) -> None:
        # A dockerimage app has no build; a commit comparison is meaningless.
        report = assess_rebuild_drift(
            resource_name="app", builds=False, running_commit="aaa", head_commit="bbb"
        )
        assert report.is_blocked is False


class TestCodeDrift:
    def test_same_commit_is_clean(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="abc123", head_commit="abc123"
        )
        assert report.severity is Severity.OK

    def test_moved_head_blocks(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="abc123", head_commit="def456"
        )
        assert report.is_blocked
        (finding,) = report.blocking
        assert finding.axis is DriftAxis.CODE
        assert finding.source_value == "abc123"
        assert finding.target_value == "def456"

    def test_unknown_running_commit_blocks_rather_than_assuming(self) -> None:
        # Fail closed: if we cannot compare, we cannot rule drift out.
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit=None, head_commit="def456"
        )
        assert report.is_blocked

    def test_unknown_head_blocks(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="abc", head_commit=None
        )
        assert report.is_blocked

    def test_both_unknown_does_not_block_on_the_code_axis(self) -> None:
        # Nothing known about either side and no other signal: there is no code
        # comparison to make. Other axes may still block.
        report = assess_rebuild_drift(resource_name="app", builds=True)
        assert not any(f.axis is DriftAxis.CODE for f in report.findings)


class TestTopologyDrift:
    def test_identical_topology_is_clean(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="a",
            running_topology="fp1",
            head_topology="fp1",
        )
        assert report.severity is Severity.OK

    def test_changed_topology_blocks_even_at_the_same_commit(self) -> None:
        # This is the data-loss axis: a renamed volume in the compose at HEAD
        # silently invalidates the old->new mapping computed from the source.
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="a",
            running_topology="fp1",
            head_topology="fp2",
        )
        assert report.is_blocked
        (finding,) = report.blocking
        assert finding.axis is DriftAxis.TOPOLOGY
        assert "different volumes" in finding.summary

    def test_missing_topology_does_not_block(self) -> None:
        # Not every building resource is compose-backed; a nixpacks app has no
        # topology to compare.
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="a", head_commit="a"
        )
        assert not any(f.axis is DriftAxis.TOPOLOGY for f in report.findings)


class TestBaseImageDrift:
    def test_unpinned_bases_warn_but_do_not_block(self) -> None:
        # Coolify forces `docker build --pull`; we cannot fix it from here, so
        # we are honest rather than obstructive.
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="a",
            unpinned_base_images=("node:20", "alpine:3"),
        )
        assert report.is_blocked is False
        assert report.severity is Severity.WARN
        (finding,) = report.findings
        assert finding.axis is DriftAxis.BASE_IMAGE
        assert "node:20" in finding.detail

    def test_no_unpinned_bases_is_clean(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="a", head_commit="a"
        )
        assert report.severity is Severity.OK


class TestSeverityAggregation:
    def test_block_wins_over_warn(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="b",
            unpinned_base_images=("node:20",),
        )
        assert report.severity is Severity.BLOCK

    def test_all_axes_can_fire_at_once(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="b",
            running_topology="x",
            head_topology="y",
            unpinned_base_images=("node:20",),
        )
        axes = {f.axis for f in report.findings}
        assert axes == {DriftAxis.CODE, DriftAxis.TOPOLOGY, DriftAxis.BASE_IMAGE}
        assert len(report.blocking) == 2


class TestNormalise:
    @pytest.mark.parametrize(
        "field", ["uuid", "id", "created_at", "status", "fqdn", "server_uuid", "config_hash"]
    )
    def test_expected_differences_are_stripped(self, field: str) -> None:
        assert field not in normalise({field: "x", "keep": "y"})

    def test_real_fields_survive(self) -> None:
        assert normalise({"uuid": "a", "ports_exposes": "3000"}) == {"ports_exposes": "3000"}


class TestDiffConfig:
    def test_identical_configs_are_clean(self) -> None:
        report = diff_config(
            resource_name="app",
            source={"uuid": "a", "ports_exposes": "3000"},
            target={"uuid": "b", "ports_exposes": "3000"},
            patchable=frozenset({"ports_exposes"}),
        )
        assert report.reconciled == ()
        assert report.unreconciled == ()

    def test_patchable_difference_is_reconciled(self) -> None:
        report = diff_config(
            resource_name="app",
            source={"ports_exposes": "3000"},
            target={"ports_exposes": "80"},
            patchable=frozenset({"ports_exposes"}),
        )
        assert report.reconciled == ("ports_exposes",)
        assert report.unreconciled == ()

    def test_unpatchable_difference_is_reported_not_hidden(self) -> None:
        # The whole bargain of the API-only constraint: what we cannot fix, we
        # report — we never silently lose it.
        report = diff_config(
            resource_name="app",
            source={"docker_compose_raw": "old"},
            target={"docker_compose_raw": "new"},
            patchable=frozenset(),
        )
        assert report.reconciled == ()
        (finding,) = report.unreconciled
        assert "docker_compose_raw" in finding.summary
        assert report.is_clean is False

    def test_uuid_difference_is_not_reported(self) -> None:
        report = diff_config(
            resource_name="app",
            source={"uuid": "aaa"},
            target={"uuid": "bbb"},
            patchable=frozenset(),
        )
        assert report.unreconciled == ()

    def test_unknown_settings_are_surfaced(self) -> None:
        report = diff_config(
            resource_name="app", source={}, target={}, patchable=frozenset()
        )
        # The settings gap: readable-nowhere fields must appear as known-unknowns.
        assert "is_build_server_enabled" in report.unknown
        assert report.is_clean is False

    def test_docker_recoverable_settings_are_excluded_from_unknown(self) -> None:
        # is_force_https_enabled shows up as a Traefik label on the container, so
        # docker inspect can recover it — it is not an unknown.
        report = diff_config(
            resource_name="app", source={}, target={}, patchable=frozenset()
        )
        assert "is_force_https_enabled" not in report.unknown
        assert "is_gzip_enabled" not in report.unknown


class TestSettingsGapConstants:
    def test_recoverable_is_a_subset_of_unreadable(self) -> None:
        assert RECOVERABLE_FROM_DOCKER <= UNREADABLE_SETTINGS

    def test_genuinely_unrecoverable_settings_are_documented(self) -> None:
        # These have no observable footprint on a container, so no amount of
        # docker inspect recovers them. They must be asked, not guessed.
        unrecoverable = UNREADABLE_SETTINGS - RECOVERABLE_FROM_DOCKER
        assert "is_build_server_enabled" in unrecoverable
        assert "use_build_secrets" in unrecoverable
        assert "disable_build_cache" in unrecoverable
