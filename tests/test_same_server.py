"""A migration whose target server is its source server is refused.

On one server every custom host resolves to both sides, so the DNS gate cannot
tell a cutover from none (dns/gate.classify: an address both sides answer as
proves neither), labels kept verbatim would declare the source's routers twice
on one proxy, and bind mounts would be mirrored onto themselves. The refusal is
the plan's, derived on every read, so a plan.json from before it is judged too.

Also here: the picker, which never offers the source's own server.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
    same_server_refusal,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.ui import report

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
OLD = ServerRef(uuid="s1", name="old", ip="10.0.0.1")
NEW = ServerRef(uuid="s2", name="new", ip="10.0.0.2")


def _plan(target: ServerRef = OLD, **resource: Any) -> MigrationPlan:
    snapshot = ResourceSnapshot(
        uuid="db1", name="pg", collection="databases", kind=ResourceKind.DATABASE
    )
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=OLD,
        target_server=target,
        resources=(ResourcePlan(snapshot=snapshot, strategy=Strategy.RECREATE_ONLY, **resource),),
    )


class TestSameServerRefusal:
    def test_the_same_server_is_refused_and_says_why(self) -> None:
        line = same_server_refusal(OLD, OLD.model_copy(update={"name": "renamed"}))
        assert line is not None
        assert "'renamed' is the source server" in line
        assert "--accept-dns-host" in line and "Pick another target server" in line

    def test_another_server_is_not(self) -> None:
        # Same IP, another SSH port: Coolify keeps one record per endpoint, and
        # a different uuid is a different server (ServersController v4.7.2:637).
        assert same_server_refusal(OLD, NEW.model_copy(update={"ip": OLD.ip})) is None

    def test_an_empty_uuid_proves_nothing(self) -> None:
        blank = ServerRef(uuid="", name="?", ip="")
        assert same_server_refusal(blank, blank) is None


class TestPlan:
    def test_blocks_the_plan_not_its_resources(self) -> None:
        plan = _plan()
        assert plan.is_blocked
        assert plan.plan_refusals and plan.blocking_reasons == plan.plan_refusals
        assert plan.blocked_resources == ()
        assert not plan.resources[0].is_blocked

    def test_another_target_is_not_blocked(self) -> None:
        assert not _plan(NEW).is_blocked

    def test_a_plan_saved_before_the_rule_is_judged_by_it(self) -> None:
        saved = _plan().model_dump_json()
        assert MigrationPlan.model_validate_json(saved).is_blocked

    @pytest.mark.parametrize("policy", list(FinalizePolicy))
    def test_under_every_policy(self, policy: FinalizePolicy) -> None:
        assert _plan().model_copy(update={"finalize_policy": policy}).is_blocked

    def test_reported_once_for_the_whole_plan(self) -> None:
        text = report.plain_plan(_plan())
        assert text.count("is the source server") == 1
        assert "\nblocking: the target server 'old' is the source server" in text
        assert "blocked=False" in text  # the resource itself is fine

    def test_the_blocking_panel_shows_it(self) -> None:
        from rich.console import Console

        from bg_coolify_migrate.ui.console import THEME

        panel = report.blocking_panel(_plan())
        assert panel is not None
        console = Console(width=200, record=True, no_color=True, theme=THEME)
        console.print(panel)
        assert "is the source server" in console.export_text()

    def test_no_offline_offer_for_a_plan_stopping_cannot_clear(self) -> None:
        from bg_coolify_migrate.domain.compose import MountClass
        from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest

        anon = "b" * 64
        manifest = VolumeManifest(
            items=(
                VolumeItem(
                    mount_class=MountClass.ANONYMOUS,
                    decision=Decision.REFUSE,
                    reason="anonymous volume",
                    source_name=anon,
                    source_path=f"/var/lib/docker/volumes/{anon}/_data",
                    mount_path="/cache",
                ),
            )
        )
        snapshot = ResourceSnapshot(
            uuid="a1", name="web", collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
        )
        resource = ResourcePlan(
            snapshot=snapshot, strategy=Strategy.COPY_DATA, manifest=manifest
        )
        same = _plan().model_copy(update={"resources": (resource,)})
        assert resource.offline_only and same.offline_only_resources == ()
        other = same.model_copy(update={"target_server": NEW})
        assert other.offline_only_resources == (resource,)


# ── the picker never offers the source's own server ──────────────────────────


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


class TestSourceServerOfThePick:
    ROWS: ClassVar[list[tuple[str, dict[str, Any]]]] = [
        ("applications", {"uuid": "a1", "name": "web"}),
        ("databases", {"uuid": "d1"}),
    ]

    async def test_the_whole_environment_reads_its_first_resource(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.cli import _source_server_uuid

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1", "server_uuid": "s1"})
        )
        respx_mock.get(f"{BASE}/servers/s1").mock(
            return_value=httpx.Response(200, json={"uuid": "s1"})
        )
        assert await _source_server_uuid(api, self.ROWS, None) == "s1"

    async def test_the_picked_resource_is_read_by_name_or_uuid(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.cli import _source_server_uuid

        respx_mock.get(f"{BASE}/databases/d1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "d1", "destination": {"server": {"uuid": "s3"}}}
            )
        )
        respx_mock.get(f"{BASE}/servers/s3").mock(
            return_value=httpx.Response(200, json={"uuid": "s3"})
        )
        assert await _source_server_uuid(api, self.ROWS, "d1") == "s3"

    async def test_an_unreadable_scope_excludes_nothing(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.cli import _source_server_uuid

        respx_mock.get(f"{BASE}/applications/a1").mock(return_value=httpx.Response(500))
        assert await _source_server_uuid(api, self.ROWS, "web") is None
        assert await _source_server_uuid(api, self.ROWS, "nope") is None
        assert await _source_server_uuid(api, [], None) is None

    async def test_a_scope_without_a_server_excludes_nothing(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.cli import _source_server_uuid

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        assert await _source_server_uuid(api, self.ROWS, None) is None
