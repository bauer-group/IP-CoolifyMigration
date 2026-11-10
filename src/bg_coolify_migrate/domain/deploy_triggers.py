"""What can deploy a source or a target behind the migration's back.

PURE module: no IO.

A source and its target share every deploy trigger Coolify has, and none of
them asks whether a migration is running:

* **Auto-deploy.** A push to the deployed branch deploys every application whose
  ``is_auto_deploy_enabled`` is on — the only thing ``isDeployable()`` checks
  (Application v4.1.2:1106-1113, v4.7.2:1506-1513; Webhook/Github.php
  v4.1.2:133,351, v4.7.2:151,522). Its column defaults to TRUE (migration
  2023_03_27_081717 :19). A target made from a private GitHub App carries the
  source's repository id, one with manual webhooks the source's secrets, so ONE
  push deploys both: the never-started target before the DNS gate (invariant 7)
  and the stopped source after VERIFY (writes nobody copies).
* **Preview deployments.** A pull-request event deploys a preview wherever
  ``is_preview_deployments_enabled`` is on (isPRDeployable, Application
  v4.1.2:1115-1122, v4.7.2:1515-1522; column default false, :22), and the stop
  never reaches a preview (StopApplication filters pullRequestId=0).
* **A deployment already queued or running** when the stop is asked for. The
  stop cancels nothing (StopApplication v4.1.2:16-74, v4.7.2:19-102 touch no
  queue row) and ``queue_next_deployment`` starts what is queued
  (bootstrap/helpers/applications.php v4.1.2:120-140; v4.7.2:258-277 via
  start_next_queued_deployments :279-309).
* **POST /deploy by uuid or tag, a manual deploy, the UI, MCP.** None checks
  auto-deploy: DeployController::by_tags (v4.1.2:444, v4.7.2:488) and
  deploy_resource (v4.1.2:496, v4.7.2:540) never call isDeployable. Not this
  tool's to switch off: the plan says so (:func:`run_note`).

So this module decides, and the engine performs:

* every TARGET application is created with auto-deploy OFF
  (:data:`TARGET_CREATE`) and armed only at FINALIZE, with the source's original
  (:func:`target_arm_body`) — every target gets its tags there too, since a tag
  deploy ignores auto-deploy; their names are read at CREATE
  (:data:`TAGS_JOURNAL_KEY`), while every source still exists. Each target is
  journalled before it is armed (:data:`ARM_REQUESTED`), and a rollback switches
  its auto-deploy off again before it stops it (:func:`target_disarm_body`);
* every SOURCE application is disarmed at QUIESCE, before the stop: auto-deploy
  off, previews off where on (:func:`disarm_body`), the originals journalled
  first (:data:`JOURNAL_KEY`);
* a rollback gives back only what the disarm took, and only where it is still
  missing (:func:`restore_body`) — so replaying it changes nothing;
* FINALIZE under keep or rename leaves the source disarmed for good
  (:func:`kept_source_notice`).

``settings`` is readable from v4.2.0 only: application_by_uuid eager-loads it
there (ApplicationsController v4.2.0:2277, v4.7.2:2848) and not at v4.1.2:1973
or v4.1.3. An unreadable value is ``None`` and treated as Coolify's default.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from bg_coolify_migrate.domain.deployments import IN_PROGRESS, QUEUED

#: The two settings, as the GET nests them under ``settings`` and the PATCH takes
#: them flat.
AUTO_DEPLOY = "is_auto_deploy_enabled"
PREVIEWS = "is_preview_deployments_enabled"

#: Coolify's column defaults (database/migrations/2023_03_27_081717
#: _create_application_settings_table.php v4.1.2:19,22).
DEFAULT_AUTO_DEPLOY = True
DEFAULT_PREVIEWS = False

#: What every application target is created with, whatever its source has. In the
#: shared create $allowedFields at every tag (ApplicationsController v4.1.2:914,
#: v4.5.0:1340, v4.7.2:1613), validated 'boolean' (bootstrap/helpers/api.php
#: v4.1.2:98, v4.7.2:242) and stored when set (isset) by the git routes at every tag
#: (v4.1.2:1160/1405/1614, v4.7.2:1959/2205/2439) and by the dockerfile and
#: dockerimage routes from v4.5.1 (:2331/:2466; accepted and ignored before, where
#: those apps have no git remote to push from anyway).
TARGET_CREATE: Mapping[str, bool] = {AUTO_DEPLOY: False}

#: The QUIESCE journal key holding each source application's originals, read
#: before the disarm: ``{source_uuid: {"auto_deploy": bool|None, "previews": ...}}``.
#: Booleans only: no secret can ride it.
JOURNAL_KEY = "deploy_triggers"

#: CREATE's journal key holding each source's tag NAMES, for FINALIZE to give its
#: target: ``{source_uuid: [name, ...]}``. Read at CREATE because a DELETE
#: finalize removes the sources before the targets get their tags, and a deleted
#: source's tags go with it: soft-deleted first from 4.3.11, so its GET .../tags
#: 404s at once, then force-deleted by DeleteResourceJob (v4.3.10:130,
#: v4.7.2:171; a database's tags detached just before, :127/:168). Names only:
#: a tag is a label.
TAGS_JOURNAL_KEY = "source_tags"

#: FINALIZE's journal key (a ``step_started`` detail): every target application
#: uuid FINALIZE is about to give its auto-deploy, written BEFORE the request, so
#: a crash between the two still leaves the way back. The rollback's
#: DISARM_TARGETS switches each one off again (:func:`target_disarm_body`).
#: Uuids only.
ARM_REQUESTED = "arm_requested"


@dataclass(frozen=True, slots=True)
class Triggers:
    """An application's two push-driven deploy switches. ``None``: unreadable."""

    auto_deploy: bool | None
    previews: bool | None

    def journal(self) -> dict[str, bool | None]:
        return {"auto_deploy": self.auto_deploy, "previews": self.previews}


def _bool(value: object) -> bool | None:
    # ApplicationSetting casts both to 'boolean' (v4.2.0:62,65; v4.7.2 :74), so
    # anything else is a shape this tool does not know: unreadable.
    return value if isinstance(value, bool) else None


def read_triggers(resource: Mapping[str, Any]) -> Triggers:
    """The switches of an application GET. Both ``None`` before 4.2 (no ``settings``)."""
    settings = resource.get("settings")
    if not isinstance(settings, Mapping):
        return Triggers(auto_deploy=None, previews=None)
    return Triggers(
        auto_deploy=_bool(settings.get(AUTO_DEPLOY)), previews=_bool(settings.get(PREVIEWS))
    )


def disarm_body(original: Triggers, current: Triggers | None = None) -> dict[str, bool]:
    """The PATCH that switches a source's push-driven deploys off. Empty: nothing to do.

    Auto-deploy goes off unless the source had it off — an unreadable one (4.1.x)
    too, since a push there would revive the stopped source just the same.
    Previews go off only where they were READ on: before 4.2 the update array
    refuses the field (v4.1.2:2368; v4.2.0:2695 takes it), and that is exactly
    where they cannot be read. ``current`` (a fresh read, on a resumed QUIESCE)
    drops what is already off.
    """
    now = current or original
    body: dict[str, bool] = {}
    if original.auto_deploy is not False and now.auto_deploy is not False:
        body[AUTO_DEPLOY] = False
    if original.previews is True and now.previews is not False:
        body[PREVIEWS] = False
    return body


def restore_body(original: Triggers, current: Triggers) -> dict[str, bool]:
    """The PATCH that gives a disarmed source back what the disarm took. PURE.

    Only the switches :func:`disarm_body` turned off, and only where ``current``
    (read now) does not hold the original again: a rollback is replayed by
    ``rollback <id>``, and a switch the operator changed on a setting this run
    never touched is not ours to revert. An unreadable original was disarmed as
    if on, so it comes back as Coolify's default (on).
    """
    taken = disarm_body(original)
    body: dict[str, bool] = {}
    if AUTO_DEPLOY in taken and current.auto_deploy is not True:
        body[AUTO_DEPLOY] = DEFAULT_AUTO_DEPLOY
    if PREVIEWS in taken and current.previews is not True:
        body[PREVIEWS] = True
    return body


def target_arm_body(original: Triggers) -> dict[str, bool]:
    """FINALIZE's settings PATCH for a target: the source's auto-deploy as it was.

    Unreadable (4.1.x) arms Coolify's default, which is what every target got
    before targets were created disarmed. Previews are not carried: no create
    took them before either (tests/test_api_fields.py KNOWN_APPLICATION_GAP), and
    the plan says so where the source had them on.
    """
    value = DEFAULT_AUTO_DEPLOY if original.auto_deploy is None else original.auto_deploy
    return {AUTO_DEPLOY: value}


def target_disarm_body(current: Triggers) -> dict[str, bool]:
    """The rollback's PATCH that takes back what FINALIZE armed on a target. PURE.

    A rollback after a SUCCEEDED keep or rename run stops the target, drops its
    volumes and only then deletes it, with the source's restart (up to the
    deploy timeout) in between: armed, a push in that window redeploys the target
    — Compose recreates its volumes empty, and users still sent there by DNS
    reach an empty instance — and a target the rollback KEEPS stays armed for
    good. So auto-deploy goes off, unless it is READ off already; an unreadable
    one (4.1.x, no ``settings``) too, since the update array takes the field at
    every tag (ApplicationsController v4.1.2:2368,2634; v4.7.2:3662,4036).
    Previews are left as they are: FINALIZE never armed them
    (:func:`target_arm_body`). A settings-only PATCH deploys nothing (v4.1.2:2680,
    v4.7.2:4130), so this is safe to repeat on every replay.
    """
    return {} if current.auto_deploy is False else {AUTO_DEPLOY: False}


def serialise(originals: Mapping[str, Triggers]) -> dict[str, dict[str, bool | None]]:
    """``{source_uuid: {"auto_deploy": ..., "previews": ...}}`` for the journal."""
    return {uuid: triggers.journal() for uuid, triggers in originals.items()}


def deserialise(raw: object) -> dict[str, Triggers]:
    """Read :data:`JOURNAL_KEY` back. Tolerant: a malformed entry is left out."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Triggers] = {}
    for uuid, entry in raw.items():
        if isinstance(uuid, str) and isinstance(entry, Mapping):
            out[uuid] = Triggers(
                auto_deploy=_bool(entry.get("auto_deploy")), previews=_bool(entry.get("previews"))
            )
    return out


def deserialise_tags(raw: object) -> dict[str, list[str]]:
    """Read :data:`TAGS_JOURNAL_KEY` back. PURE, and tolerant.

    A source missing from the result was not read at CREATE (the read failed,
    or the journal predates it): FINALIZE then asks the source itself. A
    malformed entry counts as missing; a name that is not a string is left out.
    """
    if not isinstance(raw, Mapping):
        return {}
    return {
        uuid: [name for name in names if isinstance(name, str)]
        for uuid, names in raw.items()
        if isinstance(uuid, str) and isinstance(names, list)
    }


def deserialise_uuids(raw: object) -> list[str]:
    """Read :data:`ARM_REQUESTED` back: each uuid once, in order. PURE, and tolerant."""
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(u for u in raw if isinstance(u, str) and u))


# ── the deployment queue ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ActiveDeployment:
    """The part of a queued or running deployment row this tool may hold."""

    uuid: str
    status: str
    application_uuid: str | None
    """Read off ``deployment_url``: the row's ``application_id`` is the hidden
    numeric id, which no application GET discloses."""


#: ``deployment_url`` is the path of ``$application->link()."/deployment/{uuid}"``
#: (applications.php v4.1.2:19-20, v4.7.2:31-32), and link() is the route
#: project.application.configuration —
#: ``/project/{p}/environment/{e}/application/{application_uuid}`` (routes/web.php
#: v4.1.2:217-218, v4.7.2:425-426).
_APPLICATION_IN_URL = re.compile(r"/application/([^/?#]+)/deployment/")


def application_uuid_in(deployment_url: object) -> str | None:
    """The application uuid a ``deployment_url`` names, or ``None``."""
    if not isinstance(deployment_url, str):
        return None
    match = _APPLICATION_IN_URL.search(deployment_url)
    return match.group(1) if match else None


def read_active_deployments(answer: object) -> tuple[ActiveDeployment, ...]:
    """Reduce a ``GET /deployments`` answer to :class:`ActiveDeployment` rows.

    The route lists the team's QUEUED and IN_PROGRESS rows (DeployController
    ::deployments v4.1.2:66-80, v4.7.2:75-91), sorted by id with their keys kept
    (``->sortBy('id')``): a JSON array, or an object keyed by position once the
    sort moved a row. Only uuid, status and the application's uuid survive —
    ``logs`` is the full deployment log, visible to a token that reads sensitive
    data (v4.7.2:27-40), and build output carries secrets (invariant 5).
    """
    rows: Iterable[object]
    if isinstance(answer, list):
        rows = answer
    elif isinstance(answer, Mapping):
        rows = answer.values()
    else:
        return ()
    out: list[ActiveDeployment] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        uuid = row.get("deployment_uuid")
        out.append(
            ActiveDeployment(
                uuid=uuid if isinstance(uuid, str) else "",
                status=str(row.get("status") or ""),
                application_uuid=application_uuid_in(row.get("deployment_url")),
            )
        )
    return tuple(out)


def pending_for(
    deployments: Iterable[ActiveDeployment], sources: Collection[str]
) -> tuple[ActiveDeployment, ...]:
    """The deployments still queued or running for one of ``sources``. PURE.

    Each would start its application after the stop (see the module docstring),
    a preview's too. A row whose application cannot be told is not counted: the
    route serves the whole team, and refusing over someone else's build would
    make a busy team unmigratable.
    """
    return tuple(
        d
        for d in deployments
        if d.status in (QUEUED, IN_PROGRESS) and d.application_uuid in sources
    )


# ── what the operator is told ────────────────────────────────────────────────


def run_note() -> str:
    """The plan note for every plan that moves an application."""
    return (
        "deploys during the run: QUIESCE switches each source application's auto-deploy "
        "off (and its preview deployments, where on), and every target is created with "
        "auto-deploy off and gets its tags only at FINALIZE, so a git push or a tag deploy "
        "reaches neither side before the cutover. A deployment already queued or running "
        "for a source is waited for before the stop; one that does not end within the "
        "deploy timeout refuses the run while the source still serves. A CI call to POST "
        "/deploy (by uuid or tag), a manual deploy, the API and MCP bypass auto-deploy and "
        "still start a source: pause them from QUIESCE until FINALIZE - a source deployed "
        "during the run rolls the run back. Under finalize keep or rename the kept "
        "source's auto-deploy stays off afterwards; a rollback switches it back on."
    )


def application_warnings(resource: Mapping[str, Any]) -> list[str]:
    """Plan warnings for one application source's deploy switches. PURE."""
    triggers = read_triggers(resource)
    if triggers.auto_deploy is None:
        return [
            "Coolify before 4.2 does not return this application's settings, so its "
            "auto-deploy setting cannot be read: QUIESCE switches it off all the same, and "
            "FINALIZE switches the target's on - Coolify's default - as a rollback does the "
            "source's. If the source had auto-deploy off, switch it off again there "
            "afterwards. Its preview deployments cannot be read or switched off through "
            "Coolify's API before 4.2 either: if they are on, switch them off in Coolify "
            "before the run, or a pull request can deploy a preview of the stopped source."
        ]
    if triggers.previews:
        return [
            "preview deployments are on: QUIESCE switches them off together with auto-deploy "
            "(a pull request would otherwise deploy a preview the stop never reaches), and "
            "the target is created without them - Coolify's default. A rollback switches "
            "them back on for the source, under keep or rename they stay off there; switch "
            "them on for the target in Coolify if you want them."
        ]
    return []


def kept_source_notice(names: Iterable[str], *, migration_id: str) -> str:
    """FINALIZE keep/rename: the source stays disarmed, and what still reaches it."""
    return (
        f"{', '.join(names)} (the kept source) keep(s) auto-deploy and preview deployments "
        "OFF: they share their repository and webhook with the target, so a push would "
        "otherwise deploy the stale source beside it. A CI call to POST /deploy by uuid or "
        "tag, a manual deploy and the UI still reach the source - they bypass auto-deploy. "
        f"coolify-migrate rollback {migration_id} switches them back on."
    )


def arm_failed_notice(name: str, target_uuid: str, value: bool, *, reason: str) -> str:
    """FINALIZE could not give a target its auto-deploy: what to set by hand."""
    # The toggle is on the application's Advanced page (Livewire
    # Project/Application/Advanced.php v4.1.2:112, v4.7.2:132).
    state = "on" if value else "off"
    return (
        f"the target of {name} ({target_uuid}) was created with auto-deploy off, and "
        f"switching it {state} as its source had it failed ({reason}): switch "
        f"'Auto Deploy' {state} on its Advanced page in Coolify, or pushes "
        + ("will not deploy it." if value else "may deploy it.")
    )
