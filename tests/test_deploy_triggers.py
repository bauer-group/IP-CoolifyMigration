"""The deploy switches of sources and targets: the pure decisions.

domain/deploy_triggers.py decides what FINALIZE arms on a target. No mocks:
every decision is a function of a GET.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain import coolify_versions as cv
from bg_coolify_migrate.domain import deploy_triggers as dt
from bg_coolify_migrate.domain.deploy_triggers import Triggers

ON = Triggers(auto_deploy=True, previews=False)
OFF = Triggers(auto_deploy=False, previews=False)
BOTH = Triggers(auto_deploy=True, previews=True)
UNREAD = Triggers(auto_deploy=None, previews=None)


def _app(**settings: Any) -> dict[str, Any]:
    return {"uuid": "app1", "settings": settings}


class TestReadTriggers:
    def test_reads_the_nested_settings(self) -> None:
        got = dt.read_triggers(
            _app(is_auto_deploy_enabled=True, is_preview_deployments_enabled=True)
        )
        assert got == BOTH

    def test_a_41_get_has_no_settings(self) -> None:
        # application_by_uuid eager-loads settings from v4.2.0 only (:2277).
        assert dt.read_triggers({"uuid": "app1"}) == UNREAD

    def test_a_flat_value_is_never_read(self) -> None:
        # The GET nests the settings; a flat key would be some other shape.
        assert dt.read_triggers({"is_auto_deploy_enabled": False}) == UNREAD

    def test_a_non_boolean_is_unreadable(self) -> None:
        assert dt.read_triggers(_app(is_auto_deploy_enabled=1)).auto_deploy is None


class TestTargets:
    def test_created_with_auto_deploy_off(self) -> None:
        assert dict(dt.TARGET_CREATE) == {"is_auto_deploy_enabled": False}

    @pytest.mark.parametrize(
        ("original", "armed"), [(ON, True), (OFF, False), (BOTH, True), (UNREAD, True)]
    )
    def test_armed_with_the_sources_original(self, original: Triggers, armed: bool) -> None:
        # Unreadable (4.1.x): Coolify's default, what every target got before.
        assert dt.target_arm_body(original) == {"is_auto_deploy_enabled": armed}


class TestNotes:
    def test_a_failed_arm_says_what_to_set(self) -> None:
        on = dt.arm_failed_notice("shop", "appT", True, reason="HTTP 500")
        off = dt.arm_failed_notice("shop", "appT", False, reason="HTTP 500")
        assert "switch 'Auto Deploy' on" in on and "will not deploy it" in on
        assert "switch 'Auto Deploy' off" in off and "may deploy it" in off


class TestRegistry:
    """Every field through the release registry (AGENTS.md)."""

    def test_every_application_route_creates_with_it(self) -> None:
        for route in cv.APPLICATION_ROUTES:
            assert cv.carries(f"applications/{route}", "is_auto_deploy_enabled", (4, 1, 2))
            assert cv.carries(f"applications/{route}", "is_auto_deploy_enabled", None)

    def test_the_settings_patch_takes_auto_deploy_everywhere(self) -> None:
        for version in ((4, 1, 2), (4, 2, 0), (4, 7, 2), None):
            assert cv.carries("applications.settings", "is_auto_deploy_enabled", version)

    def test_nothing_else_rides_the_settings_patch(self) -> None:
        assert cv.known_fields("applications.settings") == {"is_auto_deploy_enabled"}
