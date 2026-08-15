"""Per-endpoint request field whitelists.

PURE module: no IO.

**Why this exists.** Coolify's controllers enforce ``$allowedFields`` and return
``422 "This field is not allowed."`` for anything else::

    array_diff(array_keys($request->all()), $allowedFields)

So you can NEVER round-trip a ``GET`` response into a ``POST`` body — the GET
returns the full model (dozens of columns), the POST accepts a curated subset,
and the difference is a 422 per extra field. Every request body this tool sends
is therefore filtered through an explicit whitelist declared here.

**Keeping it honest.** These lists are transcribed from upstream source, which
means they can drift when Coolify changes. ``tests/test_api_fields.py`` validates
them against the live ``openapi.json``, and a scheduled CI job re-runs that
against Coolify ``main`` — so drift breaks our CI rather than someone's
production migration. Note that OpenAPI and ``$allowedFields`` genuinely disagree
in places (the attributes are documentation; the arrays are the enforcement), so
where they conflict, **the arrays win** and the test records the exception.

Verified against coollabsio/coolify@main.
"""

from __future__ import annotations

import re
from typing import Any

# ── Databases ────────────────────────────────────────────────────────────────
# DatabasesController::create_database. Required: project_uuid, server_uuid, plus
# one of environment_name|environment_uuid. destination_uuid becomes mandatory
# only when the server has more than one destination.

DATABASE_COMMON: frozenset[str] = frozenset(
    {
        "name",
        "description",
        # ALWAYS send this. The model's created hook parses the tag to pick the
        # volume mount path (Postgres >=18 -> /var/lib/postgresql, else
        # /var/lib/postgresql/data). An unpinned image can land mirrored bytes
        # where the server will not look for them.
        "image",
        "public_port",
        "public_port_timeout",
        "is_public",
        "project_uuid",
        "environment_name",
        "environment_uuid",
        "server_uuid",
        "destination_uuid",
        "instant_deploy",
        "limits_memory",
        "limits_memory_swap",
        "limits_memory_swappiness",
        "limits_memory_reservation",
        "limits_cpus",
        "limits_cpuset",
        "limits_cpu_shares",
        # NOT tags — carried post-create by resources.copy_tags. See SERVICE_CREATE.
        # NOT health_check_*. They come back in every GET, and the obvious move is
        # to send them on. Coolify lists them in no $allowedFields — not on create,
        # not on update — so any request carrying one is rejected wholesale with
        # 422 and nothing is created at all.
        #
        # Readable, never settable: the mirror image of the settings gap on
        # applications. Unsettable is not the same as unimportant, so the planner
        # reports a source whose health check deviates rather than quietly
        # dropping it — see database_health_check_warnings below.
    }
)

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
        "endpoint, so the target starts with defaults. Re-apply by hand: "
        + ", ".join(deviations)
    ]

#: Engine-specific credential/config fields, keyed by API path segment.
DATABASE_ENGINE_FIELDS: dict[str, frozenset[str]] = {
    "postgresql": frozenset(
        {
            "postgres_user",
            "postgres_password",
            "postgres_db",
            "postgres_initdb_args",
            "postgres_host_auth_method",
            "postgres_conf",
        }
    ),
    "mysql": frozenset(
        {"mysql_root_password", "mysql_password", "mysql_user", "mysql_database", "mysql_conf"}
    ),
    "mariadb": frozenset(
        {
            "mariadb_conf",
            "mariadb_root_password",
            "mariadb_user",
            "mariadb_password",
            "mariadb_database",
        }
    ),
    "mongodb": frozenset(
        {
            "mongo_conf",
            "mongo_initdb_root_username",
            "mongo_initdb_root_password",
            "mongo_initdb_database",
        }
    ),
    "redis": frozenset({"redis_password", "redis_conf"}),
    "keydb": frozenset({"keydb_password", "keydb_conf"}),
    "dragonfly": frozenset({"dragonfly_password"}),
    "clickhouse": frozenset({"clickhouse_admin_user", "clickhouse_admin_password"}),
}

DATABASE_REQUIRED: frozenset[str] = frozenset({"project_uuid", "server_uuid"})

# ── Services ─────────────────────────────────────────────────────────────────
# ServicesController::create_service. `type` XOR `docker_compose_raw`; sending
# BOTH is a 422. docker_compose_raw MUST be base64-encoded and is re-dumped via
# Yaml::dump(Yaml::parse(...)) — comments and formatting are destroyed.

SERVICE_CREATE: frozenset[str] = frozenset(
    {
        "type",
        "name",
        "description",
        "project_uuid",
        "environment_name",
        "environment_uuid",
        "server_uuid",
        "destination_uuid",
        "instant_deploy",
        "docker_compose_raw",
        "urls",
        "force_domain_override",
        "is_container_label_escape_enabled",
        # NOT tags — and this is now a permanent decision, not a version wait.
        #
        # History: shipped once (2.5.6) from a field read off `main` that no
        # release carried, which broke every migration against every Coolify that
        # existed. Reverted in 2.5.7. It became real in v4.2.0 (ServicesController
        # :358, ApplicationsController:1123, DatabasesController:1762 + all eight
        # engine routes) and our fleet has since moved to 4.3.2, so the old
        # "target is too old" objection has expired.
        #
        # It stays out because tags are copied OFF the critical path instead, by
        # resources.copy_tags as a post-create POST /{collection}/{uuid}/tags. That
        # is better than a create field on both axes that matter here: it works
        # against 4.1.2 and 4.3.x alike (the route simply 404s on the older one),
        # and it is non-fatal. A create field can be neither — an unlisted field is
        # a 422 that destroys the whole resource, so cosmetic metadata would sit on
        # the critical path of a cutover forever.
        # Adjudication recorded in tests/test_api_fields.py::KNOWN_TAGS_GAP.
    }
)

#: Identical to SERVICE_CREATE — the compose branch has NO extra create field.
#:
#: This once added ``connect_to_docker_network``, read from a second
#: ``$allowedFields`` at ServicesController line 505. That list is real, but it
#: sits AFTER the extra-field rejection at line 332, which validates *both*
#: branches against the first list (line 296) — the one without it. So a compose
#: create carrying ``connect_to_docker_network`` is rejected 422 before line 505
#: is ever reached. The field is settable only on update; ``create_service``
#: carries it with a follow-up PATCH. Found by the e2e compose-service migration.
SERVICE_CREATE_CUSTOM_COMPOSE: frozenset[str] = SERVICE_CREATE

#: PATCH /v1/services/{uuid}. Unlike applications, compose IS updatable here.
SERVICE_UPDATE: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "instant_deploy",
        "docker_compose_raw",
        "connect_to_docker_network",
        "urls",
        "force_domain_override",
        "is_container_label_escape_enabled",
    }
)

SERVICE_REQUIRED: frozenset[str] = frozenset({"project_uuid", "server_uuid"})

# ── Applications ─────────────────────────────────────────────────────────────
# All five create routes share one $allowedFields (ApplicationsController.php:914).

APPLICATION_CREATE: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "project_uuid",
        "environment_name",
        "environment_uuid",
        "server_uuid",
        "destination_uuid",
        "instant_deploy",
        "git_repository",
        "git_branch",
        "git_commit_sha",
        "github_app_uuid",
        "private_key_uuid",
        "build_pack",
        "ports_exposes",
        "ports_mappings",
        "base_directory",
        "publish_directory",
        "install_command",
        "build_command",
        "start_command",
        "static_image",
        "dockerfile",
        "dockerfile_location",
        # NOT dockerfile_target_build. It is in the UPDATE list and in every GET,
        # so it looks like a create field and was transcribed as one — but it
        # appears in NO create $allowedFields, neither at v4.1.2 (:914) nor at
        # v4.3.3 (:1194). A source app with a multi-stage build target set would
        # therefore 422 the whole create, and the resource is never made.
        #
        # Never triggered in our fleet (0 of 39 apps carry one, measured
        # 2026-08-15 against paas.cloud), which is exactly why it survived: a
        # latent whitelist error only fires for the one operator who uses the
        # feature. APPLICATION_UPDATE re-adds it explicitly below — it is genuinely
        # settable there, just not on create.
        "docker_registry_image_name",
        "docker_registry_image_tag",
        "docker_compose_location",
        "docker_compose_raw",
        "docker_compose_custom_start_command",
        "docker_compose_custom_build_command",
        "docker_compose_domains",
        "domains",
        "redirect",
        "custom_labels",
        "custom_docker_run_options",
        "custom_nginx_configuration",
        "watch_paths",
        # NOT tags — carried post-create by resources.copy_tags. See SERVICE_CREATE.
        "health_check_enabled",
        "health_check_path",
        "health_check_port",
        "health_check_host",
        "health_check_method",
        "health_check_return_code",
        "health_check_scheme",
        "health_check_response_text",
        "health_check_interval",
        "health_check_timeout",
        "health_check_retries",
        "health_check_start_period",
        "limits_memory",
        "limits_memory_swap",
        "limits_memory_swappiness",
        "limits_memory_reservation",
        "limits_cpus",
        "limits_cpuset",
        "limits_cpu_shares",
        "manual_webhook_secret_github",
        "manual_webhook_secret_gitlab",
        "manual_webhook_secret_bitbucket",
        "manual_webhook_secret_gitea",
        "post_deployment_command",
        "post_deployment_command_container",
        "pre_deployment_command",
        "pre_deployment_command_container",
        # Settings. These were write-only when this list was written; upstream has
        # since added `->with('settings')` to application_by_uuid, so they DO come
        # back on the GET now — but nested under a `settings` object, whereas the
        # create body takes them flat. filter_body reads the flat dict, so the
        # values never arrive and the target comes up on defaults. That is an
        # accepted outcome, not a gap to close: Coolify's defaults already enable
        # the settings worth having. See KNOWN_APPLICATION_GAP in
        # tests/test_api_fields.py for the reasoning and the 13 sibling fields.
        "is_static",
        "is_spa",
        "is_auto_deploy_enabled",
        "is_force_https_enabled",
        "connect_to_docker_network",
        "use_build_server",
        "is_container_label_escape_enabled",
        "is_preserve_repository_enabled",
        "is_http_basic_auth_enabled",
        "http_basic_auth_username",
        "http_basic_auth_password",
    }
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

# ── Storages ─────────────────────────────────────────────────────────────────
# POST /v1/{kind}/{uuid}/storages.
#
# The name you send is NOT the name you get: upstream forces
# `name = '{resource_uuid}-{name}'`. Volume names cannot be preserved.
#
# Services additionally require `resource_uuid` to target the sub-resource.

STORAGE_CREATE: frozenset[str] = frozenset(
    {"type", "name", "mount_path", "host_path", "content", "is_directory", "fs_path"}
)
STORAGE_CREATE_SERVICE: frozenset[str] = STORAGE_CREATE | {"resource_uuid"}
STORAGE_REQUIRED: frozenset[str] = frozenset({"type", "mount_path"})

#: Fields valid only for `type=persistent`.
STORAGE_PERSISTENT_ONLY: frozenset[str] = frozenset({"name", "host_path"})
#: Fields valid only for `type=file`.
STORAGE_FILE_ONLY: frozenset[str] = frozenset({"content", "is_directory", "fs_path"})

# ── Environment variables ────────────────────────────────────────────────────
# Identical for single create and bulk. Bulk body is {"data": [...]} — the key is
# literally `data`. Unlike create endpoints, bulk SILENTLY DROPS unknown keys
# rather than erroring, which makes a typo invisible; filter anyway.

ENV_FIELDS: frozenset[str] = frozenset(
    {
        "key",
        "value",
        "is_preview",
        "is_literal",
        "is_multiline",
        "is_shown_once",
        "is_runtime",
        "is_buildtime",
        "comment",
    }
)

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


def database_allowed(engine: str) -> frozenset[str]:
    """The full whitelist for one engine's create endpoint.

    Raises:
        KeyError: On an unknown engine — better than silently sending a body
            that will 422 field-by-field.
    """
    return DATABASE_COMMON | DATABASE_ENGINE_FIELDS[engine]
