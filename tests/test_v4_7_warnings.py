"""Plan warnings and wording for what Coolify 4.7 keeps where no API reaches.

Each rule is pinned from both sides, like tests/test_domain_fidelity.py: it fires
on the shape Coolify 4.7.0 writes, and it is SILENT on a stock resource and on a
release that cannot hold the state. The release tags behind every rule are cited
where the rule lives.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain import fidelity

V47 = (4, 7, 0)
V471 = (4, 7, 1)
V46 = (4, 6, 0)

# ── shared image names: a 4.7.0 restart skips the check ──────────────────────

IMAGE = "ghcr.io/acme/app"


def _pushing(pack: str | None) -> dict[str, Any]:
    return {"uuid": "app1", "build_pack": pack, "docker_registry_image_name": IMAGE}


TEAM = [{"uuid": "app2", "name": "other", "build_pack": "dockerfile", "docker_registry_image_name": IMAGE}]


class TestSharedImageRestarts:
    @pytest.mark.parametrize("pack", ["nixpacks", "static", "railpack"])
    @pytest.mark.parametrize("version", [V47, V471])
    def test_a_restart_only_pack_is_no_longer_refused(self, pack: str, version: Any) -> None:
        """Its rollback /restart skips the shared-image check from 4.7.0."""
        assert fidelity.shared_image_refusals(_pushing(pack), TEAM, version=version) == []

    @pytest.mark.parametrize("pack", ["dockerfile", None, "something-new"])
    @pytest.mark.parametrize("version", [V47, V471])
    def test_a_pack_whose_restart_is_a_full_deploy_still_refuses(
        self, pack: str | None, version: Any
    ) -> None:
        [line] = fidelity.shared_image_refusals(_pushing(pack), TEAM, version=version)
        assert "other" in line and "dockerfile" in line

    @pytest.mark.parametrize("pack", ["nixpacks", "static", "railpack", "dockerfile"])
    @pytest.mark.parametrize("version", [V46, None])
    def test_up_to_4_6_and_on_an_unknown_version_every_pack_refuses(
        self, pack: str, version: Any
    ) -> None:
        """An unknown version keeps the refusal: it costs a rename, the miss a
        source left down after a rollback."""
        assert len(fidelity.shared_image_refusals(_pushing(pack), TEAM, version=version)) == 1

    def test_the_create_still_drops_the_name_on_4_7(self) -> None:
        """The create 422 is unchanged (ApplicationsController v4.7.0:235-242)."""
        assert fidelity.unshareable_image_name(_pushing("nixpacks"), version=V47) == IMAGE
