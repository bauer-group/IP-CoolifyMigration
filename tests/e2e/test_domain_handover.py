"""The domain hand-over against a real Coolify: make-before-break, and the DNS gate.

(A) An application on a custom domain migrates WITHOUT parking: the source keeps
    its real host through CREATE, QUIESCE and every step up to FINALIZE, the
    target holds it from CREATE on, and FINALIZE releases it from the source as
    its last step (journalled first). This is what keeps a Coolify-managed DNS
    record (4.4+) alive: it is deleted only when its hostname loses its last
    holder, and here some live resource holds it at every instant.

(B) — rollback AFTER finalize restoring the domains before the restart — lives
    next to its fixtures: test_rollback.py::test_rollback_after_rename_restores_domains
    (application) and test_service_domains.py::test_rollback_after_finalize_restores_
    service_domains (service).

(C) A custom host that resolves to NEITHER server blocks the DNS gate for every
    provider: ``--accept-dns`` does not answer it, accepting that one host by
    name does. Uses ``one.one.one.one`` — Cloudflare's resolver host, which
    resolves to 1.1.1.1/1.0.0.1, never a rig server, and whose zone is on
    Cloudflare (so the 4.4+ plan warning shows too). Skips when the workstation
    has no public DNS.

(D) FINALIZE re-reads the target before the source lets go: a host the target
    lost since CREATE stays on the source, and the run names it.

Managed-DNS rows are NOT seeded here: that needs an IntegrationToken, a zone and
a record through tinker, and the rig cannot reach Cloudflare anyway. The record
logic is unit-tested (tests/test_rollback_domains*.py); this pins the order.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome, State
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import make_migration_id, resume_migration, run_migration
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings

from .conftest import ssh_to
from .test_rollback import deploy_domained_app

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

#: Resolves to Cloudflare's 1.1.1.1/1.0.0.1 from anywhere — neither rig server.
ELSEWHERE_HOST = "one.one.one.one"


async def _plan(
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
    app: dict[str, Any],
    policy: FinalizePolicy,
) -> Any:
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        return await build_plan(
            api,
            source_host,
            project=app["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=policy,
            transfer_mode=TransferMode.DIRECT,
        )


async def _app_on(api: CoolifyClient, name: str, server_uuid: str) -> dict[str, Any] | None:
    for row in await api.get("/applications") or []:
        if not isinstance(row, dict) or row.get("name") != name:
            continue
        full = await api.get_resource("applications", str(row["uuid"]))
        if server_uuid_of(full) == server_uuid:
            return full
    return None


async def test_the_source_keeps_its_custom_host_until_finalize(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> None:
    """(A) No parking: the source holds its real host until FINALIZE lets go."""
    async with deploy_domained_app(api, settings, rig) as app:
        real = f"http://{app['host']}"
        plan = await _plan(api, settings, rig, app, FinalizePolicy.RENAME)
        seen: dict[str, tuple[str, str]] = {}

        async def watch(state: State) -> None:
            # Fires BEFORE each step: at QUIESCE the target is built and has
            # claimed the host; at FINALIZE everything but the release is done.
            if state not in (State.QUIESCE, State.DNS_GATE, State.FINALIZE):
                return
            source = await api.get_resource("applications", app["app_uuid"])
            target = await _app_on(api, app["app_name"], str(rig["server_b"]["uuid"]))
            seen[state.value] = (
                str(source.get("fqdn") or ""),
                str((target or {}).get("fqdn") or ""),
            )

        mid = make_migration_id(plan.project, plan.environment)
        result = await run_migration(api, settings, plan, migration_id=mid, on_state=watch)
        assert result.outcome is Outcome.SUCCEEDED, f"migration did not succeed: {result}"

        for state in (State.QUIESCE, State.DNS_GATE, State.FINALIZE):
            source_fqdn, target_fqdn = seen[state.value]
            assert source_fqdn == real, (
                f"the source lost its host before FINALIZE ({state}): {source_fqdn!r}"
            )
            assert "old-" not in source_fqdn, "the source was parked"
            assert app["host"] in target_fqdn, f"the target did not hold the host at {state}"

        source = await api.get_resource("applications", app["app_uuid"])
        assert not source.get("fqdn"), "FINALIZE should have released the custom host"
        finalize = Journal.open(settings.resolved_state_dir(), mid).undo_info("finalize")
        assert finalize.get("releasing_domains") == [app["app_uuid"]], finalize


async def test_a_host_resolving_elsewhere_blocks_until_accepted_by_name(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> None:
    """(C) ELSEWHERE blocks even with --accept-dns; --accept-dns-host passes it."""
    async with deploy_domained_app(api, settings, rig, host=ELSEWHERE_HOST) as app:
        plan = await _plan(api, settings, rig, app, FinalizePolicy.KEEP)
        baseline = {entry.host: entry for entry in plan.dns_baseline}
        if not baseline.get(ELSEWHERE_HOST) or not baseline[ELSEWHERE_HOST].addresses:
            pytest.skip("no public DNS from this workstation: one.one.one.one did not resolve")
        # Its zone is on Cloudflare: from 4.4 the plan says who changes the record.
        assert any("the zone is on Cloudflare" in note for note in plan.notes), plan.notes

        mid = make_migration_id(plan.project, plan.environment)
        first = await run_migration(api, settings, plan, migration_id=mid, accept_dns=True)
        assert first.outcome is Outcome.BLOCKED, f"expected the DNS gate to block: {first}"
        assert first.reached is State.DNS_GATE
        assert f"{ELSEWHERE_HOST} (elsewhere)" in str(first.error)

        # --accept-dns answers a cutover only, never a host DNS cannot place.
        again = await resume_migration(api, settings, plan, mid, accept_dns=True)
        assert again.outcome is Outcome.BLOCKED, f"--accept-dns must not pass ELSEWHERE: {again}"

        done = await resume_migration(
            api, settings, plan, mid, accept_hosts=frozenset({ELSEWHERE_HOST})
        )
        assert done.outcome is Outcome.SUCCEEDED, f"accepting the host should pass: {done}"
        gate = Journal.open(settings.resolved_state_dir(), mid).undo_info("dns_gate")
        assert gate.get("accepted") == [ELSEWHERE_HOST], gate


async def test_finalize_keeps_a_host_the_target_lost_on_the_source(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> None:
    """(D) FINALIZE re-reads the target: a host it lost since CREATE stays on the source.

    A deploy can drop a target's domain after CREATE claimed it — a dockercompose
    app reloads its compose from git on every deploy and loses the entry of a
    service renamed there. The rig cannot push to a repository, so the loss is
    made directly: just before FINALIZE the target's domain is changed to
    another host. Releasing the source then would leave the real host held by
    nothing (from 4.4 Coolify deletes a record it manages); it must stay put,
    and the run must say so.
    """
    async with deploy_domained_app(api, settings, rig) as app:
        real = f"http://{app['host']}"
        plan = await _plan(api, settings, rig, app, FinalizePolicy.KEEP)

        async def lose_the_host(state: State) -> None:
            if state is not State.FINALIZE:
                return
            target = await _app_on(api, app["app_name"], str(rig["server_b"]["uuid"]))
            assert target is not None, "no target before FINALIZE"
            await api.update_resource(
                "applications",
                str(target["uuid"]),
                {"domains": f"http://moved-{app['host']}"},
            )

        mid = make_migration_id(plan.project, plan.environment)
        result = await run_migration(
            api, settings, plan, migration_id=mid, on_state=lose_the_host
        )
        assert result.outcome is Outcome.SUCCEEDED, f"migration did not succeed: {result}"
        assert any(
            f"no longer holds {app['host']}" in notice for notice in result.notices
        ), result.notices

        source = await api.get_resource("applications", app["app_uuid"])
        assert source.get("fqdn") == real, "the source let go of a host no target holds"
