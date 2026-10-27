"""Service domains against a real Coolify: carried, released, never parked.

A service keeps its domains on its sub-applications. The stack here has three,
one of each kind the tool must tell apart:

* ``web``   — a custom domain WITH a port. From 4.3.15 the port lives in a hidden
  per-domain override; only the GET's appended ``url`` still shows it, so this
  proves the override reached the target.
* ``api``   — a server-bound URL under the source's (plan-supplied) wildcard,
  which must be rewritten onto the target's.
* ``admin`` — Coolify's own sslip.io URL, which names the SOURCE's uuid and IP
  and must not be carried at all.

``.invalid`` never resolves, so the DNS gate reads every custom host as
UNRESOLVED and passes without --accept-dns. The template-renamed-a-container
refusal cannot be staged here (the catalog is fixed per instance); it is
unit-tested only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.dns import extract as dns_extract
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import run_migration
from bg_coolify_migrate.errors import VerificationError
from bg_coolify_migrate.settings.base import Settings

from .conftest import ssh_to

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

COMPOSE = """\
services:
  web:
    image: nginx:1.27-alpine
    environment:
      - SERVICE_URL_WEB_80
    volumes:
      - webdata:/usr/share/nginx/html
  api:
    image: nginx:1.27-alpine
    environment:
      - SERVICE_URL_API_80
  admin:
    image: nginx:1.27-alpine
    environment:
      - SERVICE_URL_ADMIN_80
volumes:
  webdata:
"""

SQUAT_COMPOSE = """\
services:
  squat:
    image: nginx:1.27-alpine
    environment:
      - SERVICE_URL_SQUAT_80
"""


def _apps(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(a["name"]): a
        for a in service.get("applications") or []
        if isinstance(a, dict) and a.get("name")
    }


def _url(app: dict[str, Any]) -> str:
    return str(app.get("url") or app.get("fqdn") or "")


async def _container(host: Any, service_uuid: str, name: str, *, timeout: float = 180) -> str:
    """The running ``{name}-{service_uuid}`` container's name, once it exists."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await host.run(
            f"docker ps --format '{{{{.Names}}}}' | grep {service_uuid} | grep '^{name}-'"
        )
        names = result.stdout.strip().splitlines()
        if names:
            return names[0]
        await asyncio.sleep(4)
    raise AssertionError(f"no {name} container for service {service_uuid} within {timeout}s")


async def _routed_hosts(host: Any, container: str) -> set[str]:
    result = await host.run(f"docker inspect -f '{{{{json .Config.Labels}}}}' {container}")
    result.check()
    labels = json.loads(result.stdout.strip() or "{}")
    return {h.host for h in dns_extract.from_labels(labels)}


async def _service_on(api: CoolifyClient, name: str, server_uuid: str) -> dict[str, Any] | None:
    for svc in await api.get("/services") or []:
        if not isinstance(svc, dict) or svc.get("name") != name:
            continue
        full = await api.get_resource("services", str(svc["uuid"]))
        if server_uuid_of(full) == server_uuid:
            return full
    return None


@contextlib.asynccontextmanager
async def _stack(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], *, web_port: str
) -> AsyncIterator[dict[str, Any]]:
    """Deploy the three-container stack on server-a with its domains set."""
    sfx = uuidlib.uuid4().hex[:8]
    project = await api.post("/projects", {"name": f"sd-{sfx}", "description": "bgcm domains"})
    project_uuid = str(project["uuid"])
    name = f"site-{sfx}"
    web_url = f"https://shop-{sfx}.e2e.invalid{web_port}"
    api_url = f"http://api-{sfx}.a-{sfx}.e2e.invalid"
    try:
        created = await api.post(
            "/services",
            {
                "name": name,
                "project_uuid": project_uuid,
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "docker_compose_raw": base64.b64encode(COMPOSE.encode()).decode(),
                "instant_deploy": False,
            },
        )
        service_uuid = str(created["uuid"])
        await api.patch(
            f"/services/{service_uuid}",
            {"urls": [{"name": "web", "url": web_url}, {"name": "api", "url": api_url}]},
        )
        await api.post(f"/services/{service_uuid}/start")
        before = _apps(await api.get_resource("services", service_uuid))
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            web = await _container(source, service_uuid, "web")
            await source.run(f"docker exec {web} sh -c 'echo {sfx} > /usr/share/nginx/html/fp.txt'")
        yield {
            "sfx": sfx,
            "project": f"sd-{sfx}",
            "project_uuid": project_uuid,
            "name": name,
            "uuid": service_uuid,
            "web_url": web_url,
            "api_url": api_url,
            "admin_url": _url(before["admin"]),
            "source_wildcard": f"a-{sfx}.e2e.invalid",
            "target_wildcard": f"b-{sfx}.e2e.invalid",
        }
    finally:
        with contextlib.suppress(Exception):
            for svc in await api.get("/services") or []:
                if isinstance(svc, dict) and sfx in str(svc.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource("services", str(svc["uuid"]), delete_volumes=True)
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


async def _plan(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], stack: dict[str, Any]
) -> Any:
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        return await build_plan(
            api,
            source_host,
            project=stack["project"],
            environment="production",
            target_server="e2e-server-b",
            # Only the stack: (C) parks a squatter in the same project.
            only_resource=stack["name"],
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
            source_wildcard=stack["source_wildcard"],
            target_wildcard=stack["target_wildcard"],
        )


async def test_service_domains_are_carried_and_released(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> None:
    """(A) RENAME: the target routes the source's domains; the source lets go."""
    async with _stack(api, settings, rig, web_port=":8443") as stack:
        assert stack["admin_url"], "the rig did not generate admin's sslip URL"
        plan = await _plan(api, settings, rig, stack)
        result = await run_migration(api, settings, plan)
        assert result.outcome is Outcome.SUCCEEDED, f"migration did not succeed: {result}"

        target = await _service_on(api, stack["name"], str(rig["server_b"]["uuid"]))
        assert target is not None, "no service with the original name landed on server-b"
        apps = _apps(target)
        # The port survived: on 4.5 it lives in the hidden override, so only a
        # carried `url` (not `fqdn`) could have put it there.
        assert _url(apps["web"]) == stack["web_url"]
        assert _url(apps["api"]) == f"http://api-{stack['sfx']}.{stack['target_wildcard']}"
        admin = _url(apps["admin"])
        assert "sslip.io" in admin and str(target["uuid"]) in admin, admin
        assert admin != stack["admin_url"], "the source's sslip host was carried"

        # Derived env rows are the target's own, never the source's hosts.
        for env in await api.get_envs("services", str(target["uuid"])):
            if dns_extract.is_service_url_key(str(env.get("key"))):
                value = str(env.get("value") or "")
                assert stack["source_wildcard"] not in value, env["key"]
                assert stack["uuid"] not in value, env["key"]

        # The renamed source released its custom host; nothing was ever parked.
        source = _apps(await api.get_resource("services", stack["uuid"]))
        assert f"shop-{stack['sfx']}" not in _url(source["web"])
        assert "old-" not in _url(source["web"])

        async with ssh_to(api, str(rig["server_b"]["uuid"]), settings) as target_host:
            web = await _container(target_host, str(target["uuid"]), "web")
            fp = await target_host.run(f"docker exec {web} cat /usr/share/nginx/html/fp.txt")
            assert fp.stdout.strip() == stack["sfx"], "web's volume did not arrive"
            routed = await _routed_hosts(target_host, web)
        if not routed:
            pytest.skip("rig server-b generates no proxy labels (proxy type none)")
        assert f"shop-{stack['sfx']}.e2e.invalid" in routed


async def test_rollback_leaves_the_source_domains_untouched(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(B) A fault after COPY: the source comes back on exactly its own hosts."""

    async def _boom(_ctx: object) -> dict[str, Any]:
        raise VerificationError("injected fault: pretend verification found a mismatch")

    monkeypatch.setattr(steps, "step_verify", _boom)
    async with _stack(api, settings, rig, web_port=":8443") as stack:
        plan = await _plan(api, settings, rig, stack)
        result = await run_migration(api, settings, plan)
        assert result.outcome is Outcome.ROLLED_BACK, f"expected rollback, got {result}"

        source = _apps(await api.get_resource("services", stack["uuid"]))
        assert _url(source["web"]) == stack["web_url"]
        assert _url(source["api"]) == stack["api_url"]

        for _ in range(24):
            if await _service_on(api, stack["name"], str(rig["server_b"]["uuid"])) is None:
                break
            await asyncio.sleep(5)
        assert await _service_on(api, stack["name"], str(rig["server_b"]["uuid"])) is None

        # Nothing parked the source, so the restart re-parses it onto its real
        # hosts — no restore-before-restart ordering is needed for services.
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
            web = await _container(source_host, stack["uuid"], "web")
            routed = await _routed_hosts(source_host, web)
        if not routed:
            pytest.skip("rig server-a generates no proxy labels (proxy type none)")
        assert f"shop-{stack['sfx']}.e2e.invalid" in routed
        assert not [h for h in routed if h.startswith("old-")]


async def test_a_domain_held_elsewhere_refuses_before_the_stop(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any]
) -> None:
    """(C) A third resource holds the host: CREATE refuses, the source never stops.

    PORTLESS on purpose. From 4.3.15 the conflict check is port-blind against a
    portless holder — `shop:8443` never matches a stored `shop` — so a
    port-bearing URL would not 409 at all.
    """
    async with _stack(api, settings, rig, web_port="") as stack:
        squat = await api.post(
            "/services",
            {
                "name": f"squat-{stack['sfx']}",
                "project_uuid": stack["project_uuid"],
                "environment_name": "production",
                "server_uuid": rig["server_a"]["uuid"],
                "docker_compose_raw": base64.b64encode(SQUAT_COMPOSE.encode()).decode(),
                "instant_deploy": False,
            },
        )
        await api.patch(
            f"/services/{squat['uuid']}",
            {
                "urls": [{"name": "squat", "url": stack["web_url"]}],
                "force_domain_override": True,
            },
        )
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
            web = await _container(source_host, stack["uuid"], "web")
            started = (
                await source_host.run(f"docker inspect -f '{{{{.State.StartedAt}}}}' {web}")
            ).stdout.strip()

        plan = await _plan(api, settings, rig, stack)
        result = await run_migration(api, settings, plan)
        assert result.outcome is not Outcome.SUCCEEDED
        assert "already in use" in str(result.error)

        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
            again = (
                await source_host.run(f"docker inspect -f '{{{{.State.StartedAt}}}}' {web}")
            ).stdout.strip()
        assert again == started, "the source was stopped for a refusal CREATE could make"
        source = _apps(await api.get_resource("services", stack["uuid"]))
        assert _url(source["web"]) == stack["web_url"]
        for _ in range(24):
            if await _service_on(api, stack["name"], str(rig["server_b"]["uuid"])) is None:
                break
            await asyncio.sleep(5)
        assert await _service_on(api, stack["name"], str(rig["server_b"]["uuid"])) is None
