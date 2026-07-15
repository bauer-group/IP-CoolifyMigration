"""The crash-safe migration journal.

Append-only JSONL, fsynced per record. Neither tool this replaces has any journal
at all: coolify-mover has no transactions, so a failure at statement 7 of 10
leaves a half-built service that nothing will ever clean up, and re-running
creates a *second* clone with a new uuid.

Design constraints:

* **Append-only + fsync per record.** A record that is not on disk when the
  process dies is a compensating action that will never run. The cost (one fsync
  per state transition, a handful per migration) is irrelevant next to a
  multi-gigabyte copy.
* **Never journal secrets.** Environment variable values, private keys and
  APP_KEY never enter a record. The journal must remain useful without them, so
  we store *references* — a uuid, a key fingerprint, a name — rather than values.
  This is enforced by :func:`_assert_no_secrets`, which raises rather than
  redacting: a redaction is a bug we hid, an exception is a bug we found.
* **Versioned.** ``SCHEMA_VERSION`` is written into every record so a future
  version can refuse to resume a journal it does not understand rather than
  misinterpreting one.
* **The journal is a hypothesis, not a fact.** ``resume`` must reconcile it
  against reality before trusting it — Geczy's reuse of a stale
  ``coolify_backup.tar.gz`` with no validation is the anti-pattern.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from bg_coolify_migrate.errors import MigrationError

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1

#: Keys that must never appear in a journal record. Checked structurally rather
#: than by pattern-matching values, because the whole point is to make it
#: impossible to write a secret rather than to detect one afterwards.
_FORBIDDEN_KEYS = frozenset(
    {
        "value",
        "real_value",
        "password",
        "private_key",
        "app_key",
        "token",
        "secret",
        "postgres_password",
        "mysql_password",
        "mysql_root_password",
        "mariadb_password",
        "mariadb_root_password",
        "redis_password",
        "keydb_password",
        "dragonfly_password",
        "clickhouse_admin_password",
        "http_basic_auth_password",
    }
)

#: A very rough shape check for things that look like credentials, applied to
#: string values as a second line of defence.
_SECRET_SHAPES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"^base64:[A-Za-z0-9+/]{40,}={0,2}$"),
)


class JournalError(MigrationError):
    """The journal could not be read, written, or trusted."""

    exit_code = 14


class RecordType(str):
    """Marker type for readability; values are free-form."""


class JournalRecord(BaseModel):
    """One immutable entry.

    Records are facts about what happened, not instructions. A ``step_completed``
    means the side effect is done and its compensation is now owed.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = SCHEMA_VERSION
    seq: int
    ts: str
    event: str
    """started | step_started | step_completed | step_failed | blocked |
    rollback_started | rollback_step | finished"""
    state: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    """Undo information lives here — the target uuid to delete, the volume names
    to drop, the ephemeral key fingerprint to revoke. Enough for a compensation
    to run after a total crash of the operator's machine, and nothing more."""


def _assert_no_secrets(payload: dict[str, Any], *, path: str = "detail") -> None:
    """Refuse to write anything that looks like a credential.

    Raises rather than redacting. A silent redaction hides a bug in the caller;
    an exception surfaces it while it is still cheap to fix.
    """
    for key, value in payload.items():
        here = f"{path}.{key}"
        if key.lower() in _FORBIDDEN_KEYS:
            raise JournalError(
                f"refusing to journal {here}: it names a secret",
                hint=(
                    "The journal must never contain credential values — store a "
                    "reference (uuid, key name, fingerprint) instead."
                ),
            )
        if isinstance(value, dict):
            _assert_no_secrets(value, path=here)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    _assert_no_secrets(item, path=f"{here}[{i}]")
        elif isinstance(value, str):
            for shape in _SECRET_SHAPES:
                if shape.search(value):
                    raise JournalError(
                        f"refusing to journal {here}: the value looks like a credential"
                    )


class Journal:
    """Append-only journal for one migration.

    Usage::

        journal = Journal.create(state_dir, migration_id)
        journal.append("step_completed", state="create_target",
                       detail={"target_uuid": "abc"})
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = self._last_seq()

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def create(cls, state_dir: Path, migration_id: str) -> Journal:
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(state_dir / f"{migration_id}.journal.jsonl")

    @classmethod
    def open(cls, state_dir: Path, migration_id: str) -> Journal:
        path = state_dir / f"{migration_id}.journal.jsonl"
        if not path.exists():
            raise JournalError(
                f"no journal for migration {migration_id!r}",
                hint=f"Looked in {state_dir}. Run `coolify-migrate status` to list migrations.",
            )
        return cls(path)

    # ── writing ──────────────────────────────────────────────────────────────

    def _last_seq(self) -> int:
        last = 0
        for record in self.read():
            last = max(last, record.seq)
        return last

    def append(
        self,
        event: str,
        *,
        state: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> JournalRecord:
        """Append a record and fsync it.

        The fsync is not optional: a record that is not durable when the process
        dies is a compensating action that will never run.
        """
        payload = detail or {}
        _assert_no_secrets(payload)

        self._seq += 1
        record = JournalRecord(
            seq=self._seq,
            ts=datetime.now(UTC).isoformat(),
            event=event,
            state=state,
            detail=payload,
        )
        line = record.model_dump_json() + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

        # NOTE: not `event=event` — structlog reserves `event` for the log
        # message itself, so passing our field under that name is a TypeError.
        log.debug("journal.append", record_event=event, state=state, seq=record.seq)
        return record

    # ── reading ──────────────────────────────────────────────────────────────

    def read(self) -> Iterator[JournalRecord]:
        """Every record, in order.

        A truncated final line — the signature of a power cut mid-write — is
        skipped with a warning rather than aborting the read. The preceding
        records are still valid facts, and refusing to read them would strand a
        recoverable migration.
        """
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.warning(
                        "journal.truncated_record",
                        path=str(self.path),
                        line=lineno,
                        hint="likely a crash mid-write; earlier records remain valid",
                    )
                    continue
                if data.get("schema_version", 0) > SCHEMA_VERSION:
                    raise JournalError(
                        f"journal {self.path.name} was written by a newer version "
                        f"(schema {data['schema_version']} > {SCHEMA_VERSION})",
                        hint="Upgrade bg-coolify-migrate, or resume with the version that wrote it.",
                    )
                yield JournalRecord.model_validate(data)

    def completed_states(self) -> list[str]:
        """States whose side effects are done, and whose compensation is owed."""
        return [
            r.state
            for r in self.read()
            if r.event == "step_completed" and r.state is not None
        ]

    def last_event(self) -> JournalRecord | None:
        last: JournalRecord | None = None
        for record in self.read():
            last = record
        return last

    def undo_info(self, state: str) -> dict[str, Any]:
        """Merged detail from every record for one state.

        Merged rather than last-wins because a state can record several facts
        (target uuid at start, volume names as they are created) and a
        compensation needs all of them.
        """
        merged: dict[str, Any] = {}
        for record in self.read():
            if record.state == state:
                merged.update(record.detail)
        return merged

    @property
    def is_finished(self) -> bool:
        last = self.last_event()
        return last is not None and last.event in ("finished", "rolled_back")


def default_state_dir() -> Path:
    """Where journals live.

    Uses platformdirs so this is correct on Windows (``%LOCALAPPDATA%``) as well
    as POSIX, rather than assuming a ``~/.config`` that does not exist there.
    """
    from platformdirs import user_state_dir

    return Path(user_state_dir("bg-coolify-migrate", "BAUER GROUP")) / "migrations"


def list_migrations(state_dir: Path) -> list[str]:
    """Migration ids with a journal, newest first."""
    if not state_dir.exists():
        return []
    journals = sorted(
        state_dir.glob("*.journal.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return [p.name.removesuffix(".journal.jsonl") for p in journals]
