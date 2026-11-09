"""What an application deployment's row in Coolify says about a migration.

PURE module: no IO.

Every application deploy this tool asks for — the target's first ``/start``, the
rollback's ``/restart`` of the source — is a row in Coolify's deployment queue,
and both answer 200 with its ``deployment_uuid`` the moment it is queued
(ApplicationsController v4.1.2:3615-3621/3807-3812, v4.7.0:4988-4994/5180-5185).
"Accepted" is therefore no evidence of anything. A deploy that fails before it
creates a container leaves the daemon silent, and silence reads exactly like a
build still running — so without the row a blocked START waited out the whole
``deploy_timeout`` with the source down, and a failed ``/restart`` was journalled
as a restart and reported ROLLED_BACK while the source stayed down.

``GET /deployments/{uuid}`` (ability ``read``) reads the row on every release this
tool supports: the route (routes/api.php v4.1.2:80, v4.3.10:147, v4.5.0:154,
v4.6.0:161, v4.7.0:164), the controller (DeployController::deployment_by_uuid
v4.1.2:119-139, identical at v4.7.0:132-152) and the five statuses
(ApplicationDeploymentStatus, identical v4.1.2 .. v4.7.1) are unchanged. So the
watch is ungated; only the image-scan gate's verdict is 4.7.0+, read by key
presence.

Only ``status`` and the gate's ``blocked``/``headline`` ever leave the client.
The row's ``logs`` is the whole deployment log, made visible to a token that can
read sensitive data (DeployController v4.7.0:27-40), and build output carries
secrets (invariant 5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: ApplicationDeploymentStatus. ``cancelled-by-user`` is also what a newer API or
#: webhook deployment that supersedes a queued one writes (applications.php
#: v4.7.0:190-239); restarts are never superseded.
QUEUED = "queued"
IN_PROGRESS = "in_progress"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled-by-user"

#: Coolify's headline is its own secret-free sentence (SecurityGate v4.7.0:198-216,
#: or an ImageScanErrorCode message naming a server); capped for a message line.
_HEADLINE_MAX = 240

#: An ImageScanErrorCode value ('scanning_off', 'custom_command', 'unknown', ...,
#: Enums/ImageScanErrorCode.php v4.7.0:7-21, unchanged in v4.7.1): a fixed code,
#: never command output. Anything else in the field is not kept.
_ERROR_CODE = re.compile(r"[a-z_]{1,32}")


@dataclass(frozen=True, slots=True)
class Deployment:
    """The part of a deployment row this tool may hold. Nothing else is kept."""

    uuid: str
    status: str
    gate_blocked: bool = False
    gate_headline: str = ""
    gate_error: str = ""
    """Why the scan could not run, when that is what blocked: the gate then blocks
    on "If the scan cannot run: Block", not on a finding (SecurityGate
    v4.7.0:52-62)."""


class Progress(StrEnum):
    PENDING = "pending"
    """Queued, in progress — or a status this tool does not know, which must
    never fail a run that the daemon may still show healthy."""
    FINISHED = "finished"
    FAILED = "failed"
    """Failed, cancelled, or no such row: this deployment will start nothing."""


@dataclass(frozen=True, slots=True)
class Verdict:
    progress: Progress
    reason: str = ""
    """Why it failed, secret-free, naming the deployment; empty otherwise."""
    gate_blocked: bool = False
    gate_error: str = ""
    """Set when the gate blocked because the scan could not run."""


def read_deployment(uuid: str, row: Any) -> Deployment:
    """Reduce a ``GET /deployments/{uuid}`` answer to :class:`Deployment`.

    ``scan_gate_result`` is an array cast, not hidden (ApplicationDeploymentQueue
    v4.7.0:175,188-197), written only when the gate ran (DeploymentImageScan
    v4.7.0:839-879): absent or null before 4.7.0 and under "Block deploys: Off".
    Its ``error`` is the code of a scan that could not run (SecurityGate::stored
    v4.7.0:175-192), null when findings decided.
    """
    if not isinstance(row, dict):
        return Deployment(uuid=uuid, status="")
    gate = row.get("scan_gate_result")
    blocked = isinstance(gate, dict) and gate.get("blocked") is True
    headline = error = ""
    if blocked and isinstance(gate, dict):
        headline = " ".join(str(gate.get("headline") or "").split())[:_HEADLINE_MAX]
        code = gate.get("error")
        error = code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""
    return Deployment(
        uuid=uuid,
        status=str(row.get("status") or ""),
        gate_blocked=blocked,
        gate_headline=headline,
        gate_error=error,
    )


def deployment_uuid_of(answer: Any) -> str | None:
    """The ``deployment_uuid`` of a ``/start`` or ``/restart`` answer, if it has one.

    A "skipped" answer — a deployment of the same commit already queued — carries
    only a message (ApplicationsController v4.1.2:3597-3604, v4.7.0:4970-4977), and
    databases and services queue no deployment at all: nothing to watch.
    """
    uuid = answer.get("deployment_uuid") if isinstance(answer, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


def judge(uuid: str, deployment: Deployment | None) -> Verdict:
    """What one deployment's row says. ``None`` is a 404: Coolify has no such row.

    A 404 on a route that exists everywhere is an answer, not an old release:
    ``/start`` and ``/restart`` mint the uuid before they ask the queue, and answer
    200 with it even when the queue was full and nothing was created
    (applications.php v4.1.2:40-44, v4.7.0:58-63; action_deploy/action_restart
    check only 'skipped').
    """
    if deployment is None:
        return Verdict(
            Progress.FAILED,
            f"Coolify has no deployment {uuid}: it answered with that uuid but queued "
            "nothing, as it does when the server's deployment queue is full",
        )
    if deployment.status == FINISHED:
        return Verdict(Progress.FINISHED)
    if deployment.status == FAILED and deployment.gate_blocked:
        return Verdict(
            Progress.FAILED,
            f"Coolify's image-scan gate blocked deployment {uuid}"
            + (f" ({deployment.gate_headline})" if deployment.gate_headline else ""),
            gate_blocked=True,
            gate_error=deployment.gate_error,
        )
    if deployment.status == FAILED:
        return Verdict(Progress.FAILED, f"deployment {uuid} failed; its log in Coolify says why")
    if deployment.status == CANCELLED:
        return Verdict(Progress.FAILED, f"deployment {uuid} was cancelled in Coolify")
    return Verdict(Progress.PENDING)


#: Why a blocked START is no judgement of the image: the source passes the same
#: gate with the same image because it has a baseline. A finished deployment's
#: completed scan is the baseline (DeploymentImageScan v4.7.0:489-509), and a new
#: application has none — no earlier finished deployment either (:182-186,
#: :515-523) — so under On every exploit-signal finding is new (SecurityGate
#: v4.7.0:15-19, :85-92). The most specific level that sets a mode wins, a null
#: inherits (ImageScanPolicyResolver v4.7.0:112-127, :175-195), and a new
#: application has no row of its own: Off on the team does nothing where the
#: target server sets On.
FIRST_DEPLOY_GATE_HINT = (
    "Coolify's image-scan gate ('Block deploys', 4.7+) refused the target's first "
    "deploy. A new application has no running image to compare with, so every "
    "finding counts as new, even one the source runs too. Set 'Block deploys' to "
    "Off for the migration at the most specific level that sets it (the target "
    "server, else the team), or add team-wide ignores for these findings in "
    "Coolify, then run the migration again - this run is rolled back. The source is "
    "still intact."
)

#: The same refusal when no finding decided: the scan could not run and "If the
#: scan cannot run" is Block (SecurityGate v4.7.0:52-62). Scanning is off for a
#: server by default (server_settings.is_image_scan_enabled, migration
#: 2026_10_29_100000:119) and a custom compose command is never scanned
#: (DeploymentImageScan v4.7.0:729-744); on a self-hosted v4.7.0 no scan runs at
#: all, a template the image lacks (fixed by v4.7.1's Dockerfile).
SCAN_ERROR_GATE_HINT = (
    "Coolify's image-scan gate ('Block deploys', 4.7+) refused the target's first "
    "deploy because the scan could not run and 'If the scan cannot run' is Block. "
    "Turn image scanning on for the target server, or set 'If the scan cannot run' "
    "to Deploy for the migration at the most specific level that sets it (the target "
    "server, else the team); on a self-hosted Coolify 4.7.0 no scan can run, which "
    "4.7.1 fixes. Then run the migration again - this run is rolled back. The source "
    "is still intact."
)


def start_failure_hint(verdict: Verdict) -> str:
    """What to do about a target deployment that failed (``verdict`` is FAILED)."""
    if not verdict.gate_blocked:
        return "Check that deployment's log in Coolify. The source is still intact."
    return SCAN_ERROR_GATE_HINT if verdict.gate_error else FIRST_DEPLOY_GATE_HINT


def restart_blocked_notice(name: str, deployment_uuid: str, *, migration_id: str) -> str:
    """The way out of a source whose restart the image-scan gate blocked.

    "Deploy anyway" is the UI's only bypass (Livewire Deployment/Show.php
    v4.7.0:170, unchanged in v4.7.1, is the sole writer of scan_gate_bypassed_by)
    and starts a NEW deployment, so the rollback, which retries a restart whose
    deployment failed, would redeploy the source a second time after it. Off
    helps only at the most specific level that sets a mode
    (ImageScanPolicyResolver v4.7.0:175-195): on the team it does nothing where
    the app or its server sets On or Strict.
    """
    unpin = f"coolify-migrate unpin {migration_id}"
    return (
        f"{name} is still DOWN: Coolify's image-scan gate blocked the deployment "
        f"{deployment_uuid} that would restart it. Either click 'Deploy anyway' on that "
        f"deployment in Coolify and, once it runs, {unpin} - not the rollback, which "
        "would restart it a second time; or set 'Block deploys' to Off at the most "
        "specific level that sets it (the app, then its server, then the team), "
        f"then coolify-migrate rollback {migration_id}."
    )

