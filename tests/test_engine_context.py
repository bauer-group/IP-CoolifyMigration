"""The pre-stop mount capture has to survive a crash.

Coolify's stop is `docker stop` followed by `docker rm -f`, so once a stack is
quiesced the containers are gone and nothing can re-derive what they had
mounted. QUIESCE records it, and after a crash the journal is the only copy in
existence — a resumed run cannot go and look.

If this round trip loses anything, DISCOVER rebuilds an empty manifest, COPY
copies nothing, and the migration reports success. That is the whole failure this
capture exists to prevent, so it is worth a test of its own.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from bg_coolify_migrate.domain.manifest import DockerMount
from bg_coolify_migrate.engine.context import deserialise_mounts, serialise_mounts

VOLUME = DockerMount(
    container="pg-1",
    type="volume",
    name="postgres-data-abc123",
    source="/var/lib/docker/volumes/postgres-data-abc123/_data",
    destination="/var/lib/postgresql/data",
    rw=True,
)
#: A bind has no volume name, and is invisible to the API — the capture is the
#: only place it is ever written down.
BIND = DockerMount(
    container="pg-1",
    type="bind",
    name=None,
    source="/srv/conf",
    destination="/etc/app",
    rw=False,
)


def test_round_trips_through_json() -> None:
    """The journal is JSONL, so the capture must survive encode and decode."""
    original = {"abc123": [VOLUME, BIND]}

    # Through an actual JSON round trip, not just the two helpers: the journal
    # writes text, and a type that only survives in memory would pass a test
    # that skipped this.
    wire = json.loads(json.dumps(serialise_mounts(original)))

    assert deserialise_mounts(wire) == original


def test_keeps_resources_apart() -> None:
    original = {"one": [VOLUME], "two": [BIND]}
    assert deserialise_mounts(json.loads(json.dumps(serialise_mounts(original)))) == original


@pytest.mark.parametrize("absent", [None, "", [], "nonsense"])
def test_tolerates_a_journal_without_a_capture(absent: object) -> None:
    """A journal written before this existed must not crash a resume.

    Returning {} rather than raising is safe only because DISCOVER treats a
    missing capture as fatal by name. Absent means absent.
    """
    assert deserialise_mounts(absent) == {}


@pytest.mark.parametrize("corrupt", [{"uuid": "not-a-list"}, {"uuid": [1, 2]}, {"uuid": [{}]}])
def test_refuses_a_corrupt_capture(corrupt: object) -> None:
    """Corrupt is not absent, and must not be quietly rounded down to it.

    The first cut of this filtered unparseable entries out, which turned
    `{"uuid": [1, 2]}` into `{"uuid": []}` — and an empty list does not read as
    "we lost this", it reads as "this resource has no volumes". DISCOVER would
    have believed it, copied nothing, and reported success.
    """
    with pytest.raises((ValueError, ValidationError)):
        deserialise_mounts(corrupt)


def test_no_mounts_is_still_a_capture() -> None:
    """A stateless resource legitimately has none, and that is not corruption."""
    assert deserialise_mounts({"uuid": []}) == {"uuid": []}
