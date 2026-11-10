"""The deploy switches against a real Coolify (domain/deploy_triggers.py).

From QUIESCE until FINALIZE neither side may deploy on a push: the source is
switched off before the stop, the target is created switched off. FINALIZE arms
the target with the source's original; under keep the source stays off, and a
rollback switches it back on.

The rig's application is an nginx dockerimage app. Its create honours
is_auto_deploy_enabled from v4.5.1 (ApplicationsController v4.5.1:2466), which
the rig (coolify:latest) is far past.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome, State
from bg_coolify_migrate.engine.runner import make_migration_id, rollback_migration, run_migration
from bg_coolify_migrate.settings.base import Settings

from .test_domain_handover import _app_on, _plan
from .test_rollback import deploy_domained_app

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


def _auto(resource: dict[str, Any] | None) -> object:
    return ((resource or {}).get("settings") or {}).get("is_auto_deploy_enabled")


async def test_both_sides_wait_disarmed_and_a_kept_source_stays_so(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> None:
    async with deploy_domained_app(api, settings, rig) as app:
        original = _auto(await api.get_resource("applications", app["app_uuid"]))
        assert original is True, "the rig app should carry Coolify's default (on)"
        plan = await _plan(api, settings, rig, app, FinalizePolicy.KEEP)
        seen: dict[str, tuple[object, object]] = {}

        async def watch(state: State) -> None:
            # Fires BEFORE each step: the target exists from QUIESCE on, and
            # FINALIZE has not armed anything yet.
            if state not in (State.DNS_GATE, State.START_TARGET, State.FINALIZE):
                return
            source = await api.get_resource("applications", app["app_uuid"])
            target = await _app_on(api, app["app_name"], str(rig["server_b"]["uuid"]))
            seen[state.value] = (_auto(source), _auto(target))

        mid = make_migration_id(plan.project, plan.environment)
        result = await run_migration(api, settings, plan, migration_id=mid, on_state=watch)
        assert result.outcome is Outcome.SUCCEEDED, f"migration did not succeed: {result}"

        for state in (State.DNS_GATE, State.START_TARGET, State.FINALIZE):
            assert seen[state.value] == (False, False), (state, seen[state.value])

        target = await _app_on(api, app["app_name"], str(rig["server_b"]["uuid"]))
        assert _auto(target) is original, "FINALIZE gives the target its source's auto-deploy"
        kept = await api.get_resource("applications", app["app_uuid"])
        assert _auto(kept) is False, "keep leaves the source's auto-deploy off"
        assert any("the kept source" in notice for notice in result.notices), result.notices

        rolled = await rollback_migration(api, settings, plan, mid)
        assert rolled.outcome is Outcome.ROLLED_BACK, f"rollback did not finish: {rolled}"
        restored = await api.get_resource("applications", app["app_uuid"])
        assert _auto(restored) is original, "the rollback switches the source back on"
