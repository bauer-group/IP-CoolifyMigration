"""A rollback after FINALIZE takes back the tags FINALIZE gave, and is owed without FINALIZE done.

Two gaps the F1 review left open:

* FINALIZE gives every target its source's tag names, and ``POST /deploy?tag=``
  ignores auto-deploy (DeployController::by_tags v4.7.2:488-538). DISARM_TARGETS
  only switched auto-deploy off, so a CI run deploying by tag redeployed the
  stopped target on the volumes the rollback drops — and a target the rollback
  KEEPS stayed tagged for good. FINALIZE now journals each target's names
  before it attaches them (``TAGS_GIVEN``), and DISARM_TARGETS detaches exactly
  those (``DELETE /{collection}/{uuid}/tags/{tag_uuid}``, v4.2.0:160 .. v4.7.5:313).
* DISARM_TARGETS was owed only by a COMPLETED FINALIZE (or the explicit
  ``rollback`` of one in flight). A FINALIZE killed after it armed a target,
  resumed, and failed before completing rolled back automatically without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain import deploy_triggers as dt
from bg_coolify_migrate.domain.deploy_triggers import GivenTags, Triggers
from bg_coolify_migrate.domain.statemachine import (
    ARM_REQUESTED,
    TAGS_GIVEN,
    Compensation,
    Outcome,
    State,
    owed_states,
    rollback_plan,
    states_through,
    targets_armed,
)
from bg_coolify_migrate.engine import compensations
from bg_coolify_migrate.engine import deploy_triggers as engine
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import Saga
from bg_coolify_migrate.errors import QuiesceError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.test_deploy_triggers_engine import (
    BASE,
    HOST,
    App,
    _app,
    _db,
    _host,
    _journal_originals,
    _plan,
)


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path, deploy_timeout=10.0),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_host(),  # type: ignore[arg-type]
        target_host=_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


def _rows(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"uuid": u, "name": n, "created_at": None, "updated_at": None} for u, n in pairs]


def _source_tags(ctx: MigrationContext, **names: list[str]) -> None:
    ctx.journal.append(
        "step_started", state="create_target", detail={dt.TAGS_JOURNAL_KEY: dict(names)}
    )


# ── the decisions ────────────────────────────────────────────────────────────


class TestTagsToDetach:
    def test_only_the_names_finalize_gave(self) -> None:
        rows = _rows(("t1", "prod"), ("t2", "manual"), ("t3", "eu"))
        assert dt.tags_to_detach(rows, ["prod", "eu"]) == ["t1", "t3"]

    def test_case_and_edges_do_not_hide_one(self) -> None:
        # Upstream lowercases and trims every name (normalizeTagNames).
        assert dt.tags_to_detach(_rows(("t1", "prod")), [" PROD "]) == ["t1"]

    def test_tolerant_of_what_is_not_a_tag(self) -> None:
        rows: list[Any] = [None, {"uuid": "", "name": "prod"}, {"uuid": "t1"}, {"name": "prod"}]
        assert dt.tags_to_detach(rows, ["prod"]) == []
        assert dt.tags_to_detach({"uuid": "t1"}, ["prod"]) == []
        assert dt.tags_to_detach(_rows(("t1", "prod")), []) == []


class TestGivenJournal:
    def test_round_trip_and_tolerance(self) -> None:
        given = {"appT": GivenTags("applications", ("prod", "eu"))}
        assert dt.deserialise_given(dt.serialise_given(given)) == given
        assert dt.deserialise_given(None) == {}
        assert dt.deserialise_given({"appT": {"collection": "applications"}}) == {}
        assert dt.deserialise_given({"": {"collection": "x", "names": []}}) == {}
        assert dt.deserialise_given({"dbT": {"collection": "databases", "names": ["a", 3]}}) == {
            "dbT": GivenTags("databases", ("a",))
        }

    def test_the_journal_accepts_it(self, tmp_path: Path) -> None:
        journal = Journal.create(tmp_path, "m1")
        given = {"appT": GivenTags("applications", ("prod",))}
        journal.append("step_started", state="finalize", detail={TAGS_GIVEN: dt.serialise_given(given)})
        assert dt.deserialise_given(journal.undo_info("finalize")[TAGS_GIVEN]) == given

    def test_one_constant_each(self) -> None:
        # The domain module re-exports the statemachine's keys; one spelling.
        assert dt.ARM_REQUESTED == ARM_REQUESTED and dt.TAGS_GIVEN == TAGS_GIVEN


class TestOwedWithoutACompletedFinalize:
    _through_healthcheck = tuple(s.value for s in states_through(State.HEALTHCHECK))

    @pytest.mark.parametrize(
        "marker",
        [
            {ARM_REQUESTED: ["appT"]},
            {TAGS_GIVEN: {"dbT": {"collection": "databases", "names": ["prod"]}}},
        ],
    )
    def test_a_journalled_arm_owes_finalize(self, marker: dict[str, Any]) -> None:
        # The automatic rollback of a failed run passes no in_flight state.
        owed = owed_states(self._through_healthcheck, markers={"finalize": marker})
        assert State.FINALIZE.value in owed
        comps = [s.compensation for s in rollback_plan([State(s) for s in owed])]
        assert comps[0] is Compensation.DISARM_TARGETS

    def test_nothing_journalled_owes_nothing(self) -> None:
        owed = owed_states(self._through_healthcheck, markers={"finalize": {"policy": "rename"}})
        assert State.FINALIZE.value not in owed
        assert not targets_armed({ARM_REQUESTED: [], TAGS_GIVEN: {}})


# ── FINALIZE journals the names before it attaches them ─────────────────────


class TestGiveTags:
    async def test_journalled_before_the_post(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _journal_originals(ctx, app1=Triggers(False, False))
        _source_tags(ctx, app1=["prod", "eu"])
        App("appT", auto=False).mount(respx_mock)
        seen: list[dict[str, GivenTags]] = []

        def post(_request: httpx.Request) -> httpx.Response:
            seen.append(engine.given_tags(ctx))
            return httpx.Response(201, json=[])

        respx_mock.post(f"{BASE}/applications/appT/tags").mock(side_effect=post)

        await engine.arm_targets(ctx)

        assert seen == [{"appT": GivenTags("applications", ("prod", "eu"))}]

    async def test_names_read_from_the_source_are_journalled_too(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        # CREATE could not read them: FINALIZE asks the source itself.
        ctx.plan = _plan(_db())
        ctx.target_uuids["db1"] = "dbT"
        respx_mock.get(f"{BASE}/databases/db1/tags").mock(
            return_value=httpx.Response(200, json=_rows(("t1", "prod")))
        )
        post = respx_mock.post(f"{BASE}/databases/dbT/tags").mock(
            return_value=httpx.Response(201, json=[])
        )

        await engine.arm_targets(ctx)

        assert post.called
        assert engine.given_tags(ctx) == {"dbT": GivenTags("databases", ("prod",))}

    async def test_a_record_that_cannot_be_written_attaches_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.plan = _plan(_db())
        ctx.target_uuids["db1"] = "dbT"
        _source_tags(ctx, db1=["prod"])
        post = respx_mock.post(f"{BASE}/databases/dbT/tags")
        append = ctx.journal.append

        def refuse(event: str, **kw: Any) -> Any:
            if TAGS_GIVEN in (kw.get("detail") or {}):
                raise OSError("disk full")
            return append(event, **kw)

        monkeypatch.setattr(ctx.journal, "append", refuse)

        await engine.arm_targets(ctx)  # never fails FINALIZE

        assert not post.called

    async def test_a_resumed_finalize_writes_no_second_record(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db())
        ctx.target_uuids["db1"] = "dbT"
        _source_tags(ctx, db1=["prod"])
        respx_mock.post(f"{BASE}/databases/dbT/tags").mock(
            return_value=httpx.Response(201, json=[])
        )

        await engine.arm_targets(ctx)
        await engine.arm_targets(ctx)

        records = [r for r in ctx.journal.read() if TAGS_GIVEN in r.detail]
        assert len(records) == 1

    async def test_a_source_without_tags_journals_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_db())
        ctx.target_uuids["db1"] = "dbT"
        _source_tags(ctx, db1=[])
        await engine.arm_targets(ctx)
        assert engine.given_tags(ctx) == {}
        assert not respx_mock.calls


# ── DISARM_TARGETS detaches them ─────────────────────────────────────────────


def _given(**entries: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        TAGS_GIVEN: dt.serialise_given(
            {uuid: GivenTags(collection, tuple(names)) for uuid, (collection, names) in entries.items()}
        )
    }


class TestDetach:
    async def test_only_the_given_names_leave_and_a_replay_detaches_nothing(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        tags = {"t1": "prod", "t2": "manual"}  # "manual" was added after FINALIZE

        def listing(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_rows(*tags.items()))

        def detach(request: httpx.Request) -> httpx.Response:
            tag_uuid = request.url.path.rsplit("/", 1)[1]
            if tags.pop(tag_uuid, None) is None:
                return httpx.Response(404, json={"message": "Tag not found on resource."})
            return httpx.Response(200, json={"message": "Tag removed."})

        respx_mock.get(f"{BASE}/services/svcT/tags").mock(side_effect=listing)
        delete = respx_mock.delete(url__regex=rf"{BASE}/services/svcT/tags/.+").mock(
            side_effect=detach
        )
        undo = _given(svcT=("services", ["prod"]))

        assert await engine.disarm_targets(ctx, undo) == []
        assert tags == {"t2": "manual"}
        await engine.disarm_targets(ctx, undo)
        assert delete.call_count == 1

    async def test_a_target_that_is_gone_is_done(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/databases/dbT/tags").mock(
            return_value=httpx.Response(404, json={"message": "Database not found."})
        )
        delete = respx_mock.delete(url__regex=rf"{BASE}/databases/dbT/tags/.+")
        await engine.disarm_targets(ctx, _given(dbT=("databases", ["prod"])))
        assert not delete.called

    async def test_a_link_already_gone_is_done(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/databases/dbT/tags").mock(
            return_value=httpx.Response(200, json=_rows(("t1", "prod")))
        )
        respx_mock.delete(f"{BASE}/databases/dbT/tags/t1").mock(
            return_value=httpx.Response(404, json={"message": "Tag not found on resource."})
        )
        await engine.disarm_targets(ctx, _given(dbT=("databases", ["prod"])))

    async def test_failures_are_loud_named_and_the_others_still_run(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.plan = _plan(_app(), _db())
        ctx.target_uuids.update({"app1": "appT", "db1": "dbT"})
        respx_mock.get(f"{BASE}/applications/appT").mock(return_value=httpx.Response(500))
        respx_mock.get(f"{BASE}/applications/appT/tags").mock(return_value=httpx.Response(500))
        respx_mock.get(f"{BASE}/databases/dbT/tags").mock(
            return_value=httpx.Response(200, json=_rows(("t1", "prod")))
        )
        detach = respx_mock.delete(f"{BASE}/databases/dbT/tags/t1").mock(
            return_value=httpx.Response(200, json={"message": "Tag removed."})
        )
        undo = {
            ARM_REQUESTED: ["appT"],
            **_given(appT=("applications", ["prod"]), dbT=("databases", ["prod"])),
        }

        with pytest.raises(RuntimeError) as err:
            await engine.disarm_targets(ctx, undo)

        message = str(err.value)
        assert message.startswith("auto-deploy NOT switched off for: the target of shop (appT")
        assert "tags NOT removed from: the target of shop (appT: prod;" in message
        assert "a deploy by tag redeploys it" in message
        assert detach.called, "the database target's tag still went"


# ── the saga: a resumed FINALIZE that fails ──────────────────────────────────


class TestResumedFinalizeThatFails:
    async def test_the_automatic_rollback_starts_with_disarm_targets(
        self, ctx: MigrationContext
    ) -> None:
        journal = ctx.journal
        for state in states_through(State.HEALTHCHECK):
            journal.append("step_completed", state=state.value, detail={})
        # The first attempt journalled and armed, then was killed.
        journal.append("step_started", state="finalize", detail={"policy": "rename"})
        journal.append("step_started", state="finalize", detail={ARM_REQUESTED: ["appT"]})
        ran: list[Compensation] = []

        def recorder(comp: Compensation) -> Any:
            async def run(_ctx: MigrationContext, _undo: dict[str, Any]) -> None:
                ran.append(comp)

            return run

        async def finalize(_ctx: MigrationContext) -> dict[str, Any]:
            raise QuiesceError("could not read the source server's containers")

        saga = Saga(
            journal=journal,
            context=ctx,
            steps={State.FINALIZE: finalize},
            compensations={c: recorder(c) for c in Compensation},
        )
        result = await saga.run()

        assert result.outcome is Outcome.ROLLED_BACK
        assert ran[0] is Compensation.DISARM_TARGETS
        assert Compensation.RESTORE_SOURCE_NAME in ran
        assert ran.index(Compensation.DISARM_TARGETS) < ran.index(Compensation.STOP_TARGET)

    async def test_the_compensation_handler_detaches(
        self, ctx: MigrationContext, respx_mock: respx.Router
    ) -> None:
        ctx.journal.append(
            "step_started", state="finalize", detail=_given(dbT=("databases", ["prod"]))
        )
        respx_mock.get(f"{BASE}/databases/dbT/tags").mock(
            return_value=httpx.Response(200, json=_rows(("t1", "prod")))
        )
        detach = respx_mock.delete(f"{BASE}/databases/dbT/tags/t1").mock(
            return_value=httpx.Response(200, json={"message": "Tag removed."})
        )
        handler = compensations.build_compensations()[Compensation.DISARM_TARGETS]

        await handler(ctx, ctx.journal.undo_info("finalize"))

        assert detach.called
