"""Schedules that must keep firing after the move, decided at plan time.

PURE module: no IO.

Coolify hangs schedules off a resource that are part of no resource GET and no
create body: scheduled tasks (applications and services). Every migration before
this module recreated the stack without them — the nightly cleanup or report cron
silently stopped on the target, and nothing said so.

They are carried at CREATE_TARGET by ``api/schedules.py``. What makes copying
them with the source's ``enabled`` safe before START is upstream's own guard, not
ours: the scheduler skips any task whose resource's status column does not
contain 'running' (ScheduledJobManager v4.1.2:394-405, v4.5.0:670-681), and a
target created with instant_deploy=false reads 'exited' (the column default)
until START_TARGET. That status skip is THE guard — the UI's "execute now" and
``artisan`` RunScheduledJobsManually dispatch the job without it, but neither is
anything this tool calls.

This module decides what the copy cannot reproduce. Refusals are creates that
WILL 422; warnings are behaviour the operator has to judge (AGENTS.md). Every
rule is silent on a stock setup, so a warning here is never furniture.

No line produced here ever contains a task's ``command``: Coolify itself treats
it as sensitive from 4.4 (hidden without can_read_sensitive), and it carries
credentials often enough — a curl header, a DSN. Names only (invariant 5).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from bg_coolify_migrate.domain.versions import Version, at_least

# ── where each rule starts ───────────────────────────────────────────────────

#: Task timeout gains max:36000 on create and update (ScheduledTasksController
#: v4.4.0:73, v4.5.0:88; plain integer|min:1 up to v4.3.24).
TASK_TIMEOUT_MAX_SINCE: Version = (4, 4, 0)
#: ScheduledTaskJob also matches a task's container by its exact FULL name
#: (commit 5ae125ad3, first released in v4.5.0:190; prefix-only at v4.4.7:174).
TASK_CONTAINER_EXACT_NAME_SINCE: Version = (4, 5, 0)

#: The UI has always capped a task timeout at 60..36000 (Add.php v4.1.2:44), so
#: only a task created through the API before 4.4 can hold more.
_TASK_TIMEOUT_MAX = 36000


def _name(task: dict[str, Any]) -> str:
    return str(task.get("name") or "?")


def server_timezone(server: dict[str, Any] | None) -> str | None:
    """The clock a server's schedules fire on, or ``None`` when the GET does not say.

    ScheduledJobManager evaluates every cron in the server's
    ``settings.server_timezone`` and falls back to ``config('app.timezone')`` —
    'UTC' — for an empty value (v4.1.2:541-546, v4.5.0:832-837). GET
    /servers/{uuid} serialises it under the eager-loaded ``settings``. A server
    record WITHOUT the key is "we do not know", never "UTC": comparing an unknown
    against a known value would invent a difference.
    """
    if not isinstance(server, dict):
        return None
    settings = server.get("settings")
    if not isinstance(settings, dict) or "server_timezone" not in settings:
        return None
    return str(settings.get("server_timezone") or "UTC")


# ── scheduled tasks ──────────────────────────────────────────────────────────


def scheduled_task_refusals(tasks: list[dict[str, Any]], *, version: Version | None) -> list[str]:
    """Tasks whose create on the target WILL 422."""
    if not at_least(version, TASK_TIMEOUT_MAX_SINCE):
        return []
    too_long = [
        f"{_name(t)!r} ({timeout}s)"
        for t in tasks
        if isinstance(timeout := t.get("timeout"), int)
        and not isinstance(timeout, bool)
        and timeout > _TASK_TIMEOUT_MAX
    ]
    if not too_long:
        return []
    return [
        f"scheduled task(s) {', '.join(too_long)} run with a timeout above "
        f"{_TASK_TIMEOUT_MAX}s. Coolify >= 4.4 refuses that on create — only a task "
        "created through the API before 4.4 can hold it — so the target's copy would "
        f"be rejected outright. Lower it to {_TASK_TIMEOUT_MAX}s or less on the source "
        "(the UI's own limit), then migrate."
    ]


def scheduled_task_warnings(
    tasks: list[dict[str, Any]],
    *,
    source_uuid: str,
    source_server: dict[str, Any] | None,
    target_server: dict[str, Any] | None,
    version: Version | None,
) -> list[str]:
    """Tasks that are carried but will not behave on the target as on the source."""
    out: list[str] = []

    if at_least(version, TASK_CONTAINER_EXACT_NAME_SINCE) and source_uuid:
        # A container field holding the FULL name embeds the source uuid, and the
        # target's containers are named after its own. Carried verbatim and
        # reported, never rewritten: deriving a name by string-replacing a uuid is
        # the bug invariant 10 forbids. Before 4.5 such a value matched only by
        # luck (a single container ignores the field), which the target
        # reproduces, so it stays silent there.
        named = sorted(
            _name(t)
            for t in tasks
            if isinstance(container := t.get("container"), str) and source_uuid in container
        )
        if named:
            out.append(
                f"scheduled task(s) {named} name their container by its full name, which "
                "contains the source's uuid; the target's containers are named after the "
                "target's. With more than one container running, the task fails with "
                "'No valid container was found'. Set the container to the bare compose "
                "service name (e.g. 'web') on the source, or on the target afterwards."
            )

    source_tz, target_tz = server_timezone(source_server), server_timezone(target_server)
    armed = sorted(_name(t) for t in tasks if t.get("enabled") is not False)
    if armed and source_tz and target_tz and source_tz != target_tz:
        out.append(
            f"scheduled task(s) {armed} fire on the server's clock: {source_tz} on the "
            f"source server, {target_tz} on the target. The same frequency runs at a "
            "different wall-clock time after the move — align the target server's "
            "timezone, or adjust the frequencies on the target."
        )
    return out


def task_container_mismatches(tasks: list[dict[str, Any]], available: set[str]) -> list[str]:
    """Tasks whose ``container`` names nothing the TARGET runs. PURE.

    A service target is created from the instance's CURRENT template catalog and a
    dockercompose app from git HEAD — not from the source's compose — so a renamed
    compose service leaves the task pointing at nothing. Empty when at most one
    container exists: ScheduledTaskJob then ignores the field (v4.5.0:190,
    v4.1.2:149).
    """
    if len(available) <= 1:
        return []
    return sorted(
        _name(t)
        for t in tasks
        if isinstance(container := t.get("container"), str)
        and container
        and container not in available
    )


def copy_count_mismatches(sent: Iterable[str], found: Iterable[str]) -> dict[str, tuple[int, int]]:
    """``{name: (created, present)}`` wherever the two counts differ. PURE.

    The client retries a POST on 429/502/503/504, and a proxy error that arrives
    after Coolify committed the row turns one create into two. Nothing upstream
    deduplicates (the tables have no uniqueness constraint), so the only proof of
    "exactly once" is to count what the target holds afterwards.
    """
    want, got = Counter(sent), Counter(found)
    return {n: (want[n], got[n]) for n in sorted(set(want) | set(got)) if want[n] != got[n]}
