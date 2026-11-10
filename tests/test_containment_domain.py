"""The containment rules: did anything start a stack the run holds still? PURE.

Between QUIESCE and FINALIZE the source must stay stopped and the target
unstarted, and nothing in Coolify keeps them so: a git push with auto-deploy
on, a CI ``/deploy`` by uuid or tag, a dashboard deploy. These tests pin what
counts as evidence — a container running now, one whose StartedAt is after the
stop, a ``start`` in the daemon's event log — and what does not.
"""

from __future__ import annotations

import json

import pytest

from bg_coolify_migrate.domain import containment as c
from bg_coolify_migrate.domain.statemachine import (
    START_REQUESTED,
    STARTED_EARLY,
    STOP_REQUESTED,
    State,
    owed_states,
    rollback_plan,
    target_may_run,
)

STACK = {"coolify.projectName": "shop", "coolify.environmentName": "production"}
TRIPLE = {**STACK, "coolify.resourceName": "postgres"}
SRC = c.Watched(name="postgres", uuid="db1aaaaaaaaaaaaaaaaaaaaa", labels=TRIPLE)
TGT = c.Watched(name="postgres", uuid="db2bbbbbbbbbbbbbbbbbbbbb", labels=TRIPLE)
STOP = 1_800_000_000  # the source's clock just before the stop


def _event(name: str, when: int, **attributes: str) -> str:
    return json.dumps(
        {
            "status": "start",
            "Type": "container",
            "Action": "start",
            "Actor": {"ID": "x", "Attributes": {"name": name, "image": "img", **attributes}},
            "time": when,
        }
    )


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2027-01-15T08:00:00Z", 1_800_000_000),
            ("2027-01-15T08:00:00.123456789Z", 1_800_000_000),
            ("2027-01-15T09:00:00+01:00", 1_800_000_000),
            ("2027-01-15T08:00:00", 1_800_000_000),
            ("0001-01-01T00:00:00Z", None),  # docker's zero time: never started
            ("", None),
            ("yesterday", None),
            (None, None),
        ],
    )
    def test_docker_times(self, raw: object, expected: int | None) -> None:
        assert c.parse_docker_time(raw) == expected

    def test_started_at_from_inspect(self) -> None:
        state = json.dumps({"Status": "exited", "StartedAt": "2027-01-15T08:00:00.5Z"})
        assert c.started_at_of(state) == STOP
        assert c.started_at_of("not json") is None
        assert c.started_at_of("[]") is None
        assert c.started_at_of('{"Status": "exited"}') is None

    def test_the_event_log(self) -> None:
        stdout = "\n".join(
            [
                "oldest 1799999000",
                _event("db1aaaaaaaaaaaaaaaaaaaaa", STOP + 5, **TRIPLE),
                json.dumps({"Type": "container", "Action": "die", "time": STOP}),
                json.dumps({"Type": "network", "Action": "start", "time": STOP}),
                json.dumps({"Type": "container", "Action": "start", "time": "soon"}),
                "garbage",
                "{not json",
                "[1]",
            ]
        )
        log = c.parse_event_log(stdout)
        assert log.oldest == 1_799_999_000
        [start] = log.starts
        assert start.name == "db1aaaaaaaaaaaaaaaaaaaaa"
        assert start.time == STOP + 5
        assert start.attributes["coolify.resourceName"] == "postgres"

    def test_an_event_log_without_events(self) -> None:
        assert c.parse_event_log("") == c.EventLog()
        assert c.parse_event_log("oldest \n").oldest is None



class TestTheSource:
    def test_a_running_container_is_a_revival(self) -> None:
        seen = [c.Seen(name="postgres-db1aaaaaaaaaaaaaaaaaaaaa", state="running", labels={})]
        [breach] = c.source_breaches([SRC], seen, c.EventLog(), since=STOP, stack=STACK)
        assert breach.resource == "postgres"
        assert breach.describe().endswith("is running")

    def test_one_started_after_the_stop_and_stopped_again(self) -> None:
        seen = [c.Seen(name="x", state="exited", labels=TRIPLE, started_at=STOP + 60)]
        [breach] = c.source_breaches([SRC], seen, None, since=STOP, stack=STACK)
        assert "after the stop, and has stopped again" in breach.why
        assert "2027-01-15 08:01:00 UTC" in breach.why

    def test_one_that_last_ran_before_the_stop_is_not(self) -> None:
        # An exited one-shot left from before the migration: it cannot have written since.
        seen = [c.Seen(name="x", state="exited", labels=TRIPLE, started_at=STOP - 3600)]
        assert c.source_breaches([SRC], seen, None, since=STOP, stack=STACK) == []

    def test_without_the_stop_clock_only_running_counts(self) -> None:
        # A journal written before the record: StartedAt and events cannot be judged.
        seen = [
            c.Seen(name="a", state="exited", labels=TRIPLE, started_at=STOP + 60),
            c.Seen(name="b", state="restarting", labels=TRIPLE),
        ]
        events = c.EventLog(starts=(c.StartEvent("c", STOP + 1, TRIPLE),))
        breaches = c.source_breaches([SRC], seen, events, since=None, stack=STACK)
        assert [b.container for b in breaches] == ["b"]

    def test_one_that_came_and_went_is_in_the_event_log(self) -> None:
        # A deploy whose health check failed removes its container: only the log saw it.
        events = c.EventLog(
            starts=(
                c.StartEvent("web-db1aaaaaaaaaaaaaaaaaaaaa-1", STOP + 9, {}),
                c.StartEvent("before", STOP - 1, TRIPLE),
                c.StartEvent("unrelated", STOP + 9, {"coolify.projectName": "other"}),
            )
        )
        [breach] = c.source_breaches([SRC], [], events, since=STOP, stack=STACK)
        assert breach.container == "web-db1aaaaaaaaaaaaaaaaaaaaa-1"
        assert "event log" in breach.why

    def test_a_renamed_stack_is_found_by_its_uuid(self) -> None:
        # Renamed after PLAN, then deployed: the NEW names are on the container.
        renamed = {"coolify.projectName": "shop-2", "coolify.name": "db1aaaaaaaaaaaaaaaaaaaaa"}
        seen = [c.Seen(name="whatever", state="running", labels=renamed)]
        [breach] = c.source_breaches([SRC], seen, None, since=STOP, stack=STACK)
        assert breach.resource == "postgres"

    def test_the_stack_s_other_containers_count_too(self) -> None:
        events = c.EventLog(starts=(c.StartEvent("sidecar", STOP, STACK),))
        [breach] = c.source_breaches([SRC], [], events, since=STOP, stack=STACK)
        assert breach.resource == "the stack"
        seen = [c.Seen(name="debug", state="running", labels={})]  # mounts a migrated volume
        [breach] = c.source_breaches([SRC], seen, None, since=STOP, stack=STACK)
        assert breach.resource == "the stack"

    def test_a_container_is_reported_once(self) -> None:
        seen = [c.Seen(name="x", state="running", labels=TRIPLE)]
        events = c.EventLog(starts=(c.StartEvent("x", STOP + 1, TRIPLE),))
        assert len(c.source_breaches([SRC], seen, events, since=STOP, stack=STACK)) == 1


class TestTheTarget:
    def test_a_running_target_is_an_early_start(self) -> None:
        seen = [c.Seen(name="db2bbbbbbbbbbbbbbbbbbbbb", state="running", labels={})]
        [breach] = c.target_breaches([TGT], seen, None)
        assert breach.uuid == TGT.uuid
        assert breach.why == "is running"

    def test_any_start_of_its_uuid_counts_whenever_it_was(self) -> None:
        # The uuid did not exist before CREATE: no start of it is anyone's but an early one.
        seen = [c.Seen(name="db2bbbbbbbbbbbbbbbbbbbbb", state="exited", labels={}, started_at=5)]
        [breach] = c.target_breaches([TGT], seen, None)
        assert "has stopped again" in breach.why

    def test_a_name_label_match_counts_only_while_it_runs(self) -> None:
        # Names are not unique the way a uuid is: a stopped namesake proves nothing.
        stopped = [c.Seen(name="other", state="exited", labels=TRIPLE, started_at=5)]
        assert c.target_breaches([TGT], stopped, None) == []
        running = [c.Seen(name="other", state="running", labels=TRIPLE)]
        assert len(c.target_breaches([TGT], running, None)) == 1

    def test_a_never_started_one_is_not(self) -> None:
        seen = [c.Seen(name="db2bbbbbbbbbbbbbbbbbbbbb", state="created", labels={})]
        assert c.target_breaches([TGT], seen, None) == []

    def test_one_that_came_and_went(self) -> None:
        events = c.EventLog(
            starts=(
                c.StartEvent("x", 5, {"com.docker.compose.project": TGT.uuid}),
                c.StartEvent("namesake", 5, TRIPLE),  # by labels only: not evidence
            )
        )
        [breach] = c.target_breaches([TGT], [], events)
        assert breach.container == "x" and breach.uuid == TGT.uuid

    def test_unrelated_containers_are_ignored(self) -> None:
        seen = [c.Seen(name="proxy", state="running", labels={"coolify.managed": "true"})]
        assert c.target_breaches([TGT], seen, c.EventLog()) == []


class TestTheEventLogWindow:
    def test_complete_when_it_holds_an_event_older_than_the_stop(self) -> None:
        assert c.event_log_gap(c.EventLog(oldest=STOP - 1), STOP) is None

    def test_a_gap_when_its_oldest_event_is_newer(self) -> None:
        # The daemon keeps 256 events: it dropped what came before.
        assert c.event_log_gap(c.EventLog(oldest=STOP + 100), STOP) == STOP + 100

    @pytest.mark.parametrize(
        ("log", "since"), [(None, STOP), (c.EventLog(oldest=STOP + 1), None), (c.EventLog(), STOP)]
    )
    def test_nothing_to_say(self, log: c.EventLog | None, since: int | None) -> None:
        assert c.event_log_gap(log, since) is None


class TestMessages:
    def test_names_the_checkpoint_and_each_container(self) -> None:
        message = c.breach_message(
            "the source was started again", "the DNS gate", [c.Breach("postgres", "x", "is running")]
        )
        assert message == "the source was started again (found at the DNS gate):\n  postgres: x is running"

    def test_the_source_hint_names_the_causes_and_what_happens_next(self) -> None:
        hint = c.source_hint()
        for cause in ("auto-deploy", "/deploy", "dashboard", "queued or building"):
            assert cause in hint
        assert "rolls back" in hint and "what it wrote survives" in hint
        assert "serving since START" not in hint
        assert "serving since START" in c.source_hint(target_serving=True)

    def test_the_target_hint(self) -> None:
        assert "same push to both" in c.TARGET_HINT
        assert "ACME" in c.TARGET_HINT and "rolls back" in c.TARGET_HINT


class TestStopTargetIsOwedBeforeStartCompletes:
    """STOP_TARGET once a target may run — not only once START completed."""

    def test_completed_start(self) -> None:
        assert target_may_run([State.START_TARGET.value], {})

    def test_a_start_asked_for(self) -> None:
        assert target_may_run([], {START_REQUESTED: ["t1"]})

    def test_a_target_someone_else_started(self) -> None:
        assert target_may_run([], {STARTED_EARLY: ["t1"]})

    def test_never(self) -> None:
        assert not target_may_run([], {})
        assert not target_may_run([], {START_REQUESTED: []})

    def test_owed_by_a_failing_start_before_the_source_restart(self) -> None:
        completed = [s.value for s in (State.CREATE_TARGET, State.QUIESCE, State.DISCOVER)]
        markers = {
            State.QUIESCE.value: {STOP_REQUESTED: True},
            State.START_TARGET.value: {START_REQUESTED: ["t1"]},
        }
        owed = owed_states([*completed, "copy", "verify", "dns_gate"], markers=markers)
        assert State.START_TARGET.value in owed
        plan = [s.compensation.value for s in rollback_plan([State(s) for s in owed])]
        # The target stops FIRST, before the source gets its domains back and restarts.
        assert plan[0] == "stop_target"
        assert plan.index("stop_target") < plan.index("restart_source")

    def test_owed_by_an_early_start_found_at_copy(self) -> None:
        markers = {State.START_TARGET.value: {STARTED_EARLY: ["t1"]}}
        assert State.START_TARGET.value in owed_states(["create_target"], markers=markers)

    def test_not_owed_without_a_marker(self) -> None:
        assert State.START_TARGET.value not in owed_states(["create_target"], markers={})
