"""Tests for the F1 step implementations and their compensations.

These are the migration. Every test here corresponds to a guarantee documented in
docs/safety.md.
"""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.drift import DriftAxis, DriftFinding, RebuildDriftReport, Severity
from bg_coolify_migrate.domain.kinds import DatabaseEngine, GitAuth, ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.naming import VolumeEndpoint, VolumePair
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.context import EphemeralKey, MigrationContext
from bg_coolify_migrate.errors import (
    CoolifyApiError,
    DnsGateBlocked,
    PreflightError,
    RebuildDriftBlocked,
    TransferError,
    VerificationError,
)
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer import ssh
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"


def _snapshot(**kw: object) -> ResourceSnapshot:
    base = {
        "uuid": "db1",
        "name": "postgres",
        "collection": "databases",
        "kind": ResourceKind.DATABASE,
        "engine": DatabaseEngine.POSTGRESQL,
        "image": "postgres:16",
    }
    return ResourceSnapshot(**{**base, **kw})  # type: ignore[arg-type]


def _manifest(*, bytes_: int = 1024) -> VolumeManifest:
    return VolumeManifest(
        items=(
            VolumeItem(
                mount_class=MountClass.NAMED,
                decision=Decision.MIGRATE,
                reason="named volume",
                source_name="postgres-data-db1",
                source_path="/var/lib/docker/volumes/postgres-data-db1/_data",
                mount_path="/var/lib/postgresql/data",
                bytes=bytes_,
            ),
        )
    )


def _plan(**kw: object) -> MigrationPlan:
    base = {
        "project": "shop",
        "environment": "production",
        "source_server": ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        "target_server": ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        "resources": (
            ResourcePlan(snapshot=_snapshot(), strategy=Strategy.COPY_DATA, manifest=_manifest()),
        ),
    }
    return MigrationPlan(**{**base, **kw})  # type: ignore[arg-type]


def _compose_plan(
    *, git_auth: GitAuth = GitAuth.PUBLIC, manifest: VolumeManifest | None = None
) -> MigrationPlan:
    """A public git-compose app — the shape behind the 2026-07-22 failures."""
    return _plan(
        resources=(
            ResourcePlan(
                snapshot=_snapshot(
                    uuid="app1",
                    name="wp",
                    collection="applications",
                    kind=ResourceKind.APP_GIT_COMPOSE,
                    engine=None,
                    image=None,
                    git_repository="acme/wordpress-stack",
                    git_branch="main",
                    git_auth=git_auth,
                ),
                strategy=Strategy.COPY_DATA,
                manifest=manifest if manifest is not None else _manifest(),
            ),
        )
    )


def _compose_manifest() -> VolumeManifest:
    return VolumeManifest(
        items=(
            VolumeItem(
                mount_class=MountClass.NAMED,
                decision=Decision.MIGRATE,
                reason="named volume",
                source_name="app1_app",
                source_path="/var/lib/docker/volumes/app1_app/_data",
                mount_path="/var/www/html",
                bytes=1024,
            ),
        )
    )


def _mock_compose_create_routes(respx_mock: respx.Router) -> None:
    """Everything CREATE needs up to (not including) the compose-adoption gate."""
    respx_mock.get(f"{BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
    )
    respx_mock.get(f"{BASE}/projects/p1").mock(
        return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
    )
    respx_mock.get(f"{BASE}/servers/s2").mock(
        return_value=httpx.Response(200, json={"uuid": "s2", "destinations": [{"uuid": "d1"}]})
    )
    respx_mock.get(f"{BASE}/applications/app1").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": "app1",
                "git_repository": "acme/wordpress-stack",
                "git_branch": "main",
                "build_pack": "dockercompose",
            },
        )
    )
    respx_mock.post(f"{BASE}/applications/public").mock(
        return_value=httpx.Response(201, json={"uuid": "tgt1"})
    )
    respx_mock.get(f"{BASE}/applications/app1/envs").mock(return_value=httpx.Response(200, json=[]))


def _source_host(*, probe: str = "REACH") -> FakeHost:
    """A healthy source. ``probe`` is what ssh.can_reach will conclude.

    A PARAMETER, not a stub to override afterwards: FakeHost matches routes in
    insertion order, so a later `.on()` for the same pattern is dead code that
    silently keeps the default. A test asking for NOPE and quietly getting REACH
    asserts nothing — which is how this helper first got written.
    """
    host = FakeHost()
    host.on(r"command -v rsync", exit_status=0)
    host.on(r"command -v docker", exit_status=0)
    host.on(r"docker ps", stdout="")
    host.on(r"command -v bash", stdout=probe)
    return host


def _target_host(*, free_kb: str = "99999999") -> FakeHost:
    from bg_coolify_migrate.transfer.ssh import SshTarget

    host = FakeHost(SshTarget(host="10.0.0.2"))
    host.on(r"command -v rsync", exit_status=0)
    host.on(r"command -v docker", exit_status=0)
    host.on(r"df -Pk", stdout=free_kb)
    return host


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_source_host(),  # type: ignore[arg-type]
        target_host=_target_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


class TestCaptureMounts:
    """The 'no containers' guard protects stateful resources from silently copying
    nothing - but a stateless / no-volume resource must migrate, not abort."""

    async def test_refuses_when_there_are_volumes_but_no_containers(
        self, ctx: MigrationContext
    ) -> None:
        # Default ctx: a COPY_DATA resource with a volume, source `docker ps` empty.
        from bg_coolify_migrate.engine.steps import _capture_mounts

        with pytest.raises(PreflightError, match="has volumes to migrate"):
            await _capture_mounts(ctx)

    async def test_proceeds_when_a_no_volume_resource_has_no_containers(
        self, ctx: MigrationContext
    ) -> None:
        # A rebuild app with no volumes and no running containers must NOT abort -
        # there is nothing to capture; it is recreated (rebuilt) on the target.
        from bg_coolify_migrate.engine.steps import _capture_mounts

        ctx.plan = _plan(
            resources=(
                ResourcePlan(
                    snapshot=_snapshot(
                        name="alam00000/bentopdf",
                        collection="applications",
                        kind=ResourceKind.APP_GIT_BUILD,
                        engine=None,
                        image=None,
                        builds=True,
                    ),
                    strategy=Strategy.REBUILD,
                    manifest=VolumeManifest(),  # no volumes to migrate
                ),
            )
        )
        captured = await _capture_mounts(ctx)  # no raise
        # An EXPLICIT empty capture (not a missing key), so DISCOVER does not treat
        # it as a lost capture and abort at the next step.
        assert ctx.pre_stop_mounts == {"db1": []}
        assert captured == {"db1": []}

    async def test_returns_the_container_names_it_captured(self, ctx: MigrationContext) -> None:
        # QUIESCE's undo_info counted the FINAL snapshot's containers — always 0,
        # because Coolify removes containers as it stops them. The pre-stop capture
        # is the only honest answer to "what did we stop?", so it reports the names.
        from bg_coolify_migrate.engine.steps import _capture_mounts

        host = FakeHost()
        host.on(
            r"docker ps -a",
            stdout='{"ID": "abc123", "Names": "db-abc123", "State": "running", "Labels": ""}',
        )
        host.on(r"docker inspect", stdout="[]")
        ctx.source_host = host  # type: ignore[assignment]

        captured = await _capture_mounts(ctx)
        assert captured == {"db1": ["db-abc123"]}

    async def test_copy_is_a_noop_and_installs_no_key_without_volumes(
        self, ctx: MigrationContext
    ) -> None:
        # No volume pairs -> copy must not install an ephemeral key or re-check the
        # source; a no-data migration should not have a way to fail there.
        from bg_coolify_migrate.engine.steps import step_copy

        ctx.volume_pairs = {}
        result = await step_copy(ctx)
        assert result["volumes_copied"] == []
        assert ctx.ephemeral_key is None


class TestPreflight:
    @pytest.fixture(autouse=True)
    def _coolify_version(self, respx_mock: respx.Router) -> None:
        """Preflight records the instance version; most tests here ignore it.

        Autouse because it is infrastructure, not subject. Note `text=`, not
        `json=`: /version answers a BARE STRING (see CoolifyClient.version, found
        by the e2e rig after a unit test mocked it as JSON). Six hand-written
        copies would be six chances to re-encode that mistake.
        """
        respx_mock.get(f"{BASE}/version").mock(return_value=httpx.Response(200, text="4.1.2"))

    async def test_passes_a_healthy_setup(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        result = await steps.step_preflight(ctx)
        assert result["free_bytes"] > result["required_bytes"]

    async def test_unreachable_transfer_endpoint_blocks_before_quiesce(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """The check that would have saved the 2026-07-21 run.

        rsync could not reach its endpoint, but nothing found out until COPY —
        which runs AFTER quiesce. So the source was stopped, the copy failed, and
        the whole thing rolled back. Here it is an error with the source
        untouched.
        """
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        source = _source_host(probe="NOPE")
        ctx.source_host = source  # type: ignore[assignment]
        ctx.tunnel_port = 44087

        with pytest.raises(PreflightError, match=re.escape("cannot reach 127.0.0.1:44087")):
            await steps.step_preflight(ctx)

    async def test_undeterminable_endpoint_does_not_block(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """A blind spot must not become a veto.

        A host with neither bash nor nc tells us nothing about reachability.
        Treating that as "unreachable" would refuse migrations that work — the
        preflight would start causing the outages it exists to prevent.
        """
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        source = _source_host(probe="UNKNOWN")
        ctx.source_host = source  # type: ignore[assignment]

        await steps.step_preflight(ctx)  # must not raise

    async def test_no_volumes_means_no_endpoint_probe(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """A stateless migration never opens the socket, so it is not gated on one.

        The source host below stubs NO probe at all: FakeHost errors on an
        unstubbed command, so this passes only if the probe was never sent.
        """
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        source = FakeHost()
        source.on(r"command -v rsync", exit_status=0)
        source.on(r"command -v docker", exit_status=0)
        source.on(r"docker ps", stdout="")
        ctx.source_host = source  # type: ignore[assignment]
        ctx.plan = _plan(
            resources=(
                ResourcePlan(
                    snapshot=_snapshot(),
                    strategy=Strategy.REBUILD,
                    manifest=VolumeManifest(items=()),
                ),
            )
        )

        await steps.step_preflight(ctx)  # must not raise

    async def test_git_compose_target_that_cannot_read_the_repo_blocks(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """The check that would have saved the 2026-07-22 run.

        Coolify loads a dockercompose app's compose by running git ON the target
        server's shell; when that fails, the target never gets its volumes and
        DISCOVER fails 3 minutes after the source was stopped. Probing the same
        command here turns an outage-plus-rollback into an error with the source
        untouched.
        """
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        ctx.plan = _compose_plan()
        target = _target_host()
        target.on(
            r"timeout 20 git ls-remote",
            exit_status=128,
            stderr="fatal: unable to access 'https://github.com/...': Could not resolve host",
        )
        ctx.target_host = target  # type: ignore[assignment]

        with pytest.raises(PreflightError, match=re.escape("cannot read https://github.com/acme")):
            await steps.step_preflight(ctx)

    async def test_a_target_without_git_blocks_with_the_shell_story(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Deploys clone inside the helper container, so a server can deploy git
        # apps for years while LoadComposeFile — plain git on the host — fails.
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        ctx.plan = _compose_plan()
        target = _target_host()
        target.on(r"timeout 20 git ls-remote", exit_status=127, stderr="sh: 1: git: not found")
        ctx.target_host = target  # type: ignore[assignment]

        with pytest.raises(PreflightError, match="cannot read"):
            await steps.step_preflight(ctx)

    async def test_a_private_repo_behind_a_public_source_names_the_right_knob(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """Second run, 2026-07-22: git and egress were fine — the repo had gone
        private while the app's source stayed public. The install-git hint sent
        the operator to the wrong knob; an auth refusal must name the real one."""
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        ctx.plan = _compose_plan()
        target = _target_host()
        target.on(
            r"timeout 20 git ls-remote",
            exit_status=128,
            stderr="fatal: could not read Username for 'https://github.com': "
            "No such device or address",
        )
        ctx.target_host = target  # type: ignore[assignment]

        with pytest.raises(PreflightError, match="requires authentication") as exc_info:
            await steps.step_preflight(ctx)
        hint = exc_info.value.hint or ""
        assert "private git source" in hint
        assert "Install git" not in hint

    async def test_an_unrunnable_probe_does_not_block(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # No `timeout` binary on the target: the probe cannot run, which is not
        # evidence about the target's git. Same doctrine as the endpoint probe —
        # a preflight that fails closed on its own blind spot causes the outages
        # it exists to prevent.
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        ctx.plan = _compose_plan()
        target = _target_host()
        target.on(r"timeout 20 git ls-remote", exit_status=127, stderr="sh: 1: timeout: not found")
        ctx.target_host = target  # type: ignore[assignment]

        await steps.step_preflight(ctx)  # must not raise

    async def test_private_repos_are_not_probed(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The deploy key lives on the control plane and is not ours to borrow.
        # FakeHost errors on an unstubbed command, so this passes only if no
        # ls-remote was ever sent.
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        ctx.plan = _compose_plan(git_auth=GitAuth.DEPLOY_KEY)

        await steps.step_preflight(ctx)  # must not raise
        assert not any("ls-remote" in c for c in ctx.target_host.commands)  # type: ignore[attr-defined]

    async def test_records_the_coolify_version(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """One control plane manages both servers, so there is one version.

        Journalled, never gated — but journalled BEFORE anything is touched, so a
        failed run answers "which Coolify was this?" without a bisect of upstream
        release dates. That question cost an investigation after the 2.5.6 tags
        404, which is why this round trip exists at all.
        """
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        result = await steps.step_preflight(ctx)
        assert result["coolify_version"] == "4.1.2"

    async def test_missing_rsync_fails_before_anything_stops(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # rsync auto-installs, but if it cannot (no package manager) we fail HERE,
        # before the source is stopped - discovering it later would be an outage.
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        host = FakeHost()
        host.on(r"command -v rsync", exit_status=1)  # missing
        host.on(r"command -v \S+", exit_status=1)  # and no package manager to install it
        ctx.source_host = host  # type: ignore[assignment]
        from bg_coolify_migrate.errors import TransferError

        with pytest.raises(TransferError, match="could not be installed"):
            await steps.step_preflight(ctx)

    async def test_insufficient_disk_is_proportional_to_the_payload(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        ctx.plan = _plan(
            resources=(
                ResourcePlan(
                    snapshot=_snapshot(),
                    strategy=Strategy.COPY_DATA,
                    manifest=_manifest(bytes_=100 * 1024**3),
                ),
            )
        )
        ctx.target_host = _target_host(free_kb="1024")  # type: ignore[assignment]
        with pytest.raises(PreflightError, match="free but needs"):
            await steps.step_preflight(ctx)

    async def test_drift_asks_before_proceeding(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        drift = RebuildDriftReport(
            resource_name="web",
            builds=True,
            findings=(
                DriftFinding(axis=DriftAxis.CODE, severity=Severity.WARN, summary="HEAD moved"),
            ),
        )
        ctx.plan = _plan(
            resources=(ResourcePlan(snapshot=_snapshot(), strategy=Strategy.REBUILD, drift=drift),)
        )
        with pytest.raises(RebuildDriftBlocked, match="HEAD moved"):
            await steps.step_preflight(ctx)

    async def test_accept_drift_answers_the_question_in_advance(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Never implicit: unattended we cannot ask, so we stop instead.
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        drift = RebuildDriftReport(
            resource_name="web",
            builds=True,
            findings=(
                DriftFinding(axis=DriftAxis.CODE, severity=Severity.WARN, summary="HEAD moved"),
            ),
        )
        ctx.plan = _plan(
            resources=(
                ResourcePlan(
                    snapshot=_snapshot(),
                    strategy=Strategy.REBUILD,
                    drift=drift,
                    manifest=_manifest(),
                ),
            )
        )
        ctx.accept_drift = True
        await steps.step_preflight(ctx)  # must not raise

    async def test_previews_block(self, ctx: MigrationContext, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        host = FakeHost()
        host.on(r"command -v", exit_status=0)
        host.on(
            r"docker ps",
            stdout=json.dumps(
                {
                    "ID": "c1",
                    "Names": "web-pr-7",
                    "State": "running",
                    "Labels": "coolify.pullRequestId=7",
                }
            ),
        )
        ctx.source_host = host  # type: ignore[assignment]
        from bg_coolify_migrate.errors import QuiesceError

        with pytest.raises(QuiesceError, match="preview deployment"):
            await steps.step_preflight(ctx)


class TestCreateTarget:
    async def test_creates_stopped_and_journals_each_immediately(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A crash after creating the third of five must still delete all three.
        respx_mock.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx_mock.get(f"{BASE}/projects/p1").mock(
            return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
        )
        respx_mock.get(f"{BASE}/servers/s2").mock(
            return_value=httpx.Response(200, json={"uuid": "s2", "destinations": [{"uuid": "d1"}]})
        )
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1", "postgres_password": "s3cret"})
        )
        respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))

        result = await steps.step_create_target(ctx)
        assert result["target_uuids"] == {"db1": "db2"}
        assert ctx.target_uuids["db1"] == "db2"
        # Journalled before the envs were copied, not after.
        assert any(r.detail.get("target_uuids") == {"db1": "db2"} for r in ctx.journal.read())

    async def test_compose_target_waits_for_its_compose_before_quiesce(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Git access is verified through Coolify's OWN machinery, every auth mode.

        LoadComposeFile clones on the target with Coolify's credentials; CREATE
        waits for its outcome (raw saved, storages parsed) while the source is
        still serving. This is what replaces borrowing keys for private repos.
        """
        ctx.plan = _compose_plan(manifest=_compose_manifest())
        _mock_compose_create_routes(respx_mock)
        respx_mock.get(f"{BASE}/applications/tgt1").mock(
            side_effect=[
                httpx.Response(200, json={"uuid": "tgt1", "docker_compose_raw": None}),
                httpx.Response(200, json={"uuid": "tgt1", "docker_compose_raw": "services: {}"}),
            ]
        )
        respx_mock.get(f"{BASE}/applications/tgt1/storages").mock(
            return_value=httpx.Response(
                200,
                json={"persistent_storages": [{"name": "tgt1_app", "mount_path": "/var/www/html"}]},
            )
        )

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(steps.asyncio, "sleep", no_sleep)

        result = await steps.step_create_target(ctx)
        assert result["target_uuids"] == {"app1": "tgt1"}

    async def test_compose_target_that_never_loads_fails_with_zero_downtime(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The 2026-07-22 failure, moved to where it belongs: BEFORE the stop.
        object.__setattr__(ctx.settings, "target_storage_timeout", 0.0)
        ctx.plan = _compose_plan(manifest=_compose_manifest())
        _mock_compose_create_routes(respx_mock)
        respx_mock.get(f"{BASE}/applications/tgt1").mock(
            return_value=httpx.Response(200, json={"uuid": "tgt1", "docker_compose_raw": None})
        )

        with pytest.raises(TransferError, match="never loaded its compose") as exc_info:
            await steps.step_create_target(ctx)
        assert "never stopped" in (exc_info.value.hint or "")

    async def test_compose_drift_in_volumes_fails_before_the_stop(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # The compose loaded, but HEAD no longer declares the volume KEY the
        # running source uses. DISCOVER caught this only after the outage began.
        object.__setattr__(ctx.settings, "target_storage_timeout", 0.0)
        ctx.plan = _compose_plan(manifest=_compose_manifest())
        _mock_compose_create_routes(respx_mock)
        respx_mock.get(f"{BASE}/applications/tgt1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "tgt1", "docker_compose_raw": "services: {}"}
            )
        )
        respx_mock.get(f"{BASE}/applications/tgt1/storages").mock(
            return_value=httpx.Response(200, json={"persistent_storages": []})
        )

        with pytest.raises(TransferError, match="declares no volume for key"):
            await steps.step_create_target(ctx)

    async def test_compose_gate_matches_volume_keys_not_mount_paths(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """REGRESSION (covalida, 2026-07-22): one volume, several mount paths.

        The running source mounts app1_app at /var/www/html while the freshly
        parsed target declares the same compose key at a different path — the
        sighting of a profile-gated service that never runs. The gate must
        accept the KEY instead of waiting 180s for a mount path that can never
        appear, and extra target-only keys (sftp-keys) must not block.
        """
        object.__setattr__(ctx.settings, "target_storage_timeout", 0.0)
        ctx.plan = _compose_plan(manifest=_compose_manifest())
        _mock_compose_create_routes(respx_mock)
        respx_mock.get(f"{BASE}/applications/tgt1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "tgt1", "docker_compose_raw": "services: {}"}
            )
        )
        respx_mock.get(f"{BASE}/applications/tgt1/storages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "persistent_storages": [
                        {"name": "tgt1_app", "mount_path": "/srv/app"},
                        {"name": "tgt1_sftp-keys", "mount_path": "/etc/ssh/keys"},
                    ]
                },
            )
        )

        result = await steps.step_create_target(ctx)
        assert result["target_uuids"] == {"app1": "tgt1"}

    async def test_never_reads_the_main_only_tags_endpoint(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """REGRESSION (2.5.6): create_target must not touch /tags.

        2.5.6 read it on the critical path, so every migration against a real
        Coolify died at create_target with a 404 — the endpoint exists only on
        unreleased `main`. The mock below is registered precisely so it can be
        asserted UNUSED; an unmocked call would also fail, but silently as a
        connection error rather than as this named regression.
        """
        tags_route = respx_mock.get(f"{BASE}/databases/db1/tags").mock(
            return_value=httpx.Response(404, json={"message": "Not found."})
        )
        respx_mock.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        respx_mock.get(f"{BASE}/projects/p1").mock(
            return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
        )
        respx_mock.get(f"{BASE}/servers/s2").mock(
            return_value=httpx.Response(200, json={"uuid": "s2", "destinations": [{"uuid": "d1"}]})
        )
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1", "postgres_password": "s3cret"})
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))
        route = respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )

        await steps.step_create_target(ctx)
        assert not tags_route.called, "create_target read the main-only /tags endpoint"
        assert "tags" not in json.loads(route.calls[0].request.read().decode())


class TestTransferEndpoint:
    """The address rsync dials.

    Had NO unit test until 2.6.1, which is precisely how a hostname reached
    production where an address was bound: the only coverage was e2e, on hosts
    that happened to resolve `localhost`. The failure surfaced at COPY — after
    quiesce — so it cost downtime rather than a preflight error.
    """

    def test_an_open_tunnel_is_dialled_by_address_never_by_name(
        self, ctx: MigrationContext
    ) -> None:
        ctx.tunnel_port = 44087
        host, port, _ = steps._transfer_endpoint(ctx)

        # Asserted as a PROPERTY, not just a value: anything that needs resolving
        # on the source is the bug, and `localhost` is only the instance of it we
        # happened to ship. ip_address() raises on any hostname.
        ipaddress.ip_address(host)
        assert host == "127.0.0.1"
        assert port == 44087

    def test_dialled_address_is_the_one_forward_to_binds(self) -> None:
        """The two ends cannot drift, because there is only one constant.

        The regression in structural form: ssh.py bound the literal "127.0.0.1"
        while steps.py dialled the name "localhost" — two literals in two files,
        agreeing by luck until a host disagreed.
        """
        assert steps.LOOPBACK is ssh.LOOPBACK

    def test_no_tunnel_means_the_target_itself(self, ctx: MigrationContext) -> None:
        ctx.tunnel_port = None
        host, port, _ = steps._transfer_endpoint(ctx)
        assert host == "10.0.0.2"
        assert port == 22

    def test_never_dials_loopback_without_an_open_tunnel(self, ctx: MigrationContext) -> None:
        """The reason this reads ctx.tunnel_port instead of probing again.

        The old code probed a SECOND time and, when its answer disagreed with the
        runner's, returned `ctx.tunnel_port or target_port` — loopback with the
        TARGET's port. rsync would have dialled 127.0.0.1:22 on the source: its
        own sshd, mirroring the volume onto the wrong machine under the target's
        path. No tunnel, no loopback. Ever.
        """
        ctx.tunnel_port = None
        host, port, _ = steps._transfer_endpoint(ctx)
        assert host != steps.LOOPBACK
        assert (host, port) == ("10.0.0.2", 22)


class TestDnsGate:
    async def test_blocks_when_dns_points_at_the_source(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate reads the TARGET's domains; point it at the mocked resource.
        ctx.target_uuids["db1"] = "db1"
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "db1", "fqdn": "https://shop.example.com"}
            )
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))

        from bg_coolify_migrate.dns.extract import Hostname, HostnameOrigin
        from bg_coolify_migrate.dns.gate import Resolution

        async def fake_resolve(hostnames, config=None):  # type: ignore[no-untyped-def]
            return [
                Resolution(
                    Hostname("shop.example.com", HostnameOrigin.FQDN, False),
                    ("10.0.0.1",),
                    ttl=3600,
                )
            ]

        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_all", fake_resolve)

        with pytest.raises(DnsGateBlocked) as exc:
            await steps.step_dns_gate(ctx)
        # The message must explain the mechanism, not just say "blocked".
        assert "ACME" in str(exc.value)
        assert "shop.example.com" in str(exc.value)

    async def test_passes_when_dns_points_at_the_target(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.target_uuids["db1"] = "db1"
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "db1", "fqdn": "https://shop.example.com"}
            )
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))

        from bg_coolify_migrate.dns.extract import Hostname, HostnameOrigin
        from bg_coolify_migrate.dns.gate import Resolution

        async def fake_resolve(hostnames, config=None):  # type: ignore[no-untyped-def]
            return [
                Resolution(Hostname("shop.example.com", HostnameOrigin.FQDN, False), ("10.0.0.2",))
            ]

        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_all", fake_resolve)
        result = await steps.step_dns_gate(ctx)
        assert result["ready"] == ["shop.example.com"]

    async def test_no_real_hostnames_passes(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # A database with no domain cannot block a cutover.
        ctx.target_uuids["db1"] = "db1"
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1"})
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))
        result = await steps.step_dns_gate(ctx)
        assert result["hostnames"] == 0

    async def test_server_bound_url_is_never_resolved_or_gated(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A URL under the source server's wildcard is rewritten onto the target,
        # not cut over: it must not be resolved (it points at the source forever)
        # and must never block.
        ctx.plan = _plan(
            source_server=ServerRef(
                uuid="s1",
                name="old",
                ip="10.0.0.1",
                wildcard_domain="app.0046-20.cloud.bauer-group.com",
            ),
            target_server=ServerRef(
                uuid="s2",
                name="new",
                ip="10.0.0.2",
                wildcard_domain="app.0047-20.cloud.bauer-group.com",
            ),
        )
        ctx.target_uuids["db1"] = "db1"
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(
                200,
                json={"uuid": "db1", "fqdn": "https://pdf-tool.app.0046-20.cloud.bauer-group.com"},
            )
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))

        async def fake_resolve(hostnames, config=None):  # type: ignore[no-untyped-def]
            # Server-bound hosts are excluded before resolution.
            assert list(hostnames) == []
            return []

        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_all", fake_resolve)
        result = await steps.step_dns_gate(ctx)
        assert result["server_bound"] == ["pdf-tool.app.0046-20.cloud.bauer-group.com"]

    async def test_accept_dns_downgrades_a_custom_cutover_block_to_a_warning(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With accept_dns the operator has chosen to finalize and cut DNS over in
        # parallel (propagation lags), so a custom domain on the source warns
        # instead of blocking.
        ctx.accept_dns = True
        ctx.target_uuids["db1"] = "db1"
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "db1", "fqdn": "https://shop.example.com"}
            )
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))

        from bg_coolify_migrate.dns.extract import Hostname, HostnameOrigin
        from bg_coolify_migrate.dns.gate import Resolution

        async def fake_resolve(hostnames, config=None):  # type: ignore[no-untyped-def]
            return [
                Resolution(
                    Hostname("shop.example.com", HostnameOrigin.FQDN, False),
                    ("10.0.0.1",),
                    ttl=3600,
                )
            ]

        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_all", fake_resolve)
        # Must NOT raise.
        result = await steps.step_dns_gate(ctx)
        assert result["cutover_accepted"] == ["shop.example.com"]

    async def test_hostname_server_ip_is_resolved_so_a_custom_domain_gates(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Coolify's server `ip` is a HOSTNAME here. The gate must resolve it to an
        # address, else the custom domain's A record is compared to a name, never
        # matches, and the cutover is silently missed.
        from bg_coolify_migrate.dns.extract import Hostname, HostnameOrigin
        from bg_coolify_migrate.dns.gate import Resolution

        ctx.plan = _plan(
            source_server=ServerRef(uuid="s1", name="old", ip="0046-20.cloud.bauer-group.com"),
            target_server=ServerRef(uuid="s2", name="new", ip="0047-20.cloud.bauer-group.com"),
        )
        ctx.target_uuids["db1"] = "db1"
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "db1", "fqdn": "https://speakup.bauer-group.com"}
            )
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))

        server_ips = {
            "0046-20.cloud.bauer-group.com": "1.1.1.1",
            "0047-20.cloud.bauer-group.com": "2.2.2.2",
        }

        async def fake_resolve_one(hostname, config=None):  # type: ignore[no-untyped-def]
            return Resolution(hostname, (server_ips[hostname.host],))

        async def fake_resolve_all(hostnames, config=None):  # type: ignore[no-untyped-def]
            # The custom domain still points at the SOURCE server's resolved IP.
            return [
                Resolution(
                    Hostname("speakup.bauer-group.com", HostnameOrigin.FQDN, False),
                    ("1.1.1.1",),
                    ttl=300,
                )
            ]

        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_one", fake_resolve_one)
        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_all", fake_resolve_all)

        with pytest.raises(DnsGateBlocked) as exc:
            await steps.step_dns_gate(ctx)
        assert "speakup.bauer-group.com" in str(exc.value)


class TestVerify:
    async def test_differences_are_fatal_and_the_target_is_not_started(
        self, ctx: MigrationContext
    ) -> None:
        ctx.volume_pairs["db1"] = [
            VolumePair(source=VolumeEndpoint("old", "/data"), target=VolumeEndpoint("new", "/data"))
        ]
        source = FakeHost()
        source.on(r"sha256sum", stdout="aaa  ./f\n")
        source.on(r"find --version", stdout="GNU findutils")
        source.on(r"find \. -printf", stdout="./f|f|644|999|999|\n")
        target = FakeHost()
        target.on(r"sha256sum", stdout="bbb  ./f\n")
        target.on(r"find --version", stdout="GNU findutils")
        target.on(r"find \. -printf", stdout="./f|f|644|999|999|\n")
        ctx.source_host = source  # type: ignore[assignment]
        ctx.target_host = target  # type: ignore[assignment]

        with pytest.raises(VerificationError) as exc:
            await steps.step_verify(ctx)
        assert "will NOT be started" in str(exc.value)
        assert "untouched" in str(exc.value)

    async def test_identical_volumes_pass(self, ctx: MigrationContext) -> None:
        ctx.volume_pairs["db1"] = [
            VolumePair(source=VolumeEndpoint("old", "/data"), target=VolumeEndpoint("new", "/data"))
        ]

        def host() -> FakeHost:
            h = FakeHost()
            h.on(r"sha256sum", stdout="aaa  ./f\n")
            h.on(r"find --version", stdout="GNU findutils")
            h.on(r"find \. -printf", stdout="./f|f|644|999|999|\n")
            return h

        ctx.source_host = host()  # type: ignore[assignment]
        ctx.target_host = host()  # type: ignore[assignment]
        result = await steps.step_verify(ctx)
        assert result["differences"] == 0


class TestFinalize:
    async def test_keep_leaves_the_source_alone(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(finalize_policy=FinalizePolicy.KEEP)
        ctx.source_host = FakeHost()  # type: ignore[assignment]
        ctx.target_host = FakeHost()  # type: ignore[assignment]
        ctx.source_host.on(r"rm -rf", exit_status=0)  # type: ignore[attr-defined]
        ctx.target_host.on(r"sed -i", exit_status=0)  # type: ignore[attr-defined]

        result = await steps.step_finalize(ctx)
        assert result["policy"] == "keep"
        assert "kept postgres" in result["actions"]

    async def test_rename_also_releases_the_fqdn(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Without this the old proxy keeps the router rule and keeps renewing a
        # certificate for a hostname it no longer serves.
        ctx.plan = _plan(finalize_policy=FinalizePolicy.RENAME)
        respx_mock.patch(f"{BASE}/databases/db1").mock(return_value=httpx.Response(200, json={}))
        ctx.source_host = FakeHost()  # type: ignore[assignment]
        ctx.target_host = FakeHost()  # type: ignore[assignment]
        ctx.source_host.on(r"rm -rf", exit_status=0)  # type: ignore[attr-defined]
        ctx.target_host.on(r"sed -i", exit_status=0)  # type: ignore[attr-defined]

        result = await steps.step_finalize(ctx)
        assert any("renamed postgres" in a for a in result["actions"])

    async def test_delete_removes_the_source_and_its_volumes(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(finalize_policy=FinalizePolicy.DELETE)
        route = respx_mock.delete(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={})
        )
        ctx.source_host = FakeHost()  # type: ignore[assignment]
        ctx.target_host = FakeHost()  # type: ignore[assignment]
        ctx.source_host.on(r"rm -rf", exit_status=0)  # type: ignore[attr-defined]
        ctx.target_host.on(r"sed -i", exit_status=0)  # type: ignore[attr-defined]

        result = await steps.step_finalize(ctx)
        assert "deleted postgres" in result["actions"]
        assert route.calls[0].request.url.params["deleteVolumes"] == "true"

    async def test_finalize_revokes_the_ephemeral_key(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(finalize_policy=FinalizePolicy.KEEP)
        source = FakeHost()
        source.on(r"rm -rf", exit_status=0)
        target = FakeHost()
        target.on(r"sed -i", exit_status=0)
        ctx.source_host = source  # type: ignore[assignment]
        ctx.target_host = target  # type: ignore[assignment]

        await steps.step_finalize(ctx)
        assert any("sed -i" in c for c in target.commands)
        assert any("rm -rf" in c for c in source.commands)


class TestCompensations:
    async def test_delete_target_removes_its_volumes_too(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Correct HERE and only here: these are volumes WE created minutes ago.
        route = respx_mock.delete(f"{BASE}/databases/db2").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_create_target(ctx, {"target_uuids": {"db1": "db2"}})
        assert route.calls[0].request.url.params["deleteVolumes"] == "true"

    async def test_delete_target_with_nothing_recorded_is_a_noop(
        self, ctx: MigrationContext
    ) -> None:
        await compensations.undo_create_target(ctx, {})

    async def test_restart_source_uses_restart_not_start(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """The compensation that ends the outage must use /restart, not /start.

        /start guards on Coolify's status column, which lags the daemon: after
        QUIESCE removed the container the column can still read "running", and
        /start then 400s "already running" while the source is in fact down.
        /restart carries no such guard. The e2e rollback test caught this; a
        /start here would leave the source dead and the outage un-ended.
        """
        start = respx_mock.post(f"{BASE}/databases/db1/start").mock(
            return_value=httpx.Response(400, json={"message": "Database is already running."})
        )
        restart = respx_mock.post(f"{BASE}/databases/db1/restart").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_quiesce(ctx, {})
        assert restart.called, "the source was not restarted"
        assert not start.called, "used /start, which the stale status column defeats"

    async def test_restart_failure_is_reported_not_swallowed(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.post(f"{BASE}/databases/db1/restart").mock(return_value=httpx.Response(500))
        with pytest.raises(RuntimeError, match="could not restart"):
            await compensations.undo_quiesce(ctx, {})

    async def test_drop_volumes(self, ctx: MigrationContext) -> None:
        target = FakeHost()
        target.on(r"docker volume rm", exit_status=0)
        ctx.target_host = target  # type: ignore[assignment]
        await compensations.undo_copy(ctx, {"volumes_copied": ["postgres-data-db2"]})
        assert any("docker volume rm" in c for c in target.commands)

    async def test_drop_volumes_tolerates_failure(self, ctx: MigrationContext) -> None:
        # A volume that will not drop must not stop the source from restarting.
        target = FakeHost()
        target.on(r"docker volume rm", exit_status=1, stderr="volume in use")
        ctx.target_host = target  # type: ignore[assignment]
        await compensations.undo_copy(ctx, {"volumes_copied": ["v1"]})

    async def test_stop_target(self, ctx: MigrationContext, respx_mock: respx.Router) -> None:
        ctx.target_uuids["db1"] = "db2"
        route = respx_mock.post(f"{BASE}/databases/db2/stop").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_start_target(ctx, {"started": ["db2"]})
        assert route.called

    async def test_restore_name_only_for_rename(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Nothing to undo for DELETE: it is the one irreversible step.
        await compensations.undo_restore_source_name(ctx, {"policy": "delete"})

    async def test_restore_name_after_rename(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.patch(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={})
        )
        await compensations.undo_restore_source_name(ctx, {"policy": "rename"})
        assert '"name":"postgres"' in route.calls[0].request.read().decode()

    async def test_revoke_key(self, ctx: MigrationContext) -> None:
        source = FakeHost()
        source.on(r"rm -rf", exit_status=0)
        target = FakeHost()
        target.on(r"sed -i", exit_status=0)
        ctx.source_host = source  # type: ignore[assignment]
        ctx.target_host = target  # type: ignore[assignment]
        ctx.ephemeral_key = EphemeralKey(
            private_key="x", public_key="y", fingerprint="SHA256:z", remote_path="/root/k"
        )
        await compensations.undo_revoke_key(ctx, {})
        assert any("sed -i" in c for c in target.commands)


class TestContext:
    def test_collection_lookup(self, ctx: MigrationContext) -> None:
        assert ctx.collection_of("db1") == "databases"

    def test_unknown_resource_raises(self, ctx: MigrationContext) -> None:
        with pytest.raises(KeyError):
            ctx.collection_of("nope")

    def test_all_target_uuids(self, ctx: MigrationContext) -> None:
        ctx.target_uuids["db1"] = "db2"
        assert ctx.all_target_uuids() == [("databases", "db2")]


def _host_with_a_running_container() -> FakeHost:
    """A source whose daemon still reports the stack up.

    A fresh host rather than another `.on()` on the shared one: FakeHost matches
    in insertion order, and _source_host() already answers `docker ps` with
    nothing — so a route added afterwards never wins, and the test silently
    exercises the already-down path instead.
    """
    line = json.dumps(
        {
            "ID": "id-pg",
            "Names": "pg-1",
            "State": "running",
            "Labels": "coolify.managed=true,coolify.projectName=shop",
        }
    )
    host = FakeHost()
    host.on(r"docker ps", stdout=line)
    host.on(r"docker inspect", stdout="running 0")
    return host


class TestRequestStop:
    """Asking Coolify to stop a stack it wrongly believes is already stopped.

    `POST /{kind}/{uuid}/stop` returns 400 "already stopped" — and dispatches
    nothing — whenever the resource's `status` column contains 'exited'. That
    column defaults to 'exited' and is advanced by ServerManagerJob every
    minute, so it lags the daemon: shortly after a deploy Coolify refuses to
    stop a container that is serving traffic.

    Both wrong answers cost a migration. Raising aborts over a stale row.
    Shrugging it off waits out the whole gate timeout for a stop nobody
    requested.
    """

    async def test_stops_normally(self, ctx: MigrationContext, respx_mock: respx.Router) -> None:
        route = respx_mock.post(f"{BASE}/databases/db1/stop").mock(
            return_value=httpx.Response(200, json={"message": "stopping"})
        )
        await steps._request_stop(ctx)
        assert route.call_count == 1

    async def test_retries_while_coolify_catches_up(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refusal is checked against the daemon, and re-asked, not believed."""
        monkeypatch.setattr(steps, "_STOP_RETRY_INTERVAL", 0)
        responses = [
            httpx.Response(400, json={"message": "Database is already stopped."}),
            httpx.Response(400, json={"message": "Database is already stopped."}),
            httpx.Response(200, json={"message": "stopping"}),
        ]
        route = respx_mock.post(f"{BASE}/databases/db1/stop").mock(
            side_effect=lambda request: responses.pop(0)
        )
        # The daemon says it is up, so Coolify is merely behind.
        ctx.source_host = _host_with_a_running_container()  # type: ignore[assignment]

        await steps._request_stop(ctx)
        assert route.call_count == 3

    async def test_stops_asking_once_the_daemon_agrees(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refused AND genuinely down is the one case where the 400 was honest."""
        monkeypatch.setattr(steps, "_STOP_RETRY_INTERVAL", 0)
        route = respx_mock.post(f"{BASE}/databases/db1/stop").mock(
            return_value=httpx.Response(400, json={"message": "Database is already stopped."})
        )
        # _source_host() already answers `docker ps` with nothing: genuinely down.

        await steps._request_stop(ctx)
        assert route.call_count == 1

    async def test_gives_up_quietly_and_lets_the_gate_speak(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate below is the one authorised to fail.

        It fails with the list of containers still running, which is what an
        operator needs. Raising here would replace that with a story about an
        HTTP call.
        """
        monkeypatch.setattr(steps, "_STOP_RETRY_INTERVAL", 0)
        monkeypatch.setattr(steps, "_STOP_REFUSAL_WINDOW", 0)
        respx_mock.post(f"{BASE}/databases/db1/stop").mock(
            return_value=httpx.Response(400, json={"message": "Database is already stopped."})
        )
        ctx.source_host = _host_with_a_running_container()  # type: ignore[assignment]

        await steps._request_stop(ctx)  # must not raise

    async def test_a_real_failure_still_raises(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        """Only "already stopped" is tolerated; 500 is a stop that failed."""
        respx_mock.post(f"{BASE}/databases/db1/stop").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        with pytest.raises(CoolifyApiError):
            await steps._request_stop(ctx)


class TestAwaitTargetVolumes:
    """DISCOVER must wait out the async LoadComposeFile job, not race it."""

    async def test_returns_once_expected_mount_paths_appear(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.domain.naming import VolumeEndpoint

        calls = {"n": 0}

        async def fake_read(api, *, collection, uuid):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] < 3:
                return []  # compose not loaded yet
            return [VolumeEndpoint(name="t_data", mount_path="/var/globaleaks")]

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("bg_coolify_migrate.api.resources.read_volume_endpoints", fake_read)
        monkeypatch.setattr(steps.asyncio, "sleep", no_sleep)

        eps = await steps._await_target_volumes(
            ctx, collection="applications", target_uuid="t", expected={"/var/globaleaks"}
        )
        assert {"/var/globaleaks"} <= {e.mount_path for e in eps}
        assert calls["n"] == 3  # polled until it appeared, no earlier

    async def test_times_out_and_returns_the_incomplete_reading(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the storages never materialise, return what we have so the caller's
        # unpaired-volume check raises the precise error (not a silent success).
        object.__setattr__(ctx.settings, "target_storage_timeout", 9.0)

        async def fake_read(api, *, collection, uuid):  # type: ignore[no-untyped-def]
            return []

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("bg_coolify_migrate.api.resources.read_volume_endpoints", fake_read)
        monkeypatch.setattr(steps.asyncio, "sleep", no_sleep)

        eps = await steps._await_target_volumes(
            ctx, collection="applications", target_uuid="t", expected={"/var/globaleaks"}
        )
        assert eps == []

    async def test_timeout_with_an_unloaded_compose_names_the_real_failure(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 2026-07-22 run: seen=[] after 180s, then a raw traceback.

        An APP_GIT_COMPOSE target whose docker_compose_raw is STILL empty at
        timeout never ran LoadComposeFile — an infrastructure failure on the
        target server, not a compose mismatch. Name it, instead of letting the
        pairing step refuse with a story about differing composes.
        """
        object.__setattr__(ctx.settings, "target_storage_timeout", 0.0)

        async def fake_read(api, *, collection, uuid):  # type: ignore[no-untyped-def]
            return []

        monkeypatch.setattr("bg_coolify_migrate.api.resources.read_volume_endpoints", fake_read)
        respx_mock.get(f"{BASE}/applications/t").mock(
            return_value=httpx.Response(200, json={"uuid": "t", "docker_compose_raw": None})
        )

        with pytest.raises(TransferError, match="never loaded its compose from git"):
            await steps._await_target_volumes(
                ctx,
                collection="applications",
                target_uuid="t",
                expected={"/var/www/html"},
                compose_from_git=True,
            )

    async def test_timeout_with_a_loaded_compose_defers_to_the_pairing_error(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The compose DID load but declares different volumes: genuine drift
        # between what the source runs and the branch HEAD. That diagnosis
        # belongs to the pairing step, which sees both sides — so return.
        object.__setattr__(ctx.settings, "target_storage_timeout", 0.0)

        async def fake_read(api, *, collection, uuid):  # type: ignore[no-untyped-def]
            return []

        monkeypatch.setattr("bg_coolify_migrate.api.resources.read_volume_endpoints", fake_read)
        respx_mock.get(f"{BASE}/applications/t").mock(
            return_value=httpx.Response(
                200, json={"uuid": "t", "docker_compose_raw": "services: {}"}
            )
        )

        eps = await steps._await_target_volumes(
            ctx,
            collection="applications",
            target_uuid="t",
            expected={"/var/www/html"},
            compose_from_git=True,
        )
        assert eps == []


class TestDiscoverPairing:
    """A pairing refusal is a diagnosis, not a crash.

    VolumePairingError is a ValueError; unwrapped it crashed the saga as
    "unexpected error in discover" with a full traceback — which is exactly how
    the 2026-07-22 run reported a knowable condition.
    """

    async def test_unpairable_volumes_raise_a_transfer_error_with_both_sides(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.domain.manifest import DockerMount

        ctx.target_uuids = {"db1": "tgt1"}
        ctx.pre_stop_mounts = {
            "db1": [
                DockerMount(
                    container="c1",
                    type="volume",
                    name="postgres-data-db1",
                    destination="/var/lib/postgresql/data",
                )
            ]
        }
        object.__setattr__(ctx.settings, "target_storage_timeout", 0.0)

        async def fake_manifest(host, *, mounts, api_storages, uuid, measure):  # type: ignore[no-untyped-def]
            return _manifest()

        async def fake_read(api, *, collection, uuid):  # type: ignore[no-untyped-def]
            return []  # the target declares nothing

        monkeypatch.setattr(steps, "build_manifest", fake_manifest)
        monkeypatch.setattr("bg_coolify_migrate.api.resources.read_volume_endpoints", fake_read)

        with pytest.raises(TransferError, match="no counterpart on the target") as exc_info:
            await steps.step_discover(ctx)
        # The operator gets both sides, not a traceback.
        assert "/var/lib/postgresql/data" in (exc_info.value.hint or "")

    async def test_compose_volumes_pair_by_key_across_differing_mount_paths(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION (covalida, 2026-07-22): mount paths identify nothing.

        The live containers mount uploads at /var/www/html/wp-content/uploads;
        both sides DECLARE it at /srv/uploads (a dormant sftp service's mount).
        The target also declares /data twice (redis-data + backup-data) — fatal
        for mount-path pairing, irrelevant for key pairing — plus volumes of
        profile-gated services that legitimately start empty.
        """
        from bg_coolify_migrate.domain.manifest import DockerMount

        ctx.plan = _compose_plan(
            manifest=VolumeManifest(
                items=(
                    VolumeItem(
                        mount_class=MountClass.NAMED,
                        decision=Decision.MIGRATE,
                        reason="named volume",
                        source_name="app1_uploads",
                        source_path="/var/lib/docker/volumes/app1_uploads/_data",
                        mount_path="/var/www/html/wp-content/uploads",
                        bytes=1024,
                    ),
                    VolumeItem(
                        mount_class=MountClass.NAMED,
                        decision=Decision.MIGRATE,
                        reason="named volume",
                        source_name="app1_redis-data",
                        source_path="/var/lib/docker/volumes/app1_redis-data/_data",
                        mount_path="/data",
                        bytes=1024,
                    ),
                )
            )
        )
        ctx.target_uuids = {"app1": "tgt1"}
        ctx.pre_stop_mounts = {
            "app1": [
                DockerMount(
                    container="wordpress",
                    type="volume",
                    name="app1_uploads",
                    destination="/var/www/html/wp-content/uploads",
                )
            ]
        }
        object.__setattr__(ctx.settings, "target_storage_timeout", 0.0)

        async def fake_manifest(host, *, mounts, api_storages, uuid, measure):  # type: ignore[no-untyped-def]
            return ctx.plan.resources[0].manifest

        async def fake_read(api, *, collection, uuid):  # type: ignore[no-untyped-def]
            return [
                VolumeEndpoint("tgt1_uploads", "/srv/uploads"),
                VolumeEndpoint("tgt1_redis-data", "/data"),
                VolumeEndpoint("tgt1_backup-data", "/data"),
                VolumeEndpoint("tgt1_sftp-keys", "/etc/ssh/keys"),
            ]

        monkeypatch.setattr(steps, "build_manifest", fake_manifest)
        monkeypatch.setattr("bg_coolify_migrate.api.resources.read_volume_endpoints", fake_read)

        result = await steps.step_discover(ctx)
        assert result["volume_pairs"]["app1"] == [
            {
                "source": "app1_redis-data",
                "target": "tgt1_redis-data",
                "mount_path": "/data",
            },
            {
                "source": "app1_uploads",
                "target": "tgt1_uploads",
                "mount_path": "/var/www/html/wp-content/uploads",
            },
        ]


class TestUndoParkedDomains:
    """Rollback must un-park the source domains create_target freed."""

    async def test_rollback_restores_the_parked_source_domain(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.engine.compensations import undo_create_target

        route = respx_mock.patch(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1"})
        )
        undo_info = {
            "target_uuids": {},
            "parked_domains": {"db1": {"domains": "https://speakup.bauer-group.com"}},
        }
        await undo_create_target(ctx, undo_info)
        assert json.loads(route.calls[0].request.read()) == {
            "domains": "https://speakup.bauer-group.com"
        }

    async def test_no_parked_domains_makes_no_restore_call(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.engine.compensations import undo_create_target

        await undo_create_target(ctx, {"target_uuids": {}})
        assert not respx_mock.calls

    async def test_target_is_deleted_before_the_source_domain_is_reclaimed(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Reclaiming a custom domain the target still holds would 409, so the
        # target must be deleted FIRST, then the source domain restored.
        from bg_coolify_migrate.engine.compensations import undo_create_target

        order: list[str] = []

        def rec_delete(request: httpx.Request) -> httpx.Response:
            order.append("delete")
            return httpx.Response(200, json={})

        def rec_patch(request: httpx.Request) -> httpx.Response:
            order.append("patch")
            return httpx.Response(200, json={"uuid": "db1"})

        respx_mock.delete(f"{BASE}/databases/dbT").mock(side_effect=rec_delete)
        respx_mock.patch(f"{BASE}/databases/db1").mock(side_effect=rec_patch)

        await undo_create_target(
            ctx,
            {
                "target_uuids": {"db1": "dbT"},
                "parked_domains": {"db1": {"domains": "https://speakup.bauer-group.com"}},
            },
        )
        assert order == ["delete", "patch"]


class TestMaybeTunnel:
    """Whether the reverse forward is opened at all.

    This branch decides where every byte of the migration travels, and it had no
    unit coverage until 2.6.2 -- the same blind spot that let a hostname and a
    dash-only probe reach production.
    """

    async def _run(self, ctx: MigrationContext) -> None:
        from bg_coolify_migrate.engine.runner import maybe_tunnel

        async with maybe_tunnel(ctx):
            pass

    async def test_opens_the_tunnel_when_the_target_is_unreachable(
        self, ctx: MigrationContext
    ) -> None:
        source = _source_host(probe="NOPE")
        ctx.source_host = source  # type: ignore[assignment]

        await self._run(ctx)
        assert source.forwards == [("10.0.0.2", 22)]
        assert ctx.tunnel_port == 44087

    async def test_skips_the_tunnel_when_the_target_is_reachable(
        self, ctx: MigrationContext
    ) -> None:
        source = _source_host()  # stubs bash -> REACH
        ctx.source_host = source  # type: ignore[assignment]

        await self._run(ctx)
        assert source.forwards == []
        assert ctx.tunnel_port is None

    async def test_an_undeterminable_probe_takes_the_tunnel(self, ctx: MigrationContext) -> None:
        """`reachable is not True`, not `not reachable`.

        A host with no bash and no nc tells us nothing. The tunnel works whether
        or not a direct route exists, so an unknown must fall THAT way -- reading
        it as "reachable" would dial a route that may not be there, after the
        source is already stopped.
        """
        source = _source_host(probe="UNKNOWN")
        ctx.source_host = source  # type: ignore[assignment]

        await self._run(ctx)
        assert source.forwards == [("10.0.0.2", 22)]
        assert ctx.tunnel_port == 44087
