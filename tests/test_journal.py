"""Tests for the crash-safe journal.

The secret-refusal and truncation tests matter most: the first stops a
credential reaching disk, the second is what a power cut mid-write actually looks
like.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_coolify_migrate.journal.store import (
    SCHEMA_VERSION,
    Journal,
    JournalError,
    default_state_dir,
    list_migrations,
)


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal.create(tmp_path, "mig-001")


class TestAppendAndRead:
    def test_roundtrip(self, journal: Journal) -> None:
        journal.append("started", state="init", detail={"project": "shop"})
        (record,) = list(journal.read())
        assert record.event == "started"
        assert record.state == "init"
        assert record.detail == {"project": "shop"}
        assert record.schema_version == SCHEMA_VERSION

    def test_sequence_increments(self, journal: Journal) -> None:
        journal.append("a")
        journal.append("b")
        assert [r.seq for r in journal.read()] == [1, 2]

    def test_sequence_survives_reopen(self, tmp_path: Path) -> None:
        # A resumed run must not restart numbering and collide with prior records.
        first = Journal.create(tmp_path, "m")
        first.append("a")
        second = Journal.open(tmp_path, "m")
        second.append("b")
        assert [r.seq for r in second.read()] == [1, 2]

    def test_timestamps_are_utc_iso(self, journal: Journal) -> None:
        record = journal.append("x")
        assert record.ts.endswith("+00:00")

    def test_open_missing_raises_with_a_hint(self, tmp_path: Path) -> None:
        with pytest.raises(JournalError, match="no journal"):
            Journal.open(tmp_path, "nope")

    def test_read_of_empty_journal_is_empty(self, journal: Journal) -> None:
        assert list(journal.read()) == []


class TestSecretRefusal:
    @pytest.mark.parametrize(
        "key",
        ["value", "real_value", "password", "private_key", "app_key", "token", "postgres_password"],
    )
    def test_forbidden_keys_are_refused(self, journal: Journal, key: str) -> None:
        # Raises rather than redacting: a silent redaction hides the caller's bug.
        with pytest.raises(JournalError, match="names a secret"):
            journal.append("x", detail={key: "hunter2"})

    def test_forbidden_key_is_case_insensitive(self, journal: Journal) -> None:
        with pytest.raises(JournalError, match="names a secret"):
            journal.append("x", detail={"PASSWORD": "hunter2"})

    def test_nested_forbidden_key_is_refused(self, journal: Journal) -> None:
        with pytest.raises(JournalError, match="names a secret"):
            journal.append("x", detail={"env": {"value": "hunter2"}})

    def test_forbidden_key_in_a_list_is_refused(self, journal: Journal) -> None:
        with pytest.raises(JournalError, match="names a secret"):
            journal.append("x", detail={"envs": [{"key": "A"}, {"value": "hunter2"}]})

    def test_pem_shaped_value_is_refused(self, journal: Journal) -> None:
        with pytest.raises(JournalError, match="looks like a credential"):
            journal.append("x", detail={"blob": "-----BEGIN RSA PRIVATE KEY-----\nabc"})

    def test_app_key_shaped_value_is_refused(self, journal: Journal) -> None:
        with pytest.raises(JournalError, match="looks like a credential"):
            journal.append("x", detail={"blob": "base64:" + "A" * 44})

    def test_nothing_is_written_when_refused(self, journal: Journal) -> None:
        with pytest.raises(JournalError):
            journal.append("x", detail={"password": "hunter2"})
        assert list(journal.read()) == []

    def test_references_are_allowed(self, journal: Journal) -> None:
        # The journal stays useful without secrets: we store references.
        journal.append(
            "step_completed",
            state="copy",
            detail={"key_fingerprint": "SHA256:abc", "target_uuid": "u2", "env_keys": ["DB_URL"]},
        )
        assert len(list(journal.read())) == 1


class TestCompletedStates:
    def test_only_completed_steps_count(self, journal: Journal) -> None:
        journal.append("step_started", state="copy")
        journal.append("step_completed", state="create_target")
        journal.append("step_failed", state="copy")
        assert journal.completed_states() == ["create_target"]

    def test_order_is_preserved(self, journal: Journal) -> None:
        journal.append("step_completed", state="create_target")
        journal.append("step_completed", state="quiesce")
        assert journal.completed_states() == ["create_target", "quiesce"]


class TestUndoInfo:
    def test_merges_across_records_for_one_state(self, journal: Journal) -> None:
        # A state records several facts over time; a compensation needs all.
        journal.append("step_started", state="copy", detail={"volumes": ["a"]})
        journal.append("step_completed", state="copy", detail={"key_fingerprint": "SHA256:x"})
        assert journal.undo_info("copy") == {"volumes": ["a"], "key_fingerprint": "SHA256:x"}

    def test_ignores_other_states(self, journal: Journal) -> None:
        journal.append("step_completed", state="copy", detail={"a": 1})
        journal.append("step_completed", state="quiesce", detail={"b": 2})
        assert journal.undo_info("copy") == {"a": 1}

    def test_unknown_state_is_empty(self, journal: Journal) -> None:
        assert journal.undo_info("nope") == {}


class TestCrashResilience:
    def test_truncated_final_line_is_skipped_not_fatal(self, journal: Journal) -> None:
        # The signature of a power cut mid-write. The earlier records are still
        # valid facts; refusing to read them would strand a recoverable migration.
        journal.append("step_completed", state="create_target")
        with journal.path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 2, "event": "step_comp')  # truncated

        records = list(journal.read())
        assert len(records) == 1
        assert records[0].state == "create_target"
        assert journal.completed_states() == ["create_target"]

    def test_newer_schema_is_refused_rather_than_misread(self, journal: Journal) -> None:
        with journal.path.open("a", encoding="utf-8") as fh:
            fh.write('{"schema_version": 999, "seq": 1, "ts": "x", "event": "started"}\n')
        with pytest.raises(JournalError, match="newer version"):
            list(journal.read())

    def test_blank_lines_are_tolerated(self, journal: Journal) -> None:
        journal.append("a")
        with journal.path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n")
        assert len(list(journal.read())) == 1


class TestLifecycle:
    def test_is_finished(self, journal: Journal) -> None:
        journal.append("started")
        assert journal.is_finished is False
        journal.append("finished")
        assert journal.is_finished is True

    def test_rolled_back_counts_as_finished(self, journal: Journal) -> None:
        journal.append("rolled_back")
        assert journal.is_finished is True

    def test_last_event(self, journal: Journal) -> None:
        journal.append("a")
        journal.append("b")
        last = journal.last_event()
        assert last is not None and last.event == "b"

    def test_last_event_of_empty_is_none(self, journal: Journal) -> None:
        assert journal.last_event() is None


class TestListMigrations:
    def test_lists_ids(self, tmp_path: Path) -> None:
        Journal.create(tmp_path, "m1").append("started")
        Journal.create(tmp_path, "m2").append("started")
        assert set(list_migrations(tmp_path)) == {"m1", "m2"}

    def test_missing_dir_is_empty(self, tmp_path: Path) -> None:
        assert list_migrations(tmp_path / "nope") == []


def test_default_state_dir_is_platform_correct() -> None:
    # platformdirs, not a hardcoded ~/.config that does not exist on Windows.
    path = default_state_dir()
    assert path.name == "migrations"
    assert "bg-coolify-migrate" in str(path)
