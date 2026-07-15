"""Pure domain logic — the testable core.

Every module here is total over its inputs and performs no IO. That is a
deliberate architectural constraint, not a stylistic one: the two tools this
project replaces are both broken precisely because their logic is inseparable
from their side effects, so neither can be meaningfully tested. Pushing the
decisions here and leaving ``api/``, ``discovery/`` and ``transfer/`` as thin
shells is what lets the volume-pairing algebra, the drift gates and the rollback
planner be verified exhaustively without a single container.
"""
