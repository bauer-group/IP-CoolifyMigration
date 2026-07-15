"""The quiesce gate — the correctness foundation of the whole tool.

Everything else rests on one claim: **nothing is writing while we copy**. If that
claim is false, byte-exact verification is meaningless because we verified a torn
snapshot faithfully.

Three verified Coolify behaviours make "call stop and proceed" unsafe:

1. **Every stop endpoint is asynchronous.** ``action_stop`` is a
   ``dispatch(...)``; the HTTP call returns before anything has stopped.
2. **Applications: previews are not stopped.**
   ``StopApplication::dispatch($application, false, $dockerCleanup)`` — the
   second argument is ``$previewDeployments``, and the API always passes
   ``false``, so ``getCurrentApplicationContainerStatus(..., 0)`` filters to the
   base deployment. Preview containers keep running and **keep writing**.
3. **Services: containers are found from DB records, not labels.**
   ``StopService`` stops ``"{$application->name}-{$service->uuid}"`` built from
   the parsed model. A compose container Coolify never parsed into a
   ``ServiceApplication``/``ServiceDatabase`` row is therefore never stopped.

So we never trust the endpoint. We ask the daemon, by label, with ``-a``, and we
require **every** container — previews included — to be genuinely stopped.

This gate has no ``--force``. coolify-mover's equivalent is opt-in, swallows its
own failure, and never waits for ``exited``; its default path hot-copies a live
Postgres data directory.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from bg_coolify_migrate.discovery.docker import Container, inspect_state, list_containers
from bg_coolify_migrate.errors import QuiesceError
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: How long to wait for a stack to stop before giving up. Generous on purpose:
#: a large database can legitimately take minutes to flush, and rushing it is
#: what produces the SIGKILL we refuse to accept.
DEFAULT_STOP_TIMEOUT = 300.0

#: How often to re-ask the daemon.
POLL_INTERVAL = 2.0


@dataclass(frozen=True, slots=True)
class QuiesceReport:
    """What the daemon says about a stack's containers."""

    containers: tuple[Container, ...]
    elapsed: float

    @property
    def running(self) -> tuple[Container, ...]:
        return tuple(c for c in self.containers if not c.is_stopped)

    @property
    def killed(self) -> tuple[Container, ...]:
        return tuple(c for c in self.containers if c.was_killed)

    @property
    def previews(self) -> tuple[Container, ...]:
        return tuple(c for c in self.containers if c.is_preview)

    @property
    def is_quiesced(self) -> bool:
        return not self.running and not self.killed


async def snapshot(host: RemoteHost, *, label_filters: dict[str, str]) -> QuiesceReport:
    """Current state of a stack's containers, with exit codes resolved.

    ``docker ps`` does not report exit codes, so each stopped container is
    inspected individually — we must distinguish a clean shutdown from a SIGKILL
    at the stop timeout.
    """
    containers = await list_containers(host, label_filters=label_filters)
    resolved: list[Container] = []
    for c in containers:
        if c.is_stopped:
            state, exit_code = await inspect_state(host, c.id or c.name)
            resolved.append(
                Container(
                    id=c.id, name=c.name, state=state, labels=c.labels, exit_code=exit_code
                )
            )
        else:
            resolved.append(c)
    return QuiesceReport(containers=tuple(resolved), elapsed=0.0)


async def assert_previews_absent(host: RemoteHost, *, label_filters: dict[str, str]) -> None:
    """Refuse to proceed while preview deployments exist.

    Preflight, not quiesce: the API's stop will not touch them, so if they are
    present when we start we would copy under a live writer.

    Raises:
        QuiesceError: If any container has ``coolify.pullRequestId != 0``.
    """
    report = await snapshot(host, label_filters=label_filters)
    previews = report.previews
    if not previews:
        return
    names = ", ".join(sorted(c.name for c in previews))
    raise QuiesceError(
        f"{len(previews)} preview deployment(s) present: {names}",
        hint=(
            "Coolify's stop endpoint does NOT stop preview containers "
            "(StopApplication filters pullRequestId=0), so they would keep writing while "
            "volumes are mirrored — a torn snapshot with no error.\n"
            "Delete them first: DELETE /v1/applications/{uuid}/previews/{pr_id}, or use "
            "--delete-previews. They are rebuilt from the PR, so nothing of value is lost."
        ),
    )


async def wait_until_stopped(
    host: RemoteHost,
    *,
    label_filters: dict[str, str],
    timeout: float = DEFAULT_STOP_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
) -> QuiesceReport:
    """Poll the daemon until every container of the stack is cleanly stopped.

    Args:
        host: The source server.
        label_filters: Identifies the stack, e.g.
            ``{"coolify.projectName": "shop", "coolify.environmentName": "production"}``.
        timeout: Seconds before we give up.
        poll_interval: Seconds between polls.

    Returns:
        The final report, guaranteed quiesced.

    Raises:
        QuiesceError: If the timeout elapses with containers still running, or if
            any container was SIGKILLed. Both are fatal, never warnings.
    """
    started = time.monotonic()
    report = await snapshot(host, label_filters=label_filters)

    while True:
        elapsed = time.monotonic() - started
        report = QuiesceReport(containers=report.containers, elapsed=elapsed)

        if report.killed:
            names = ", ".join(sorted(c.name for c in report.killed))
            raise QuiesceError(
                f"container(s) were SIGKILLed rather than stopping cleanly: {names}",
                hint=(
                    "Exit code 137 means the stop timeout was hit and Docker killed the "
                    "process. A killed database has not flushed, so its volume is a torn "
                    "snapshot. Raise the stop grace period on the resource and retry.\n"
                    "This is fatal by design: mirroring an unflushed data directory "
                    "byte-exactly just gives you a faithful copy of corruption."
                ),
            )

        if not report.running:
            log.info(
                "quiesce.ok",
                containers=len(report.containers),
                elapsed=round(elapsed, 1),
            )
            return report

        if elapsed >= timeout:
            names = ", ".join(sorted(c.name for c in report.running))
            raise QuiesceError(
                f"stack did not stop within {timeout:.0f}s; still running: {names}",
                hint=(
                    "The stack must be fully stopped before its volumes can be mirrored. "
                    "Check the Coolify dashboard for a failed stop, or a container with "
                    "restart: unless-stopped that is being restarted by the daemon."
                ),
            )

        log.debug(
            "quiesce.waiting",
            running=len(report.running),
            elapsed=round(elapsed, 1),
        )
        await asyncio.sleep(poll_interval)
        report = await snapshot(host, label_filters=label_filters)


async def assert_still_stopped(
    host: RemoteHost, *, label_filters: dict[str, str], since: QuiesceReport
) -> None:
    """Re-verify after the copy that nothing restarted mid-transfer.

    A container with ``restart: unless-stopped`` can be brought back by the
    daemon, or an operator can hit Deploy in the dashboard, while a large
    transfer is in flight. Either invalidates the whole copy, and neither raises
    anything on its own — so we check rather than assume.

    Raises:
        QuiesceError: If anything is running again, or if the container set
            changed.
    """
    now = await snapshot(host, label_filters=label_filters)

    if now.running:
        names = ", ".join(sorted(c.name for c in now.running))
        raise QuiesceError(
            f"container(s) restarted during the copy: {names}",
            hint=(
                "The mirrored data is a torn snapshot and must not be trusted. Something "
                "restarted the stack — a `restart:` policy, or a deploy triggered from the "
                "dashboard. The target's volumes will be dropped and the source restarted."
            ),
        )

    before = {c.name for c in since.containers}
    after = {c.name for c in now.containers}
    if before != after:
        raise QuiesceError(
            "the set of containers changed during the copy",
            hint=(
                f"before: {sorted(before)}\nafter:  {sorted(after)}\n"
                "The stack was modified mid-transfer; the copy cannot be trusted."
            ),
        )
