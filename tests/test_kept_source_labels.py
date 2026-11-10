"""A kept or renamed source whose hand-kept labels outlive FINALIZE's release.

FINALIZE removes a kept source's custom hosts from its fqdn. With readonly
labels on, that PATCH regenerates custom_labels from the new fqdn; with them off
it leaves them as stored (ApplicationsController v4.1.2:2668 .. v4.7.2:4107),
and every non-compose deploy writes the stored labels into the container
(ApplicationDeploymentJob v4.7.2:4740-4763). So the kept source's next deploy
routes the moved hosts again, next to the target.

The plan refuses keep and rename for such a source — derived from its policy
on every read, because ``run`` swaps the policy onto a plan that is already
built — and FINALIZE re-checks at release time with the same predicate, so a
source switched to hand-kept labels since PLAN is never reported as released.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import PreflightError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.ui import report
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
WILD = "app.0046-20.cloud.bauer-group.com"
SHOP = "https://shop.example.com"
ROUTER = "traefik.http.routers.http-0-a1.rule=Host(`shop.example.com`) && PathPrefix(`/`)"


def _app(*, readonly: bool | None, labels: str = ROUTER, **kw: Any) -> dict[str, Any]:
    """An application GET; ``readonly=None`` is a 4.1.x GET (no settings)."""
    out: dict[str, Any] = {
        "uuid": "a1",
        "build_pack": "dockerimage",
        "fqdn": f"{SHOP},https://x.{WILD}",
        "custom_labels": labels,
    }
    if readonly is not None:
        out["settings"] = {"is_container_label_readonly_enabled": readonly}
    return {**out, **kw}


# ── the pure predicate ───────────────────────────────────────────────────────


class TestReadonlyLabels:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ({"settings": {"is_container_label_readonly_enabled": True}}, True),
            ({"settings": {"is_container_label_readonly_enabled": False}}, False),
            # 4.1.x returns no settings at all; a non-boolean proves nothing.
            ({}, None),
            ({"settings": None}, None),
            ({"settings": {}}, None),
            ({"settings": {"is_container_label_readonly_enabled": "0"}}, None),
        ],
    )
    def test_read_by_key_never_guessed(self, source: dict[str, Any], expected: bool | None) -> None:
        assert fidelity.readonly_labels(source) is expected


class TestLabelRoutedReleaseHosts:
    def test_a_traefik_rule_naming_a_released_host(self) -> None:
        # The server-bound host is not released (it collides with nothing), so
        # it is not what this asks about.
        source = _app(readonly=False, labels=f"{ROUTER}\ntraefik.x.rule=Host(`x.{WILD}`)")
        assert fidelity.label_routed_release_hosts(source, source_wildcard=WILD) == [
            "shop.example.com"
        ]

    def test_base64_labels_and_host_sni_are_read(self) -> None:
        labels = "traefik.tcp.routers.t.rule=HostSNI(`shop.example.com`)"
        source = _app(readonly=False, labels=base64.b64encode(labels.encode()).decode())
        assert fidelity.label_routed_release_hosts(source, source_wildcard=WILD) == [
            "shop.example.com"
        ]

    def test_a_caddy_site_label(self) -> None:
        source = _app(readonly=False, labels="caddy_0=https://shop.example.com")
        assert fidelity.label_routed_release_hosts(source, source_wildcard=WILD) == [
            "shop.example.com"
        ]

    def test_a_host_regexp_cannot_be_ruled_out(self) -> None:
        source = _app(readonly=False, labels="traefik.x.rule=HostRegexp(`^.+\\.example\\.com$`)")
        [entry] = fidelity.label_routed_release_hosts(source, source_wildcard=WILD)
        assert entry.startswith("HostRegexp(")

    def test_nothing_to_release_nothing_to_route(self) -> None:
        # Only server-bound and generated hosts: FINALIZE releases none of them.
        source = _app(
            readonly=False,
            fqdn=f"https://x.{WILD},http://a1.10.0.0.1.sslip.io",
            labels="traefik.x.rule=HostRegexp(`{s:.+}`)\ncaddy_0=http://a1.10.0.0.1.sslip.io",
        )
        assert fidelity.label_routed_release_hosts(source, source_wildcard=WILD) == []

    def test_labels_that_route_another_host_only(self) -> None:
        source = _app(readonly=False, labels="traefik.x.rule=Host(`other.example.com`)")
        assert fidelity.label_routed_release_hosts(source, source_wildcard=WILD) == []

    @pytest.mark.parametrize("labels", ["", None, "   "])
    def test_no_labels(self, labels: str | None) -> None:
        source = _app(readonly=False, labels=labels)  # type: ignore[arg-type]
        assert fidelity.label_routed_release_hosts(source, source_wildcard=WILD) == []

    def test_a_compose_application_regenerates_its_labels_from_its_domains(self) -> None:
        # deploy_docker_compose_buildpack never reads custom_labels (ADJ v4.7.2:876).
        source = _app(readonly=False, build_pack="dockercompose")
        assert fidelity.label_routed_release_hosts(source, source_wildcard=WILD) == []


class TestHostsRoutedAfterRelease:
    def test_only_when_readonly_labels_is_known_off(self) -> None:
        assert fidelity.hosts_routed_after_release(_app(readonly=False), source_wildcard=WILD) == [
            "shop.example.com"
        ]
        # On: the release's PATCH regenerates the labels without the host.
        assert fidelity.hosts_routed_after_release(_app(readonly=True), source_wildcard=WILD) == []
        # Unknown (4.1.x): never claimed either way here; the plan warns instead.
        assert fidelity.hosts_routed_after_release(_app(readonly=None), source_wildcard=WILD) == []


class TestKeptSourceLines:
    def test_the_refusal_names_the_hosts_and_both_ways_out(self) -> None:
        [line] = fidelity.kept_source_label_refusals(_app(readonly=False), source_wildcard=WILD)
        assert "shop.example.com" in line
        assert "--finalize keep or rename" in line
        assert "Readonly labels on" in line and "--finalize delete" in line
        assert fidelity.kept_source_label_warnings(_app(readonly=False), source_wildcard=WILD) == []

    def test_managed_labels_need_neither(self) -> None:
        source = _app(readonly=True)
        assert fidelity.kept_source_label_refusals(source, source_wildcard=WILD) == []
        assert fidelity.kept_source_label_warnings(source, source_wildcard=WILD) == []

    def test_an_unreadable_setting_warns_instead(self) -> None:
        source = _app(readonly=None)
        assert fidelity.kept_source_label_refusals(source, source_wildcard=WILD) == []
        [line] = fidelity.kept_source_label_warnings(source, source_wildcard=WILD)
        assert "shop.example.com" in line and "Coolify 4.1" in line

    def test_an_unreadable_setting_without_a_routed_host_is_silent(self) -> None:
        source = _app(readonly=None, labels="traefik.x.rule=Host(`other.example.com`)")
        assert fidelity.kept_source_label_warnings(source, source_wildcard=WILD) == []


# ── the plan: derived from its policy on read ───────────────────────────────


def _snapshot(uuid: str = "a1", name: str = "shop") -> ResourceSnapshot:
    return ResourceSnapshot(
        uuid=uuid, name=name, collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
    )


def _plan(policy: FinalizePolicy = FinalizePolicy.RENAME, **kw: Any) -> MigrationPlan:
    resource = ResourcePlan(
        snapshot=_snapshot(),
        strategy=Strategy.RECREATE_ONLY,
        kept_source_refusals=("labels route shop.example.com",),
        kept_source_warnings=("labels may route shop.example.com",),
        **kw,
    )
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=(resource,),
        finalize_policy=policy,
    )


class TestPlanUnderItsPolicy:
    @pytest.mark.parametrize("policy", [FinalizePolicy.KEEP, FinalizePolicy.RENAME])
    def test_keep_and_rename_are_refused(self, policy: FinalizePolicy) -> None:
        plan = _plan(policy)
        assert plan.is_blocked
        assert plan.blocking_reasons == ("shop: labels route shop.example.com",)
        assert plan.blocked_resources == plan.resources
        assert "shop: labels may route shop.example.com" in plan.warnings
        # The resource on its own is not blocked: only a plan knows its policy.
        assert plan.resources[0].is_blocked is False

    def test_delete_removes_the_source_and_the_reason(self) -> None:
        plan = _plan(FinalizePolicy.DELETE)
        assert not plan.is_blocked
        assert plan.blocked_resources == ()
        assert not any("may route" in w for w in plan.warnings)

    def test_follows_a_policy_swapped_onto_a_built_plan(self) -> None:
        # cli.py builds the plan, then swaps --finalize on with model_copy.
        built = _plan(FinalizePolicy.RENAME)
        swapped = built.model_copy(update={"finalize_policy": FinalizePolicy.DELETE})
        assert built.is_blocked and not swapped.is_blocked
        assert swapped.model_copy(update={"finalize_policy": FinalizePolicy.KEEP}).is_blocked

    def test_survives_plan_json(self) -> None:
        plan = _plan(FinalizePolicy.KEEP)
        assert MigrationPlan.model_validate_json(plan.model_dump_json()).is_blocked

    def test_a_plan_saved_before_the_field_loads_unblocked(self) -> None:
        saved = json.loads(_plan().model_dump_json())
        for resource in saved["resources"]:
            del resource["kept_source_refusals"]
            del resource["kept_source_warnings"]
        assert not MigrationPlan.model_validate(saved).is_blocked

    def test_the_report_shows_it_under_its_policy(self) -> None:
        text = report.plain_plan(_plan(FinalizePolicy.RENAME))
        assert "blocked: True" in text
        assert "blocked=True" in text
        assert "  blocking: labels route shop.example.com" in text
        assert "blocking:" not in report.plain_plan(_plan(FinalizePolicy.DELETE))

    def test_the_blocking_panel_names_the_resource(self) -> None:
        from rich.console import Console

        from bg_coolify_migrate.ui.console import THEME

        panel = report.blocking_panel(_plan(FinalizePolicy.KEEP))
        assert panel is not None
        console = Console(width=200, record=True, no_color=True, theme=THEME)
        console.print(panel)
        console.print(report.resources_table(_plan(FinalizePolicy.KEEP)))
        out = console.export_text()
        assert "labels route shop.example.com" in out and "BLOCKED" in out
        assert report.blocking_panel(_plan(FinalizePolicy.DELETE)) is None

    def test_an_offline_only_stack_is_not_stopped_for_a_plan_it_cannot_clear(self) -> None:
        # Stopping it clears the anonymous volumes, not the policy's refusal:
        # downtime and no migration.
        from bg_coolify_migrate.domain.compose import MountClass
        from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest

        manifest = VolumeManifest(
            items=(
                VolumeItem(
                    mount_class=MountClass.ANONYMOUS,
                    decision=Decision.REFUSE,
                    reason="anonymous volume of a running container",
                    source_name="a" * 64,
                    source_path=f"/var/lib/docker/volumes/{'a' * 64}/_data",
                    mount_path="/cache",
                ),
            )
        )
        refused = _plan(FinalizePolicy.RENAME, manifest=manifest)
        assert refused.resources[0].offline_only
        assert refused.offline_only_resources == ()
        deleting = _plan(FinalizePolicy.DELETE, manifest=manifest)
        assert deleting.offline_only_resources == deleting.resources


class TestPlannerWiring:
    async def test_a_hand_labelled_source_is_refused_under_its_policy_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_engine_planner import _plan_one_app

        full = _app(readonly=False)
        full["fqdn"] = SHOP
        plan = await _plan_one_app(monkeypatch, full, version="4.5.0")
        [resource] = plan.resources
        assert resource.refusals == ()
        [line] = resource.kept_source_refusals
        assert "shop.example.com" in line
        assert plan.is_blocked  # built with the default policy, rename
        assert not plan.model_copy(update={"finalize_policy": FinalizePolicy.DELETE}).is_blocked

    async def test_a_4_1_source_is_warned_about(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_engine_planner import _plan_one_app

        full = _app(readonly=None)
        full["fqdn"] = SHOP
        plan = await _plan_one_app(monkeypatch, full, version="4.1.2")
        assert not plan.is_blocked
        assert any("Coolify 4.1" in w for w in plan.warnings)


# ── PREFLIGHT: the second line, for a plan that bypassed the CLI ─────────────


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    source = (
        FakeHost()
        .on(r"command -v rsync", exit_status=0)
        .on(r"command -v docker", exit_status=0)
        .on(r"docker ps", stdout="")
        .on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        .on(r"authorized_keys\.bak", exit_status=0)
        .on(r"rm -rf /root/\.coolify-migrate/", exit_status=0)
    )
    target = (
        FakeHost()
        .on(r"command -v rsync", exit_status=0)
        .on(r"command -v docker", exit_status=0)
        .on(r"df -Pk", stdout="99999999")
        .on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        .on(r"authorized_keys\.bak", exit_status=0)
    )
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=source,  # type: ignore[arg-type]
        target_host=target,  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


class TestPreflight:
    @pytest.fixture(autouse=True)
    def _instance(self, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/version").mock(return_value=httpx.Response(200, text="4.5.0"))
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )

    async def test_refuses_what_the_policy_refuses(self, ctx: MigrationContext) -> None:
        ctx.plan = _plan(FinalizePolicy.KEEP)
        with pytest.raises(PreflightError, match=r"shop: labels route shop\.example\.com"):
            await steps.step_preflight(ctx)

    async def test_passes_the_same_plan_under_delete(self, ctx: MigrationContext) -> None:
        ctx.plan = _plan(FinalizePolicy.DELETE)
        await steps.step_preflight(ctx)  # must not raise


# ── plan --finalize: plan judges the policy run will use ─────────────────────


class TestPlanFinalizeOption:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
        from bg_coolify_migrate.observability.logging_setup import reset_logging
        from bg_coolify_migrate.settings.base import reset_settings_cache

        reset_logging()
        reset_settings_cache()
        monkeypatch.chdir(tmp_path)
        for var in ("COOLIFY_URL", "COOLIFY_TOKEN", "STATE_DIR"):
            monkeypatch.delenv(var, raising=False)
        yield
        # The CLI configures structlog to cache each logger on first use; left
        # configured, a later test's capture_logs() misses a cached logger.
        reset_logging()

    def _invoke(self, monkeypatch: pytest.MonkeyPatch, *argv: str) -> Any:
        from typer.testing import CliRunner

        import bg_coolify_migrate.cli as cli_mod

        monkeypatch.setenv("COOLIFY_URL", HOST)
        monkeypatch.setenv("COOLIFY_TOKEN", "tok")
        respx.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"uuid": "k1", "private_key": "-----BEGIN"}])
        )

        async def _selection(*_a: object, **_k: object) -> object:
            return cli_mod.Selection("shop", "production", "shop"), "new"

        async def _jobs(*_a: object, **_k: object) -> object:
            return "shop", [("shop", "production", "shop")]

        async def _build(*_a: object, **_k: object) -> MigrationPlan:
            return _plan()  # built with the planner's default policy, rename

        monkeypatch.setattr(cli_mod, "_resolve_selection", _selection)
        monkeypatch.setattr(cli_mod, "_resolve_jobs", _jobs)
        monkeypatch.setattr(cli_mod, "_build", _build)
        monkeypatch.setattr(cli_mod, "is_interactive", lambda: False)
        return CliRunner().invoke(
            cli_mod.app, ["plan", "shop/production/shop", "--to", "new", *argv]
        )

    @respx.mock
    def test_the_default_rename_is_judged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._invoke(monkeypatch)
        assert result.exit_code == 2
        assert "labels route shop.example.com" in result.stdout

    @respx.mock
    def test_delete_is_judged_as_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._invoke(monkeypatch, "--finalize", "delete")
        assert result.exit_code == 0, result.stdout
        assert "finalize: delete" in result.stdout

    @respx.mock
    def test_an_unknown_policy_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._invoke(monkeypatch, "--finalize", "nope")
        assert result.exit_code == 2
        assert "--finalize must be keep|rename|delete" in result.stderr
