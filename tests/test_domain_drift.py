"""Tests for drift detection.

The design rule these encode: **we build the target exactly as the source is
configured, then report what could still differ and let the operator decide.**

Drift is advisory. New image versions and moved branches are normal; whether they
are compatible is a judgement about a specific stack. What we owe the operator is
a *concrete* question — "may pull a newer image" is not actionable, "may cross a
major version and refuse to start on the copied data" is.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.domain.drift import (
    RECOVERABLE_FROM_DOCKER,
    UNREADABLE_SETTINGS,
    DriftAxis,
    Severity,
    assess_image_drift,
    assess_rebuild_drift,
    diff_config,
    normalise,
)


class TestNothingToSay:
    def test_a_pinned_non_building_resource_is_silent(self) -> None:
        report = assess_rebuild_drift(
            resource_name="pg", builds=False, images=("postgres:16.4.1",)
        )
        assert report.severity is Severity.OK
        assert report.requires_confirmation is False

    def test_no_images_and_no_build_is_silent(self) -> None:
        report = assess_rebuild_drift(resource_name="pg", builds=False)
        assert report.findings == ()

    def test_a_non_building_resource_ignores_commits(self) -> None:
        # A dockerimage app has no build; a commit comparison is meaningless.
        report = assess_rebuild_drift(
            resource_name="app", builds=False, running_commit="aaa", head_commit="bbb"
        )
        assert not any(f.axis is DriftAxis.CODE for f in report.findings)


class TestImageDrift:
    def test_a_moving_tag_on_a_database_needs_a_decision(self) -> None:
        # The case worth stopping for: we copy the data directory byte-exactly,
        # and a newer MAJOR engine may simply refuse to read it.
        findings = assess_image_drift(
            resource_name="pg", images=("postgres:latest",), is_database=True
        )
        assert any(f.severity is Severity.WARN for f in findings)
        assert any("refuse to start" in f.detail for f in findings)

    def test_a_moving_tag_elsewhere_is_only_a_notice(self) -> None:
        # nginx:latest picking up a new build is unremarkable.
        findings = assess_image_drift(
            resource_name="web", images=("nginx:latest",), is_database=False
        )
        assert all(f.severity is Severity.NOTICE for f in findings)

    def test_a_minor_floating_tag_is_only_a_notice(self) -> None:
        # postgres:16 -> 16.4 is a non-event. Do not spend attention on it.
        findings = assess_image_drift(
            resource_name="pg", images=("postgres:16",), is_database=True
        )
        assert all(f.severity is Severity.NOTICE for f in findings)

    def test_a_pinned_image_says_nothing(self) -> None:
        assert assess_image_drift(resource_name="pg", images=("postgres@sha256:abc",)) == ()

    def test_an_exact_version_says_nothing(self) -> None:
        assert assess_image_drift(resource_name="pg", images=("postgres:16.4.1",)) == ()

    def test_an_unversioned_database_tag_flags_coolifys_mount_path_guess(self) -> None:
        # Coolify regexes the tag for a number to pick the volume mount path and
        # silently takes the pre-18 path when it finds none.
        findings = assess_image_drift(
            resource_name="pg", images=("postgres:latest",), is_database=True
        )
        assert any("cannot read a version" in f.summary for f in findings)

    def test_a_versioned_database_tag_does_not_flag_the_mount_path(self) -> None:
        findings = assess_image_drift(
            resource_name="pg", images=("postgres:16",), is_database=True
        )
        assert not any("cannot read a version" in f.summary for f in findings)

    def test_every_image_is_assessed(self) -> None:
        findings = assess_image_drift(
            resource_name="stack", images=("nginx:latest", "redis:7")
        )
        assert len(findings) == 2

    def test_image_drift_applies_to_non_building_resources(self) -> None:
        # A database never builds, but its tag still resolves at deploy time.
        report = assess_rebuild_drift(
            resource_name="pg", builds=False, images=("postgres:latest",), is_database=True
        )
        assert report.requires_confirmation


class TestCodeDrift:
    def test_same_commit_says_nothing(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="abc123", head_commit="abc123"
        )
        assert report.severity is Severity.OK

    def test_moved_head_needs_a_decision_but_does_not_refuse(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="abc123", head_commit="def456"
        )
        assert report.requires_confirmation
        (finding,) = report.needs_decision
        assert finding.axis is DriftAxis.CODE
        assert finding.source_value == "abc123"
        assert finding.target_value == "def456"

    def test_the_code_message_is_honest_about_the_stakes(self) -> None:
        # "Usually fine" — because it usually is. Alarming about the routine case
        # is how you train people to stop reading warnings.
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="a", head_commit="b"
        )
        (finding,) = report.needs_decision
        assert "Usually fine" in finding.detail
        assert "schema migrations" in finding.detail

    def test_an_unknown_side_is_surfaced_not_assumed_away(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit=None, head_commit="def456"
        )
        assert report.requires_confirmation
        (finding,) = report.needs_decision
        assert "cannot compare" in finding.summary

    def test_both_unknown_makes_no_code_claim(self) -> None:
        report = assess_rebuild_drift(resource_name="app", builds=True)
        assert not any(f.axis is DriftAxis.CODE for f in report.findings)


class TestTopologyDrift:
    def test_identical_topology_says_nothing(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="a",
            running_topology="fp1",
            head_topology="fp1",
        )
        assert report.severity is Severity.OK

    def test_changed_topology_is_advisory_not_a_wall(self) -> None:
        """pair_by_mount_path is the real check, and a more precise one.

        A volume RENAMED in git still maps correctly, because we pair by mount
        path. A fingerprint comparison would have blocked that harmless case. One
        genuinely added, removed or re-pathed raises VolumePairingError at
        DISCOVER — where the decision is actually made.
        """
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="a",
            running_topology="fp1",
            head_topology="fp2",
        )
        (finding,) = report.needs_decision
        assert finding.axis is DriftAxis.TOPOLOGY
        assert finding.severity is Severity.WARN
        assert "renamed volume is handled correctly" in finding.detail

    def test_missing_topology_makes_no_claim(self) -> None:
        # A nixpacks app has no compose topology to compare.
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="a", head_commit="a"
        )
        assert not any(f.axis is DriftAxis.TOPOLOGY for f in report.findings)


class TestBaseImageDrift:
    def test_unpinned_bases_are_a_notice(self) -> None:
        # Coolify forces `docker build --pull`; we cannot fix it from here, so we
        # are honest rather than obstructive.
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="a",
            unpinned_base_images=("node:20", "alpine:3"),
        )
        assert report.requires_confirmation is False
        assert report.severity is Severity.NOTICE
        (finding,) = report.notices
        assert finding.axis is DriftAxis.BASE_IMAGE
        assert "node:20" in finding.detail

    def test_no_unpinned_bases_says_nothing(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="a", head_commit="a"
        )
        assert report.severity is Severity.OK


class TestSeverityAggregation:
    def test_warn_outranks_notice(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="b",
            unpinned_base_images=("node:20",),
        )
        assert report.severity is Severity.WARN

    def test_notices_do_not_demand_a_decision(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="a",
            unpinned_base_images=("node:20",),
        )
        assert report.requires_confirmation is False
        assert len(report.notices) == 1

    def test_all_axes_can_fire_at_once(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="b",
            running_topology="x",
            head_topology="y",
            unpinned_base_images=("node:20",),
            images=("postgres:latest",),
            is_database=True,
        )
        axes = {f.axis for f in report.findings}
        assert axes == {DriftAxis.IMAGE, DriftAxis.CODE, DriftAxis.TOPOLOGY, DriftAxis.BASE_IMAGE}

    def test_nothing_ever_produces_a_hard_block(self) -> None:
        # Drift is a judgement about the operator's stack, not ours to refuse.
        report = assess_rebuild_drift(
            resource_name="app",
            builds=True,
            running_commit="a",
            head_commit="b",
            running_topology="x",
            head_topology="y",
            images=("postgres:latest",),
            is_database=True,
        )
        assert not any(f.severity is Severity.BLOCK for f in report.findings)
        assert report.severity is Severity.WARN

    def test_summary_lines_are_readable(self) -> None:
        report = assess_rebuild_drift(
            resource_name="app", builds=True, running_commit="a", head_commit="b"
        )
        (line,) = report.summary_lines()
        assert line.startswith("code: ")


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

    def test_unpatchable_difference_is_reported_not_hidden(self) -> None:
        # The whole bargain of the API-only constraint.
        report = diff_config(
            resource_name="app",
            source={"docker_compose_raw": "old"},
            target={"docker_compose_raw": "new"},
            patchable=frozenset(),
        )
        (finding,) = report.unreconciled
        assert "docker_compose_raw" in finding.summary
        assert report.is_clean is False

    def test_uuid_difference_is_not_reported(self) -> None:
        report = diff_config(
            resource_name="app", source={"uuid": "aaa"}, target={"uuid": "bbb"}, patchable=frozenset()
        )
        assert report.unreconciled == ()

    def test_unknown_settings_are_surfaced(self) -> None:
        report = diff_config(resource_name="app", source={}, target={}, patchable=frozenset())
        assert "is_build_server_enabled" in report.unknown
        assert report.is_clean is False

    def test_docker_recoverable_settings_are_excluded_from_unknown(self) -> None:
        # is_force_https_enabled shows up as a Traefik label, so docker inspect
        # can recover it — it is not an unknown.
        report = diff_config(resource_name="app", source={}, target={}, patchable=frozenset())
        assert "is_force_https_enabled" not in report.unknown
        assert "is_gzip_enabled" not in report.unknown


class TestSettingsGapConstants:
    def test_recoverable_is_a_subset_of_unreadable(self) -> None:
        assert RECOVERABLE_FROM_DOCKER <= UNREADABLE_SETTINGS

    def test_genuinely_unrecoverable_settings_are_documented(self) -> None:
        # No observable footprint on a container, so no amount of docker inspect
        # recovers them. They must be asked, not guessed.
        unrecoverable = UNREADABLE_SETTINGS - RECOVERABLE_FROM_DOCKER
        assert "is_build_server_enabled" in unrecoverable
        assert "use_build_secrets" in unrecoverable
        assert "disable_build_cache" in unrecoverable
