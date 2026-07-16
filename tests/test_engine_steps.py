"""Tests for the F1 step implementations and their compensations.

These are the migration. Every test here corresponds to a guarantee documented in
docs/safety.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.drift import DriftAxis, DriftFinding, RebuildDriftReport, Severity
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
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
    VerificationError,
)
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
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


def _source_host() -> FakeHost:
    host = FakeHost()
    host.on(r"command -v rsync", exit_status=0)
    host.on(r"command -v docker", exit_status=0)
    host.on(r"docker ps", stdout="")
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
        await _capture_mounts(ctx)  # no raise
        # An EXPLICIT empty capture (not a missing key), so DISCOVER does not treat
        # it as a lost capture and abort at the next step.
        assert ctx.pre_stop_mounts == {"db1": []}

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
    async def test_passes_a_healthy_setup(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/security/keys").mock(
            return_value=httpx.Response(200, json=[{"private_key": "x"}])
        )
        result = await steps.step_preflight(ctx)
        assert result["free_bytes"] > result["required_bytes"]

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
            resources=(
                ResourcePlan(snapshot=_snapshot(), strategy=Strategy.REBUILD, drift=drift),
            )
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
                    snapshot=_snapshot(), strategy=Strategy.REBUILD, drift=drift, manifest=_manifest()
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
                {"ID": "c1", "Names": "web-pr-7", "State": "running", "Labels": "coolify.pullRequestId=7"}
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
        assert any(
            r.detail.get("target_uuids") == {"db1": "db2"} for r in ctx.journal.read()
        )


class TestDnsGate:
    async def test_blocks_when_dns_points_at_the_source(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1", "fqdn": "https://shop.example.com"})
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
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1", "fqdn": "https://shop.example.com"})
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
        respx_mock.get(f"{BASE}/databases/db1").mock(
            return_value=httpx.Response(200, json={"uuid": "db1"})
        )
        respx_mock.get(f"{BASE}/databases/db1/envs").mock(return_value=httpx.Response(200, json=[]))
        result = await steps.step_dns_gate(ctx)
        assert result["hostnames"] == 0


class TestVerify:
    async def test_differences_are_fatal_and_the_target_is_not_started(
        self, ctx: MigrationContext
    ) -> None:
        ctx.volume_pairs["db1"] = [
            VolumePair(
                source=VolumeEndpoint("old", "/data"), target=VolumeEndpoint("new", "/data")
            )
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
            VolumePair(
                source=VolumeEndpoint("old", "/data"), target=VolumeEndpoint("new", "/data")
            )
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
