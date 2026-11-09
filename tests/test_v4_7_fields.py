"""The request fields Coolify v4.7.0 adds, carried from that release only.

Scheduled tasks gain ``use_shell`` (ScheduledTasksController v4.7.0:81). It ships
together with its column, so it is ``column_gated``: never sent on 4.6.x, sent on
an unknown release only when the source GET carries the key. Without it a
no-shell task came back under ``sh -c``.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.api.fields import SCHEDULED_TASK_CREATE
from bg_coolify_migrate.api.schedules import build_scheduled_task_body
from tests.test_scheduled_tasks import _task

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
