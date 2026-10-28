"""Every difference between Coolify releases this tool knows about, in ONE place.

PURE module: no IO.

Coolify's API contract moves with every release: a create field appears, a
storage field disappears, a validator tightens, a GET starts hiding a value. This
module is the single table of those facts, so that supporting the next release is
a DATA change here — a row with the tag it starts in — and not a hunt through the
code for every place that compared a version. Three tables:

* :data:`FEATURES` — named behaviours, each with the :class:`Gate` (the release
  range) it holds in. Rules ask :func:`supports`.
* :data:`CREATE_FIELDS` / :data:`UPDATE_FIELDS` — per endpoint, the request
  fields this tool sends, each with its Gate and, where the source GET nests it,
  where to read it (``read_from``). Request bodies ask :func:`fields_for`;
  ``api/fields.py`` builds its whitelists from them.
* :data:`VALIDATED_MIN` / :data:`VALIDATED_MAX` — the releases this contract has
  been audited against (release tags, never ``main``).

The instance version is read once per run (the planner's GET /version), recorded
on the plan and used by the plan's rules and CREATE's bodies alike, so the two
can never disagree about which release they are talking to.

**An UNKNOWN version** (unreadable, unparseable) is answered per use, and the two
uses fail in opposite directions on purpose:

* a rule that REFUSES or WARNS applies — "we could not tell which Coolify this
  is" is not evidence that the protection is unnecessary (:func:`supports`);
* a field is SENT only if every release in the validated range accepts it — the
  intersection — so an unknown instance never gets a field that 422s the whole
  resource (:meth:`Field.sent_on`). The one exception is a field whose column
  shipped together with its acceptance (``column_gated``): the source GET can
  only carry its key where the create takes it, and filter_body sends only keys
  the source has.

Versions are int tuples, never strings: ``4.3.10`` is newer than ``4.3.3``
numerically and older lexically (domain/versions.py).

How to add a release: AGENTS.md, "Supporting a new Coolify release".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from bg_coolify_migrate.domain.versions import Version

# ── the validated range ──────────────────────────────────────────────────────

#: The Coolify releases this tool's API contract has been validated against, as
#: an INCLUSIVE range. Anything outside is flagged as untested (cli.doctor), not
#: silently trusted.
#:
#: A range, not a single version, because a single one is wrong almost always. The
#: field tables below are transcribed from upstream source and match a SPAN of
#: releases; pinning one value made every run against the 4.3.2 fleet warn about a
#: version that had in fact been checked. A warning that fires on a correct setup
#: trains operators to ignore warnings, which costs more than it ever saves.
#:
#: Verified 2026-08-16 by diffing every `$allowedFields` array and every API route
#: between tags v4.1.2 and v4.3.3: nothing was removed, only added. Extend the
#: upper bound only after repeating that diff — never because a newer tag exists.
#:
#: Re-verified 2026-08-21 for v4.3.10, that diff repeated: ApplicationsController,
#: ServicesController, DatabasesController and ValidationPatterns are BYTE-IDENTICAL
#: to v4.3.3, all 29 $allowedFields arrays unchanged, and no route removed since
#: v4.1.2. The only changes are two added /settings/email routes and one reworded
#: error string in the server-create path, which this tool never calls.
#:
#: Re-verified 2026-10-26 for v4.5.0, and this time the diff was NOT clean —
#: v4.3.10 -> v4.5.0 is the first span since 4.1.2 that broke the contract, so the
#: bound moved only together with the code that answers it. Removed: `host_path`
#: from every storage create array (v4.3.22, backported). Tightened: manual
#: webhook secrets >= 16 chars (4.4.0), http_basic_auth_password <= 72 (4.5.0).
#: New and invisible to the API: hidden domain port overrides (4.3.15), secret
#: manager references (4.4.0), external/driver_opts compose volumes used as
#: written (4.4.0), server roles (4.4.0), SQLite (4.4.0), and legacy env escaping
#: that new rows do not inherit (4.5.0). Each is now refused or reported at plan
#: time — see domain/fidelity.py — and the routes, stop/start/restart guards,
#: container labels, volume names and mount paths this tool depends on are
#: unchanged. Audited against release tags, not a live 4.5.0.
VALIDATED_MIN: Version = (4, 1, 2)
VALIDATED_MAX: Version = (4, 5, 0)


def format_version(version: Version) -> str:
    """``(4, 5, 0)`` -> ``"4.5.0"``."""
    return ".".join(str(part) for part in version)


# ── gates ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Gate:
    """The releases something holds in: ``since`` inclusive, ``until`` EXCLUSIVE.

    ``Gate()`` holds everywhere; ``Gate(since=(4, 5, 1))`` from 4.5.1 on;
    ``Gate(until=(4, 3, 22))`` up to and including 4.3.21.
    """

    since: Version | None = None
    until: Version | None = None

    def holds(self, version: Version | None) -> bool:
        """Whether a release is inside the gate. UNKNOWN counts as inside.

        Unknown-inside is the policy of a bare gate, which only ever decides a
        CHECK (:func:`supports`): it must fail toward checking. Fields decide
        what to SEND and answer an unknown version with :meth:`holds_throughout`
        instead (:meth:`Field.sent_on`).
        """
        if version is None:
            return True
        return (self.since is None or version >= self.since) and (
            self.until is None or version < self.until
        )

    def holds_throughout(
        self, low: Version = VALIDATED_MIN, high: Version = VALIDATED_MAX
    ) -> bool:
        """Whether EVERY release from ``low`` to ``high`` (inclusive) is inside."""
        return (self.since is None or self.since <= low) and (
            self.until is None or self.until > high
        )


ALWAYS = Gate()


# ── behaviours ───────────────────────────────────────────────────────────────

#: Named behaviours and the releases they hold in. Each entry cites the release
#: tag it starts in; a rule gated on one never refuses a migration on an
#: instance where it cannot fail that way.
FEATURES: Mapping[str, Gate] = MappingProxyType(
    {
        # applyServiceUrls lowercases scheme and host before its duplicate-URL
        # check (ValidationPatterns::normalizeApplicationDomains); 4.1.x compares
        # raw strings.
        "service_urls_normalised": Gate(since=(4, 2, 0)),
        # An explicit domain port moves into the hidden applications.domain_port_overrides.
        "port_overrides_hidden": Gate(since=(4, 3, 15)),
        # ...and a compose service's domain port too: a UI save from 4.3.15
        # (Livewire Application/Domains.php v4.3.15:1995-2011), an API save from
        # 4.3.18 (normalizeDockerComposeDomainPorts), on the 4.3 line and in
        # 4.4.0+ alike.
        "compose_port_overrides_hidden": Gate(since=(4, 3, 15)),
        # A portless compose domain falls back to its SERVICE's first port, then
        # to ports_exposes (parsers.php v4.3.16:1361); 4.3.15 routed it to
        # ports_exposes.
        "compose_service_port_fallback": Gate(since=(4, 3, 16)),
        # ...the service's first TCP port only, and nothing else (parsers.php v4.3.18:1357).
        "compose_tcp_port_fallback": Gate(since=(4, 3, 18)),
        # ManualWebhookSecret: a new secret must be >= 16 characters.
        "webhook_secret_min_length": Gate(since=(4, 4, 0)),
        # {{vault.KEY}} references, resolved from a link no GET returns.
        "secret_manager": Gate(since=(4, 4, 0)),
        # External and driver_opts compose volumes are used as written, not renamed.
        "compose_volumes_as_written": Gate(since=(4, 4, 0)),
        # Docker compose is stored verbatim instead of Yaml::dump(Yaml::parse()).
        "compose_stored_verbatim": Gate(since=(4, 4, 0)),
        # Redis/KeyDB/Dragonfly rows that predate it keep the unquoted --requirepass.
        "redis_password_quoting": Gate(since=(4, 4, 0)),
        # http_basic_auth_password gains max:72 on create.
        "basic_auth_max_length": Gate(since=(4, 5, 0)),
        # New env rows use exact escaping; rows saved before keep the legacy
        # escaping. Asked of an env row's own `version` stamp as well as of the
        # instance (locked_envs.saved_before_exact_escaping).
        "exact_env_escaping": Gate(since=(4, 5, 0)),
        # EnvironmentVariable::RUNTIME_BUILDPACK_CONTROL_VARIABLES (v4.5.0:50-55;
        # absent at v4.4.7): buildpack control keys the runtime .env carries after all.
        "runtime_buildpack_control": Gate(since=(4, 5, 0)),
        # Both compose parsers refuse `scale` / `deploy.replicas` above 1.
        "compose_scale_check": Gate(since=(4, 5, 0)),
        # convertDockerRunToCompose maps flags it used to ignore.
        "run_options_parser": Gate(since=(4, 5, 0)),
        # GET/POST/DELETE /applications/{uuid}/destinations exist (routes/api.php
        # v4.3.0:269-271).
        "application_destinations_api": Gate(since=(4, 3, 0)),
        # add_destination refuses compose apps, persistent storage and a proxy
        # mismatch; a multi-server deploy needs a Docker image
        # (ensureRegistryImageForMultipleServers).
        "multi_server_restrictions": Gate(since=(4, 4, 0)),
        # Task timeout gains max:36000 on create and update (ScheduledTasksController
        # v4.4.0:73, v4.5.0:88; plain integer|min:1 up to v4.3.24).
        "task_timeout_max": Gate(since=(4, 4, 0)),
        # ScheduledTaskJob also matches a task's container by its exact FULL name
        # (commit 5ae125ad3, first released in v4.5.0:190; prefix-only at v4.4.7:174).
        "task_container_exact_name": Gate(since=(4, 5, 0)),
        # A volume's own backup schedule (ScheduledVolumeBackup, local and S3)
        # exists from v4.3.0, on application, database and service storages. Its
        # only routes are PUT, POST run and DELETE on
        # `.../storages/{storage_uuid}/backups` — no GET on any release, and no
        # storages GET loads one — so no plan can see it.
        "volume_backups": Gate(since=(4, 3, 0)),
        # `noindex_domains` exists, and is in the application UPDATE
        # $allowedFields, from v4.3.0 (ApplicationsController v4.3.0:2796, applied
        # after the fqdn at :3115-3120; v4.5.0:3363, :3764-3775).
        "noindex_domains": Gate(since=(4, 3, 0)),
        # Managed DNS ships in v4.4.0 (absent at v4.3.24 and v4.4-rc.1).
        "managed_dns": Gate(since=(4, 4, 0)),
        # GET /databases/{uuid} nulls internal_db_url and external_db_url for a
        # sensitive caller when the credential variable is locked
        # (DatabasesController v4.5.1:262-270; isCredentialLocked in
        # StandaloneRedis :421-424, StandaloneKeydb :413, StandaloneDragonfly
        # :412 — absent at v4.5.0). A locked Redis REDIS_PASSWORD is then
        # recoverable from the source server's file alone.
        "locked_db_url_withheld": Gate(since=(4, 5, 1)),
        # The application and service GETs replace every locked value of 4+
        # characters in the generated docker_compose — labels included — with
        # `<locked>` (redactLockedValuesInGeneratedCompose, shared.php
        # v4.5.1:5812-5868; ApplicationsController :156-159, ServicesController
        # :82-84). docker_compose_raw is not redacted.
        "locked_values_redacted_in_compose": Gate(since=(4, 5, 1)),
        # An enabled Coolify health check replaces the Dockerfile's HEALTHCHECK on
        # every deploy (ApplicationDeploymentJob v4.5.1:4471-4486); up to 4.5.0 it
        # was added only when none was found (v4.5.0:4196-4198). Upstream commit
        # e200927ff, issue #10974.
        "coolify_healthcheck_replaces_dockerfile": Gate(since=(4, 5, 1)),
        # The EnvironmentVariable created hook makes NO preview twin for a
        # dockercompose app's generated magic variable — each pull-request
        # preview generates its own — and copies is_shown_once into the twins it
        # does make (EnvironmentVariable v4.5.1:147-157, isGeneratedMagicVariable
        # :179-183; v4.5.0:118-148 copied every row, unlocked).
        "compose_previews_generate_magic": Gate(since=(4, 5, 1)),
    }
)


def supports(feature: str, version: Version | None, *, if_unknown: bool = True) -> bool:
    """Whether ``version`` has the named behaviour.

    An UNKNOWN version answers ``if_unknown`` — True by default, because every
    rule asking is a check that protects a migration, and an unreadable version
    must fail toward checking. A caller that would make the protective choice
    only for a version it KNOWS has the behaviour passes ``if_unknown=False``.

    Raises:
        KeyError: On a feature that is not in :data:`FEATURES` — a typo must not
            quietly read as "every release".
    """
    gate = FEATURES[feature]
    if version is None:
        return if_unknown
    return gate.holds(version)


# ── request fields ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Field:
    """One request field an endpoint accepts on the releases its gate names."""

    name: str
    gate: Gate = ALWAYS
    read_from: str | None = None
    """Where the source GET carries the value when not at the top level, e.g.
    ``"settings"`` for an application setting the GET nests while the create
    takes it flat."""
    column_gated: bool = False
    """The field's COLUMN shipped in the same release as its acceptance, so a
    source GET carries the key only where the create takes it. Sent on an
    unknown release too: filter_body copies only keys the source has."""

    def sent_on(self, version: Version | None) -> bool:
        """Whether a body for ``version`` may carry this field.

        A known version: its gate. An unknown one: only if every release in the
        validated range accepts it (or it is ``column_gated``) — a field that
        422s takes the whole resource with it.
        """
        if version is None:
            return self.column_gated or self.gate.holds_throughout()
        return self.gate.holds(version)


def _always(*names: str) -> tuple[Field, ...]:
    return tuple(Field(name) for name in names)


#: The five application create routes. They share ONE $allowedFields
#: (ApplicationsController create_application, v4.1.2:914 .. v4.5.0:1340), but
#: are kept apart here so a field that matters for one build pack only can be
#: gated per route.
APPLICATION_ROUTES: tuple[str, ...] = (
    "public",
    "private-github-app",
    "private-deploy-key",
    "dockerfile",
    "dockerimage",
)

# ── applications ─────────────────────────────────────────────────────────────

_APPLICATION: tuple[Field, ...] = (
    *_always(
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
    ),
    # NOT dockerfile_target_build here: see _BUILD_TARGET. Up to v4.5.0 it is in
    # the UPDATE list and in every GET, so it looks like a create field and was
    # once transcribed as one — but it appears in NO create $allowedFields, neither
    # at v4.1.2 (:914) nor at v4.3.3 (:1194) nor at v4.5.0 (:1340). A source app
    # with a multi-stage build target set would therefore 422 the whole create,
    # and the resource is never made.
    #
    # Never triggered in our fleet (0 of 39 apps carry one, measured 2026-08-15
    # against paas.cloud), which is exactly why it survived: a latent whitelist
    # error only fires for the one operator who uses the feature.
    *_always(
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
    ),
    # Extra Docker network aliases other containers reach the app by. In every
    # create array since v4.1.2 and top-level in the GET, but documented by no
    # openapi schema — so the openapi canary never saw it, and every migration
    # before 2026-10-26 silently dropped the aliases (service-to-service DNS
    # breaks after cutover). test_controller_arrays_are_adjudicated now catches
    # this class: accepted by the array, invisible to the documentation.
    Field("custom_network_aliases"),
    Field("watch_paths"),
    # NOT tags — carried post-create by resources.copy_tags. See _SERVICE.
    Field("health_check_enabled"),
    # Same story as custom_network_aliases: accepted since v4.1.2, undocumented.
    # Without them a CMD health check (a worker with no HTTP listener) came up on
    # the target as an HTTP one and was marked unhealthy.
    *_always(
        "health_check_type",
        "health_check_command",
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
    ),
    # Settings, sent flat. These were write-only when this list was written;
    # upstream has since added `->with('settings')` to application_by_uuid, so
    # they DO come back on the GET now — but nested under a `settings` object.
    # These ones are read flat, so a nested value never arrives and the target
    # comes up on defaults. For the boolean settings that is an accepted outcome:
    # Coolify's defaults already enable the ones worth having. The free-text
    # naming settings are not covered by that argument, so the planner reports
    # them instead — see api/fields.application_settings_warnings, and
    # KNOWN_APPLICATION_GAP in tests/test_api_fields.py for the full reasoning.
    *_always(
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
    ),
    # Stored with Laravel's `encrypted` cast and returned DECRYPTED to a
    # read:sensitive token at every tag — no hashing on save — so the target
    # hashes the same plaintext the source did. Since 4.5.0 it is max:72 on
    # create; fidelity.application_refusals stops a longer one at plan time.
    Field("http_basic_auth_password"),
    # The hash COST of the label hash (v4.5.0). These columns, and their
    # acceptance on create, ship in the same release, so a GET can only carry
    # them where the create accepts them. Carried so a hardened source does not
    # come back with a weaker label hash. NOT http_basic_auth_hash_algorithm:
    # argon2id 422s the whole create unless the TARGET server runs Caddy >= 2.11,
    # so it is a gap with a plan warning (KNOWN_APPLICATION_GAP,
    # fidelity.application_warnings).
    Field("http_basic_auth_bcrypt_cost", Gate(since=(4, 5, 0)), column_gated=True),
    Field("http_basic_auth_argon2id_memory_cost", Gate(since=(4, 5, 0)), column_gated=True),
    Field("http_basic_auth_argon2id_time_cost", Gate(since=(4, 5, 0)), column_gated=True),
    # Three application_settings columns v4.5.1 adds to APPLICATION_SETTING_FIELDS,
    # the spread at the end of the shared create array (ApplicationsController
    # v4.5.1:57,60,66 -> :1372; absent from the v4.5.0 constant :46-72), validated
    # in sharedDataApplications (bootstrap/helpers/api.php v4.5.1:207,213,231) and
    # stored by applyApplicationSettings on every route (:239-247, called at
    # :1686/1946/2175/2323/2458). The GET returns them only under `settings`,
    # while the create takes them flat — hence read_from.
    #
    # Off on the source, the default COOLIFY_* variables stay off on the target.
    Field("include_coolify_default_variables", Gate(since=(4, 5, 1)), read_from="settings"),
    # Off on the source, the target keeps the source's custom_labels verbatim
    # instead of regenerating them (create :1733, update :3814; the deploy uses
    # custom_labels whenever set, ApplicationDeploymentJob v4.5.1:4393). Only
    # safe when those labels route nothing but the app's own custom domains:
    # create_application drops it otherwise (fidelity.labels_route_beyond_domains).
    Field("is_container_label_readonly_enabled", Gate(since=(4, 5, 1)), read_from="settings"),
    # A NEW column (migration 2026_10_23_224202, default 5) accepted in the same
    # release, `integer|min:0|max:300`; create_application clamps it like
    # ApplicationSetting::rollingUpdateDrainSeconds (v4.5.1:174-177).
    Field(
        "rolling_update_drain_seconds",
        Gate(since=(4, 5, 1)),
        read_from="settings",
        column_gated=True,
    ),
)

#: The Dockerfile build stage (`--target`). A top-level column every GET returns,
#: in the UPDATE array at every tag, and in the shared CREATE array only from
#: v4.5.1 (ApplicationsController v4.5.1:1372; v4.5.0:1340 lacks it). Sent on the
#: routes that BUILD a Dockerfile — git and inline dockerfile — never on the
#: image route, which builds nothing.
_BUILD_TARGET: tuple[Field, ...] = (Field("dockerfile_target_build", Gate(since=(4, 5, 1))),)

_APPLICATION_BY_ROUTE: Mapping[str, tuple[Field, ...]] = MappingProxyType(
    {
        "public": _APPLICATION + _BUILD_TARGET,
        "private-github-app": _APPLICATION + _BUILD_TARGET,
        "private-deploy-key": _APPLICATION + _BUILD_TARGET,
        "dockerfile": _APPLICATION + _BUILD_TARGET,
        "dockerimage": _APPLICATION,
    }
)

# ── services ─────────────────────────────────────────────────────────────────
# ServicesController::create_service. `type` XOR `docker_compose_raw`; sending
# BOTH is a 422. docker_compose_raw MUST be base64-encoded and is re-dumped via
# Yaml::dump(Yaml::parse(...)) before 4.4 — comments and formatting are destroyed.

_SERVICE: tuple[Field, ...] = _always(
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
    # History: shipped once (2.5.6) from a field read off `main` that no release
    # carried, which broke every migration against every Coolify that existed.
    # Reverted in 2.5.7. It became real in v4.2.0 (ServicesController :358,
    # ApplicationsController:1123, DatabasesController:1762 + all eight engine
    # routes) and our fleet has since moved to 4.3.2, so the old "target is too
    # old" objection has expired.
    #
    # It stays out because tags are copied OFF the critical path instead, by
    # resources.copy_tags as a post-create POST /{collection}/{uuid}/tags. That is
    # better than a create field on both axes that matter here: it works against
    # 4.1.2 and 4.3.x alike (the route simply 404s on the older one), and it is
    # non-fatal. A create field can be neither — an unlisted field is a 422 that
    # destroys the whole resource, so cosmetic metadata would sit on the critical
    # path of a cutover forever.
    # Adjudication recorded in tests/test_api_fields.py::KNOWN_TAGS_GAP.
)

#: The compose branch has NO extra create field: identical to the template one.
#:
#: This once added ``connect_to_docker_network``, read from a second
#: ``$allowedFields`` at ServicesController line 505. That list is real, but it
#: sits AFTER the extra-field rejection at line 332, which validates *both*
#: branches against the first list (line 296) — the one without it. So a compose
#: create carrying ``connect_to_docker_network`` is rejected 422 before line 505
#: is ever reached. The field is settable only on update; it is carried by the
#: follow-up PATCH in ``resources.finish_service_target``, together with the
#: domains. Found by the e2e compose-service migration.
#:
#: v4.5.0 finally accepts it on create (both arrays), but v4.1.2-v4.4.7 still
#: reject it, and the PATCH works on all of them — so it stays a PATCH.
_SERVICE_COMPOSE: tuple[Field, ...] = _SERVICE

#: PATCH /v1/services/{uuid}. Unlike applications, compose IS updatable here.
#: `urls` items are exactly {name, url} ('array:name,url', ServicesController
#: v4.1.2:971, v4.5.0:1259) — any other key is a 422. force_domain_override
#: reaches a target only when every conflicting holder is the source's own
#: sub-application (resources.finish_service_target).
_SERVICE_UPDATE: tuple[Field, ...] = (
    *_always(
        "name",
        "description",
        "instant_deploy",
        "docker_compose_raw",
        "connect_to_docker_network",
        "urls",
        "force_domain_override",
        "is_container_label_escape_enabled",
    ),
    # Turns the default COOLIFY_* variables off. In the update $allowedFields
    # from v4.5.1 (ServicesController v4.5.1:1245, applied :1331-1333, behind
    # manageEnvironment :1236); v4.5.0:1250 lacks it, a 422 for the whole PATCH —
    # domains included. In no create array: resources.finish_service_target
    # carries it in the follow-up PATCH.
    Field("include_coolify_default_variables", Gate(since=(4, 5, 1))),
)

# ── databases ────────────────────────────────────────────────────────────────
# DatabasesController::create_database. Required: project_uuid, server_uuid, plus
# one of environment_name|environment_uuid. destination_uuid becomes mandatory
# only when the server has more than one destination.

_DATABASE_COMMON: tuple[Field, ...] = _always(
    "name",
    "description",
    # ALWAYS send this. The model's created hook parses the tag to pick the volume
    # mount path (Postgres >=18 -> /var/lib/postgresql, else
    # /var/lib/postgresql/data). An unpinned image can land mirrored bytes where
    # the server will not look for them.
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
    # NOT tags — carried post-create by resources.copy_tags. See _SERVICE.
    # NOT health_check_*. They come back in every GET, and the obvious move is to
    # send them on. No CREATE $allowedFields lists them, so a create carrying one
    # is rejected wholesale with 422 and nothing is created at all. (The UPDATE
    # array does accept them, at every tag since 4.1.2 — a follow-up PATCH could
    # carry them; this tool does not do that yet.)
    #
    # Unsettable on create is not the same as unimportant, so the planner reports
    # a source whose health check deviates rather than quietly dropping it — see
    # api/fields.database_health_check_warnings.
    #
    # NOT enable_ssl / ssl_mode. Like health_check_*, readable in every GET — but
    # worse: no create or update field and no dedicated route carries them at any
    # tag v4.1.2-v4.5.0, so not even a follow-up PATCH can. Only POST
    # /databases/{uuid}/clone (v4.3.0+) does, as a whole-row server-side copy.
    # Adding them here would 422 every create. fidelity.database_tls_lost_warning
    # reports a TLS source instead.
)

#: Engine-specific credential/config fields, keyed by API path segment.
_DATABASE_ENGINE: Mapping[str, tuple[Field, ...]] = MappingProxyType(
    {
        "postgresql": _always(
            "postgres_user",
            "postgres_password",
            "postgres_db",
            "postgres_initdb_args",
            "postgres_host_auth_method",
            "postgres_conf",
        ),
        "mysql": _always(
            "mysql_root_password", "mysql_password", "mysql_user", "mysql_database", "mysql_conf"
        ),
        "mariadb": _always(
            "mariadb_conf",
            "mariadb_root_password",
            "mariadb_user",
            "mariadb_password",
            "mariadb_database",
        ),
        "mongodb": _always(
            "mongo_conf",
            "mongo_initdb_root_username",
            "mongo_initdb_root_password",
            "mongo_initdb_database",
        ),
        "redis": _always("redis_password", "redis_conf"),
        "keydb": _always("keydb_password", "keydb_conf"),
        "dragonfly": _always("dragonfly_password"),
        "clickhouse": _always("clickhouse_admin_user", "clickhouse_admin_password"),
    }
)

#: The engines, as API path segments (POST /databases/{engine}).
DATABASE_ENGINES: tuple[str, ...] = tuple(_DATABASE_ENGINE)

# ── storages ─────────────────────────────────────────────────────────────────
# POST /v1/{kind}/{uuid}/storages ($allAllowedFields in create_storage).
#
# The name you send is NOT the name you get: upstream forces
# `name = '{resource_uuid}-{name}'`. Volume names cannot be preserved.
#
# Services additionally require `resource_uuid` to target the sub-resource.

_STORAGE: tuple[Field, ...] = (
    Field("type"),
    Field("name"),
    Field("mount_path"),
    # Accepted only up to v4.3.21: v4.3.22 (backported) and v4.4.0 removed it
    # from every storage create array (ApplicationsController v4.3.21:5131 vs
    # v4.3.22:5124), so it is a 422 there. fidelity.storage_refusals refuses a
    # source that holds one wherever this gate is closed. Dropping it instead
    # would be silent data loss: the target would mount an empty named volume
    # while rsync mirrored the bytes to a host path nothing reads — and VERIFY
    # would compare those two copies and pass.
    Field("host_path", Gate(until=(4, 3, 22))),
    Field("content"),
    Field("is_directory"),
    # Marks a mount of an existing HOST file (e.g. /etc/timezone) rather than
    # content Coolify manages (v4.2.0:4695; absent at v4.1.2:4268, together with
    # its column). Without it a 4.4+ target takes the managed-file branch for an
    # absolute fs_path: it 422s on a symlink and otherwise `touch`es and
    # `chmod +x`es that host file on the target server.
    Field("is_host_file", Gate(since=(4, 2, 0)), column_gated=True),
    Field("fs_path"),
)

# ── environment variables ────────────────────────────────────────────────────
# Identical for single create and bulk, at every tag v4.1.2:2855 .. v4.5.0:3968.
# Bulk body is {"data": [...]} — the key is literally `data`. Unlike create
# endpoints, bulk SILENTLY DROPS unknown keys rather than erroring, which makes a
# typo invisible; filter anyway.

_ENV: tuple[Field, ...] = _always(
    "key",
    "value",
    "is_preview",
    "is_literal",
    "is_multiline",
    "is_shown_once",
    "is_runtime",
    "is_buildtime",
    "comment",
)

# ── scheduled tasks ──────────────────────────────────────────────────────────
# POST /v1/{applications|services}/{uuid}/scheduled-tasks
# (ScheduledTasksController::createTask). The same six fields at every release,
# v4.1.2:57 through v4.5.0:81. An ABSENT `timeout` becomes 300 and an absent
# `enabled` becomes TRUE (v4.5.0:126-127), so a disabled task must be sent with
# enabled=false explicitly or it comes back armed on the target.

_SCHEDULED_TASK: tuple[Field, ...] = _always(
    "name", "command", "frequency", "container", "timeout", "enabled"
)

# ── database backup schedules ────────────────────────────────────────────────
# POST /v1/databases/{uuid}/backups (DatabasesController::create_backup), whose
# `$backupConfigFields` rejects anything else per field. Base list v4.1.2:704.
# `backup_now` is accepted outside the list and NEVER sent: it dumps at once.
# s3_storage_uuid(s) are accepted too but set by api/schedules.build_backup_body
# from what a 4.5 row discloses, never copied off a GET (api/fields.BACKUP_S3_FIELDS).

_BACKUP: tuple[Field, ...] = (
    *_always(
        "save_s3",
        "enabled",
        "dump_all",
        "frequency",
        "databases_to_backup",
        "database_backup_retention_amount_locally",
        "database_backup_retention_days_locally",
        "database_backup_retention_max_storage_locally",
        "database_backup_retention_amount_s3",
        "database_backup_retention_days_s3",
        "database_backup_retention_max_storage_s3",
        "timeout",
    ),
    # Joins in v4.3.18 and v4.4.0 together with its COLUMN (absent at v4.3.0), so
    # a GET row carries the key exactly where create accepts it.
    Field("missing_backup_notification_days", Gate(since=(4, 3, 18)), column_gated=True),
    # Delete the local dump after the S3 upload. Every GET returns the column; the
    # create array takes it only from v4.5.1 (DatabasesController v4.5.1:1116),
    # and refuses it unless save_s3 is true (:1201-1202) — so
    # api/schedules.build_backup_body sends it only beside save_s3.
    Field("disable_local_backup", Gate(since=(4, 5, 1))),
)

# ── the tables ───────────────────────────────────────────────────────────────

#: Per create endpoint, the fields this tool sends. Keys:
#: ``applications/<route>`` (:data:`APPLICATION_ROUTES`),
#: ``services/template`` and ``services/compose``, ``databases/<engine>``
#: (:data:`DATABASE_ENGINES`), ``storages`` and ``storages/services`` (a service's
#: storage names its sub-resource), ``envs`` (bulk), ``scheduled-tasks`` and
#: ``backups``.
CREATE_FIELDS: Mapping[str, tuple[Field, ...]] = MappingProxyType(
    {
        **{f"applications/{route}": _APPLICATION_BY_ROUTE[route] for route in APPLICATION_ROUTES},
        "services/template": _SERVICE,
        "services/compose": _SERVICE_COMPOSE,
        **{
            f"databases/{engine}": _DATABASE_COMMON + fields
            for engine, fields in _DATABASE_ENGINE.items()
        },
        "storages": _STORAGE,
        "storages/services": (*_STORAGE, Field("resource_uuid")),
        "envs": _ENV,
        "scheduled-tasks": _SCHEDULED_TASK,
        "backups": _BACKUP,
    }
)

#: PATCH /v1/databases/{uuid}, sent right after the create (no create array takes
#: either field). Both are read before removeUnnecessaryFieldsFromRequest strips
#: them, so neither is an "extra" field (DatabasesController v4.5.1:688-702,
#: bootstrap/helpers/api.php :491, :502); on v4.5.0 the update reads neither and
#: include_coolify_default_variables is not stripped — a 422.
_DATABASE_UPDATE: tuple[Field, ...] = (
    # Applied :983-985, behind manageEnvironment :716.
    Field("include_coolify_default_variables", Gate(since=(4, 5, 1))),
    # Applied :980-982 — and 422'd (:720-725) unless the database's server has a
    # log drain on, so api/resources.finish_database_target sends it only then.
    Field("is_log_drain_enabled", Gate(since=(4, 5, 1))),
)

#: Per follow-up PATCH, the fields this tool sends. ``services.update``: PATCH
#: /services/{uuid}; ``databases.update``: PATCH /databases/{uuid}.
UPDATE_FIELDS: Mapping[str, tuple[Field, ...]] = MappingProxyType(
    {
        "services.update": _SERVICE_UPDATE,
        "databases.update": _DATABASE_UPDATE,
    }
)


def endpoint_fields(endpoint: str) -> tuple[Field, ...]:
    """Every field row of one endpoint, whatever its gate.

    Raises:
        KeyError: On an unknown endpoint — better than a body that 422s field by
            field.
    """
    if endpoint in CREATE_FIELDS:
        return CREATE_FIELDS[endpoint]
    return UPDATE_FIELDS[endpoint]


def fields_for(endpoint: str, version: Version | None) -> frozenset[str]:
    """The field names a request to ``endpoint`` may carry on ``version``."""
    return frozenset(f.name for f in endpoint_fields(endpoint) if f.sent_on(version))


def field_sources(endpoint: str, version: Version | None) -> Mapping[str, str | None]:
    """``{field: read_from}`` of the fields :func:`fields_for` admits."""
    return {f.name: f.read_from for f in endpoint_fields(endpoint) if f.sent_on(version)}


def known_fields(endpoint: str) -> frozenset[str]:
    """Every field ``endpoint`` takes on SOME release in the table — the union."""
    return frozenset(f.name for f in endpoint_fields(endpoint))


def carries(endpoint: str, field: str, version: Version | None) -> bool:
    """Whether a request to ``endpoint`` on ``version`` may carry ``field``."""
    return field in fields_for(endpoint, version)


def every_application_route_carries(field: str, version: Version | None) -> bool:
    """Whether an application create carries ``field`` whichever route it takes.

    For a plan line about an application setting: it may say "carried" only if
    the route the target is created through cannot matter.
    """
    return all(carries(f"applications/{route}", field, version) for route in APPLICATION_ROUTES)
