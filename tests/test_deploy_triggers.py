"""The deploy switches of sources and targets: the pure decisions.

domain/deploy_triggers.py decides what QUIESCE switches off on a source, what a
rollback gives back, what FINALIZE arms on a target, and which queued
deployments block the stop. No mocks: every decision is a function of a GET.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain import coolify_versions as cv
from bg_coolify_migrate.domain import deploy_triggers as dt
from bg_coolify_migrate.domain.deploy_triggers import ActiveDeployment, Triggers
from bg_coolify_migrate.domain.statemachine import (
    ORDER,
    Compensation,
    State,
    rollback_plan,
    states_through,
)

ON = Triggers(auto_deploy=True, previews=False)
OFF = Triggers(auto_deploy=False, previews=False)
BOTH = Triggers(auto_deploy=True, previews=True)
UNREAD = Triggers(auto_deploy=None, previews=None)


def _app(**settings: Any) -> dict[str, Any]:
    return {"uuid": "app1", "settings": settings}


class TestReadTriggers:
    def test_reads_the_nested_settings(self) -> None:
        got = dt.read_triggers(
            _app(is_auto_deploy_enabled=True, is_preview_deployments_enabled=True)
        )
        assert got == BOTH

    def test_a_41_get_has_no_settings(self) -> None:
        # application_by_uuid eager-loads settings from v4.2.0 only (:2277).
        assert dt.read_triggers({"uuid": "app1"}) == UNREAD

    def test_a_flat_value_is_never_read(self) -> None:
        # The GET nests the settings; a flat key would be some other shape.
        assert dt.read_triggers({"is_auto_deploy_enabled": False}) == UNREAD

    def test_a_non_boolean_is_unreadable(self) -> None:
        assert dt.read_triggers(_app(is_auto_deploy_enabled=1)).auto_deploy is None


class TestDisarm:
    def test_auto_deploy_on_goes_off(self) -> None:
        assert dt.disarm_body(ON) == {"is_auto_deploy_enabled": False}

    def test_previews_on_go_off_too(self) -> None:
        assert dt.disarm_body(BOTH) == {
            "is_auto_deploy_enabled": False,
            "is_preview_deployments_enabled": False,
        }

    def test_nothing_on_means_nothing_to_send(self) -> None:
        assert dt.disarm_body(OFF) == {}

    def test_an_unreadable_auto_deploy_is_switched_off_anyway(self) -> None:
        # 4.1.x: a push revives the stopped source just the same. Previews are
        # never sent there: the update array refuses them before 4.2.
        assert dt.disarm_body(UNREAD) == {"is_auto_deploy_enabled": False}

    def test_a_resumed_quiesce_sends_only_what_is_still_on(self) -> None:
        half = Triggers(auto_deploy=False, previews=True)
        assert dt.disarm_body(BOTH, half) == {"is_preview_deployments_enabled": False}
        assert dt.disarm_body(BOTH, OFF) == {}


class TestRestore:
    def test_gives_back_what_the_disarm_took(self) -> None:
        assert dt.restore_body(BOTH, OFF) == {
            "is_auto_deploy_enabled": True,
            "is_preview_deployments_enabled": True,
        }

    def test_a_replay_sends_nothing(self) -> None:
        # `rollback <id>` replays every compensation.
        assert dt.restore_body(BOTH, BOTH) == {}

    def test_never_touches_what_this_run_did_not_switch(self) -> None:
        # The source had auto-deploy off; the operator switched it on since.
        assert dt.restore_body(OFF, ON) == {}
        assert dt.restore_body(OFF, Triggers(auto_deploy=True, previews=True)) == {}

    def test_an_unreadable_original_comes_back_as_coolifys_default(self) -> None:
        assert dt.restore_body(UNREAD, UNREAD) == {"is_auto_deploy_enabled": True}

    def test_only_the_missing_part_is_sent(self) -> None:
        assert dt.restore_body(BOTH, Triggers(auto_deploy=True, previews=False)) == {
            "is_preview_deployments_enabled": True
        }


class TestTargets:
    def test_created_with_auto_deploy_off(self) -> None:
        assert dict(dt.TARGET_CREATE) == {"is_auto_deploy_enabled": False}

    @pytest.mark.parametrize(
        ("original", "armed"), [(ON, True), (OFF, False), (BOTH, True), (UNREAD, True)]
    )
    def test_armed_with_the_sources_original(self, original: Triggers, armed: bool) -> None:
        # Unreadable (4.1.x): Coolify's default, what every target got before.
        assert dt.target_arm_body(original) == {"is_auto_deploy_enabled": armed}


class TestJournal:
    def test_round_trip(self) -> None:
        originals = {"app1": BOTH, "app2": UNREAD}
        raw = dt.serialise(originals)
        assert raw == {
            "app1": {"auto_deploy": True, "previews": True},
            "app2": {"auto_deploy": None, "previews": None},
        }
        assert dt.deserialise(raw) == originals

    def test_tolerates_garbage(self) -> None:
        assert dt.deserialise(None) == {}
        assert dt.deserialise(["app1"]) == {}
        assert dt.deserialise({"app1": "on", 3: {}}) == {}

    def test_the_journal_accepts_it(self, tmp_path: Any) -> None:
        # No forbidden key, no secret shape: booleans and uuids only.
        from bg_coolify_migrate.journal.store import Journal

        journal = Journal.create(tmp_path, "m1")
        journal.append(
            "step_started", state="quiesce", detail={dt.JOURNAL_KEY: dt.serialise({"a": BOTH})}
        )
        assert dt.deserialise(journal.undo_info("quiesce")[dt.JOURNAL_KEY]) == {"a": BOTH}


class TestJournalledTags:
    def test_round_trip_through_the_journal(self, tmp_path: Any) -> None:
        from bg_coolify_migrate.journal.store import Journal

        journal = Journal.create(tmp_path, "m1")
        names = {"app1": ["prod", "ci-deploy"], "db1": []}
        journal.append("step_started", state="create_target", detail={dt.TAGS_JOURNAL_KEY: names})
        raw = journal.undo_info("create_target")[dt.TAGS_JOURNAL_KEY]
        assert dt.deserialise_tags(raw) == names

    def test_an_empty_list_is_known_and_a_malformed_entry_is_not(self) -> None:
        # Known-empty means "add nothing"; missing means "ask the source again".
        raw = {"app1": [], "app2": "prod", 3: ["x"], "db1": ["prod", 7]}
        assert dt.deserialise_tags(raw) == {"app1": [], "db1": ["prod"]}

    def test_no_record_is_no_names(self) -> None:
        assert dt.deserialise_tags(None) == {}
        assert dt.deserialise_tags(["prod"]) == {}


URL = "/project/p1/environment/e1/application/{app}/deployment/{dep}"


def _row(dep: str, app: str, status: str = "queued", **extra: Any) -> dict[str, Any]:
    return {
        "deployment_uuid": dep,
        "status": status,
        "deployment_url": URL.format(app=app, dep=dep),
        "logs": '[{"output": "DB_PASSWORD=hunter2"}]',
        "application_id": "42",
        **extra,
    }


class TestDeploymentQueue:
    def test_the_application_comes_from_the_url(self) -> None:
        assert dt.application_uuid_in(URL.format(app="app1", dep="d1")) == "app1"
        assert dt.application_uuid_in("https://x" + URL.format(app="a", dep="d")) == "a"
        assert dt.application_uuid_in("/project/p/environment/e/service/s1") is None
        assert dt.application_uuid_in(None) is None

    def test_rows_are_reduced_and_lose_their_logs(self) -> None:
        rows = dt.read_active_deployments([_row("d1", "app1", "in_progress")])
        assert rows == (ActiveDeployment(uuid="d1", status="in_progress", application_uuid="app1"),)
        assert "hunter2" not in repr(rows)

    def test_an_object_keyed_by_position_is_read_too(self) -> None:
        # ->sortBy('id') keeps the keys: once a row moved, Laravel answers an object.
        rows = dt.read_active_deployments({"1": _row("d2", "app2"), "0": _row("d1", "app1")})
        assert {r.uuid for r in rows} == {"d1", "d2"}

    def test_garbage_is_nothing(self) -> None:
        assert dt.read_active_deployments(None) == ()
        assert dt.read_active_deployments(["x", 3]) == ()

    def test_only_the_sources_queued_or_running_rows_count(self) -> None:
        rows = dt.read_active_deployments(
            [
                _row("d1", "app1", "queued"),
                _row("d2", "app1", "finished"),
                _row("d3", "other", "in_progress"),
                {"deployment_uuid": "d4", "status": "queued"},
                _row("d5", "app2", "in_progress"),
            ]
        )
        pending = dt.pending_for(rows, {"app1", "app2"})
        assert [d.uuid for d in pending] == ["d1", "d5"]


class TestNotes:
    def test_the_run_note_names_what_still_deploys(self) -> None:
        note = dt.run_note()
        assert "POST /deploy" in note and "pause them from QUIESCE until FINALIZE" in note
        assert "keep or rename" in note

    def test_the_run_note_says_what_a_revival_does_before_and_after_start(self) -> None:
        # engine/containment.py: a rollback before START asked for a target, a
        # block once it has (errors.SourceRevivedAfterStart) - never a rollback then.
        note = dt.run_note()
        assert "before START has asked Coolify to start a target rolls the run back" in note
        assert "by a resumed START or by FINALIZE - BLOCKS the run and rolls nothing back" in note
        assert "stop the source and resume, or roll back by hand" in note
        assert "a source deployed during the run rolls the run back" not in note

    def test_the_run_note_matches_what_the_checkpoints_tell_the_operator(self) -> None:
        # If containment ever changes which side of START rolls back, this note
        # must change with it.
        from bg_coolify_migrate.domain import containment

        assert "The run rolls back now" in containment.source_hint()
        after = containment.revived_after_start_hint(policy="keep", migration_id="m1")
        assert "does not roll back by itself" in after and "coolify-migrate resume m1" in after

    def test_a_41_source_is_told_its_setting_is_unreadable(self) -> None:
        [line] = dt.application_warnings({"uuid": "app1"})
        assert "before 4.2" in line and "Coolify's default" in line

    def test_previews_on_are_named(self) -> None:
        [line] = dt.application_warnings(
            _app(is_auto_deploy_enabled=True, is_preview_deployments_enabled=True)
        )
        assert line.startswith("preview deployments are on")

    def test_a_plain_source_is_not_warned_about(self) -> None:
        assert dt.application_warnings(_app(is_auto_deploy_enabled=True)) == []
        assert dt.application_warnings(_app(is_auto_deploy_enabled=False)) == []

    def test_the_kept_source_notice_names_the_way_back(self) -> None:
        notice = dt.kept_source_notice(["shop", "api"], migration_id="m1")
        assert notice.startswith("shop, api (the kept source)")
        assert "coolify-migrate rollback m1" in notice and "POST /deploy" in notice

    def test_the_kept_source_notice_says_nothing_checks_it_any_more(self) -> None:
        # FINALIZE's checkpoint is the last: a deploy of the kept source after it
        # neither rolls back nor blocks anything.
        notice = dt.kept_source_notice(["shop"], migration_id="m1")
        assert "nothing checks for that any more" in notice
        assert "rolled the run back before START had asked for a target" in notice
        assert "blocked it once START had" in notice
        assert "switches them back on, once" in notice

    def test_a_failed_arm_says_what_to_set(self) -> None:
        on = dt.arm_failed_notice("shop", "appT", True, reason="HTTP 500")
        off = dt.arm_failed_notice("shop", "appT", False, reason="HTTP 500")
        assert "switch 'Auto Deploy' on" in on and "will not deploy it" in on
        assert "switch 'Auto Deploy' off" in off and "may deploy it" in off


class TestRegistry:
    """Every field through the release registry (AGENTS.md)."""

    def test_every_application_route_creates_with_it(self) -> None:
        for route in cv.APPLICATION_ROUTES:
            assert cv.carries(f"applications/{route}", "is_auto_deploy_enabled", (4, 1, 2))
            assert cv.carries(f"applications/{route}", "is_auto_deploy_enabled", None)

    def test_the_settings_patch_takes_auto_deploy_everywhere(self) -> None:
        for version in ((4, 1, 2), (4, 2, 0), (4, 7, 2), None):
            assert cv.carries("applications.settings", "is_auto_deploy_enabled", version)

    def test_previews_only_from_42(self) -> None:
        # Not in the update array at v4.1.2:2368 or v4.1.3: a 422 there.
        assert not cv.carries("applications.settings", "is_preview_deployments_enabled", (4, 1, 3))
        assert cv.carries("applications.settings", "is_preview_deployments_enabled", (4, 2, 0))
        # Only ever sent where it was READ, i.e. where settings exist (4.2.0+).
        assert cv.carries("applications.settings", "is_preview_deployments_enabled", None)

    def test_nothing_else_rides_the_settings_patch(self) -> None:
        assert cv.known_fields("applications.settings") == {
            "is_auto_deploy_enabled",
            "is_preview_deployments_enabled",
        }


class TestRollbackOrder:
    """RESTORE_SOURCE_DEPLOY_TRIGGERS: after the domains, before the restart."""

    def _comps(self, state: State) -> list[Compensation]:
        return [s.compensation for s in rollback_plan(list(states_through(state)))]

    def test_owed_from_create_target(self) -> None:
        plan = rollback_plan(list(states_through(State.CREATE_TARGET)))
        owed = [s for s in plan if s.compensation is Compensation.RESTORE_SOURCE_DEPLOY_TRIGGERS]
        assert [s.because_of for s in owed] == [State.CREATE_TARGET.value]

    def test_without_a_restart_it_still_runs_before_the_unpin(self) -> None:
        assert self._comps(State.CREATE_TARGET) == [
            Compensation.DELETE_TARGET_RESOURCE,
            Compensation.RESTORE_SOURCE_FQDN,
            Compensation.RESTORE_SOURCE_DEPLOY_TRIGGERS,
            Compensation.RELEASE_SOURCE_PINS,
        ]

    def test_full_rollback_order(self) -> None:
        # The targets FINALIZE armed are disarmed before anything else runs.
        assert self._comps(State.FINALIZE) == [
            Compensation.DISARM_TARGETS,
            Compensation.RESTORE_SOURCE_NAME,
            Compensation.STOP_TARGET,
            Compensation.DROP_TARGET_VOLUMES,
            Compensation.REVOKE_EPHEMERAL_KEY,
            Compensation.RESTORE_SOURCE_FQDN,
            Compensation.RESTORE_SOURCE_DEPLOY_TRIGGERS,
            Compensation.RESTART_SOURCE,
            Compensation.DELETE_TARGET_RESOURCE,
            Compensation.RELEASE_SOURCE_PINS,
        ]

    @pytest.mark.parametrize("state", ORDER[ORDER.index(State.QUIESCE) : ORDER.index(State.DONE)])
    def test_between_the_domains_and_the_restart_for_every_prefix(self, state: State) -> None:
        comps = self._comps(state)
        triggers = comps.index(Compensation.RESTORE_SOURCE_DEPLOY_TRIGGERS)
        assert comps.index(Compensation.RESTORE_SOURCE_FQDN) == triggers - 1
        assert comps.index(Compensation.RESTART_SOURCE) == triggers + 1
