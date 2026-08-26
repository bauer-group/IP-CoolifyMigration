"""Tests for the interactive wizard.

The typed-confirmation test is the important one: a yes/no prompt is one
keystroke from muscle memory, and `--finalize delete` is the only irreversible
thing this tool does.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain.compose import MountClass
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import Decision, VolumeItem, VolumeManifest
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.ui import wizard
from bg_coolify_migrate.ui.wizard import (
    Cancelled,
    choose_environment,
    choose_finalize_policy,
    choose_project,
    choose_scope_environment,
    choose_scope_resource,
    choose_server,
    confirm_destructive,
    confirm_plan,
)


class FakePrompt:
    """Stands in for a questionary prompt object."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer

    def ask(self) -> Any:
        return self._answer


def _patch(monkeypatch: pytest.MonkeyPatch, name: str, answer: Any) -> list[dict[str, Any]]:
    """Replace one questionary constructor; records the kwargs it was called with."""
    calls: list[dict[str, Any]] = []

    def factory(*args: Any, **kwargs: Any) -> FakePrompt:
        calls.append({"args": args, "kwargs": kwargs})
        return FakePrompt(answer)

    monkeypatch.setattr(wizard.questionary, name, factory)
    return calls


def _plan(*, blocked: bool = False, policy: FinalizePolicy = FinalizePolicy.RENAME) -> MigrationPlan:
    manifest = (
        VolumeManifest(
            items=(
                VolumeItem(
                    mount_class=MountClass.ANONYMOUS,
                    decision=Decision.REFUSE,
                    reason="anonymous volume",
                    source_path="/x",
                    mount_path="/data",
                ),
            )
        )
        if blocked
        else VolumeManifest()
    )
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        resources=(
            ResourcePlan(
                snapshot=ResourceSnapshot(
                    uuid="db1", name="postgres", collection="databases", kind=ResourceKind.DATABASE
                ),
                strategy=Strategy.COPY_DATA,
                manifest=manifest,
            ),
        ),
        finalize_policy=policy,
    )


class TestChooseServer:
    def test_returns_the_uuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "select", "s2")
        servers = [{"uuid": "s1", "name": "old", "ip": "10.0.0.1"}]
        assert choose_server(servers, message="To?") == "s2"

    def test_excludes_the_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # You cannot migrate a project onto the server it already lives on.
        calls = _patch(monkeypatch, "select", "s2")
        servers = [
            {"uuid": "s1", "name": "old", "ip": "10.0.0.1"},
            {"uuid": "s2", "name": "new", "ip": "10.0.0.2"},
        ]
        choose_server(servers, message="To?", exclude="s1")
        titles = [c.title for c in calls[0]["kwargs"]["choices"]]
        assert not any("old" in t for t in titles)

    def test_no_eligible_servers_cancels(self) -> None:
        with pytest.raises(Cancelled):
            choose_server([{"uuid": "s1"}], message="To?", exclude="s1")

    def test_ctrl_c_cancels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "select", None)
        with pytest.raises(Cancelled):
            choose_server([{"uuid": "s1", "name": "a", "ip": "1.1.1.1"}], message="To?")


class TestChooseProject:
    def test_returns_the_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "select", "shop")
        assert choose_project([{"uuid": "p1", "name": "shop"}]) == "shop"

    def test_no_projects_cancels(self) -> None:
        with pytest.raises(Cancelled):
            choose_project([])


class TestChooseEnvironment:
    def test_no_environments_defaults_to_production(self) -> None:
        assert choose_environment([]) == "production"

    def test_single_environment_is_not_asked_about(self) -> None:
        # Do not make someone press enter for a choice of one.
        assert choose_environment(["staging"]) == "staging"

    def test_several_are_offered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "select", "staging")
        assert choose_environment(["production", "staging"]) == "staging"


class TestSelectionIsOrdered:
    """Every menu is sorted, because the API's answer is not.

    Coolify issues no ORDER BY, so rows come back in whatever order the query
    planner produced - and that changes as rows are updated. An unsorted menu is
    therefore reshuffled between runs and the entry you want is never twice in the
    same place. Reported 2026-08-26.
    """

    def test_projects_are_offered_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch(monkeypatch, "select", "x")
        choose_project(
            [
                {"uuid": "p3", "name": "11.0 Production Service"},
                {"uuid": "p1", "name": "2.0 Alpha"},
                {"uuid": "p2", "name": "01 Legacy"},
                {"uuid": "p4", "name": "shop"},
            ]
        )
        titles = [c.title for c in calls[0]["kwargs"]["choices"]]
        # Digit-aware: '2.0' before '11.0'. Plain lexicographic order inverts those
        # two, which reads as unsorted to anyone whose projects are numbered.
        assert titles == ["01 Legacy", "2.0 Alpha", "11.0 Production Service", "shop"]

    def test_servers_are_offered_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch(monkeypatch, "select", "s1")
        choose_server(
            [
                {"uuid": "s2", "name": "0047-20", "ip": "10.0.0.2"},
                {"uuid": "s3", "name": "0100-20", "ip": "10.0.0.3"},
                {"uuid": "s1", "name": "0046-20", "ip": "10.0.0.1"},
            ],
            message="Target server?",
        )
        titles = [c.title for c in calls[0]["kwargs"]["choices"]]
        assert [t.split()[0] for t in titles] == ["0046-20", "0047-20", "0100-20"]

    def test_environments_are_offered_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch(monkeypatch, "select", "production")
        choose_environment(["staging", "production", "development"])
        assert list(calls[0]["kwargs"]["choices"]) == ["development", "production", "staging"]

    def test_scope_resources_are_sorted_under_a_pinned_sentinel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch(monkeypatch, "select", "x")
        choose_scope_resource(
            [
                ("applications", {"name": "web", "uuid": "u1"}),
                ("databases", {"name": "postgres", "uuid": "u2"}),
                ("applications", {"name": "api", "uuid": "u3"}),
            ]
        )
        titles = [c.title for c in calls[0]["kwargs"]["choices"]]
        # The sentinel is an action, not a name: it stays put rather than sorting
        # among them, so it does not move around either.
        assert titles[0].startswith("- all resources")
        assert titles[1:] == ["api", "postgres", "web"]

    def test_scope_environments_are_sorted_above_a_pinned_sentinel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch(monkeypatch, "select", "x")
        choose_scope_environment(["staging", "production"])
        titles = [c.title for c in calls[0]["kwargs"]["choices"]]
        assert titles[:-1] == ["production", "staging"]
        assert titles[-1].startswith("- all environments")


class TestChooseFinalizePolicy:
    def test_returns_the_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "select", FinalizePolicy.KEEP)
        assert choose_finalize_policy() is FinalizePolicy.KEEP

    def test_reversible_option_is_offered_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch(monkeypatch, "select", FinalizePolicy.RENAME)
        choose_finalize_policy()
        choices = calls[0]["kwargs"]["choices"]
        assert choices[0].value is FinalizePolicy.RENAME
        assert "reversible" in choices[0].title

    def test_delete_is_described_honestly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch(monkeypatch, "select", FinalizePolicy.RENAME)
        choose_finalize_policy()
        delete = next(
            c for c in calls[0]["kwargs"]["choices"] if c.value is FinalizePolicy.DELETE
        )
        assert "IRREVERSIBLE" in delete.title


class TestConfirmPlan:
    def test_accepts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "confirm", True)
        assert confirm_plan(_plan()) is True

    def test_declines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "confirm", False)
        assert confirm_plan(_plan()) is False

    def test_defaults_to_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Enter must never start a migration.
        calls = _patch(monkeypatch, "confirm", True)
        confirm_plan(_plan())
        assert calls[0]["kwargs"]["default"] is False

    def test_blocked_plan_is_never_offered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch(monkeypatch, "confirm", True)
        assert confirm_plan(_plan(blocked=True)) is False
        assert calls == []  # never even asked


class TestConfirmDestructive:
    def test_non_destructive_policies_need_no_typing(self) -> None:
        assert confirm_destructive(_plan(policy=FinalizePolicy.RENAME)) is True
        assert confirm_destructive(_plan(policy=FinalizePolicy.KEEP)) is True

    def test_correct_name_confirms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "text", "shop")
        assert confirm_destructive(_plan(policy=FinalizePolicy.DELETE)) is True

    def test_wrong_name_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The speed bump: it proves the operator knows WHICH project they are
        # about to destroy, not just that they can press y.
        _patch(monkeypatch, "text", "wrong-project")
        assert confirm_destructive(_plan(policy=FinalizePolicy.DELETE)) is False

    def test_whitespace_is_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "text", "  shop  ")
        assert confirm_destructive(_plan(policy=FinalizePolicy.DELETE)) is True

    def test_empty_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, "text", "")
        assert confirm_destructive(_plan(policy=FinalizePolicy.DELETE)) is False
