"""What a rollback must refuse to do, and what it must tell the operator.

* After FINALIZE(delete) began, the target holds the ONLY copy of the data, and
  every remaining compensation aims at the target: STOP_TARGET,
  DROP_TARGET_VOLUMES, DELETE_TARGET_RESOURCE(delete_volumes=True). A rollback
  then is not an undo, it is the loss of the data.
* After DNS already points at the target, restoring and restarting the source
  is not enough: the domain stays dead until DNS points back. The rollback
  cannot do that, so it says so.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import domains
from bg_coolify_migrate.domain.plan import MigrationPlan, ServerRef
from bg_coolify_migrate.domain.statemachine import (
    Compensation,
    FinalizePolicy,
    Outcome,
    State,
    rollback_refusal,
)
from bg_coolify_migrate.engine import runner, steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import RunResult, Saga
from bg_coolify_migrate.errors import CoolifyApiError, RollbackError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.ui import run_report

FINALIZE = State.FINALIZE.value
SRC_WILDCARD = "app.0046-20.cloud.bauer-group.com"
TGT_WILDCARD = "app.0047-20.cloud.bauer-group.com"


def _recorder() -> tuple[list[Compensation], dict[Any, Any]]:
    ran: list[Compensation] = []

    def make(comp: Compensation) -> Any:
        async def undo(_ctx: Any, _info: dict[str, Any]) -> None:
            ran.append(comp)

        return undo

    return ran, {c: make(c) for c in Compensation}


class TestRollbackRefusal:
    def test_a_delete_finalize_refuses(self) -> None:
        reason = rollback_refusal({FINALIZE: {"policy": "delete"}})
        assert reason is not None
        assert "only copy" in reason

    @pytest.mark.parametrize("policy", ["rename", "keep", None])
    def test_other_policies_do_not(self, policy: str | None) -> None:
        assert rollback_refusal({FINALIZE: {"policy": policy}}) is None

    def test_no_finalize_at_all(self) -> None:
        assert rollback_refusal({}) is None


class TestTheSagaRefuses:
    async def test_rollback_after_a_delete_finalize_runs_nothing(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "g1")
        for state in (State.CREATE_TARGET, State.QUIESCE, State.COPY, State.START_TARGET):
            journal.append("step_completed", state=state.value)
        journal.append("step_completed", state=FINALIZE, detail={"policy": "delete"})
        ran, comps = _recorder()

        with pytest.raises(RollbackError, match="only copy") as exc:
            await Saga(journal=journal, context=None, steps={}, compensations=comps).rollback()

        assert ran == []
        assert exc.value.exit_code == 8
        assert str(journal.path) in (exc.value.hint or "")
        assert [r.event for r in journal.read()][-1] == "rollback_refused"
        assert not journal.is_finished  # nothing pretends it was rolled back

    async def test_a_delete_finalize_failing_part_way_is_not_rolled_back(
        self, tmp_path: Path
    ) -> None:
        # The first source is already deleted when the second delete fails:
        # rolling back would delete the only copy of the first.
        journal = Journal.create(tmp_path, "g2")
        ran, comps = _recorder()

        async def ok(_ctx: Any) -> dict[str, Any]:
            return {}

        async def finalize(_ctx: Any) -> dict[str, Any]:
            journal.append("step_started", state=FINALIZE, detail={"policy": "delete"})
            raise CoolifyApiError("DELETE /databases/db2 failed", status_code=500)

        all_steps: dict[Any, Any] = {s: ok for s in State if s is not State.DONE}
        all_steps[State.FINALIZE] = finalize

        with pytest.raises(RollbackError) as exc:
            await Saga(journal=journal, context=None, steps=all_steps, compensations=comps).run()

        assert ran == []
        assert isinstance(exc.value.__cause__, CoolifyApiError)

    async def test_a_rename_finalize_still_rolls_back(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "g3")
        journal.append("step_completed", state=State.CREATE_TARGET.value)
        journal.append("step_completed", state=FINALIZE, detail={"policy": "rename"})
        ran, comps = _recorder()
        result = await Saga(journal=journal, context=None, steps={}, compensations=comps).rollback()
        assert result.outcome is Outcome.ROLLED_BACK
        assert Compensation.RESTORE_SOURCE_NAME in ran

    async def test_f2_has_no_such_guard(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "g4")
        journal.append("step_completed", state=FINALIZE, detail={"policy": "delete"})
        ran, comps = _recorder()
        result = await Saga(
            journal=journal,
            context=None,
            steps={},
            compensations=comps,
            order=list(State),
            compensation_map={FINALIZE: (Compensation.RESTORE_SOURCE_NAME,)},
        ).rollback()
        assert result.outcome is Outcome.ROLLED_BACK
        assert ran == [Compensation.RESTORE_SOURCE_NAME]


class TestFinalizeJournalsItsPolicyFirst:
    async def test_the_policy_is_on_disk_before_anything_is_deleted(
        self, tmp_path: Path, respx_mock: respx.Router
    ) -> None:
        from tests.test_engine_steps import BASE, _plan, _source_host, _target_host

        api = CoolifyClient("https://coolify.example.com", "tok", max_retries=0)
        ctx = MigrationContext(
            api=api,
            settings=Settings(_env_file=None, state_dir=tmp_path),
            plan=_plan(finalize_policy=FinalizePolicy.DELETE),
            journal=Journal.create(tmp_path, "g5"),
            migration_id="g5",
            # No volume pins on either side (FINALIZE unpins before it deletes).
            source_host=_source_host(),  # type: ignore[arg-type]
            target_host=_target_host(),  # type: ignore[arg-type]
        )
        respx_mock.delete(f"{BASE}/databases/db1").mock(return_value=httpx.Response(500))
        try:
            with pytest.raises(CoolifyApiError):
                await steps.step_finalize(ctx)
        finally:
            await api.aclose()
        assert ctx.journal.undo_info(FINALIZE) == {"policy": "delete"}
        assert rollback_refusal(ctx.journal.details()) is not None


class TestDnsRollbackNote:
    def _note(self, gate: dict[str, Any]) -> str | None:
        return domains.dns_rollback_note(
            gate, source_ip="10.0.0.1", source_wildcard=SRC_WILDCARD, target_wildcard=TGT_WILDCARD
        )

    def test_hosts_dns_already_sends_to_the_target(self) -> None:
        note = self._note({"ready": ["shop.example.com"], "cutover_accepted": ["api.example.com"]})
        assert note is not None
        assert note.startswith("point DNS for api.example.com, shop.example.com back to 10.0.0.1")

    def test_server_bound_hosts_never_need_it(self) -> None:
        gate = {
            "ready": [f"x.{TGT_WILDCARD}"],
            "server_bound": [f"x.{SRC_WILDCARD}"],
            "cutover_accepted": [f"y.{SRC_WILDCARD}"],
        }
        assert self._note(gate) is None

    @pytest.mark.parametrize(
        "gate", [{}, {"ready": []}, {"ready": None}, {"ready": "shop.example.com"}, {"ready": [3]}]
    )
    def test_nothing_to_say(self, gate: dict[str, Any]) -> None:
        assert self._note(gate) is None


def _ctx(journal: Journal) -> Any:
    plan = MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1", wildcard_domain=SRC_WILDCARD),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2", wildcard_domain=TGT_WILDCARD),
    )
    return SimpleNamespace(journal=journal, plan=plan)


class TestTheRollbackSaysSo:
    @pytest.mark.parametrize("outcome", [Outcome.ROLLED_BACK, Outcome.ROLLBACK_FAILED])
    def test_a_rollback_after_the_dns_gate_carries_the_note(
        self, tmp_path: Path, outcome: Outcome
    ) -> None:
        journal = Journal.create(tmp_path, "n1")
        journal.append(
            "step_completed",
            state=State.DNS_GATE.value,
            detail={"ready": [], "cutover_accepted": ["shop.example.com"]},
        )
        result = runner._with_rollback_notes(
            RunResult(outcome=outcome, reached=None), _ctx(journal)
        )
        assert len(result.notes) == 1
        assert "shop.example.com back to 10.0.0.1" in result.notes[0]

    def test_a_successful_run_carries_none(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "n2")
        journal.append(
            "step_completed", state=State.DNS_GATE.value, detail={"ready": ["shop.example.com"]}
        )
        result = runner._with_rollback_notes(
            RunResult(outcome=Outcome.SUCCEEDED, reached=None), _ctx(journal)
        )
        assert result.notes == []

    def test_no_dns_gate_no_note(self, tmp_path: Path) -> None:
        result = runner._with_rollback_notes(
            RunResult(outcome=Outcome.ROLLED_BACK, reached=None),
            _ctx(Journal.create(tmp_path, "n3")),
        )
        assert result.notes == []

    def test_the_report_renders_it(self) -> None:
        from rich.console import Console

        result = RunResult(
            outcome=Outcome.ROLLED_BACK,
            reached=State.INIT,
            notes=["point DNS for shop.example.com back to 10.0.0.1"],
        )
        plain = run_report.plain_result(result, migration_id="m1")
        assert "note: point DNS for shop.example.com back to 10.0.0.1" in plain

        from bg_coolify_migrate.ui.console import THEME

        console = Console(record=True, width=120, no_color=True, theme=THEME)
        console.print(run_report.outcome_panel(result, migration_id="m1"))
        assert "point DNS for shop.example.com" in console.export_text()
