"""HEALTHCHECK counts a healthy container only once START's OWN deployment finished.

A container is up just as well when someone else deployed it — a git push or a
CI ``/deploy`` of the target, before or after START. Passing on it let FINALIZE
release the source while START's deployment had failed.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bg_coolify_migrate.domain.statemachine import State
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import TransferError
from tests.test_containment_engine import _app_plan, _tgt, ctx, no_sleep
from tests.test_engine_steps import BASE
from tests.test_pins_engine import COOLIFY

__all__ = ["ctx", "no_sleep"]  # fixtures, imported for pytest


def _running_web() -> dict[str, str]:
    return {**COOLIFY, "coolify.resourceName": "web"}


class TestHealthcheckWaitsForItsDeployment:
    @pytest.fixture(autouse=True)
    def _web_up(self, ctx: MigrationContext) -> None:
        ctx.plan = _app_plan()
        ctx.target_uuids["app1"] = "tgt1"
        _tgt(ctx).add("postgres-db2", labels=COOLIFY)
        _tgt(ctx).add("web-tgt1", labels=_running_web())
        ctx.journal.append(
            "step_completed",
            state=State.START_TARGET.value,
            detail={"started": ["db2", "tgt1"], "deployments": {"app1": "dep1"}},
        )

    async def test_a_container_that_is_up_is_not_enough(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        route = respx_mock.get(f"{BASE}/deployments/dep1").mock(
            side_effect=[
                httpx.Response(200, json={"status": "in_progress"}),
                httpx.Response(200, json={"status": "finished"}),
            ]
        )
        result = await steps.step_healthcheck(ctx)
        assert route.call_count == 2 and len(no_sleep) == 1
        assert result["containers"] == 2

    async def test_a_failed_deployment_fails_with_every_container_up(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        # Up, but someone else's: START's own deployment failed.
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json={"status": "failed"})
        )
        with pytest.raises(TransferError, match="web did not deploy"):
            await steps.step_healthcheck(ctx)
        assert no_sleep == []

    async def test_one_that_never_finishes_times_out_saying_so(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ctx.settings, "deploy_timeout", 3.0)
        respx_mock.get(f"{BASE}/deployments/dep1").mock(return_value=httpx.Response(500))
        with pytest.raises(TransferError) as excinfo:
            await steps.step_healthcheck(ctx)
        assert "containers are up, but the deployment START asked for did not finish" in str(
            excinfo.value
        )
        assert "web: its deployment dep1 could not be read" in str(excinfo.value)
        assert "another deployment's" in (excinfo.value.hint or "")
