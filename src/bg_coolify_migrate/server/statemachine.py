"""F2's state machine: migrating a whole Coolify instance.

PURE module: no IO.

Different from F1's, and the difference is instructive. F1 creates the target
first because a failed create then costs nothing; F2 cannot — there is nothing to
create until the data is there. F1's rollback is cheap because the source keeps
running; F2's outage lasts the whole transfer, so ``START_SOURCE_DOCKER`` is the
compensation that matters and everything else is cleanup.

The ordering that carries the entire feature:

    TRANSFER  ->  INSTALL_COOLIFY

``install.sh`` merges the ``.env`` with ``awk '!seen[$1]++'``, existing values
first, and only fills EMPTY or MISSING vars. So a migrated ``.env`` keeps its
``APP_KEY`` — which decrypts every credential Coolify holds. Reverse these two
and it still appears to work, right up until extraction fails and you have a
fresh key against a restored database, at which point every secret is
permanently undecryptable.

Geczy's script gets this right and never mentions it. We assert it.
"""

from __future__ import annotations

from enum import StrEnum

from bg_coolify_migrate.domain.statemachine import Compensation


class ServerState(StrEnum):
    """States of an F2 instance migration, in execution order."""

    INIT = "init"
    PREFLIGHT = "preflight"
    INVENTORY = "inventory"
    READ_APP_KEY = "read_app_key"
    """Captured BEFORE anything moves, so we can assert it survived."""
    STOP_SOURCE = "stop_source"
    """Docker down on the source. The outage starts here."""
    TRANSFER = "transfer"
    VERIFY = "verify"
    INSTALL_COOLIFY = "install_coolify"
    """MUST be after TRANSFER. See the module docstring."""
    ASSERT_APP_KEY = "assert_app_key"
    BOOT = "boot"
    RECONCILE = "reconcile"
    FENCE_SOURCE = "fence_source"
    """Stop the source's scheduler so two Coolify brains do not drive one fleet."""
    DONE = "done"


ORDER: tuple[ServerState, ...] = (
    ServerState.INIT,
    ServerState.PREFLIGHT,
    ServerState.INVENTORY,
    ServerState.READ_APP_KEY,
    ServerState.STOP_SOURCE,
    ServerState.TRANSFER,
    ServerState.VERIFY,
    ServerState.INSTALL_COOLIFY,
    ServerState.ASSERT_APP_KEY,
    ServerState.BOOT,
    ServerState.RECONCILE,
    ServerState.FENCE_SOURCE,
    ServerState.DONE,
)

#: What each state must undo. States absent from this map only read.
#:
#: Note what is NOT here: TRANSFER has no compensation of its own beyond wiping
#: the target, and INSTALL_COOLIFY has none at all — an installed Coolify on a
#: box we were told was empty is not harmful, and uninstalling it would be a
#: bigger intervention than leaving it.
COMPENSATION: dict[ServerState, tuple[Compensation, ...]] = {
    ServerState.STOP_SOURCE: (Compensation.START_SOURCE_DOCKER,),
    ServerState.TRANSFER: (Compensation.WIPE_TARGET_DATA, Compensation.REVOKE_EPHEMERAL_KEY),
    ServerState.FENCE_SOURCE: (Compensation.UNFENCE_SOURCE,),
}

#: F2 never destroys the source. There is no DELETE policy and no finalize step:
#: the old box is left intact but fenced, so "rollback" is always just "start it
#: again". This is Geczy's one genuinely good architectural decision, kept.
IRREVERSIBLE: frozenset[ServerState] = frozenset()


def transfer_precedes_install() -> bool:
    """The invariant this whole module exists to protect.

    Asserted by a test rather than trusted to review: if someone reorders these,
    APP_KEY survival becomes luck again.
    """
    return ORDER.index(ServerState.TRANSFER) < ORDER.index(ServerState.INSTALL_COOLIFY)
