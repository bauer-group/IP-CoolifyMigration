"""A containment read that gets no answer once START asked BLOCKS — it never rolls back.

After START the DNS gate has let the target start, users may have written to it,
and the rollback stops it and drops its volumes. A source daemon that did not
answer one read at FINALIZE's checkpoint — ``docker ps`` over a dropped SSH
channel, a ``docker events`` that exits non-zero — raised its own SshError or
QuiesceError there, which the executor answered with the whole saga:
STOP_TARGET, DROP_TARGET_VOLUMES, RESTART_SOURCE, DELETE_TARGET_RESOURCE. An
unread deployment history already blocked (``SourceUncheckedAfterStart``); every
other read now does too, journalling nothing. Before START the read's own error
still rolls the run back, as it always did. Every read of a check is asked even
when one fails, so a breach found beside a failed read is still reported first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import respx
from rich.console import Console

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.discovery import quiesce
from bg_coolify_migrate.domain import containment as c
from bg_coolify_migrate.domain.containment import (
    REVIVAL_SEEN_CLOCK,
    STOP_CLOCK,
    STOPPED_CLOCK,
    Unanswered,
)
from bg_coolify_migrate.domain.statemachine import START_REQUESTED, STOP_REQUESTED, Outcome, State
from bg_coolify_migrate.engine import compensations, containment, steps
from bg_coolify_migrate.engine.compensations import source_data_refusal
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.engine.executor import RunResult, Saga
from bg_coolify_migrate.errors import (
    QuiesceError,
    SourceRevived,
    SourceRevivedAfterStart,
    SourceUncheckedAfterStart,
)
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import CommandResult, SshError
from bg_coolify_migrate.ui.console import THEME
from bg_coolify_migrate.ui.run_report import outcome_panel
from tests.fake_daemon import FakeDaemon
from tests.test_engine_steps import HOST, _plan
from tests.test_pins_engine import COOLIFY, MID, VOL, _target

TVOL = "postgres-data-db2"
STOP = 1_800_000_000
SEEN = STOP + 600


class SshHiccup(FakeDaemon):
    """A daemon whose SSH channel fails (SshError) for commands containing ``needle``."""

    def __init__(self, needle: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.needle = needle

    async def run(
        self, command: str, *, timeout: float | None = None, input_text: str | None = None
    ) -> CommandResult:
        if self.needle in command:
            self.commands.append(command)
            raise SshError(f"failed to run on 10.0.0.1: {command[:60]}: channel open failed")
        return await super().run(command, timeout=timeout, input_text=input_text)


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


def _ctx(tmp_path: Path, api: CoolifyClient, source: FakeDaemon) -> MigrationContext:
    source.on(r"^date \+%s$", stdout=str(SEEN))
    ctx = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, MID),
        migration_id=MID,
        source_host=source,  # type: ignore[arg-type]
        target_host=_target(TVOL),  # type: ignore[arg-type]
    )
    ctx.target_uuids["db1"] = "db2"
    ctx.journal.append(
        "step_started",
        state=State.QUIESCE.value,
        detail={STOP_REQUESTED: True, STOP_CLOCK: STOP - 60, STOPPED_CLOCK: STOP},
    )
    return ctx


def _failing(needle: str, *, exit_fail: bool) -> FakeDaemon:
    if not exit_fail:
        return SshHiccup(needle, volumes=(VOL,))
    daemon = FakeDaemon(volumes=(VOL,))
    pattern = needle.replace(".", r"\.").replace("+", r"\+")
    daemon.on(pattern, stderr="Cannot connect to the Docker daemon", exit_status=1)
    return daemon


async def _run(ctx: MigrationContext, *, start_step: Any = None) -> RunResult:
    async def ok(_ctx: MigrationContext) -> dict[str, Any]:
        return {}

    async def create(_ctx: MigrationContext) -> dict[str, Any]:
        return {"target_uuids": {"db1": "db2"}}

    async def copy(_ctx: MigrationContext) -> dict[str, Any]:
        return {"volumes_copied": [TVOL], "key_fingerprint": None}

    async def start(_ctx: MigrationContext) -> dict[str, Any]:
        _ctx.journal.append(
            "step_started", state=State.START_TARGET.value, detail={START_REQUESTED: ["db2"]}
        )
        return {"started": ["db2"], "deployments": {}}

    saga = Saga(
        journal=ctx.journal,
        context=ctx,
        steps={
            State.CREATE_TARGET: create,
            State.QUIESCE: ok,
            State.DISCOVER: ok,
            State.COPY: copy,
            State.VERIFY: ok,
            State.DNS_GATE: ok,
            State.START_TARGET: start_step or start,
            State.HEALTHCHECK: ok,
            State.FINALIZE: steps.step_finalize,
        },
        compensations=compensations.build_compensations(),
        guard=source_data_refusal,  # as runner._execute wires it
    )
    return await saga.run()


def _src(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.source_host, FakeDaemon)
    return ctx.source_host


def _tgt(ctx: MigrationContext) -> FakeDaemon:
    assert isinstance(ctx.target_host, FakeDaemon)
    return ctx.target_host


def _assert_blocked_untouched(ctx: MigrationContext, result: RunResult) -> None:
    assert result.outcome is Outcome.BLOCKED, result.error
    assert isinstance(result.error, SourceUncheckedAfterStart)
    assert result.compensations_run == [] and result.compensations_failed == []
    assert TVOL in _tgt(ctx).volumes, "the target keeps what users wrote to it"
    events = [r.event for r in ctx.journal.read()]
    assert "blocked" in events and "rollback_started" not in events
    assert "policy" not in ctx.journal.undo_info(State.FINALIZE.value), "FINALIZE touched nothing"
    assert REVIVAL_SEEN_CLOCK not in ctx.journal.undo_info(State.QUIESCE.value), "nothing shown"


# ── the saga: FINALIZE's checkpoint, and a resumed START ────────────────────


class TestAfterStartAReadThatGetsNoAnswerBlocks:
    @pytest.mark.parametrize(
        ("needle", "exit_fail", "what"),
        [
            ("docker events", True, "its docker event log"),
            ("docker events", False, "its docker event log"),
            ("docker ps -a", False, "its containers under the stack's labels"),
            ("label=coolify.managed=true", True, "its Coolify-managed containers"),
        ],
        ids=["event-log-exit", "event-log-channel", "ps-channel", "managed-ps-exit"],
    )
    async def test_at_finalize(
        self,
        tmp_path: Path,
        api: CoolifyClient,
        respx_mock: respx.Router,
        needle: str,
        exit_fail: bool,
        what: str,
    ) -> None:
        respx_mock.route().respond(500, json={"message": "nothing may be asked"})
        ctx = _ctx(tmp_path, api, _failing(needle, exit_fail=exit_fail))

        result = await _run(ctx)

        _assert_blocked_untouched(ctx, result)
        message = str(result.error)
        assert message.startswith(
            "could not check that nothing started the stopped source (at FINALIZE)"
        )
        assert f"the source server 'old': {what} (" in message
        assert "Once the source server answers again: coolify-migrate resume m1" in message
        assert "does not roll back over a check it could not finish" in message
        assert not respx_mock.calls, "nothing was asked of Coolify"

    async def test_a_resumed_start_whose_event_log_read_fails(
        self, tmp_path: Path, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.route().respond(500, json={"message": "nothing may be asked"})
        ctx = _ctx(tmp_path, api, _failing("docker events", exit_fail=True))
        ctx.journal.append(
            "step_started", state=State.START_TARGET.value, detail={START_REQUESTED: ["db2"]}
        )

        result = await _run(ctx, start_step=steps.step_start_target)

        _assert_blocked_untouched(ctx, result)
        assert str(result.reached) == State.START_TARGET.value
        assert "(at START)" in str(result.error)
        assert not respx_mock.calls, "no target was asked to start again"

    async def test_the_panel_says_whom_to_wait_for(
        self, tmp_path: Path, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.route().respond(500, json={"message": "nothing may be asked"})
        ctx = _ctx(tmp_path, api, _failing("docker events", exit_fail=True))
        result = await _run(ctx)

        console = Console(width=200, force_terminal=False, no_color=True, theme=THEME)
        with console.capture() as capture:
            console.print(outcome_panel(result, migration_id=MID))
        out = capture.get()
        assert "the source could not be checked" in out
        assert "Resume once the source server answers again" in out
        assert "the source was started again" not in out

    async def test_the_resume_passes_once_the_daemon_answers(
        self, tmp_path: Path, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        ctx = _ctx(tmp_path, api, FakeDaemon(volumes=(VOL,)))
        _src(ctx).on(r"docker events", stderr="Cannot connect", exit_status=1)
        ctx.journal.append(
            "step_completed", state=State.START_TARGET.value, detail={"started": ["db2"]}
        )
        with pytest.raises(SourceUncheckedAfterStart):
            await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)
        _src(ctx)._routes.clear()  # the daemon answers again
        _src(ctx).on(r"^date \+%s$", stdout=str(SEEN))
        _src(ctx).events = f"oldest {STOP - 100}\n"
        await containment.assert_contained(ctx, checkpoint="FINALIZE", target=False)


class TestTheCheckpoint:
    @pytest.fixture
    def started(self, tmp_path: Path, api: CoolifyClient) -> MigrationContext:
        ctx = _ctx(tmp_path, api, FakeDaemon(volumes=(VOL,)))
        ctx.journal.append(
            "step_completed", state=State.START_TARGET.value, detail={"started": ["db2"]}
        )
        return ctx

    async def test_a_revival_found_beside_it_is_raised_first(
        self, started: MigrationContext
    ) -> None:
        _src(started).add("postgres-db1", labels=COOLIFY)
        _src(started).on(r"docker events", stderr="Cannot connect", exit_status=1)
        with pytest.raises(SourceRevivedAfterStart) as excinfo:
            await containment.assert_contained(started, checkpoint="FINALIZE", target=False)
        assert not isinstance(excinfo.value, SourceUncheckedAfterStart)
        message = str(excinfo.value)
        assert "postgres: postgres-db1 is running" in message
        assert "the source server 'old': its docker event log could not be read either" in message
        # Its starts were not shown: the resume must still judge from the stop.
        assert REVIVAL_SEEN_CLOCK not in started.journal.undo_info(State.QUIESCE.value)

    async def test_any_exception_of_a_read_blocks(
        self, started: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(*_a: Any, **_kw: Any) -> Any:
            raise ValueError("unparseable answer")

        monkeypatch.setattr(quiesce, "start_events", broken)
        with pytest.raises(SourceUncheckedAfterStart, match="unparseable answer"):
            await containment.assert_contained(started, checkpoint="FINALIZE", target=False)

    async def test_a_target_read_that_gets_no_answer_blocks_too(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        # START asked for another target, not yet for db2: db2 is still watched.
        ctx = _ctx(tmp_path, api, FakeDaemon(volumes=(VOL,)))
        _src(ctx).events = f"oldest {STOP - 100}\n"
        ctx.journal.append(
            "step_started", state=State.START_TARGET.value, detail={START_REQUESTED: ["db9"]}
        )
        _tgt(ctx).on(r"docker events", stderr="Cannot connect", exit_status=1)
        with pytest.raises(SourceUncheckedAfterStart) as excinfo:
            await containment.assert_contained(ctx, checkpoint="START")
        message = str(excinfo.value)
        assert message.startswith("could not check that nothing started an unstarted target")
        assert "the target server 'new': its docker event log" in message
        assert excinfo.value.resume_when == "once the target server answers again"


class TestBeforeStartTheRollbackStays:
    async def test_the_reads_own_error_is_raised(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        ctx = _ctx(tmp_path, api, _failing("docker ps -a", exit_fail=False))
        with pytest.raises(SshError, match="channel open failed") as excinfo:
            await containment.assert_contained(ctx, checkpoint="the end of VERIFY")
        assert not isinstance(excinfo.value, SourceRevivedAfterStart)

    async def test_an_event_log_that_cannot_be_read(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        ctx = _ctx(tmp_path, api, _failing("docker events", exit_fail=True))
        with pytest.raises(QuiesceError, match="could not read the docker event log") as excinfo:
            await containment.assert_contained(ctx, checkpoint="the DNS gate")
        assert not isinstance(excinfo.value, SourceRevivedAfterStart)

    async def test_a_breach_found_beside_it_is_named_first(
        self, tmp_path: Path, api: CoolifyClient
    ) -> None:
        ctx = _ctx(tmp_path, api, _failing("docker events", exit_fail=True))
        _src(ctx).add("postgres-db1", labels=COOLIFY)
        with pytest.raises(SourceRevived) as excinfo:
            await containment.assert_contained(ctx, checkpoint="the DNS gate")
        assert not isinstance(excinfo.value, SourceRevivedAfterStart)
        assert "rolls back now" in (excinfo.value.hint or "")
        assert "its docker event log could not be read either" in str(excinfo.value)


# ── the words ───────────────────────────────────────────────────────────────

LOG = Unanswered("source", "old", "its docker event log", "exit 1 - Cannot connect")
PS = Unanswered("target", "new", "its Coolify-managed containers", "channel open failed")


class TestTheWording:
    def test_histories_alone_keep_their_message(self) -> None:
        assert c.unchecked_message([], [("web", "502")], "FINALIZE") == (
            c.unread_history_message([("web", "502")], "FINALIZE")
        )

    def test_each_read_is_named_with_what_it_said(self) -> None:
        message = c.unchecked_message([LOG, PS], [("web", "502")], "START")
        assert message.splitlines() == [
            "could not check that nothing started the stopped source or an unstarted target "
            "(at START) - a server did not answer:",
            "  the source server 'old': its docker event log (exit 1 - Cannot connect)",
            "  the target server 'new': its Coolify-managed containers (channel open failed)",
            "  web: its deployment history could not be read either (502)",
        ]

    def test_beside_a_breach(self) -> None:
        (line,) = (b.describe() for b in c.unanswered_breaches([LOG]))
        assert line == (
            "the source server 'old': its docker event log could not be read either "
            "(exit 1 - Cannot connect)"
        )

    @pytest.mark.parametrize(
        ("unanswered", "unread", "when"),
        [
            ([], [("web", "x")], "once Coolify answers again"),
            ([LOG], [], "once the source server answers again"),
            ([LOG, PS], [], "once the source and target servers answer again"),
            ([LOG], [("web", "x")], "once the source server and Coolify answer again"),
        ],
    )
    def test_whom_the_resume_waits_for(
        self, unanswered: list[Unanswered], unread: list[tuple[str, str]], when: str
    ) -> None:
        assert c.resume_when(unanswered, unread) == when

    def test_the_hint_names_the_daemon_as_the_witness(self) -> None:
        hint = c.unchecked_after_start_hint(policy="delete", migration_id="m9", unanswered=[LOG])
        assert "Only a server's Docker daemon shows what started there" in hint
        assert "Once the source server answers again: coolify-migrate resume m9" in hint
        assert "Coolify said of" not in hint
        assert "coolify-migrate rollback m9" in hint
