"""Watching the deployments behind START and the rollback's restart (Coolify 4.7).

An application's ``/start`` and ``/restart`` answer 200 the moment Coolify queues
a deployment, and a deployment can then fail before it creates a single
container: a build error, a full queue, and from 4.7.0 the image-scan gate
("Block deploys"). Without reading the deployment row, a blocked START sat out
the whole deploy_timeout with the source down, and a blocked rollback restart
was journalled as done and reported ROLLED_BACK over a source that stayed down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from rich.console import Console

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import coolify_versions as cv
from bg_coolify_migrate.domain import deployments as d
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.manifest import VolumeManifest
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import (
    STOP_REQUESTED,
    Compensation,
    Outcome,
    State,
)
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import RunResult, Saga
from bg_coolify_migrate.errors import CoolifyApiError, TransferError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.ui.console import THEME
from bg_coolify_migrate.ui.run_report import outcome_panel
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"

_BLOCKED = {
    "status": "failed",
    "logs": '[{"output": "DB_PASSWORD=hunter2"}]',
    "scan_gate_result": {
        "blocked": True,
        "mode": "on",
        "headline": "Deploy blocked: this image has 2 security problems that attackers are using.",
        "top": [{"id": "CVE-2026-1"}],
    },
}


def _app(uuid: str = "app1", name: str = "web") -> ResourcePlan:
    return ResourcePlan(
        snapshot=ResourceSnapshot(
            uuid=uuid, name=name, collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
        ),
        strategy=Strategy.RECREATE_ONLY,
        manifest=VolumeManifest(),
    )


def _db() -> ResourcePlan:
    return ResourcePlan(
        snapshot=ResourceSnapshot(
            uuid="db1",
            name="postgres",
            collection="databases",
            kind=ResourceKind.DATABASE,
            engine=DatabaseEngine.POSTGRESQL,
            image="postgres:16",
        ),
        strategy=Strategy.RECREATE_ONLY,
        manifest=VolumeManifest(),
    )


def _plan(*resources: ResourcePlan) -> MigrationPlan:
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=resources or (_app(),),
    )


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    target = FakeHost()
    target.on(r"docker ps", stdout="")  # nothing ever comes up
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m47"),
        migration_id="m47",
        # Nothing started either, at START's containment checkpoint.
        source_host=FakeHost().quiet_docker(),  # type: ignore[arg-type]
        target_host=target.quiet_docker(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every sleep the steps and the compensations take, taken instantly."""
    slept: list[float] = []

    async def instant(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(steps.asyncio, "sleep", instant)
    monkeypatch.setattr(compensations.asyncio, "sleep", instant)
    return slept


def _deployment(status: str, **gate: Any) -> d.Deployment:
    return d.read_deployment("dep1", {"status": status, "scan_gate_result": gate or None})


# ── the pure core ────────────────────────────────────────────────────────────


class TestRelease:
    def test_the_gate_starts_in_4_7_0(self) -> None:
        assert not cv.supports("deploy_image_scan_gate", (4, 6, 0))
        assert cv.supports("deploy_image_scan_gate", (4, 7, 0))
        assert cv.supports("deploy_image_scan_gate", (4, 7, 1))


class TestReadDeployment:
    def test_keeps_the_status_and_the_gate_verdict_and_nothing_else(self) -> None:
        kept = d.read_deployment("dep1", _BLOCKED)
        assert kept == d.Deployment(
            uuid="dep1",
            status="failed",
            gate_blocked=True,
            gate_headline=_BLOCKED["scan_gate_result"]["headline"],  # type: ignore[index]
        )
        assert "hunter2" not in repr(kept)

    def test_a_gate_that_passed_carries_no_headline(self) -> None:
        kept = _deployment("finished", blocked=False, headline="")
        assert not kept.gate_blocked
        assert kept.gate_headline == ""

    def test_the_headline_is_one_bounded_line(self) -> None:
        kept = _deployment("failed", blocked=True, headline="Deploy\nblocked:  " + "x" * 500)
        assert "\n" not in kept.gate_headline
        assert kept.gate_headline.startswith("Deploy blocked: x")
        assert len(kept.gate_headline) <= 240

    def test_no_row_shape_reads_as_unknown(self) -> None:
        assert d.judge("dep1", d.read_deployment("dep1", "nope")).progress is d.Progress.PENDING

    def test_keeps_the_code_of_a_scan_that_could_not_run(self) -> None:
        kept = _deployment(
            "failed",
            blocked=True,
            error="scanning_off",
            headline="Deploy blocked: Turn on image scanning on new.",
        )
        assert kept.gate_error == "scanning_off"
        assert _deployment("failed", blocked=True, error=None).gate_error == ""

    @pytest.mark.parametrize("error", ["Scanner said: DB_PASSWORD=hunter2", 7, "x" * 40])
    def test_anything_but_a_code_is_not_kept(self, error: Any) -> None:
        assert _deployment("failed", blocked=True, error=error).gate_error == ""


class TestJudge:
    def test_finished(self) -> None:
        assert d.judge("dep1", _deployment("finished")).progress is d.Progress.FINISHED

    @pytest.mark.parametrize("status", ["queued", "in_progress", "", "paused-by-a-future-release"])
    def test_anything_else_is_pending(self, status: str) -> None:
        # A status this tool does not know must never fail a healthy run.
        assert d.judge("dep1", _deployment(status)).progress is d.Progress.PENDING

    def test_a_plain_failure_names_the_deployment(self) -> None:
        verdict = d.judge("dep1", _deployment("failed"))
        assert verdict.progress is d.Progress.FAILED
        assert not verdict.gate_blocked
        assert "dep1 failed" in verdict.reason

    def test_a_gate_block_quotes_coolify(self) -> None:
        verdict = d.judge("dep1", d.read_deployment("dep1", _BLOCKED))
        assert verdict.progress is d.Progress.FAILED
        assert verdict.gate_blocked
        assert "image-scan gate" in verdict.reason
        assert "2 security problems" in verdict.reason

    def test_the_status_decides_not_the_verdict_alone(self) -> None:
        # blocked is written just before the job fails the row; until then it runs.
        assert d.judge("dep1", _deployment("in_progress", blocked=True)).progress is (
            d.Progress.PENDING
        )

    def test_cancelled_is_a_failure(self) -> None:
        verdict = d.judge("dep1", _deployment("cancelled-by-user"))
        assert verdict.progress is d.Progress.FAILED
        assert "cancelled" in verdict.reason

    def test_no_row_is_a_failure(self) -> None:
        # /start and /restart answer 200 with a uuid even when the queue was full.
        verdict = d.judge("dep1", None)
        assert verdict.progress is d.Progress.FAILED
        assert "queue is full" in verdict.reason


class TestStartFailureHint:
    def test_findings_get_the_first_deploy_hint(self) -> None:
        verdict = d.judge("dep1", d.read_deployment("dep1", _BLOCKED))
        assert d.start_failure_hint(verdict) == d.FIRST_DEPLOY_GATE_HINT
        assert "most specific level" in d.FIRST_DEPLOY_GATE_HINT

    def test_a_scan_that_could_not_run_gets_its_own(self) -> None:
        # 'If the scan cannot run: Block' decided, not a finding: no ignore helps.
        verdict = d.judge("dep1", _deployment("failed", blocked=True, error="unknown"))
        hint = d.start_failure_hint(verdict)
        assert hint == d.SCAN_ERROR_GATE_HINT
        assert "Turn image scanning on for the target server" in hint
        assert "4.7.1" in hint

    def test_a_plain_failure_points_at_the_log(self) -> None:
        assert "log in Coolify" in d.start_failure_hint(d.judge("dep1", _deployment("failed")))


class TestDeploymentUuidOf:
    def test_a_queued_answer(self) -> None:
        answer = {"message": "Deployment request queued.", "deployment_uuid": "dep1"}
        assert d.deployment_uuid_of(answer) == "dep1"

    @pytest.mark.parametrize(
        "answer",
        [
            {"message": "Deployment already queued for this commit."},
            {"message": "Database restarting request queued."},
            {"deployment_uuid": ""},
            None,
            "ok",
        ],
    )
    def test_nothing_to_watch(self, answer: Any) -> None:
        assert d.deployment_uuid_of(answer) is None


class TestPlanWarning:
    def test_none_before_4_7(self) -> None:
        assert d.image_scan_gate_warnings(version=(4, 6, 0)) == []

    @pytest.mark.parametrize("version", [(4, 7, 0), (4, 7, 1), None])
    def test_from_4_7_and_on_an_unknown_release(self, version: Any) -> None:
        (warning,) = d.image_scan_gate_warnings(version=version)
        assert "Block deploys" in warning
        assert "FIRST deploy" in warning
        assert "rollback's restart" in warning
        # Not only Strict: a restart re-pulls or rebuilds, and On compares the image.
        assert "under On it is blocked when its image changed" in warning
        assert "most specific level that sets it" in warning

    async def test_the_planner_asks_for_every_application(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_engine_planner import _plan_one_app

        plan = await _plan_one_app(monkeypatch, {}, version="4.7.0")
        assert any("Block deploys" in w for w in plan.resources[0].warnings)
        assert not plan.is_blocked, "a warning, never a refusal"
        older = await _plan_one_app(monkeypatch, {}, version="4.6.0")
        assert not any("Block deploys" in w for w in older.resources[0].warnings)


# ── the client boundary ──────────────────────────────────────────────────────


class TestGetDeployment:
    async def test_drops_the_logs_at_the_boundary(self, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json=_BLOCKED)
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            found = await api.get_deployment("dep1")
        assert found is not None
        assert found.status == "failed"
        assert found.gate_blocked
        assert "hunter2" not in repr(found)

    async def test_a_404_is_no_such_deployment(self, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(404, json={"message": "Deployment not found."})
        )
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            assert await api.get_deployment("dep1") is None

    async def test_any_other_error_raises(self, respx_mock: respx.Router) -> None:
        respx_mock.get(f"{BASE}/deployments/dep1").mock(return_value=httpx.Response(500))
        async with CoolifyClient(HOST, "tok", max_retries=0) as api:
            with pytest.raises(CoolifyApiError):
                await api.get_deployment("dep1")


# ── START and HEALTHCHECK ────────────────────────────────────────────────────


def _started(ctx: MigrationContext, deployments: dict[str, str]) -> None:
    ctx.journal.append(
        "step_completed",
        state=State.START_TARGET.value,
        detail={"started": ["tgt1"], "deployments": deployments},
    )


class TestStartTarget:
    async def test_journals_each_application_deployment(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db(), _app())
        ctx.target_uuids.update({"db1": "db2", "app1": "tgt1"})
        respx_mock.post(f"{BASE}/databases/db2/start").mock(
            return_value=httpx.Response(200, json={"message": "Database starting request queued."})
        )
        respx_mock.post(f"{BASE}/applications/tgt1/start").mock(
            return_value=httpx.Response(
                200, json={"message": "Deployment request queued.", "deployment_uuid": "dep1"}
            )
        )
        detail = await steps.step_start_target(ctx)
        assert detail == {"started": ["db2", "tgt1"], "deployments": {"app1": "dep1"}}


class TestHealthcheckWatchesTheDeployment:
    async def test_a_gate_block_fails_at_once_with_coolify_s_headline(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        _started(ctx, {"app1": "dep1"})
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json=_BLOCKED)
        )
        with pytest.raises(TransferError) as excinfo:
            await steps.step_healthcheck(ctx)
        assert no_sleep == [], "waited for a deployment that had already failed"
        assert "2 security problems" in str(excinfo.value)
        assert excinfo.value.hint == d.FIRST_DEPLOY_GATE_HINT
        assert "hunter2" not in str(excinfo.value) + (excinfo.value.hint or "")

    async def test_a_plain_failure_points_at_its_log(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        _started(ctx, {"app1": "dep1"})
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            side_effect=[
                httpx.Response(200, json={"status": "in_progress"}),
                httpx.Response(200, json={"status": "failed"}),
            ]
        )
        with pytest.raises(TransferError) as excinfo:
            await steps.step_healthcheck(ctx)
        assert len(no_sleep) == 1
        assert "web did not deploy: deployment dep1 failed" in str(excinfo.value)
        assert "log in Coolify" in (excinfo.value.hint or "")

    async def test_no_such_deployment_fails_at_once(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        _started(ctx, {"app1": "dep1"})
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(404, json={"message": "Deployment not found."})
        )
        with pytest.raises(TransferError, match="queue is full"):
            await steps.step_healthcheck(ctx)

    async def test_a_finished_deployment_leaves_success_to_the_daemon(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ctx.settings, "deploy_timeout", 6.0)
        _started(ctx, {"app1": "dep1"})
        route = respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json={"status": "finished"})
        )
        with pytest.raises(TransferError) as excinfo:
            await steps.step_healthcheck(ctx)
        assert route.call_count == 1, "a finished deployment is read once"
        assert "did not become healthy" in str(excinfo.value)
        assert "its deployment in Coolify reads 'finished'" in str(excinfo.value)

    async def test_an_unreadable_deployment_decides_nothing(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ctx.settings, "deploy_timeout", 0.0)
        _started(ctx, {"app1": "dep1"})
        respx_mock.get(f"{BASE}/deployments/dep1").mock(return_value=httpx.Response(500))
        with pytest.raises(TransferError, match="did not become healthy"):
            await steps.step_healthcheck(ctx)

    async def test_healthy_containers_need_no_deployment_read(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        import json

        target = FakeHost()
        target.on(
            r"docker ps -a .*coolify\.resourceName=web",
            stdout=json.dumps(
                {
                    "ID": "c1",
                    "Names": "web-tgt1-1",
                    "Image": "web:1",
                    "State": "running",
                    "Labels": (
                        "coolify.managed=true,coolify.projectName=shop,"
                        "coolify.environmentName=production,coolify.resourceName=web"
                    ),
                }
            ),
        )
        ctx.target_host = target  # type: ignore[assignment]
        _started(ctx, {"app1": "dep1"})
        route = respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json={"status": "failed"})
        )
        result = await steps.step_healthcheck(ctx)
        assert result["containers"] == 1
        assert not route.called, "the daemon decides success"


def _running(name: str) -> str:
    """One running container of ``name`` in the plan's project, as ``docker ps`` prints it."""
    import json

    return json.dumps(
        {
            "ID": f"c-{name}",
            "Names": f"{name}-tgt-1",
            "Image": f"{name}:1",
            "State": "running",
            "Labels": (
                "coolify.managed=true,coolify.projectName=shop,"
                f"coolify.environmentName=production,coolify.resourceName={name}"
            ),
        }
    )


class TestAFailureWaitsForTheOtherStarts:
    """A failed target deployment fails HEALTHCHECK only once every other start landed.

    The rollback's STOP_TARGET is a plain /stop, which cancels no deployment and
    can overtake a start still on the deployment queue: a sibling that comes up
    after it serves the claimed custom hosts while DNS points at the source.
    """

    async def test_until_the_sibling_s_deployment_ends(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        ctx.plan = _plan(_app(), _app("app2", "api"))
        _started(ctx, {"app1": "dep1", "app2": "dep2"})
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json={"status": "failed"})
        )
        sibling = respx_mock.get(f"{BASE}/deployments/dep2").mock(
            side_effect=[httpx.Response(200, json={"status": "in_progress"})] * 3
            + [httpx.Response(200, json={"status": "finished"})]
        )
        with pytest.raises(TransferError) as excinfo:
            await steps.step_healthcheck(ctx)
        assert len(no_sleep) == 3, "raised while the sibling was still deploying"
        assert sibling.call_count == 4
        message = str(excinfo.value)
        assert message.startswith(
            "the target of web did not deploy: deployment dep1 failed; its log in Coolify says why"
        )
        assert "the target of api" not in message, "only the failure is named"

    async def test_no_longer_than_the_deadline(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ctx.settings, "deploy_timeout", 6.0)
        ctx.plan = _plan(_app(), _app("app2", "api"))
        _started(ctx, {"app1": "dep1", "app2": "dep2"})
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json=_BLOCKED)
        )
        respx_mock.get(f"{BASE}/deployments/dep2").mock(
            return_value=httpx.Response(200, json={"status": "in_progress"})
        )
        with pytest.raises(TransferError) as excinfo:
            await steps.step_healthcheck(ctx)
        assert len(no_sleep) == 2
        # The failure's reason, not "did not become healthy": that is what is known.
        assert "web did not deploy: Coolify's image-scan gate blocked" in str(excinfo.value)
        assert excinfo.value.hint == d.FIRST_DEPLOY_GATE_HINT

    async def test_a_sibling_without_a_deployment_lands_when_the_daemon_shows_it_up(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        target = FakeHost()
        target.on_sequence(
            r"docker ps -a .*coolify\.resourceName=postgres",
            [{"stdout": ""}, {"stdout": ""}, {"stdout": _running("postgres")}],
        )
        target.on(r"docker ps", stdout="")
        ctx.target_host = target  # type: ignore[assignment]
        ctx.plan = _plan(_app(), _db())
        _started(ctx, {"app1": "dep1"})  # a database queues no deployment
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(404, json={"message": "Deployment not found."})
        )
        with pytest.raises(TransferError, match="queue is full"):
            await steps.step_healthcheck(ctx)
        assert len(no_sleep) == 2, "the database's start had not landed before"

    async def test_every_failure_is_named(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        ctx.plan = _plan(_app(), _app("app2", "api"))
        _started(ctx, {"app1": "dep1", "app2": "dep2"})
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json={"status": "failed"})
        )
        respx_mock.get(f"{BASE}/deployments/dep2").mock(
            return_value=httpx.Response(200, json={"status": "cancelled-by-user"})
        )
        with pytest.raises(TransferError) as excinfo:
            await steps.step_healthcheck(ctx)
        assert no_sleep == [], "nothing else was on its way"
        assert "web did not deploy" in str(excinfo.value)
        assert "api did not deploy: deployment dep2 was cancelled" in str(excinfo.value)


# ── the rollback's restart ───────────────────────────────────────────────────


def _stopped(ctx: MigrationContext) -> None:
    ctx.journal.append("step_started", state=State.QUIESCE.value, detail={STOP_REQUESTED: True})
    ctx.journal.append("step_completed", state=State.QUIESCE.value, detail={})


def _restart_records(ctx: MigrationContext) -> list[dict[str, Any]]:
    return [
        r.detail
        for r in ctx.journal.read()
        if r.event == "rollback_step"
        and r.detail.get("compensation") == Compensation.RESTART_SOURCE.value
    ]


def _restart_route(respx_mock: respx.Router, *uuids: str) -> respx.Route:
    return respx_mock.post(f"{BASE}/applications/app1/restart").mock(
        side_effect=[
            httpx.Response(
                200, json={"message": "Restart request queued.", "deployment_uuid": uuid}
            )
            for uuid in uuids
        ]
    )


class TestRestartWatchesItsDeployment:
    async def test_counts_as_restarted_once_finished(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        _stopped(ctx)
        _restart_route(respx_mock, "dep1")
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            side_effect=[
                httpx.Response(200, json={"status": "queued"}),
                httpx.Response(200, json={"status": "finished"}),
            ]
        )
        await compensations.undo_quiesce(ctx, {})
        assert _restart_records(ctx) == [
            {"compensation": "restart_source", "restart_deployment": "dep1", "source": "app1"},
            {"compensation": "restart_source", "restarted": "app1"},
        ]
        assert compensations._restarted_since_stop(ctx.journal.read()) == {"app1"}

    async def test_a_gate_block_is_no_restart(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        _stopped(ctx)
        _restart_route(respx_mock, "dep1")
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json=_BLOCKED)
        )
        with pytest.raises(RuntimeError) as excinfo:
            await compensations.undo_quiesce(ctx, {})
        message = str(excinfo.value)
        assert message.startswith("could not restart: web: Coolify's image-scan gate blocked")
        assert "hunter2" not in message
        assert compensations._restarted_since_stop(ctx.journal.read()) == set()
        (notice,) = ctx.notices
        assert "web is still DOWN" in notice
        assert "Deploy anyway" in notice
        assert "coolify-migrate unpin m47 - not the rollback" in notice
        assert "coolify-migrate rollback m47" in notice
        # Off on the team does nothing where the app or its server sets a mode.
        assert "most specific level that sets it" in notice

    async def test_the_next_rollback_asks_again(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        _stopped(ctx)
        restart = _restart_route(respx_mock, "dep1", "dep2")
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json={"status": "failed"})
        )
        respx_mock.get(f"{BASE}/deployments/dep2").mock(
            return_value=httpx.Response(200, json={"status": "finished"})
        )
        with pytest.raises(RuntimeError, match="dep1 failed"):
            await compensations.undo_quiesce(ctx, {})
        await compensations.undo_quiesce(ctx, {})
        assert restart.call_count == 2
        assert compensations._restarted_since_stop(ctx.journal.read()) == {"app1"}

    async def test_an_interrupted_watch_is_resumed_not_redeployed(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        """A crash while the rollback watched a restart must not cost a second one."""
        _stopped(ctx)
        ctx.journal.append(
            "rollback_step",
            state=State.QUIESCE.value,
            detail={"compensation": "restart_source", "restart_deployment": "dep1", "source": "app1"},
        )
        restart = _restart_route(respx_mock, "dep2")
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            side_effect=[
                httpx.Response(200, json={"status": "in_progress"}),  # does it stand?
                httpx.Response(200, json={"status": "in_progress"}),
                httpx.Response(200, json={"status": "finished"}),
            ]
        )
        await compensations.undo_quiesce(ctx, {})
        assert not restart.called
        assert compensations._restarted_since_stop(ctx.journal.read()) == {"app1"}

    async def test_an_unreadable_interrupted_watch_is_watched_not_asked_again(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        """A read error decides nothing: asking again redeployed a source coming up,
        or got a 'skipped' answer with nothing to watch while dep1 still ran."""
        _stopped(ctx)
        ctx.journal.append(
            "rollback_step",
            state=State.QUIESCE.value,
            detail={"compensation": "restart_source", "restart_deployment": "dep1", "source": "app1"},
        )
        restart = respx_mock.post(f"{BASE}/applications/app1/restart").mock(
            return_value=httpx.Response(
                200, json={"message": "Deployment already queued for this commit."}
            )
        )
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            side_effect=[
                httpx.Response(502),  # does it stand? unreadable
                httpx.Response(200, json={"status": "in_progress"}),
                httpx.Response(200, json={"status": "failed"}),
            ]
        )
        with pytest.raises(RuntimeError, match="dep1 failed"):
            await compensations.undo_quiesce(ctx, {})
        assert not restart.called
        assert compensations._restarted_since_stop(ctx.journal.read()) == set()

    def test_a_request_from_before_the_last_stop_is_forgotten(self) -> None:
        def asked(source: str, deployment: str) -> dict[str, str]:
            return {
                "compensation": "restart_source",
                "restart_deployment": deployment,
                "source": source,
            }

        records = [
            _record("rollback_step", asked("app1", "d0")),
            _record("step_started", {STOP_REQUESTED: True}),
            _record("rollback_step", asked("db1", "d1")),
            _record("rollback_step", asked("db1", "d2")),
        ]
        assert compensations._restart_deployments_since_stop(records) == {"db1": "d2"}

    async def test_a_slow_queue_keeps_today_s_answer(
        self,
        ctx: MigrationContext,
        respx_mock: respx.Router,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not terminal by deploy_timeout: recorded as restarted, said so, never asked again."""
        monkeypatch.setattr(ctx.settings, "deploy_timeout", 10.0)
        _stopped(ctx)
        _restart_route(respx_mock, "dep1")
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json={"status": "queued"})
        )
        await compensations.undo_quiesce(ctx, {})
        assert compensations._restarted_since_stop(ctx.journal.read()) == {"app1"}
        (notice,) = ctx.notices
        assert "had not finished after 10s" in notice
        assert "coolify-migrate unpin m47" in notice

    async def test_no_such_deployment_is_no_restart(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        _stopped(ctx)
        _restart_route(respx_mock, "dep1")
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(404, json={"message": "Deployment not found."})
        )
        with pytest.raises(RuntimeError, match="queue is full"):
            await compensations.undo_quiesce(ctx, {})
        assert compensations._restarted_since_stop(ctx.journal.read()) == set()

    async def test_a_database_has_no_deployment_to_watch(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db())
        _stopped(ctx)
        respx_mock.post(f"{BASE}/databases/db1/restart").mock(
            return_value=httpx.Response(200, json={"message": "Database restarting request queued."})
        )
        await compensations.undo_quiesce(ctx, {})
        assert _restart_records(ctx) == [{"compensation": "restart_source", "restarted": "db1"}]

    async def test_a_skipped_restart_names_nothing_to_watch(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _stopped(ctx)
        respx_mock.post(f"{BASE}/applications/app1/restart").mock(
            return_value=httpx.Response(
                200, json={"message": "Deployment already queued for this commit."}
            )
        )
        await compensations.undo_quiesce(ctx, {})
        assert _restart_records(ctx) == [{"compensation": "restart_source", "restarted": "app1"}]


def _record(event: str, detail: dict[str, Any], *, state: str | None = None) -> Any:
    from bg_coolify_migrate.journal.store import JournalRecord

    return JournalRecord(
        seq=0, ts="t", event=event, state=state or State.QUIESCE.value, detail=detail
    )


class TestTheRollbackSaysTheSourceIsDown:
    async def test_a_blocked_restart_fails_the_rollback_and_keeps_the_pins(
        self, ctx: MigrationContext, respx_mock: respx.Router, no_sleep: list[float]
    ) -> None:
        ctx.journal.append("step_completed", state=State.CREATE_TARGET.value, detail={})
        _stopped(ctx)
        _restart_route(respx_mock, "dep1")
        respx_mock.get(f"{BASE}/deployments/dep1").mock(
            return_value=httpx.Response(200, json=_BLOCKED)
        )

        async def nothing(_ctx: Any, _info: dict[str, Any]) -> None:
            return None

        comps: dict[Any, Any] = {c: nothing for c in Compensation}
        comps[Compensation.RESTART_SOURCE] = compensations.undo_quiesce
        result = await Saga(
            journal=ctx.journal, context=ctx, steps={}, compensations=comps
        ).rollback()
        result.notices.extend(ctx.notices)

        assert result.outcome is Outcome.ROLLBACK_FAILED
        # RELEASE_SOURCE_PINS reads this: not restarted -> pins kept without a look.
        assert not compensations._restart_succeeded(ctx.journal.read())
        out = _render(result)
        assert "running again" not in out
        assert "is still DOWN" in out


def _render(result: RunResult) -> str:
    console = Console(width=200, force_terminal=False, no_color=True, theme=THEME)
    with console.capture() as capture:
        console.print(outcome_panel(result, migration_id="m47"))
    return capture.get()


class TestRolledBackReport:
    def test_a_notice_withholds_running_again(self) -> None:
        result = RunResult(
            outcome=Outcome.ROLLED_BACK,
            reached=State.HEALTHCHECK,
            compensations_run=[Compensation.RESTART_SOURCE],
            notices=["web's restart (deployment dep1) had not finished after 900s"],
        )
        out = _render(result)
        assert "running again" not in out
        assert "read what was left in place" in out
        assert "never at risk" in out

    def test_without_one_it_still_reassures(self) -> None:
        result = RunResult(
            outcome=Outcome.ROLLED_BACK,
            reached=State.HEALTHCHECK,
            compensations_run=[Compensation.RESTART_SOURCE],
        )
        assert "your source is running again" in _render(result)
