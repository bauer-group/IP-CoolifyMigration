"""Domain exceptions.

Each subclasses a stdlib base so callers who do not import us still catch them.
The philosophy is FAIL-CLOSED: nothing in this package may silently fall back.
Both tools this one replaces fail precisely by swallowing errors —
``coolify-mover`` warns and continues when stopping the source fails, then
hot-copies a live Postgres data directory.

``exit_code`` is part of the CLI contract (see docs/cli.md): callers script
against it, so the numbers are stable and must not be reordered.
"""

from __future__ import annotations

from typing import Any


class MigrationError(RuntimeError):
    """Base for every error this tool raises deliberately.

    ``exit_code`` maps onto the CLI's documented exit-code contract.
    """

    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n\nHint: {self.hint}" if self.hint else base


class PreflightError(MigrationError):
    """A precondition failed before anything was mutated. Nothing to roll back."""

    exit_code = 2


class EmptyEnvironment(PreflightError):
    """An environment has no resources to migrate.

    A distinct type so a whole-project run can SKIP an empty environment and carry
    on, without also swallowing real failures (a host key not trusted, a server
    that would not resolve) into the same "nothing to plan" bucket.
    """


class DnsGateBlocked(MigrationError):
    """A live FQDN still resolves to the source, so the target must not start.

    Not a failure — a deliberate, resumable stop. Starting the target while DNS
    still points at the source makes Traefik on the new host request an ACME
    certificate whose HTTP-01 challenge is routed to the OLD host: the challenge
    fails and burns Let's Encrypt rate limits (5 failed validations per hostname
    per hour), while two proxies claim the same Host() rule.
    """

    exit_code = 3

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        report: Any | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.report = report


class RebuildDriftBlocked(MigrationError):
    """The target would not rebuild the code that is currently running.

    Coolify's ``git_commit_sha`` does not pin a deploy: ``check_git_if_build_needed``
    resolves ``git ls-remote refs/heads/<branch>`` and overwrites the commit. So a
    migration of a git-built application ships whatever HEAD is now, which may not
    be what the mirrored data belongs to.
    """

    exit_code = 4

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        report: Any | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.report = report


class QuiesceError(MigrationError):
    """The stack did not reach a clean, fully-stopped state.

    Always fatal, never a warning: copying a volume whose writer is still alive
    yields a torn snapshot. A container killed at the stop timeout counts as a
    failure here, because a SIGKILLed database has not flushed.
    """

    exit_code = 5


class TransferError(MigrationError):
    """Data transfer failed. The run is resumable; the source is untouched."""

    exit_code = 6


class VerificationError(MigrationError):
    """Copied data does not match the source. The target must not be started."""

    exit_code = 7


class RollbackError(MigrationError):
    """A compensating action failed — the saga's worst case.

    Raised only when undo itself cannot complete. Carries the journal path so an
    operator can reconstruct what was left behind.
    """

    exit_code = 8


class CoolifyApiError(MigrationError):
    """A Coolify REST API call failed.

    Carries the status code and body because Coolify's 422 responses name the
    offending field, which is the single most useful thing when a request
    whitelist has drifted from upstream.
    """

    exit_code = 9

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: Any | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status_code = status_code
        self.body = body

    def __str__(self) -> str:
        parts = [super(MigrationError, self).__str__()]
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.body:
            parts.append(f"body: {self.body!r}")
        out = " | ".join(parts)
        return f"{out}\n\nHint: {self.hint}" if self.hint else out


class InsufficientTokenScope(CoolifyApiError):
    """The API token lacks ``root`` or ``read:sensitive``.

    This is checked eagerly at startup and is fatal, because the failure mode is
    invisible otherwise: Coolify's ``ApiSensitiveData`` middleware sets
    ``can_read_sensitive`` from the token's abilities, and controllers then call
    ``makeHidden(['value', 'real_value', ...])``. The keys simply VANISH from the
    JSON — no error, no redaction marker. A migration would happily recreate
    every environment variable with an empty value.
    """

    exit_code = 10


class UnsupportedResource(MigrationError):
    """A resource shape this tool refuses to migrate rather than guess at."""

    exit_code = 11
