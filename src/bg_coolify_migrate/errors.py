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


class EnvRecoveryError(PreflightError):
    """A locked variable the plan promised to carry cannot be read back any more.

    Raised by CREATE_TARGET before its first write, so nothing has been changed
    and there is nothing to roll back. Carries key names and reasons only —
    never a value, because str(exc) is journaled.
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


class SourceDeploymentPending(QuiesceError):
    """A source application still has a deployment queued or running.

    Raised by QUIESCE BEFORE the stop: Coolify's stop cancels no deployment and
    the queue starts what is waiting, so the source would come back up after the
    stop, beside a copy that never sees its writes. The run rolls back with the
    source still serving. This tool never cancels a deployment.
    """


class ContainmentBreach(QuiesceError):
    """Something started a stack this run holds still. Exit code 5, like the stop
    gate: the stack did not stay stopped.

    Raised by the checkpoints of ``engine/containment.py`` from QUIESCE to
    FINALIZE. Never a warning and with no override (the quiesce gate has no
    ``--force``): before START the run rolls back, and the source — never
    deleted before FINALIZE — keeps what it wrote. After START it blocks
    instead (:class:`SourceRevivedAfterStart`).
    """


class SourceRevived(ContainmentBreach):
    """The source ran again after QUIESCE stopped it: writes the copy does not have."""


class SourceRevivedAfterStart(SourceRevived):
    """The source ran again, found once START had asked Coolify to start a target.

    A BLOCK, like :class:`DnsGateBlocked` — resumable, nothing rolled back
    (``engine/executor.py``). By then the target may have served users since
    START, and the rollback deletes it with its volumes: an automatic rollback
    would trade the target's writes for the revived source's without asking.
    The operator stops the source and resumes, or rolls back by hand (the CLI
    asks first). Exit code 5, like every containment breach.
    """


class SourceUncheckedAfterStart(SourceRevivedAfterStart):
    """A source check that could not be answered, once START had asked for a target.

    Not a revival found: a check that cannot say there was none — a source
    application's deployment history that Coolify did not give. Before START
    that is a plain :class:`QuiesceError` and the run rolls back; after START it
    BLOCKS, for the reason a revival does: the rollback deletes a target that
    may have served users since, and a Coolify restarting for a while must not
    decide that. A subclass so the executor blocks on it; nothing is journalled
    for the resume, which checks again from where it was.
    """


class TargetStartedEarly(ContainmentBreach):
    """A target ran before START asked for it: invariant 7, and a writer during the copy."""


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


class AlreadyRunning(CoolifyApiError):
    """``POST /{services,databases}/{uuid}/start`` refused: 400 "already running".

    Both controllers answer it whenever the resource's status column contains
    'running', and dispatch nothing (ServicesController v4.1.2:1724-1726 ..
    v4.7.2:2183-2185, DatabasesController v4.1.2:2762-2764 .. v4.7.2:3588-3590).
    A target this run never started reads 'exited' there, so for a target this
    is evidence that someone else started it — never a successful start.
    """


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
