"""The drift gate, against a real floating-tag image.

The user's requirement, in their words: a floating tag like `:latest` should be
surfaced as a warning and the migration should ask whether to proceed — not
silently rebuild against whatever the registry now serves, and not refuse
outright. The operator decides.

`eqalpha/keydb:latest` is a real floating tag. This proves the gate does the
right thing with it:

  * without the operator's consent, the run BLOCKS at preflight — nothing is
    created, nothing is stopped, the plan names the exact reason
  * the block is BLOCKED, not FAILED: a deliberate, resumable stop, not an error

The "with consent it completes" half is covered by the engine suite, which
migrates keydb with accept_drift=True. This file owns the refusal.
"""

from __future__ import annotations

import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine.planner import build_plan
from bg_coolify_migrate.engine.runner import run_migration
from bg_coolify_migrate.settings.base import Settings

from .conftest import ssh_to, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def floating_tag_db(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """A deployed KeyDB, whose default image `eqalpha/keydb:latest` is floating."""
    suffix = uuidlib.uuid4().hex[:8]
    project = await api.post("/projects", {"name": f"drift-{suffix}", "description": "bgcm drift"})
    project_uuid = str(project["uuid"])
    try:
        db = await api.post(
            "/databases/keydb",
            {
                "name": f"keydb-{suffix}",
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "keydb_password": "rootpw",
                "instant_deploy": True,
            },
        )
        db_uuid = str(db["uuid"])
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            await wait_until_healthy(source, db_uuid)
        yield {"project": f"drift-{suffix}", "project_uuid": project_uuid, "db_uuid": db_uuid}
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            for db in await api.get("/databases") or []:
                if isinstance(db, dict) and suffix in str(db.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource(
                            "databases", str(db["uuid"]), delete_volumes=True
                        )
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def test_floating_tag_blocks_without_consent(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], floating_tag_db: dict[str, Any]
) -> None:
    """A `:latest` source blocks at preflight, names the reason, mutates nothing."""
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=floating_tag_db["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    # The plan itself should already flag it — the drift is discovered while
    # reading, before anything runs.
    assert plan.is_blocked or any(r.needs_confirmation for r in plan.resources), (
        "the plan did not flag the floating tag as needing a decision"
    )

    result = await run_migration(api, settings, plan)  # no accept_drift

    # BLOCKED, not FAILED: a question, not an error. And it stopped at PREFLIGHT,
    # so nothing was created on server-b and the source was never touched.
    assert result.outcome is Outcome.BLOCKED, f"expected a block, got {result}"
    assert "moving tag" in str(result.error) or "newer" in str(result.error), (
        f"the block did not explain the floating tag: {result.error}"
    )

    # Nothing landed on server-b.
    from bg_coolify_migrate.engine.planner import server_uuid_of

    for db in await api.get("/databases") or []:
        if isinstance(db, dict) and "keydb" in str(db.get("name", "")):
            full = await api.get_resource("databases", str(db["uuid"]))
            assert server_uuid_of(full) != str(rig["server_b"]["uuid"]), (
                "a blocked migration still created a target on server-b"
            )
