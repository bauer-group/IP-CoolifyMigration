"""An application that also deploys to an additional server is refused.

The refusal (fidelity.multi_server_refusal) rests on ONE upstream assumption a
respx mock cannot settle: that GET /applications/{uuid} really carries
`additional_servers_count` — from a global withCount scope — and, by a lazy load
in the status accessor, the `additional_servers` rows the names come from. This
file pins both against a live Coolify, then proves the plan blocks, the run
creates nothing, and removing the server lifts the refusal.

Nothing is deployed, so no image is pulled: the application is created with
instant_deploy off and the destination attached through the 4.3+ route. That is
also what makes test (3)'s API detach safe here — the API's DELETE never stops
anything, and there is nothing running to orphan.

Not testable on this rig: a full carry. With two daemons the only multi-server
shape is "additional server == target", and Coolify itself refuses to attach a
destination on an application's primary server.
"""

from __future__ import annotations

import contextlib
import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import run_migration
from bg_coolify_migrate.settings.base import Settings

from .conftest import ssh_to

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def multi_server_app(
    api: CoolifyClient, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A never-deployed nginx on server-a with server-b attached as an extra destination."""
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"multi-{suffix}"
    project = await api.post("/projects", {"name": project_name, "description": "bgcm multi"})
    project_uuid = str(project["uuid"])
    app_uuid = ""
    try:
        app = await api.post(
            "/applications/dockerimage",
            {
                "name": f"web-{suffix}",
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "docker_registry_image_name": "nginx",
                "docker_registry_image_tag": "1.27-alpine",
                "ports_exposes": "80",
                "instant_deploy": False,
            },
        )
        app_uuid = str(app["uuid"])

        destinations = await api.get_server_destinations(str(rig["server_b"]["uuid"])) or []
        destination = next(
            str(d["uuid"]) for d in destinations if isinstance(d, dict) and d.get("uuid")
        )
        # 4.5.0's add_destination guards pass: not compose, no storage, and both rig
        # servers are bootstrapped identically, so their proxies match.
        await api.post(f"/applications/{app_uuid}/destinations", {"destination_uuid": destination})

        yield {
            "project": project_name,
            "app_uuid": app_uuid,
            "app_name": f"web-{suffix}",
            "destination": destination,
        }
    finally:
        if app_uuid:
            with contextlib.suppress(Exception):
                await api.delete_resource("applications", app_uuid, delete_volumes=True)
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def _plan(api: CoolifyClient, settings: Settings, rig: dict[str, Any], project: str) -> Any:
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        return await build_plan(
            api,
            source_host,
            project=project,
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )


async def test_the_counts_are_in_a_real_response(
    api: CoolifyClient, rig: dict[str, Any], multi_server_app: dict[str, Any]
) -> None:
    """The one upstream assumption the rule rests on, read off the real GET."""
    full = await api.get_resource("applications", multi_server_app["app_uuid"])
    assert full.get("additional_servers_count") == 1
    assert full.get("additional_networks_count") == 1
    # A projection, never the raw rows: on a root token they carry proxy config
    # and server settings, and a failing assert would print them into CI logs.
    uuids = [s.get("uuid") for s in full.get("additional_servers") or [] if isinstance(s, dict)]
    assert rig["server_b"]["uuid"] in uuids, uuids


async def test_a_multi_server_app_is_refused(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], multi_server_app: dict[str, Any]
) -> None:
    """Blocked at plan time, and the run creates nothing on server-b."""
    plan = await _plan(api, settings, rig, multi_server_app["project"])

    assert plan.is_blocked
    reasons = [r for res in plan.resources for r in res.blocking_reasons]
    refusal = next((r for r in reasons if "additional server" in r), None)
    assert refusal is not None, reasons
    assert "e2e-server-b" in refusal, "the names come from the lazily loaded relation"
    # In this rig the additional server IS the target, so the remedy must say so.
    assert "except the migration's target server" in refusal

    # accept_drift so that nothing but the refusal can be what stops it.
    result = await run_migration(api, settings, plan, accept_drift=True)
    assert result.outcome is not Outcome.SUCCEEDED
    assert "additional server" in str(result.error), str(result.error)

    for app in await api.get("/applications") or []:
        if isinstance(app, dict) and app.get("name") == multi_server_app["app_name"]:
            full = await api.get_resource("applications", str(app["uuid"]))
            assert server_uuid_of(full) != str(rig["server_b"]["uuid"]), (
                "a refused migration still created a target on server-b"
            )


async def test_detaching_lifts_the_refusal(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], multi_server_app: dict[str, Any]
) -> None:
    """Remove the server, re-plan, and it is an ordinary single-server migration.

    The API detach is safe ONLY because nothing was deployed: it never stops the
    containers on the detached server, which is why the refusal's text steers
    operators to the UI instead.
    """
    await api.delete(
        f"/applications/{multi_server_app['app_uuid']}/destinations/"
        f"{multi_server_app['destination']}"
    )
    plan = await _plan(api, settings, rig, multi_server_app["project"])
    reasons = [r for res in plan.resources for r in res.blocking_reasons]
    assert not any("additional server" in r for r in reasons), reasons
