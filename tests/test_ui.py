"""Tests for console formatting and report rendering.

The plain-text renderer is not a degraded fallback but a first-class format: a
migration plan in a CI log must be greppable, and a Rich table piped into a file
is unreadable.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from bg_coolify_migrate.dns.extract import Hostname, HostnameOrigin
from bg_coolify_migrate.dns.gate import Resolution, build_report
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
)
from bg_coolify_migrate.ui.console import THEME, human_bytes, human_duration, reset_console_cache
from bg_coolify_migrate.ui.report import (
    blocking_panel,
    cutover_panel,
    dns_table,
    drift_panel,
    manifest_table,
    plain_plan,
    plan_summary,
    resources_table,
    warnings_panel,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_console_cache()


def render(renderable: object) -> str:
    # Must carry the app's THEME: our renderables reference named styles ("err",
    # "warn", ...) and a bare Console would raise MissingStyle.
    console = Console(width=200, force_terminal=False, no_color=True, theme=THEME)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


class TestHumanBytes:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024**2, "1.0 MB"),
            (1024**3, "1.0 GB"),
            (5 * 1024**4, "5.0 TB"),
        ],
    )
    def test_formats(self, value: int, expected: str) -> None:
        assert human_bytes(value) == expected

    def test_none(self) -> None:
        assert human_bytes(None) == "?"


class TestHumanDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(5, "5s"), (59, "59s"), (90, "1m 30s"), (3600, "1h 0m"), (7320, "2h 2m")],
    )
    def test_formats(self, seconds: float, expected: str) -> None:
        assert human_duration(seconds) == expected


def _manifest() -> VolumeManifest:
    return VolumeManifest(
        items=(
            VolumeItem(
                mount_class=MountClass.NAMED,
                decision=Decision.MIGRATE,
                reason="named volume",
                source_name="pg-data-old",
                source_path="/var/lib/docker/volumes/pg-data-old/_data",
                mount_path="/var/lib/postgresql/data",
                bytes=4 * 1024**3,
            ),
            VolumeItem(
                mount_class=MountClass.ANONYMOUS,
                decision=Decision.REFUSE,
                reason="anonymous volume: its id cannot be reproduced on the target",
                source_path="/x",
                mount_path="/scratch",
            ),
        )
    )


def _plan(*, blocked: bool = False) -> MigrationPlan:
    snapshot = ResourceSnapshot(
        uuid="u1", name="postgres", collection="databases", kind=ResourceKind.DATABASE
    )
    resource = ResourcePlan(
        snapshot=snapshot,
        strategy=Strategy.COPY_DATA,
        manifest=_manifest() if blocked else VolumeManifest(),
        warnings=("compose comments will be lost",),
    )
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old-host", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new-host", ip="10.0.0.2"),
        resources=(resource,),
    )


class TestManifestTable:
    def test_shows_every_item_with_its_reason(self) -> None:
        # Every line must explain itself: an operator at 3am should not have to
        # re-derive why something was skipped.
        out = render(manifest_table(_manifest()))
        assert "migrate" in out
        assert "refuse" in out
        assert "named volume" in out
        assert "cannot be reproduced" in out

    def test_shows_size_only_for_migrated(self) -> None:
        out = render(manifest_table(_manifest()))
        assert "4.0 GB" in out


class TestDriftPanel:
    def test_none_when_there_is_nothing_to_say(self) -> None:
        assert drift_panel(RebuildDriftReport(resource_name="db", builds=False)) is None
        assert drift_panel(None) is None

    def test_none_when_clean(self) -> None:
        assert drift_panel(RebuildDriftReport(resource_name="app", builds=True)) is None

    def test_a_decision_is_framed_as_a_decision_not_a_refusal(self) -> None:
        report = RebuildDriftReport(
            resource_name="app",
            builds=True,
            findings=(
                DriftFinding(
                    axis=DriftAxis.CODE,
                    severity=Severity.WARN,
                    summary="branch HEAD has moved",
                    source_value="aaa111",
                    target_value="bbb222",
                ),
            ),
        )
        out = render(drift_panel(report))
        assert "your decision" in out
        assert "aaa111" in out
        assert "bbb222" in out

    def test_a_notice_is_framed_as_information(self) -> None:
        report = RebuildDriftReport(
            resource_name="app",
            builds=True,
            findings=(
                DriftFinding(
                    axis=DriftAxis.BASE_IMAGE, severity=Severity.NOTICE, summary="unpinned FROM"
                ),
            ),
        )
        out = render(drift_panel(report))
        assert "for information" in out
        assert "your decision" not in out


class TestDnsRendering:
    def _report(self):
        return build_report(
            [
                Resolution(
                    Hostname("shop.example.com", HostnameOrigin.FQDN, False),
                    ("10.0.0.1",),
                    ttl=3600,
                )
            ],
            source_ips=frozenset({"10.0.0.1"}),
            target_ips=frozenset({"10.0.0.2"}),
        )

    def test_dns_table(self) -> None:
        out = render(dns_table(self._report()))
        assert "shop.example.com" in out
        assert "cutover_needed" in out

    def test_cutover_panel_is_actionable(self) -> None:
        out = render(cutover_panel(self._report()))
        assert "shop.example.com" in out
        assert "10.0.0.2" in out
        assert "3600" in out


class TestPlanRendering:
    def test_summary(self) -> None:
        out = render(plan_summary(_plan()))
        assert "shop" in out
        assert "old-host" in out
        assert "new-host" in out
        assert "rename" in out

    def test_resources_table(self) -> None:
        out = render(resources_table(_plan()))
        assert "postgres" in out
        assert "copy_data" in out

    def test_blocking_panel_none_when_clean(self) -> None:
        assert blocking_panel(_plan()) is None

    def test_blocking_panel_lists_reasons(self) -> None:
        out = render(blocking_panel(_plan(blocked=True)))
        assert "Blocked" in out
        assert "nothing has been changed" in out
        assert "anonymous" in out

    def test_warnings_panel(self) -> None:
        out = render(warnings_panel(_plan()))
        assert "compose comments" in out


class TestPlainPlan:
    def test_is_greppable(self) -> None:
        # A migration plan in a CI log must be line-oriented.
        out = plain_plan(_plan())
        assert "project: shop/production" in out
        assert "from: old-host (10.0.0.1)" in out
        assert "to: new-host (10.0.0.2)" in out
        assert "blocked: False" in out

    def test_includes_per_resource_lines(self) -> None:
        out = plain_plan(_plan())
        assert "resource: name=postgres kind=database strategy=copy_data" in out

    def test_includes_blocking_reasons(self) -> None:
        out = plain_plan(_plan(blocked=True))
        assert "blocked: True" in out
        assert "blocking:" in out

    def test_includes_warnings(self) -> None:
        assert "warning: postgres: compose comments will be lost" in plain_plan(_plan())
