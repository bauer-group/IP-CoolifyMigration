"""Plan warnings and wording for what Coolify 4.7 keeps where no API reaches.

Each rule is pinned from both sides, like tests/test_domain_fidelity.py: it fires
on the shape Coolify 4.7.0 writes, and it is SILENT on a stock resource and on a
release that cannot hold the state. The release tags behind every rule are cited
where the rule lives.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

from bg_coolify_migrate.domain import fidelity, schedules
from bg_coolify_migrate.domain.kinds import DatabaseEngine
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.errors import TransferError

V47 = (4, 7, 0)
V471 = (4, 7, 1)
V46 = (4, 6, 0)

# ── compose image overrides (4.7.0 security updates) ─────────────────────────

COMPOSE = """\
services:
  web:
    image: nginx:1.27.1
  db:
    image: postgres:16.1
  worker:
    build: .
"""


def _compose_app(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "uuid": "app1",
        "build_pack": "dockercompose",
        "compose_parsing_version": "3",
        "settings": {"is_raw_compose_deployment_enabled": False},
        "docker_compose_raw": COMPOSE,
        "docker_compose_image_overrides": {
            "web": {"from": "nginx:1.27.1", "to": "nginx:1.27.3"},
        },
    }
    return {**base, **kw}


class TestComposeImageOverrides:
    def test_an_active_override_warns_with_service_and_both_images(self) -> None:
        line = fidelity.compose_image_override_warning(_compose_app())
        assert line is not None
        assert "web: nginx:1.27.1 -> nginx:1.27.3" in line
        assert "Git" in line and "security update on the target" in line

    @pytest.mark.parametrize("version", [V47, V471, None])
    def test_the_plan_carries_it_whatever_the_version(self, version: Any) -> None:
        """The column exists from 4.7.0 only: its presence is the gate."""
        out = fidelity.application_warnings(_compose_app(), version=version)
        assert any("nginx:1.27.3" in line for line in out)

    def test_a_stale_override_is_silent(self) -> None:
        """The compose no longer names `from`: Coolify deploys the compose as written."""
        source = _compose_app(
            docker_compose_image_overrides={"web": {"from": "nginx:1.26.0", "to": "nginx:1.26.2"}}
        )
        assert fidelity.active_compose_image_overrides(source) == {}
        assert fidelity.compose_image_override_warning(source) is None

    def test_the_comparison_trims_like_php(self) -> None:
        source = _compose_app(
            docker_compose_image_overrides={"db": {"from": " postgres:16.1\t", "to": "postgres:16.4"}}
        )
        assert fidelity.active_compose_image_overrides(source) == {
            "db": (" postgres:16.1\t", "postgres:16.4")
        }

    def test_a_base64_compose_is_read(self) -> None:
        raw = base64.b64encode(COMPOSE.encode()).decode()
        assert fidelity.active_compose_image_overrides(_compose_app(docker_compose_raw=raw))

    @pytest.mark.parametrize(
        "change",
        [
            {"settings": {"is_raw_compose_deployment_enabled": True}},
            {"compose_parsing_version": "2"},
            {"compose_parsing_version": None},
            {"build_pack": "nixpacks"},
            {"docker_compose_image_overrides": None},
            {"docker_compose_image_overrides": {}},
            {"docker_compose_image_overrides": []},
            {"docker_compose_raw": None},
            {"docker_compose_raw": "services: [unclosed"},
        ],
    )
    def test_silent_where_upstream_applies_nothing(self, change: dict[str, Any]) -> None:
        """Raw compose deployments and parsing versions < 3 never use the parser that
        applies an override; a missing or unreadable compose matches no service."""
        assert fidelity.compose_image_override_warning(_compose_app(**change)) is None

    @pytest.mark.parametrize(
        "entry",
        [
            "nginx:1.27.3",
            {"from": "nginx:1.27.1"},
            {"from": "nginx:1.27.1", "to": 3},
            {"from": None, "to": "nginx:1.27.3"},
        ],
    )
    def test_a_malformed_entry_is_ignored(self, entry: object) -> None:
        source = _compose_app(docker_compose_image_overrides={"web": entry})
        assert fidelity.compose_image_override_warning(source) is None

    def test_a_service_without_an_image_or_unknown_to_the_compose_is_ignored(self) -> None:
        source = _compose_app(
            docker_compose_image_overrides={
                "worker": {"from": "nginx:1.27.1", "to": "nginx:1.27.3"},
                "gone": {"from": "nginx:1.27.1", "to": "nginx:1.27.3"},
                "db": {"from": "postgres:16.1", "to": "postgres:16.4"},
            }
        )
        assert fidelity.active_compose_image_overrides(source) == {
            "db": ("postgres:16.1", "postgres:16.4")
        }

    def test_a_stock_compose_app_is_silent(self) -> None:
        source = _compose_app()
        del source["docker_compose_image_overrides"]
        assert fidelity.application_warnings(source, version=V47) == []


# ── saved Git host keys (4.7.0) ──────────────────────────────────────────────

#: GitHub's published ed25519 key: its fingerprint is documented by GitHub, so
#: it checks the computation independently of the implementation.
ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
ED25519_FP = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
#: GitLab's published ed25519 key (fingerprint documented on docs.gitlab.com).
GITLAB_ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf"
GITLAB_FP = "SHA256:eUXGGm1YGsMAS7vkcx6JOJdOGHPem5gQp4taiCfCLB8"


class TestGitKnownHosts:
    def test_a_saved_self_hosted_key_warns_with_its_fingerprint_not_its_key(self) -> None:
        source = {"build_pack": "nixpacks", "git_ssh_known_hosts": f"git.example.com ssh-ed25519 {ED25519}\n"}
        line = fidelity.git_known_hosts_warning(source)
        assert line is not None
        assert f"git.example.com ssh-ed25519 {ED25519_FP}" in line
        assert ED25519 not in line
        assert "first contact" in line and "Source > SSH host keys" in line
        assert "OpenSSH" not in line  # only a compose load runs on the server

    def test_the_plan_carries_it_whatever_the_version(self) -> None:
        source = {"git_ssh_known_hosts": f"git.example.com ssh-ed25519 {ED25519}"}
        for version in (V47, None):
            assert any(ED25519_FP in line for line in fidelity.application_warnings(source, version=version))

    def test_a_compose_app_also_hears_about_old_openssh(self) -> None:
        source = {
            "build_pack": "dockercompose",
            "git_ssh_known_hosts": f"[git.example.com]:2222 ssh-ed25519 {ED25519}",
        }
        line = fidelity.git_known_hosts_warning(source)
        assert line is not None
        assert f"[git.example.com]:2222 ssh-ed25519 {ED25519_FP}" in line
        assert "OpenSSH older than 7.6" in line

    def test_lines_are_cleaned_and_deduplicated_like_upstream(self) -> None:
        raw = (
            f"  git.example.com   ssh-ed25519\t{ED25519}  \r\n"
            f"git.example.com ssh-ed25519 {ED25519}\n"
            "\n"
            "# a comment\n"
            f"@cert-authority *.example.com ssh-ed25519 {ED25519}\n"
            f"|1|aGFzaA==|aGFzaA== ssh-ed25519 {ED25519}\n"
            f"git.example.com ssh-dss {ED25519}\n"
            f"other.example.com ssh-ed25519 {GITLAB_ED25519} comment\n"
        )
        assert fidelity.trusted_git_host_keys(raw) == [f"git.example.com ssh-ed25519 {ED25519}"]

    @pytest.mark.parametrize(
        "line",
        [
            f"github.com ssh-ed25519 {ED25519}",
            f"[ssh.github.com]:443 ssh-ed25519 {ED25519}",
            f"gitlab.com ssh-ed25519 {GITLAB_ED25519}",
            f"bitbucket.org,[altssh.bitbucket.org]:443 ssh-ed25519 {ED25519}",
            f"GitHub.com ssh-ed25519 {ED25519}",
        ],
    )
    def test_a_pinned_provider_is_not_reported(self, line: str) -> None:
        """Every server checks those against Coolify's own keys, never on first use."""
        assert fidelity.git_known_hosts_warning({"git_ssh_known_hosts": line}) is None

    @pytest.mark.parametrize("raw", [None, "", "   \n", 42])
    def test_nothing_saved_is_silent(self, raw: object) -> None:
        assert fidelity.git_known_hosts_warning({"git_ssh_known_hosts": raw}) is None

    def test_the_fingerprint_matches_the_published_one(self) -> None:
        assert fidelity.known_host_fingerprint(f"h.example ssh-ed25519 {GITLAB_ED25519}") == (
            "h.example",
            "ssh-ed25519",
            GITLAB_FP,
        )

    def test_a_key_without_its_padding_still_fingerprints(self) -> None:
        """PHP's base64_decode is lenient about padding; the fingerprint must agree."""
        rsa_ish = base64.b64encode(b"\x00\x00\x00\x07ssh-rsa" + b"x" * 6).decode()
        assert rsa_ish.endswith("=")
        padded = fidelity.known_host_fingerprint(f"h ssh-rsa {rsa_ish}")
        assert fidelity.known_host_fingerprint(f"h ssh-rsa {rsa_ish.rstrip('=')}") == padded


# ── maintenance mode: what the source keeps after a keep/rename finalize ─────


class TestMaintenanceRoute:
    def test_applications_and_services_say_to_turn_it_off_on_the_source(self) -> None:
        app = fidelity.application_warnings({"is_maintenance_enabled": True}, version=V47)
        svc = fidelity.service_warnings({"is_maintenance_enabled": True}, version=V47)
        for out in (app, svc):
            [line] = [w for w in out if "maintenance" in w]
            assert "--finalize keep or rename" in line
            assert "turn maintenance mode off on the source" in line
            assert "neither the stop nor the release of its domains" in line

    def test_it_is_named_a_route_never_a_holder(self) -> None:
        """It is not a Coolify domain holder: the conflict check and the DNS
        release read fqdn and docker_compose_domains only."""
        [line] = fidelity.application_warnings({"is_maintenance_enabled": True}, version=V46)
        assert "holder" not in line and "router" not in line

    def test_off_is_silent(self) -> None:
        assert fidelity.application_warnings({"is_maintenance_enabled": False}, version=V47) == []


# ── scheduled tasks Coolify switched off itself (4.7.0) ──────────────────────

REASON = "Disabled automatically after 10 failed runs in a row since 2026-11-01 02:00:00 UTC."


def _task_warnings(tasks: list[dict[str, Any]]) -> list[str]:
    return schedules.scheduled_task_warnings(
        tasks, source_uuid="app1", source_server=None, target_server=None, version=V47
    )


class TestAutoDisabledTasks:
    def test_a_task_coolify_switched_off_is_named_never_explained(self) -> None:
        tasks = [
            {"name": "nightly", "enabled": False, "disabled_reason": REASON, "command": "pg_dump -W s3cr3t"},
            {"name": "hourly", "enabled": True},
        ]
        [line] = _task_warnings(tasks)
        assert "['nightly']" in line and "hourly" not in line
        assert "switched off by Coolify itself" in line
        assert REASON not in line and "failed runs" not in line
        assert "s3cr3t" not in line and "pg_dump" not in line  # invariant 5

    @pytest.mark.parametrize(
        "task",
        [
            {"name": "off", "enabled": False},
            {"name": "off", "enabled": False, "disabled_reason": None},
            {"name": "on", "enabled": True, "disabled_reason": REASON},
            {"name": "4.6-shaped", "enabled": False, "frequency": "@daily"},
        ],
    )
    def test_a_task_switched_off_by_hand_or_running_is_silent(self, task: dict[str, Any]) -> None:
        assert _task_warnings([task]) == []

    def test_no_version_gate(self) -> None:
        tasks = [{"name": "nightly", "enabled": False, "disabled_reason": REASON}]
        for version in (V46, None):
            out = schedules.scheduled_task_warnings(
                tasks, source_uuid="", source_server=None, target_server=None, version=version
            )
            assert len(out) == 1


# ── backup storages are not only S3 (4.7.0) ──────────────────────────────────


class TestBackupStorageWording:
    def test_the_delete_caveat_names_every_storage_type(self) -> None:
        line = schedules.volume_backup_delete_caveat(["postgres"])
        assert "every backup storage" in line
        assert all(kind in line for kind in ("S3", "SFTP", "FTPS", "WebDAV"))

    def test_a_dangling_storage_is_any_backup_storage(self) -> None:
        row = {
            "database_type": "App\\Models\\StandalonePostgresql",
            "frequency": "0 2 * * *",
            "save_s3": True,
            "s3_storage_uuids": [],
        }
        [line] = schedules.backup_warnings(
            [row], engine=DatabaseEngine.POSTGRESQL, source_timezone=None, target_timezone=None
        )
        assert "backup storage" in line and "no longer exists" in line

    def test_old_dumps_stay_in_every_storage_they_reached(self) -> None:
        row = {
            "database_type": "App\\Models\\StandalonePostgresql",
            "frequency": "0 2 * * *",
            "executions": [{"filename": "/data/coolify/backups/databases/t/db/x.dmp"}],
        }
        [line] = schedules.backup_warnings(
            [row], engine=DatabaseEngine.POSTGRESQL, source_timezone=None, target_timezone=None
        )
        assert "every backup storage they were uploaded to" in line
        assert "in S3 if uploaded" not in line


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


# ── the compose-load timeout names the 4.7 host-key cause ────────────────────


class _Api:
    async def get_resource(self, collection: str, uuid: str) -> dict[str, Any]:
        return {"uuid": uuid, "docker_compose_raw": None}


def _ctx() -> Any:
    return SimpleNamespace(
        api=_Api(),
        settings=SimpleNamespace(target_storage_timeout=0.0),
        plan=SimpleNamespace(target_server=SimpleNamespace(name="new")),
    )


class TestComposeLoadHint:
    async def test_create_names_old_openssh_and_its_remedies(self) -> None:
        resource = SimpleNamespace(snapshot=SimpleNamespace(collection="applications", name="shop"))
        with pytest.raises(TransferError, match="never loaded its compose") as exc_info:
            await steps._assert_compose_target_ready(_ctx(), resource, target_uuid="t")  # type: ignore[arg-type]
        hint = exc_info.value.hint or ""
        assert "OpenSSH is older than 7.6" in hint
        assert "/etc/ssh/ssh_known_hosts" in hint
        assert hint.index("OpenSSH") < hint.index("never stopped")

    async def test_discover_names_it_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_endpoints(api: object, *, collection: str, uuid: str) -> list[object]:
            return []

        monkeypatch.setattr("bg_coolify_migrate.api.resources.read_volume_endpoints", no_endpoints)
        with pytest.raises(TransferError, match="never loaded its compose from git") as exc_info:
            await steps._await_target_volumes(
                _ctx(),
                collection="applications",
                target_uuid="t",
                expected={"/data"},
                compose_from_git=True,
            )
        hint = exc_info.value.hint or ""
        assert "OpenSSH is older than 7.6" in hint
        assert hint.endswith("The source will be restarted and the target deleted.")
