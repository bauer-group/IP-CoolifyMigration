"""Rollback against a real instance: does the compensation actually undo?

The chaos unit tests prove the saga's rollback LOGIC converges. They cannot prove
that DELETE_TARGET_RESOURCE really removes a resource from Coolify, or that
RESTART_SOURCE really brings the source back — those are API calls to a real
instance, and an API call that looks right in a mock is exactly what this whole
week has been about.

So: deploy and seed a Postgres, inject a fault AFTER the volumes are copied
(patch VERIFY to raise), and assert the world was put back:

  * the migration reports ROLLED_BACK, not SUCCEEDED
  * the target resource is gone from server-b — the copy did not leave a
    half-built database behind
  * the source is still on server-a, running, with its data intact — the backbone
    of the whole safety story is that the source survives until FINALIZE

The fault lands at VERIFY on purpose: late enough that the target exists and its
volumes were copied (so DELETE_TARGET_RESOURCE has real work to do), early enough
that FINALIZE never ran (so the source was never renamed or deleted).
"""

from __future__ import annotations

import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.domains import parking_tag
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import make_migration_id, rollback_migration, run_migration
from bg_coolify_migrate.errors import VerificationError
from bg_coolify_migrate.settings.base import Settings

from .conftest import PG_FINGERPRINT as FINGERPRINT
from .conftest import db_exec, ssh_to, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

#: A schedule on the source: the rollback deletes the target's copy with the
#: target, and must never touch the source's own row.
BACKUP = {"frequency": "0 4 * * *", "enabled": True, "save_s3": False, "timeout": 900}


async def test_rollback_restores_the_world(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    seeded_pg: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fault after COPY must delete the target and leave the source running."""

    async def _boom(_ctx: object) -> dict[str, Any]:
        raise VerificationError("injected fault: pretend verification found a mismatch")

    # build_steps() resolves step_verify from the module namespace at call time,
    # so patching it here reaches the saga run_migration is about to build.
    monkeypatch.setattr(steps, "step_verify", _boom)

    backup_uuid = await api.create_backup(seeded_pg["db_uuid"], BACKUP)
    before = _own_backup(await api.get_backups(seeded_pg["db_uuid"]), backup_uuid)

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=seeded_pg["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.ROLLED_BACK, f"expected rollback, got {result}"

    # 1. The target was really deleted — no half-built database on server-b.
    #    Coolify's delete is async (it dispatches a deletion job), so poll rather
    #    than read once the instant run_migration returns.
    import asyncio

    for _ in range(24):
        target = await _resource_on(api, seeded_pg["db_name"], str(rig["server_b"]["uuid"]))
        if target is None:
            break
        await asyncio.sleep(5)
    assert target is None, "rollback left a target resource on server-b"

    # 2. The source is still there, under its ORIGINAL name — never renamed,
    #    because FINALIZE never ran.
    source = await _resource_on(api, seeded_pg["db_name"], str(rig["server_a"]["uuid"]))
    assert source is not None, "the source resource vanished — rollback destroyed it"

    # 3. And it is running with its data intact. The API confirming a row exists
    #    is not enough; ask the daemon.
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        await wait_until_healthy(source_host, seeded_pg["db_uuid"])
        arrived = await db_exec(source_host, seeded_pg["db_uuid"], FINGERPRINT)
    assert arrived == seeded_pg["fingerprint"], (
        f"source data changed across the rollback\n"
        f"  before: {seeded_pg['fingerprint']}\n  after:  {arrived}"
    )

    # 4. The source's backup schedule is exactly as it was: same row, same values.
    #    The target's copy went with the target (DeleteResourceJob deletes the rows).
    after = _own_backup(await api.get_backups(seeded_pg["db_uuid"]), backup_uuid)
    assert after == before, f"the rollback touched the source's schedule\n  {before}\n  {after}"


def _own_backup(rows: list[dict[str, Any]], backup_uuid: str) -> dict[str, Any]:
    """One schedule's configuration, without what a run changes (history, timestamps)."""
    row = next((r for r in rows if r.get("uuid") == backup_uuid), None)
    assert row is not None, f"backup schedule {backup_uuid} is gone"
    volatile = {"executions", "updated_at", "last_execution_at"}
    return {k: v for k, v in row.items() if k not in volatile}


async def _resource_on(
    api: CoolifyClient, name: str, server_uuid: str, *, collection: str = "databases"
) -> str | None:
    for row in await api.get(f"/{collection}") or []:
        if not isinstance(row, dict) or row.get("name") != name:
            continue
        full = await api.get_resource(collection, str(row["uuid"]))
        if server_uuid_of(full) == server_uuid:
            return str(row["uuid"])
    return None


# ── the source's domain survives a rollback ──────────────────────────────────
#
# The source keeps its custom domain until FINALIZE releases it (make-before-
# break: the target claims it at CREATE). A domain PATCH only writes Coolify's
# database; the proxy labels are baked into the container at deploy time, and
# the rollback's /restart IS a deploy. Restarting before the restore brought a
# released source back without its host (and, up to 2.9, a parked one back on
# its old-<tag>. host). The unit respx test (test_rollback_domains_engine.py) is
# the regression lock on call ORDER; this confirms on 4.5.0 that the order gives
# the restarted container the right labels.
#
# Labels can be asserted with no proxy actually serving: rig servers are created
# through the API, whose proxy type defaults to TRAEFIK, so isProxyShouldRun()
# holds and Host() labels are generated (Server.php v4.5.0:2080-2088,
# ServersController :610).

#: nginx:1.27-alpine by its index digest (Docker Hub, pushed 2025-04-16). Coolify
#: stores a 64-hex tag as `name@sha256` + hash (DockerImageParser, v4.5.0
#: ApplicationsController:2383-2406), so the image cannot move under the test.
NGINX_DIGEST = "65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"


async def _running_app_container(host: Any, app_uuid: str, *, not_named: str = "") -> str | None:
    result = await host.run(
        f"docker ps --filter name={app_uuid} --filter status=running --format '{{{{.Names}}}}'"
    )
    names = [n for n in result.stdout.split() if n and n != not_named]
    return names[0] if names else None


async def _labels(host: Any, container: str) -> dict[str, str]:
    import json

    result = await host.run(f"docker inspect -f '{{{{json .Config.Labels}}}}' {container}")
    result.check()
    return dict(json.loads(result.stdout.strip() or "{}"))


@pytest_asyncio.fixture
async def domained_app(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """An nginx dockerimage app on server-a, on a custom domain, deployed.

    Also the rig's smoke test for APPLICATIONS: every other e2e test migrates a
    database or a service, so this is the first application the rig deploys.
    If it never comes up, the fixture errors at setup, and the rollback verdict
    below is not read against a rig that cannot run applications at all.
    """
    import asyncio
    import contextlib

    suffix = uuidlib.uuid4().hex[:8]
    project = await api.post("/projects", {"name": f"rbd-{suffix}", "description": "bgcm rollback"})
    project_uuid = str(project["uuid"])
    app_name = f"web-{suffix}"
    host_name = f"rb-{suffix}.example.com"
    try:
        app = await api.post(
            "/applications/dockerimage",
            {
                "name": app_name,
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "docker_registry_image_name": "nginx",
                "docker_registry_image_tag": NGINX_DIGEST,
                "ports_exposes": "80",
                "domains": f"http://{host_name}",
                "instant_deploy": True,
            },
        )
        app_uuid = str(app["uuid"])
        container = None
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            for _ in range(60):
                container = await _running_app_container(source, app_uuid)
                if container:
                    break
                await asyncio.sleep(4)
        if container is None:
            pytest.fail(
                "smoke: a dockerimage application never came up on server-a - the rig "
                "cannot run applications, so no application test here means anything"
            )
        yield {
            "project": f"rbd-{suffix}",
            "app_name": app_name,
            "app_uuid": app_uuid,
            "host": host_name,
            "container": container,
        }
    finally:
        with contextlib.suppress(Exception):
            for row in await api.get("/applications") or []:
                if isinstance(row, dict) and suffix in str(row.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource(
                            "applications", str(row["uuid"]), delete_volumes=True
                        )
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def _assert_source_serves_its_domain(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    app: dict[str, Any],
    *,
    tag: str,
) -> None:
    """A NEW running source container carries Host(<custom>) and no old-<tag>. host."""
    import asyncio

    full = await api.get_resource("applications", app["app_uuid"])
    assert full.get("fqdn") == f"http://{app['host']}", f"fqdn not restored: {full.get('fqdn')!r}"

    labels: dict[str, str] = {}
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
        # QUIESCE removed the original container, so a running one under another
        # name is the rollback's restart.
        for _ in range(60):
            container = await _running_app_container(
                source, app["app_uuid"], not_named=app["container"]
            )
            if container:
                labels = await _labels(source, container)
                break
            await asyncio.sleep(4)
    assert labels, "the source never came back after the rollback"
    values = list(labels.values())
    assert any(f"Host(`{app['host']}`)" in v for v in values), (
        f"the restarted source has no router for {app['host']}: {values}"
    )
    assert not any(f"old-{tag}." in v for v in values), (
        "the restarted source carries an old-<tag>. host - something parked it"
    )


async def _assert_target_gone(api: CoolifyClient, rig: dict[str, Any], name: str) -> None:
    import asyncio

    target = None
    for _ in range(24):
        target = await _resource_on(
            api, name, str(rig["server_b"]["uuid"]), collection="applications"
        )
        if target is None:
            return
        await asyncio.sleep(5)
    raise AssertionError(f"rollback left target {target} on server-b")


async def test_rollback_brings_the_source_back_on_its_own_domain(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    domained_app: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fault after QUIESCE: the restarted source must route its custom domain."""

    async def _boom(_ctx: object) -> dict[str, Any]:
        raise VerificationError("injected fault: pretend verification found a mismatch")

    monkeypatch.setattr(steps, "step_verify", _boom)

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=domained_app["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    result = await run_migration(api, settings, plan)
    assert result.outcome is Outcome.ROLLED_BACK, f"expected rollback, got {result}"

    tag = parking_tag(plan.project, plan.environment)
    await _assert_source_serves_its_domain(api, settings, rig, domained_app, tag=tag)
    await _assert_target_gone(api, rig, domained_app["app_name"])


async def test_rollback_after_rename_restores_domains(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    domained_app: dict[str, Any],
) -> None:
    """Run to DONE with RENAME, then roll back: name AND blanked domains come back.

    FINALIZE(rename) releases the source's custom domains as its last step
    (``{"domains": ""}`` for an app with only a custom host, which 4.5 accepts and
    stores as null), so this is also the first live run of that PATCH. RENAME
    only — a rollback after DELETE is refused, because the target is then the
    only copy.
    """
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=domained_app["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    mid = make_migration_id(plan.project, plan.environment)
    # example.com never resolves to the source; accept_dns lets the gate pass.
    result = await run_migration(api, settings, plan, migration_id=mid, accept_dns=True)
    assert result.outcome is Outcome.SUCCEEDED, f"expected DONE, got {result}"
    renamed = await api.get_resource("applications", domained_app["app_uuid"])
    assert renamed.get("name") != domained_app["app_name"]
    assert not renamed.get("fqdn"), "FINALIZE(rename) should have released the domains"

    back = await rollback_migration(api, settings, plan, mid)
    assert back.outcome is Outcome.ROLLED_BACK, f"expected rollback, got {back}"

    source = await api.get_resource("applications", domained_app["app_uuid"])
    assert source.get("name") == domained_app["app_name"]
    tag = parking_tag(plan.project, plan.environment)
    await _assert_source_serves_its_domain(api, settings, rig, domained_app, tag=tag)
    await _assert_target_gone(api, rig, domained_app["app_name"])
