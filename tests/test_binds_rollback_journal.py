"""A rollback journals each bind removal the moment it succeeds.

The removals used to be journalled once, after the whole loop. A refused path
was already kept and the loop went on, but any other failure — an ``SshError``
from an ``rm`` that failed — escaped with the earlier removals unrecorded, and
the replayed rollback its failure asks for ran ``rm -rf`` on them again: on
whatever someone had put there since.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.engine import compensations
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.transfer import binds as transfer_binds
from bg_coolify_migrate.transfer.ssh import SshError
from tests.test_binds_engine import _copied, _tgt, bind_context


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    context = bind_context(tmp_path)
    yield context
    await context.api.aclose()


def _removal_records(ctx: MigrationContext) -> list[list[str]]:
    return [
        r.detail[binds.BINDS_REMOVED]
        for r in ctx.journal.read()
        if r.event == "rollback_step" and binds.BINDS_REMOVED in r.detail
    ]


def _owed(ctx: MigrationContext) -> list[str]:
    return list(binds.owed_bind_targets((r.event, r.detail) for r in ctx.journal.read()))


async def test_a_failed_rm_leaves_the_earlier_removals_journalled(ctx: MigrationContext) -> None:
    target = _tgt(ctx)
    target.paths.update({"/srv/a/x": binds.STATE_FILE, "/srv/b/y": binds.STATE_FILE})
    _copied(ctx, "/srv/a", "missing", ["/srv/a"])
    _copied(ctx, "/srv/b", "missing", ["/srv/b"])
    target.on(r" sh rm /srv/b$", exit_status=1, stderr="rm: Read-only file system")

    with pytest.raises(SshError):  # still loud: the rollback reports it
        await compensations.undo_copy(ctx, {})

    assert target.state("/srv/a") == binds.STATE_MISSING
    assert target.state("/srv/b/y") == binds.STATE_FILE
    assert _removal_records(ctx) == [["/srv/a"]]
    assert _owed(ctx) == ["/srv/b"]

    # The replay the failure asks for. Someone has used /srv/a since: it is not
    # this run's any more, and is not deleted a second time.
    target.paths["/srv/a/theirs"] = binds.STATE_FILE
    target._routes.clear()
    await compensations.undo_copy(ctx, {})
    assert target.state("/srv/a/theirs") == binds.STATE_FILE
    assert target.state("/srv/b") == binds.STATE_MISSING
    assert _removal_records(ctx) == [["/srv/a"], ["/srv/b"]]
    assert _owed(ctx) == []


async def test_a_refused_path_is_kept_and_every_other_removal_journalled(
    ctx: MigrationContext,
) -> None:
    target = _tgt(ctx)
    target.paths.update(
        {"/srv/a/x": binds.STATE_FILE, "/srv/b/y": binds.STATE_FILE, "/srv/c/z": binds.STATE_FILE}
    )
    for path in ("/srv/a", "/srv/b", "/srv/c"):
        _copied(ctx, path, "missing", [path])
    target.on(r" sh rm /srv/b$", exit_status=transfer_binds.EXIT_REDIRECTED)

    with pytest.raises(RuntimeError, match=r"KEPT.*/srv/b"):
        await compensations.undo_copy(ctx, {})

    assert _removal_records(ctx) == [["/srv/a"], ["/srv/c"]]
    assert _owed(ctx) == ["/srv/b"]
