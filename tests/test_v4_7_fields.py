"""The request fields Coolify v4.7.0 adds, carried from that release only.

Four application settings join APPLICATION_SETTING_FIELDS (ApplicationsController
v4.7.0:55-56,60,83 -> create :1613), and scheduled tasks gain ``use_shell``
(ScheduledTasksController v4.7.0:81). Each ships together with its column, so
each is ``column_gated``: never sent on 4.6.x, sent on an unknown release only
when the source GET carries the key. Without them a source that turned a setting
off came back on the defaults, and a no-shell task came back under ``sh -c``.

``gitea_app_uuid`` (same release) is NOT carried: it belongs to a create route
this tool does not use, and a Gitea-App source is refused at plan time.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.api.fields import APPLICATION_CREATE, SCHEDULED_TASK_CREATE, filter_for
from bg_coolify_migrate.api.schedules import build_scheduled_task_body
from bg_coolify_migrate.domain import coolify_versions as cv
from bg_coolify_migrate.engine.planner import _resolve_git_auth
from bg_coolify_migrate.errors import UnsupportedResource
from tests.test_scheduled_tasks import _task

#: The four v4.7.0 settings and a NON-default value for each — what a source that
#: changed them carries. Defaults: true, true, false, true (migrations
#: 2026_11_05_163404, 2026_11_03_072701, 2026_10_29_100000).
_V4_7_SETTINGS: dict[str, bool] = {
    "include_network_hosts_in_build": False,
    "is_auto_cancel_queued_deployments_enabled": False,
    "is_auto_cancel_building_deployments_enabled": True,
    "is_image_scan_enabled": False,
}


# ── application settings ─────────────────────────────────────────────────────


class TestApplicationSettings:
    @pytest.mark.parametrize("route", cv.APPLICATION_ROUTES)
    def test_carried_from_v4_7_0_on_every_route(self, route: str) -> None:
        """v4.6.0 refuses all four on create — a 422 for the whole resource."""
        endpoint = f"applications/{route}"
        settings = set(_V4_7_SETTINGS)
        assert not settings & cv.fields_for(endpoint, (4, 6, 0))
        assert settings <= cv.fields_for(endpoint, (4, 7, 0))
        assert settings <= cv.fields_for(endpoint, (4, 7, 1))
        sources = cv.field_sources(endpoint, (4, 7, 0))
        assert {sources[f] for f in settings} == {"settings"}, "the GET nests them"

    def test_an_unknown_release_sends_them_only_when_the_source_has_them(self) -> None:
        """column_gated: a GET carries the key only where the create takes it."""
        assert set(_V4_7_SETTINGS) <= cv.fields_for("applications/public", None)
        assert filter_for({"settings": {}}, "applications/public", None) == {}

    @pytest.mark.parametrize("version", [(4, 7, 0), None])
    def test_the_nested_value_is_carried_flat_false_included(
        self, version: tuple[int, ...] | None
    ) -> None:
        """REGRESSION guard: a False must survive — three of the four default True."""
        body = filter_for({"settings": dict(_V4_7_SETTINGS)}, "applications/public", version)
        assert body == _V4_7_SETTINGS
        assert body["is_image_scan_enabled"] is False

    def test_never_read_from_the_top_level(self) -> None:
        """The GET returns them under `settings` only (ApplicationsController :2848)."""
        assert filter_for(dict(_V4_7_SETTINGS), "applications/public", (4, 7, 0)) == {}

    def test_never_sent_to_a_v4_6_target(self) -> None:
        source = {"settings": dict(_V4_7_SETTINGS)}
        assert filter_for(source, "applications/dockerimage", (4, 6, 0)) == {}

    def test_image_scanning_off_reaches_the_image_route_too(self) -> None:
        """An image app's deploy is gated too: rolling_update checks the gate
        before it starts anything (ApplicationDeploymentJob v4.7.0:3043)."""
        source = {"settings": {"is_image_scan_enabled": False}}
        body = filter_for(source, "applications/dockerimage", (4, 7, 0))
        assert body == {"is_image_scan_enabled": False}


# ── the Gitea App route ──────────────────────────────────────────────────────


class TestGiteaAppSource:
    def test_its_uuid_is_sent_on_no_route_and_no_release(self) -> None:
        assert "gitea_app_uuid" not in APPLICATION_CREATE
        for route in cv.APPLICATION_ROUTES:
            for version in ((4, 6, 0), (4, 7, 0), (4, 7, 1), None):
                assert not cv.carries(f"applications/{route}", "gitea_app_uuid", version)

    async def test_a_gitea_source_is_refused_without_reading_its_tokens(self) -> None:
        """Refused, never created as public — and GET /gitea-apps is never read.

        That list returns the source's OAuth tokens in clear to a token that can
        read sensitive data (GiteaController v4.7.0:14-24).
        """
        calls: list[str] = []

        class _Api:
            async def get(self, path: str) -> Any:
                calls.append(path)
                raise AssertionError(f"unexpected GET {path}")

        with pytest.raises(UnsupportedResource, match="cannot be recreated") as exc_info:
            await _resolve_git_auth(
                _Api(),  # type: ignore[arg-type]
                {
                    "name": "forge",
                    "git_repository": "acme/forge",
                    "source_type": r"App\Models\GiteaApp",
                    "source_id": 3,
                },
            )
        assert calls == []
        hint = exc_info.value.hint or ""
        assert "Gitea-App sources are not supported yet" in hint
        assert "v4.1.2" not in hint, "the old hint said no Gitea route exists"


# ── scheduled tasks ──────────────────────────────────────────────────────────


class TestScheduledTaskUseShell:
    @pytest.mark.parametrize("use_shell", [False, True])
    def test_carried_on_v4_7_0(self, use_shell: bool) -> None:
        """An ABSENT use_shell is stored TRUE (ScheduledTasksController v4.7.0:128)."""
        body = build_scheduled_task_body(_task(use_shell=use_shell), version=(4, 7, 0))
        assert body["use_shell"] is use_shell
        assert set(body) <= SCHEDULED_TASK_CREATE

    def test_never_sent_to_a_v4_6_target(self) -> None:
        body = build_scheduled_task_body(_task(use_shell=False), version=(4, 6, 0))
        assert "use_shell" not in body

    @pytest.mark.parametrize("version", [(4, 7, 0), None])
    def test_a_row_without_the_key_sends_nothing(self, version: tuple[int, ...] | None) -> None:
        """A pre-4.7 row has no such column; nothing is invented for it."""
        assert "use_shell" not in build_scheduled_task_body(_task(), version=version)

    def test_an_unknown_release_carries_what_the_row_has(self) -> None:
        """column_gated: only a 4.7+ GET carries the key, and 4.7+ accepts it."""
        body = build_scheduled_task_body(_task(use_shell=False), version=None)
        assert body["use_shell"] is False
