"""What the v4.5.2 -> v4.6.0 audit read at the release tag, asked of a real Coolify.

The unit tests mock these responses, and a mock never proves a route or a rule
exists. Both tests skip on a Coolify that predates them.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.versions import parse_version
from bg_coolify_migrate.errors import CoolifyApiError

pytestmark = pytest.mark.e2e


async def _application_body(api: CoolifyClient, rig: dict[str, Any], suffix: str) -> dict[str, Any]:
    project = await api.post("/projects", {"name": f"v46-{suffix}", "description": "bgcm e2e"})
    return {
        "project_uuid": project["uuid"],
        "environment_name": "production",
        "server_uuid": rig["server_a"]["uuid"],
        "git_repository": "https://git.example.com/bgcm/app.git",
        "git_branch": "main",
        "build_pack": "nixpacks",
        "ports_exposes": "3000",
        "instant_deploy": False,
        "autogenerate_domain": False,
    }


async def test_an_image_name_is_unique_and_the_dns_route_answers(
    api: CoolifyClient, rig: dict[str, Any]
) -> None:
    version = parse_version(await api.version())
    if not supports("registry_image_name_unique", version, if_unknown=False):
        pytest.skip("this Coolify predates the rule")

    suffix = uuid4().hex[:8]
    without_name = await _application_body(api, rig, suffix)
    named = {**without_name, "docker_registry_image_name": f"registry.example.com/bgcm/{suffix}"}
    source = await api.post("/applications/public", {**named, "name": "source"})
    try:
        with pytest.raises(CoolifyApiError) as refused:
            await api.post("/applications/public", {**named, "name": "same-name"})
        assert refused.value.status_code == 422
        assert "docker_registry_image_name" in str(refused.value.body)

        # What CREATE sends instead.
        target = await api.post("/applications/public", {**without_name, "name": "target"})
        await api.delete_resource("applications", str(target["uuid"]), delete_volumes=True)

        status = await api.get(f"/applications/{source['uuid']}/dns")
        assert isinstance(status["hostnames"], list)
    finally:
        await api.delete_resource("applications", str(source["uuid"]), delete_volumes=True)


async def test_a_git_password_is_hidden_from_the_resource_get(
    api: CoolifyClient, rig: dict[str, Any]
) -> None:
    version = parse_version(await api.version())
    if not supports("git_https_credentials", version, if_unknown=False):
        pytest.skip("this Coolify predates the credentials")

    body = await _application_body(api, rig, uuid4().hex[:8])
    created = await api.post(
        "/applications/public",
        {
            **body,
            "name": "with-credentials",
            "git_https_username": "deploy",
            "git_https_password": "e2e-not-a-real-token",
        },
    )
    try:
        full = await api.get_resource("applications", str(created["uuid"]))
        assert full["git_https_username"] == "deploy"
        assert "git_https_password" not in full
    finally:
        await api.delete_resource("applications", str(created["uuid"]), delete_volumes=True)
