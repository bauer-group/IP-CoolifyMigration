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

So this module decides, and the engine performs: every TARGET application is
created with auto-deploy OFF (:data:`TARGET_CREATE`) and armed only at
FINALIZE, with the source's original (:func:`target_arm_body`).

``settings`` is readable from v4.2.0 only: application_by_uuid eager-loads it
there (ApplicationsController v4.2.0:2277, v4.7.2:2848) and not at v4.1.2:1973
or v4.1.3. An unreadable value is ``None`` and treated as Coolify's default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True, slots=True)
class Triggers:
    """An application's two push-driven deploy switches. ``None``: unreadable."""

    auto_deploy: bool | None
    previews: bool | None


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


def target_arm_body(original: Triggers) -> dict[str, bool]:
    """FINALIZE's settings PATCH for a target: the source's auto-deploy as it was.

    Unreadable (4.1.x) arms Coolify's default, which is what every target got
    before targets were created disarmed. Previews are not carried: no create
    took them before either (tests/test_api_fields.py KNOWN_APPLICATION_GAP).
    """
    value = DEFAULT_AUTO_DEPLOY if original.auto_deploy is None else original.auto_deploy
    return {AUTO_DEPLOY: value}


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
