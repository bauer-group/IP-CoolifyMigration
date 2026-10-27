"""An interrupted or half-undone CREATE must still converge.

Three routes left a CREATE's partial work — targets made, source domains parked —
with nothing that would ever undo it:

* a crash, SIGKILL or Ctrl-C mid-CREATE (BaseException skips the in-step undo,
  and the saga compensates only COMPLETED states, so ``rollback`` planned nothing
  and reported ROLLED_BACK);
* an in-step undo that itself failed (reported FAILED, journal finished, resume
  refused, rollback planned nothing);
* a resume after such a crash, which created a second set of targets and parked
  the already-parked hosts again.

The parked source looks fine until its next deploy, which serves only
``old-<tag>.`` hosts — the custom domain dies days later, far from its cause.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

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
)
from bg_coolify_migrate.domain.statemachine import (
    Compensation,
    Outcome,
    State,
    owed_states,
)
from bg_coolify_migrate.engine import compensations, steps
from bg_coolify_migrate.engine.compensations import build_compensations
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import CoolifyApiError, RollbackError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.conftest import FakeHost

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SHOP = "https://shop.example.com"
CREATE = State.CREATE_TARGET.value


def _plan() -> MigrationPlan:
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=(
            ResourcePlan(
                snapshot=ResourceSnapshot(
                    uuid="app1",
                    name="shop",
                    collection="applications",
                    kind=ResourceKind.APP_DOCKERIMAGE,
                ),
                strategy=Strategy.COPY_DATA,
            ),
        ),
    )


def _host() -> FakeHost:
    """No volume pins, and the key revoke every target delete now runs first."""
    return (
        FakeHost()
        .on(r"label=bg-coolify-migrate\.role=pin", stdout="")
        .on(r"authorized_keys\.bak", exit_status=0)
        .on(r"rm -rf /root/\.coolify-migrate/", exit_status=0)
    )


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_host(),  # type: ignore[arg-type]
        target_host=_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


def _mock_create(respx_mock: respx.Router, *, create: Any) -> None:
    respx_mock.get(f"{BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
    )
    respx_mock.get(f"{BASE}/projects/p1").mock(
        return_value=httpx.Response(200, json={"environments": [{"name": "production"}]})
    )
    respx_mock.get(f"{BASE}/servers/s2/destinations").mock(
        return_value=httpx.Response(200, json=[{"uuid": "d1", "network": "coolify"}])
    )
    respx_mock.get(f"{BASE}/applications/app1").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": "app1",
                "name": "shop",
                "build_pack": "dockerimage",
                "docker_registry_image_name": "nginx",
                "fqdn": SHOP,
            },
        )
    )
    respx_mock.post(f"{BASE}/applications/dockerimage").mock(side_effect=create)
    respx_mock.get(f"{BASE}/applications/app1/storages").mock(
        return_value=httpx.Response(200, json={"persistent_storages": [], "file_storages": []})
    )
    respx_mock.get(f"{BASE}/applications/app1/tags").mock(return_value=httpx.Response(200, json=[]))
    # Read before any write by the locked-variable pre-pass, then by copy_envs.
    respx_mock.get(f"{BASE}/applications/app1/envs").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(f"{BASE}/applications/app1/scheduled-tasks").mock(
        return_value=httpx.Response(200, json=[])
    )


def _interrupted(ctx: MigrationContext, *, target: str = "appOld") -> None:
    """What a crash mid-CREATE leaves: the step's own records, and no ending."""
    ctx.journal.append("step_started", state=CREATE)
    ctx.journal.append(
        "step_started",
        state=CREATE,
        detail={"target_uuids": {}, "parked_domains": {"app1": {"domains": SHOP}}},
    )
    ctx.journal.append(
        "step_started",
        state=CREATE,
        detail={"target_uuids": {"app1": target}, "parked_domains": {"app1": {"domains": SHOP}}},
    )


def _saga(ctx: MigrationContext, steps_: dict[Any, Any] | None = None) -> Saga:
    return Saga(
        journal=ctx.journal, context=ctx, steps=steps_ or {}, compensations=build_compensations()
    )


class TestOwedStates:
    def test_completed_only_by_default(self) -> None:
        assert owed_states(["create_target", "quiesce"]) == ("create_target", "quiesce")

    def test_the_in_flight_state_is_owed(self) -> None:
        assert owed_states(["plan"], in_flight=CREATE) == ("plan", CREATE)

    def test_an_unfinished_undo_is_owed(self) -> None:
        assert owed_states([], undo_incomplete=[CREATE]) == (CREATE,)

    def test_no_duplicates(self) -> None:
        assert owed_states([CREATE], in_flight=CREATE, undo_incomplete=[CREATE]) == (CREATE,)

    def test_quiesce_is_never_owed_for_being_in_flight(self) -> None:
        # A crash before the stop request leaves the source RUNNING; restarting
        # it would be an outage of our own making.
        assert owed_states([CREATE], in_flight="quiesce") == (CREATE,)
        assert owed_states([CREATE], undo_incomplete=["quiesce"]) == (CREATE,)


class TestJournalReadsTheInterruption:
    def test_in_flight_is_the_step_with_no_ending(self, ctx: MigrationContext) -> None:
        ctx.journal.append("step_completed", state="plan")
        _interrupted(ctx)
        assert ctx.journal.in_flight_state() == CREATE

    @pytest.mark.parametrize("ending", ["step_completed", "step_failed", "blocked"])
    def test_an_ended_step_is_not_in_flight(self, ctx: MigrationContext, ending: str) -> None:
        _interrupted(ctx)
        ctx.journal.append(ending, state=CREATE)
        assert ctx.journal.in_flight_state() is None

    def test_an_empty_journal_has_nothing_in_flight(self, ctx: MigrationContext) -> None:
        assert ctx.journal.in_flight_state() is None

    def test_undo_incomplete_latest_word_wins(self, ctx: MigrationContext) -> None:
        ctx.journal.append("step_started", state=CREATE, detail={"undo_incomplete": True})
        assert ctx.journal.undo_incomplete_states() == [CREATE]
        ctx.journal.append("step_started", state=CREATE, detail={"undo_incomplete": False})
        assert ctx.journal.undo_incomplete_states() == []


class TestRollbackAfterACrash:
    async def test_rollback_undoes_an_in_flight_create(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _interrupted(ctx, target="appT")
        order: list[str] = []

        def rec(label: str) -> Any:
            def handler(_request: httpx.Request) -> httpx.Response:
                order.append(label)
                return httpx.Response(200, json={})

            return handler

        respx_mock.delete(f"{BASE}/applications/appT").mock(side_effect=rec("delete"))
        patch = respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=rec("patch"))

        result = await _saga(ctx).rollback()

        assert result.outcome is Outcome.ROLLED_BACK
        assert order == ["delete", "patch"]
        assert json.loads(patch.calls[0].request.read()) == {"domains": SHOP}
        assert result.compensations_run == [
            Compensation.DELETE_TARGET_RESOURCE,
            Compensation.RESTORE_SOURCE_FQDN,
            Compensation.RELEASE_SOURCE_PINS,
        ]

    async def test_a_cancelled_create_leaves_exactly_what_rollback_needs(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Ctrl-C (CancelledError, a BaseException) after the target was created:
        # the in-step undo never runs, so the journal is all there is.
        _mock_create(respx_mock, create=[httpx.Response(201, json={"uuid": "appT"})])
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )

        reads: list[str] = []

        def ctrl_c(_request: httpx.Request) -> httpx.Response:
            # The first read is the locked-variable pre-pass; the second is
            # copy_envs, after the target exists.
            reads.append("envs")
            if len(reads) == 1:
                return httpx.Response(200, json=[])
            raise asyncio.CancelledError

        respx_mock.get(f"{BASE}/applications/app1/envs").mock(side_effect=ctrl_c)

        with pytest.raises(asyncio.CancelledError):
            await steps.step_create_target(ctx)

        assert ctx.journal.in_flight_state() == CREATE
        info = ctx.journal.undo_info(CREATE)
        assert info["target_uuids"] == {"app1": "appT"}
        assert info["parked_domains"] == {"app1": {"domains": SHOP}}

        respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await _saga(ctx).rollback()
        assert result.outcome is Outcome.ROLLED_BACK

    async def test_a_target_already_gone_counts_as_deleted(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # Re-running a rollback must converge, not fail forever on its own work.
        respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(404, json={"message": "Application not found."})
        )
        await compensations.undo_create_target(ctx, {"target_uuids": {"app1": "appT"}})

    async def test_other_delete_failures_still_count(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.delete(f"{BASE}/applications/appT").mock(return_value=httpx.Response(500))
        with pytest.raises(RuntimeError, match="could not delete target"):
            await compensations.undo_create_target(ctx, {"target_uuids": {"app1": "appT"}})

    async def test_an_unknown_target_is_skipped(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        await compensations.undo_create_target(ctx, {"target_uuids": {"nope": "x"}})
        assert not respx_mock.calls

    async def test_f2_plans_from_completions_only(self, ctx: MigrationContext) -> None:
        # A machine with its own map gets none of F1's rules.
        _interrupted(ctx)
        saga = Saga(
            journal=ctx.journal,
            context=ctx,
            steps={},
            compensations=build_compensations(),
            order=list(State),
            compensation_map={CREATE: (Compensation.DELETE_TARGET_RESOURCE,)},
        )
        result = await saga.rollback()
        assert result.outcome is Outcome.ROLLED_BACK
        assert result.compensations_run == []


class TestAFailedUndoIsRecoverable:
    async def test_an_unpark_failure_during_create_undo_is_loud(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create(
            respx_mock, create=[httpx.Response(422, json={"message": "Validation failed."})]
        )
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            side_effect=[httpx.Response(200, json={}), httpx.Response(500)]
        )

        with pytest.raises(RollbackError) as exc:
            await steps.step_create_target(ctx)

        assert exc.value.exit_code == 8
        assert isinstance(exc.value.__cause__, CoolifyApiError)
        assert exc.value.__cause__.status_code == 422
        assert "never stopped" in (exc.value.hint or "")
        assert "dockerimage failed" in (exc.value.hint or "")  # the original speaks
        assert "domains NOT restored: shop" in str(exc.value)
        assert ctx.journal.undo_incomplete_states() == [CREATE]

    async def test_the_run_ends_rollback_failed_and_rollback_finishes_it(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _mock_create(respx_mock, create=[httpx.Response(201, json={"uuid": "appT"})])
        # Readable for the pre-pass, then failing in copy_envs, after the create.
        respx_mock.get(f"{BASE}/applications/app1/envs").mock(
            side_effect=[httpx.Response(200, json=[]), httpx.Response(500)]
        )
        patch = respx_mock.patch(f"{BASE}/applications/app1").mock(
            side_effect=[httpx.Response(200, json={}), httpx.Response(500)]
        )
        respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )

        result = await _saga(ctx, {State.CREATE_TARGET: steps.step_create_target}).run()

        # Not FAILED ("nothing changed"): the source is still parked.
        assert result.outcome is Outcome.ROLLBACK_FAILED
        assert result.exit_code == 8
        assert isinstance(result.error, RollbackError)
        assert ctx.journal.is_finished  # resume refuses ...

        # ... so the explicit rollback is the retry, and it converges.
        patch.side_effect = None
        patch.return_value = httpx.Response(200, json={})
        again = await _saga(ctx).rollback()
        assert again.outcome is Outcome.ROLLED_BACK
        assert json.loads(patch.calls[-1].request.read()) == {"domains": SHOP}


class TestResumeAfterACrash:
    async def test_resume_undoes_the_interrupted_attempt_then_creates_afresh(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _interrupted(ctx, target="appOld")
        ctx.target_uuids["app1"] = "appOld"  # what resume_migration rehydrates
        order: list[str] = []
        bodies: list[dict[str, Any]] = []

        def patch(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.read()))
            order.append("patch")
            return httpx.Response(200, json={})

        def delete(_request: httpx.Request) -> httpx.Response:
            order.append("delete appOld")
            return httpx.Response(200, json={})

        def create(_request: httpx.Request) -> httpx.Response:
            order.append("create")
            return httpx.Response(201, json={"uuid": "appNew"})

        _mock_create(respx_mock, create=create)
        respx_mock.get(f"{BASE}/applications/app1/envs").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx_mock.patch(f"{BASE}/applications/app1").mock(side_effect=patch)
        respx_mock.delete(f"{BASE}/applications/appOld").mock(side_effect=delete)

        result = await steps.step_create_target(ctx)

        assert order == ["delete appOld", "patch", "patch", "create"]
        assert bodies[0] == {"domains": SHOP}  # un-parked first ...
        parked = bodies[1]["domains"]
        # ... so the fresh park starts from the REAL host, never old-x.old-x.
        assert parked.count("old-") == 1 and parked.endswith(".shop.example.com")
        assert result == {"target_uuids": {"app1": "appNew"}}
        assert ctx.target_uuids == {"app1": "appNew"}
        info = ctx.journal.undo_info(CREATE)
        assert info["target_uuids"] == {"app1": "appNew"}
        assert info["parked_domains"] == {"app1": {"domains": SHOP}}

    async def test_a_resume_whose_undo_fails_creates_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        _interrupted(ctx, target="appOld")
        respx_mock.delete(f"{BASE}/applications/appOld").mock(return_value=httpx.Response(500))
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )
        create = respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(201, json={"uuid": "appNew"})
        )

        with pytest.raises(RollbackError, match="could not delete target"):
            await steps.step_create_target(ctx)

        assert not create.called
        assert ctx.journal.undo_incomplete_states() == [CREATE]

    async def test_a_fresh_run_has_nothing_to_undo(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.journal.append("step_started", state=CREATE)  # the saga's own record
        _mock_create(respx_mock, create=[httpx.Response(201, json={"uuid": "appT"})])
        respx_mock.get(f"{BASE}/applications/app1/envs").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx_mock.patch(f"{BASE}/applications/app1").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await steps.step_create_target(ctx)
        assert result == {"target_uuids": {"app1": "appT"}}
        assert not [c for c in respx_mock.calls if c.request.method == "DELETE"]
