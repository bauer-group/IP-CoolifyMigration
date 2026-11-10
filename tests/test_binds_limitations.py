"""Two known limitations of the bind copy, pinned as what they are: loud refusals.

Neither loses data, and both refuse a setup that is healthy. Loosening either
must be a decision, not a side effect: ``domain.binds.ancestor_problem`` and
``engine.steps._copy_binds`` document why each holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import BindPair
from bg_coolify_migrate.engine import steps
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.errors import TransferError
from tests.test_binds_engine import (
    SRC,
    TGT,
    _copied,
    _journalled_targets,
    _record_copies,
    _tgt,
    bind_context,
)


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    context = bind_context(tmp_path)
    yield context
    await context.api.aclose()


@pytest.mark.parametrize("link", ["/data", "/data/coolify"])
def test_a_symlinked_data_dir_refuses_a_relative_bind(link: str) -> None:
    # Coolify's data moved to a larger disk behind a symlink.
    states = {"/data": binds.STATE_DIR, "/data/coolify": binds.STATE_DIR, link: binds.STATE_SYMLINK}
    problem = binds.ancestor_problem(f"{TGT}/data", states)
    assert problem is not None and f"{link}, a parent" in problem


async def test_a_resumed_copy_refuses_a_nested_target_the_outer_copy_filled(
    ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.bind_pairs = {
        "svc1": [
            BindPair(f"{SRC}/data", f"{TGT}/data", "/data", kind=binds.KIND_DIR),
            BindPair(f"{SRC}/data/uploads", f"{TGT}/data/uploads", "/up", kind=binds.KIND_DIR),
        ]
    }
    # The crash came after the outer pair was journalled and copied.
    _copied(ctx, f"{TGT}/data", "missing", [f"{TGT}/data"])
    _tgt(ctx).paths[f"{TGT}/data/uploads/a.png"] = binds.STATE_FILE
    copies = _record_copies(monkeypatch)

    with pytest.raises(TransferError, match="already holds data"):
        await steps._copy_binds(ctx)

    assert copies == [("dir", f"{SRC}/data", f"{TGT}/data")]
    assert [t["path"] for t in _journalled_targets(ctx)] == [f"{TGT}/data"]
