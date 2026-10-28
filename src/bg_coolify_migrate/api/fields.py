"""Per-endpoint request field whitelists — views of the release registry.

PURE module: no IO.

**Why this exists.** Coolify's controllers enforce ``$allowedFields`` and return
``422 "This field is not allowed."`` for anything else::

    array_diff(array_keys($request->all()), $allowedFields)

So you can NEVER round-trip a ``GET`` response into a ``POST`` body — the GET
returns the full model (dozens of columns), the POST accepts a curated subset,
and the difference is a 422 per extra field. Every request body this tool sends
is therefore filtered through an explicit whitelist.

**Where the lists live.** In ``domain/coolify_versions.py``, per endpoint and per
release: :func:`fields_for` is the whitelist for one endpoint on one Coolify
version, and :func:`filter_for` builds a body from a source GET with it (reading
a field the GET nests where the registry says). The frozensets below are the
UNION over every release in the table — what an endpoint takes somewhere — for
tests and documentation; no request is filtered through them.

**Keeping it honest.** The registry is transcribed from upstream source, which
means it can drift when Coolify changes. ``tests/test_api_fields.py`` diffs it
against ``openapi.json`` AND the controllers' ``$allowedFields`` at the newest
RELEASE tag (never ``main`` — see the 2.5.6 tags regression), and a weekly CI job
runs that — so drift breaks our CI rather than someone's production migration.
OpenAPI and ``$allowedFields`` genuinely disagree in places (the attributes are
documentation; the arrays are the enforcement), so where they conflict, **the
arrays win** and the test records the exception.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from bg_coolify_migrate.domain import coolify_versions as cv
from bg_coolify_migrate.domain.coolify_versions import fields_for
from bg_coolify_migrate.domain.versions import Version

__all__ = ["fields_for"]

# ── databases ────────────────────────────────────────────────────────────────

_DATABASE_FIELDS: dict[str, frozenset[str]] = {
    engine: cv.known_fields(f"databases/{engine}") for engine in cv.DATABASE_ENGINES
}
#: The fields every engine's create route shares.
DATABASE_COMMON: frozenset[str] = frozenset.intersection(*_DATABASE_FIELDS.values())
#: Engine-specific credential/config fields, keyed by API path segment.
DATABASE_ENGINE_FIELDS: dict[str, frozenset[str]] = {
    engine: fields - DATABASE_COMMON for engine, fields in _DATABASE_FIELDS.items()
}
DATABASE_REQUIRED: frozenset[str] = frozenset({"project_uuid", "server_uuid"})

#: Readable in a GET, settable through no endpoint. Values are Coolify's column
#: defaults, read out of the live schema rather than inferred — the first draft
#: guessed 30/30/3/30 and got four of five wrong, which would have warned on
#: every stock database and taught operators to skip warnings.
#: test_health_check_defaults_match_schema keeps them honest.
DATABASE_HEALTH_CHECK_DEFAULTS: dict[str, object] = {
    "health_check_enabled": True,
    "health_check_interval": 15,
    "health_check_timeout": 5,
    "health_check_retries": 5,
    "health_check_start_period": 5,
}


def database_health_check_warnings(source: dict[str, object]) -> list[str]:
    """Warn about health-check settings the API cannot carry to the target.

    Empty when the source runs Coolify's defaults, which is the overwhelmingly
    common case — no point taxing every migration with a warning about a setting
    nobody touched.
    """
    deviations = [
        f"{field}={source[field]!r} (target will get {default!r})"
        for field, default in DATABASE_HEALTH_CHECK_DEFAULTS.items()
        if field in source and source[field] != default
    ]
    if not deviations:
        return []
    return [
        "health check settings cannot be migrated — Coolify accepts them on no "
        "create endpoint, so the target starts with defaults. Re-apply by hand: "
        + ", ".join(deviations)
    ]


# ── services ─────────────────────────────────────────────────────────────────

SERVICE_CREATE: frozenset[str] = cv.known_fields("services/template")
#: Identical to SERVICE_CREATE — the compose branch has NO extra create field
#: (the registry says why connect_to_docker_network is not one).
SERVICE_CREATE_CUSTOM_COMPOSE: frozenset[str] = cv.known_fields("services/compose")
SERVICE_UPDATE: frozenset[str] = cv.known_fields("services.update")
SERVICE_REQUIRED: frozenset[str] = frozenset({"project_uuid", "server_uuid"})

# ── applications ─────────────────────────────────────────────────────────────

#: Every application create field on some release — the five routes share one
#: $allowedFields upstream.
APPLICATION_CREATE: frozenset[str] = frozenset().union(
    *(cv.known_fields(f"applications/{route}") for route in cv.APPLICATION_ROUTES)
)

#: PATCH /v1/applications/{uuid}.
#:
#: NOTE the two deliberate absences, both verified:
#:   * ``docker_compose_raw`` — a dockercompose application's compose is loaded
#:     from git by ``LoadComposeFile``; it cannot be set here. (Services CAN.)
#:   * ``dockerfile`` — likewise not updatable.
#: Attempting either is a 422, so they must never enter a PATCH body.
APPLICATION_UPDATE: frozenset[str] = (
    APPLICATION_CREATE
    - {
        "docker_compose_raw",
        "dockerfile",
        "project_uuid",
        "server_uuid",
        "destination_uuid",
        "environment_name",
        "environment_uuid",
    }
) | {"dockerfile_location", "dockerfile_target_build"}

#: Route -> the fields that route additionally REQUIRES.
APPLICATION_ROUTE_REQUIRED: dict[str, frozenset[str]] = {
    "public": frozenset({"git_repository", "git_branch", "build_pack"}),
    "private-github-app": frozenset(
        {"git_repository", "git_branch", "build_pack", "github_app_uuid"}
    ),
    "private-deploy-key": frozenset(
        {"git_repository", "git_branch", "build_pack", "private_key_uuid"}
    ),
    "dockerfile": frozenset({"dockerfile"}),
    "dockerimage": frozenset({"docker_registry_image_name"}),
}

APPLICATION_REQUIRED: frozenset[str] = frozenset({"project_uuid", "server_uuid"})


def application_settings_warnings(source: dict[str, Any]) -> list[str]:
    """Warn about naming settings the migration drops on the way to the target. PURE.

    ``source`` is the application GET. Coolify >=4.2 nests the settings under
    ``settings``; the create body takes them flat and filter_body only reads the
    flat dict, so the target always comes up with these unset. Most settings are
    booleans whose defaults are what people want anyway — these two are text the
    operator chose, and dropping them changes what the application is called:

    * ``custom_internal_name`` — the container name, and with it the hostname
      other containers on the network reach the app by. Which mode makes it apply
      flipped in 4.4: up to 4.3.x it is used when
      ``is_consistent_container_name_enabled`` is OFF (ApplicationDeploymentJob
      v4.3.2:263); from 4.4.0 only when it is ON (the API turns it on implicitly
      when the name is set). Losing it can break service-to-service traffic.
    * ``custom_container_name_prefix`` (4.4.0+) — replaces the uuid at the start of
      the generated, per-deploy container name. Cosmetic: nothing can depend on a
      name that changes on every deploy.

    Empty when neither is set, which is the overwhelmingly common case — a warning
    on every stock application would teach operators to skip the warnings panel.
    """
    settings = source.get("settings")
    if not isinstance(settings, dict):
        # 4.1.x does not eager-load settings: nothing to read, nothing to claim.
        return []
    out: list[str] = []
    # Deliberately NOT conditioned on is_consistent_container_name_enabled: the
    # flag's meaning flipped in 4.4 and the planner does not know the version
    # here. A rare false alarm costs a glance; a missed one costs reachability.
    if internal := settings.get("custom_internal_name"):
        out.append(
            f"custom_internal_name={internal!r} cannot be migrated, so the target "
            "comes up with Coolify's generated container name. If other containers "
            f"reach this application as {internal!r}, they will not find it on the "
            "target. Set it on the target by hand (Advanced > Container Names) "
            "before switching traffic over."
        )
    if prefix := settings.get("custom_container_name_prefix"):
        out.append(
            f"custom_container_name_prefix={prefix!r} cannot be migrated, so the "
            "target's containers are named after its uuid instead. Cosmetic — re-set "
            "it on the target by hand if you rely on it in `docker ps` or logs."
        )
    return out

# ── storages ─────────────────────────────────────────────────────────────────
# POST /v1/{kind}/{uuid}/storages. `host_path` up to 4.3.21 only, `is_host_file`
# from 4.2.0 — see the registry.

STORAGE_CREATE: frozenset[str] = cv.known_fields("storages")
STORAGE_CREATE_SERVICE: frozenset[str] = cv.known_fields("storages/services")
STORAGE_REQUIRED: frozenset[str] = frozenset({"type", "mount_path"})

#: Fields valid only for `type=persistent`.
STORAGE_PERSISTENT_ONLY: frozenset[str] = frozenset({"name", "host_path"})
#: Fields valid only for `type=file`.
STORAGE_FILE_ONLY: frozenset[str] = frozenset({"content", "is_directory", "is_host_file", "fs_path"})

# ── scheduled tasks ──────────────────────────────────────────────────────────

SCHEDULED_TASK_CREATE: frozenset[str] = cv.known_fields("scheduled-tasks")
SCHEDULED_TASK_REQUIRED: frozenset[str] = frozenset({"name", "command", "frequency"})
#: Only these two have task routes (routes/api.php v4.1.2:195-205, v4.5.0:441-453),
#: and the scheduled_tasks table has only application_id/service_id — a database
#: cannot hold one through any API.
SCHEDULED_TASK_COLLECTIONS: frozenset[str] = frozenset({"applications", "services"})

# ── database backup schedules ────────────────────────────────────────────────

BACKUP_CREATE: frozenset[str] = cv.known_fields("backups")
#: Accepted too, but never copied off a GET: build_backup_body sets them from the
#: destinations a 4.5 row discloses. `s3_storage_uuids` exists from v4.5.0 only.
BACKUP_S3_FIELDS: frozenset[str] = frozenset({"s3_storage_uuid", "s3_storage_uuids"})
#: Returned by every GET and never sent. disable_local_backup joins the create
#: array only at v4.5.1 (DatabasesController $backupConfigFields) and 422s on
#: v4.5.0, the newest release we validate, so the plan reports it instead;
#: description is in no create array at all.
BACKUP_UNSETTABLE: frozenset[str] = frozenset({"disable_local_backup", "description"})

# ── environment variables ────────────────────────────────────────────────────

ENV_FIELDS: frozenset[str] = cv.known_fields("envs")

#: Coolify >=4.2 tightened what an environment variable KEY may look like.
#:
#: ``app/Support/ValidationPatterns.php::ENVIRONMENT_VARIABLE_KEY_PATTERN``::
#:
#:     4.1.2:  /\A[^=\x00]+\z/u               anything without '=' or NUL
#:     4.3.x:  /\A[A-Za-z_][A-Za-z0-9_.]*\z/u letter/underscore, then word chars + '.'
#:
#: Why this matters far more than a normal validation tweak: the rule is enforced
#: by ``create_bulk_envs`` — the exact endpoint :meth:`CoolifyClient.set_envs_bulk`
#: uses — and upstream validates and SAVES inside the same ``foreach``, with no
#: transaction. A rejected key at position N therefore leaves entries 1..N-1
#: already written and returns 422. The target ends up with a PARTIAL environment,
#: mid-cutover, which is far worse than a clean refusal.
#:
#: Keys created under the old rule survive in the database untouched, so an
#: instance upgraded to 4.3.x can hold keys its own API would now reject. That is
#: the migration-relevant case: source and target share one control plane, and the
#: source's stored keys are replayed through the new validator.
_ENV_KEY_PATTERN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.]*\Z")

#: Upstream's rule list is ``['required', 'string', 'max:255', 'regex:...']`` and
#: Laravel applies ALL of them, so length is a separate failure from shape.
_ENV_KEY_MAX_LENGTH = 255


def env_key_rejection(key: str) -> str | None:
    """Why Coolify >=4.2 would reject this env var key, or ``None`` if it passes.

    PURE. Returns a human-readable reason rather than a bool, because the operator
    has to fix the key by hand on the source and "invalid" alone does not say how.

    Length is checked BEFORE shape: a 260-character key matches the pattern and
    would still be rejected by ``max:255``, so testing the regex first reports a
    doomed key as clean.
    """
    if not key:
        return "is empty"
    if len(key) > _ENV_KEY_MAX_LENGTH:
        return f"is {len(key)} characters (Coolify allows at most {_ENV_KEY_MAX_LENGTH})"
    if _ENV_KEY_PATTERN.match(key):
        return None
    if not re.match(r"[A-Za-z_]", key[0]):
        return f"starts with {key[0]!r} — must start with a letter or underscore"
    bad = sorted({c for c in key[1:] if not re.match(r"[A-Za-z0-9_.]", c)})
    return "contains " + ", ".join(repr(c) for c in bad) + " — only letters, digits, '_' and '.'"


def env_key_warnings(envs: list[dict[str, Any]]) -> list[str]:
    """Warn about source env keys the target's API would reject. PURE.

    Empty for the overwhelmingly common case of ``SCREAMING_SNAKE_CASE`` keys — 0
    of 2751 keys across our own fleet were affected (measured 2026-08-15) — so
    this does not tax every migration with noise.

    Deliberately a WARNING and not a hard block. The rule bites only instances
    that predate 4.2 and hold legacy keys; refusing to plan on it would block
    migrations that cannot fail this way. But it is surfaced at PLAN time, because
    the alternative is discovering it as a half-written env set during cutover.
    """
    out: list[str] = []
    for env in envs:
        key = str(env.get("key") or "")
        if not key:
            # A keyless entry is never sent: build_env_entries drops it (`if
            # entry.get("key")`). Warning about something that cannot reach the API
            # would be a false alarm, and this warning has to stay trustworthy.
            continue
        reason = env_key_rejection(key)
        if reason:
            out.append(
                f"environment variable {key!r} {reason}. Coolify >=4.2 rejects it on "
                "the bulk endpoint, and because upstream saves inside the validation "
                "loop the target would be left with a PARTIAL environment. Rename it "
                "on the source before migrating."
            )
    return out


def filter_body(body: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """Keep only whitelisted keys, dropping ``None`` values.

    ``None`` is dropped rather than sent: Coolify's validators treat a present-
    but-null field differently from an absent one for several fields, and "we
    have no opinion" is expressed by omission.
    """
    return {k: v for k, v in body.items() if k in allowed and v is not None}


def rejected_keys(body: dict[str, Any], allowed: frozenset[str]) -> frozenset[str]:
    """Keys that would trigger a 422. Used to assert we never send one."""
    return frozenset(body) - allowed


def missing_required(body: dict[str, Any], required: frozenset[str]) -> frozenset[str]:
    """Required keys absent from the body, so we fail before the round trip."""
    return frozenset(k for k in required if body.get(k) is None)


def filter_for(
    body: Mapping[str, Any], endpoint: str, version: Version | None
) -> dict[str, Any]:
    """The part of ``body`` a request to ``endpoint`` on ``version`` may carry.

    :func:`filter_body` with the registry's whitelist for that release — plus the
    fields the source GET NESTS (``read_from``, e.g. an application setting under
    ``settings``), taken from there.
    """
    out: dict[str, Any] = {}
    for name, nested in cv.field_sources(endpoint, version).items():
        holder = body.get(nested) if nested else body
        if isinstance(holder, Mapping) and holder.get(name) is not None:
            out[name] = holder[name]
    return out


def database_allowed(engine: str, version: Version | None = None) -> frozenset[str]:
    """The whitelist for one engine's create endpoint.

    With a ``version``, the fields that release accepts; without one, every field
    the route takes on some release.

    Raises:
        KeyError: On an unknown engine — better than silently sending a body
            that will 422 field-by-field.
    """
    if version is None:
        return _DATABASE_FIELDS[engine]
    return fields_for(f"databases/{engine}", version)
