"""Tests for the per-endpoint request whitelists.

These encode verified upstream `$allowedFields` arrays. The last test in this
file fetches Coolify's live openapi.json and reports drift — it is marked
`integration` so it never blocks a local run, and a scheduled CI job runs it so
that upstream drift breaks OUR ci rather than someone's production migration.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from bg_coolify_migrate.api.fields import (
    APPLICATION_CREATE,
    APPLICATION_ROUTE_REQUIRED,
    APPLICATION_UPDATE,
    DATABASE_COMMON,
    DATABASE_ENGINE_FIELDS,
    DATABASE_HEALTH_CHECK_DEFAULTS,
    ENV_FIELDS,
    SERVICE_CREATE,
    SERVICE_CREATE_CUSTOM_COMPOSE,
    SERVICE_UPDATE,
    STORAGE_CREATE,
    STORAGE_CREATE_SERVICE,
    STORAGE_FILE_ONLY,
    STORAGE_PERSISTENT_ONLY,
    application_settings_warnings,
    database_allowed,
    database_health_check_warnings,
    env_key_rejection,
    env_key_warnings,
    filter_body,
    missing_required,
    rejected_keys,
)
from bg_coolify_migrate.domain import coolify_versions as cv
from bg_coolify_migrate.domain.kinds import DatabaseEngine
from bg_coolify_migrate.domain.versions import Version, parse_version


class TestFilterBody:
    def test_drops_unlisted_keys(self) -> None:
        # Sending an unlisted key is a 422 "This field is not allowed."
        assert filter_body({"name": "x", "bogus": 1}, frozenset({"name"})) == {"name": "x"}

    def test_drops_none_values(self) -> None:
        # Coolify's validators treat present-but-null differently from absent for
        # several fields; "no opinion" is expressed by omission.
        assert filter_body({"name": "x", "description": None}, frozenset({"name", "description"})) == {
            "name": "x"
        }

    def test_keeps_falsy_non_none_values(self) -> None:
        # instant_deploy=False is a REAL instruction — never drop it.
        body = filter_body({"instant_deploy": False, "public_port": 0}, frozenset({"instant_deploy", "public_port"}))
        assert body == {"instant_deploy": False, "public_port": 0}

    def test_empty_string_survives(self) -> None:
        assert filter_body({"description": ""}, frozenset({"description"})) == {"description": ""}


class TestRejectedKeys:
    def test_identifies_would_be_422s(self) -> None:
        assert rejected_keys({"a": 1, "b": 2}, frozenset({"a"})) == frozenset({"b"})

    def test_clean_body_rejects_nothing(self) -> None:
        assert rejected_keys({"a": 1}, frozenset({"a", "b"})) == frozenset()

    def test_a_get_response_would_be_rejected_wholesale(self) -> None:
        # The reason filter_body exists: a GET returns the full model, a POST
        # accepts a curated subset. Round-tripping is a 422 per extra field.
        get_response = {
            "id": 5,
            "uuid": "abc",
            "name": "app",
            "created_at": "...",
            "updated_at": "...",
            "status": "running",
            "config_hash": "deadbeef",
        }
        rejected = rejected_keys(get_response, APPLICATION_CREATE)
        assert "id" in rejected
        assert "uuid" in rejected
        assert "status" in rejected
        assert "config_hash" in rejected


class TestMissingRequired:
    def test_detects_absent(self) -> None:
        assert missing_required({"a": 1}, frozenset({"a", "b"})) == frozenset({"b"})

    def test_none_counts_as_missing(self) -> None:
        assert missing_required({"a": None}, frozenset({"a"})) == frozenset({"a"})

    def test_satisfied(self) -> None:
        assert missing_required({"a": 1, "b": 2}, frozenset({"a", "b"})) == frozenset()


class TestDatabaseFields:
    def test_every_engine_has_a_field_set(self) -> None:
        assert set(DATABASE_ENGINE_FIELDS) == {e.value for e in DatabaseEngine}

    def test_image_is_accepted_and_must_be_pinned(self) -> None:
        # The model's created hook parses the tag to choose the volume mount path
        # (Postgres >=18 moves to /var/lib/postgresql). Unpinned = wrong path.
        assert "image" in DATABASE_COMMON

    def test_placement_fields_present(self) -> None:
        for field in ("server_uuid", "project_uuid", "destination_uuid", "environment_name"):
            assert field in DATABASE_COMMON

    def test_instant_deploy_present_so_we_can_create_stopped(self) -> None:
        # Always created with instant_deploy=false: nothing may start before the
        # DNS gate has run.
        assert "instant_deploy" in DATABASE_COMMON

    def test_database_allowed_merges_common_and_engine(self) -> None:
        allowed = database_allowed("postgresql")
        assert "postgres_password" in allowed
        assert "server_uuid" in allowed
        assert "mysql_password" not in allowed

    def test_unknown_engine_raises_rather_than_silently_allowing(self) -> None:
        with pytest.raises(KeyError):
            database_allowed("cockroachdb")

    @pytest.mark.parametrize(
        ("engine", "credential"),
        [
            ("postgresql", "postgres_password"),
            ("mysql", "mysql_root_password"),
            ("mariadb", "mariadb_root_password"),
            ("mongodb", "mongo_initdb_root_password"),
            ("redis", "redis_password"),
            ("keydb", "keydb_password"),
            ("dragonfly", "dragonfly_password"),
            ("clickhouse", "clickhouse_admin_password"),
        ],
    )
    def test_each_engine_exposes_its_credential(self, engine: str, credential: str) -> None:
        assert credential in database_allowed(engine)

    def test_tags_are_not_whitelisted_on_any_engine(self) -> None:
        # REGRESSION (2.5.6). See TestServiceFields.test_tags_are_not_whitelisted.
        for engine in DATABASE_ENGINE_FIELDS:
            assert "tags" not in database_allowed(engine)


class TestServiceFields:
    def test_type_and_compose_both_accepted_but_are_mutually_exclusive(self) -> None:
        # Upstream: `type` required_without docker_compose_raw and vice versa;
        # sending BOTH is a 422. The whitelist allows either; the caller chooses.
        assert "type" in SERVICE_CREATE
        assert "docker_compose_raw" in SERVICE_CREATE

    def test_create_rejects_connect_to_docker_network(self) -> None:
        """It is settable only on update, not create — either branch.

        The endpoint validates both the templated and the compose branch against
        one allowedFields (ServicesController line 296), and that list has no
        connect_to_docker_network. The second list at line 505 sits after the
        rejection and never applies. The e2e compose migration 422'd on this.
        """
        assert "connect_to_docker_network" not in SERVICE_CREATE
        assert "connect_to_docker_network" not in SERVICE_CREATE_CUSTOM_COMPOSE
        # There is no compose-only create field: the two lists are identical.
        assert SERVICE_CREATE_CUSTOM_COMPOSE == SERVICE_CREATE

    def test_service_compose_is_updatable(self) -> None:
        # Unlike applications, PATCH /services DOES accept docker_compose_raw,
        # and it is the only place connect_to_docker_network can be set.
        assert "docker_compose_raw" in SERVICE_UPDATE
        assert "connect_to_docker_network" in SERVICE_UPDATE

    def test_update_cannot_move_a_service_between_servers(self) -> None:
        # Resources cannot be relocated via PATCH — that is why the tool creates
        # a new resource on the target rather than repointing the old one.
        assert "server_uuid" not in SERVICE_UPDATE
        assert "project_uuid" not in SERVICE_UPDATE

    def test_tags_are_not_whitelisted(self) -> None:
        """REGRESSION (2.5.6): `tags` is real upstream, but only on `main`.

        It IS in ServicesController's create $allowedFields — read on the default
        branch. Tag management merged 2026-07-07; the newest release, v4.1.2, is
        from 2026-06-04. So on every Coolify an operator can actually install, a
        create body carrying `tags` is a 422 on the whole resource.

        Whitelists are transcribed from upstream source, which makes the branch
        that source is read from part of the contract. Re-add only when a RELEASE
        carries it — the drift canary now pins the latest release tag.
        """
        assert "tags" not in SERVICE_CREATE
        assert "tags" not in SERVICE_CREATE_CUSTOM_COMPOSE
        assert "tags" not in SERVICE_UPDATE


class TestApplicationFields:
    def test_compose_raw_is_accepted_on_create(self) -> None:
        assert "docker_compose_raw" in APPLICATION_CREATE

    def test_compose_raw_is_NOT_accepted_on_update(self) -> None:
        # Verified: a dockercompose application's compose is loaded from git by
        # LoadComposeFile and cannot be PATCHed. Sending it is a 422.
        assert "docker_compose_raw" not in APPLICATION_UPDATE

    def test_dockerfile_is_NOT_accepted_on_update(self) -> None:
        assert "dockerfile" not in APPLICATION_UPDATE

    def test_update_cannot_relocate(self) -> None:
        for field in ("server_uuid", "project_uuid", "destination_uuid", "environment_name"):
            assert field not in APPLICATION_UPDATE

    def test_tags_are_not_whitelisted(self) -> None:
        # REGRESSION (2.5.6). See TestServiceFields.test_tags_are_not_whitelisted.
        assert "tags" not in APPLICATION_CREATE

    def test_git_routes_require_repository_and_branch(self) -> None:
        # This is the wall that makes a raw-YAML compose stack impossible to
        # create as an application: build_pack=dockercompose is only reachable
        # on these routes, and each demands a git remote.
        for route in ("public", "private-github-app", "private-deploy-key"):
            required = APPLICATION_ROUTE_REQUIRED[route]
            assert "git_repository" in required
            assert "git_branch" in required
            assert "build_pack" in required

    def test_github_app_route_requires_its_uuid(self) -> None:
        assert "github_app_uuid" in APPLICATION_ROUTE_REQUIRED["private-github-app"]

    def test_deploy_key_route_requires_its_uuid(self) -> None:
        assert "private_key_uuid" in APPLICATION_ROUTE_REQUIRED["private-deploy-key"]

    def test_dockerimage_route_requires_an_image_name(self) -> None:
        assert "docker_registry_image_name" in APPLICATION_ROUTE_REQUIRED["dockerimage"]

    def test_dockerimage_route_needs_no_git(self) -> None:
        assert "git_repository" not in APPLICATION_ROUTE_REQUIRED["dockerimage"]

    def test_write_only_settings_are_creatable(self) -> None:
        # Settable on create, unreadable on GET — the settings gap.
        for field in ("is_static", "is_force_https_enabled", "connect_to_docker_network"):
            assert field in APPLICATION_CREATE

    def test_undocumented_but_accepted_fields_are_carried(self) -> None:
        """REGRESSION: accepted on create since v4.1.2, documented by no openapi.

        Invisible to the openapi canary, so every migration dropped them: the
        network aliases other containers reach the app by, and the CMD health check
        a worker without an HTTP listener depends on.
        """
        for field in ("custom_network_aliases", "health_check_type", "health_check_command"):
            assert field in APPLICATION_CREATE

    @pytest.mark.parametrize("route", cv.APPLICATION_ROUTES)
    def test_v4_5_1_create_fields_are_carried_from_that_release(self, route: str) -> None:
        """v4.5.0 refuses all four on create — a 422 for the whole resource."""
        settings = {
            "include_coolify_default_variables",
            "is_container_label_readonly_enabled",
            "rolling_update_drain_seconds",
        }
        endpoint = f"applications/{route}"
        assert not settings & cv.fields_for(endpoint, (4, 5, 0))
        assert settings <= cv.fields_for(endpoint, (4, 5, 1))
        sources = cv.field_sources(endpoint, (4, 5, 1))
        assert {sources[f] for f in settings} == {"settings"}, "the GET nests them"
        build_target = "dockerfile_target_build" in cv.fields_for(endpoint, (4, 5, 1))
        assert build_target is (route != "dockerimage"), "only where a Dockerfile is built"
        assert "dockerfile_target_build" not in cv.fields_for(endpoint, (4, 5, 0))

    def test_an_unknown_release_gets_none_of_the_v4_5_1_fields(self) -> None:
        """...except the drain seconds, whose column ships with its acceptance."""
        unknown = cv.fields_for("applications/public", None)
        assert "include_coolify_default_variables" not in unknown
        assert "is_container_label_readonly_enabled" not in unknown
        assert "dockerfile_target_build" not in unknown
        assert "rolling_update_drain_seconds" in unknown

    def test_basic_auth_hash_costs_are_carried_but_not_the_algorithm(self) -> None:
        """argon2id 422s the whole create unless the target runs Caddy >= 2.11."""
        for field in (
            "http_basic_auth_bcrypt_cost",
            "http_basic_auth_argon2id_memory_cost",
            "http_basic_auth_argon2id_time_cost",
        ):
            assert field in APPLICATION_CREATE
        assert "http_basic_auth_hash_algorithm" not in APPLICATION_CREATE


class TestStorageFields:
    def test_services_need_resource_uuid_to_target_the_sub_resource(self) -> None:
        assert "resource_uuid" not in STORAGE_CREATE
        assert "resource_uuid" in STORAGE_CREATE_SERVICE

    def test_type_and_mount_path_are_the_core(self) -> None:
        assert "type" in STORAGE_CREATE
        assert "mount_path" in STORAGE_CREATE

    def test_persistent_and_file_fields_are_disjoint(self) -> None:
        # Type-mixing is rejected by upstream: `content`/`is_directory`/`fs_path`
        # are invalid for persistent; `name`/`host_path` invalid for file.
        assert frozenset() == STORAGE_PERSISTENT_ONLY & STORAGE_FILE_ONLY

    def test_host_file_mounts_keep_their_flag(self) -> None:
        """Without is_host_file a 4.4+ target treats /etc/timezone as managed content.

        It then 422s on a symlink, or touches and chmods the target's host file.
        """
        assert "is_host_file" in STORAGE_CREATE
        assert "is_host_file" in STORAGE_FILE_ONLY


class TestEnvFields:
    def test_core_fields(self) -> None:
        assert {"key", "value"} <= ENV_FIELDS

    def test_is_shown_once_is_a_real_field(self) -> None:
        # Settable, so it must round-trip. NOT merely cosmetic: a locked variable's
        # value is removed from every API read (applications and services since
        # 4.2, databases since 4.4), even for a root token — which is why
        # fidelity.env_warnings reports every locked variable it cannot carry.
        assert "is_shown_once" in ENV_FIELDS

    def test_build_and_runtime_flags(self) -> None:
        assert "is_runtime" in ENV_FIELDS
        assert "is_buildtime" in ENV_FIELDS


#: Resolved once per session — two tests share it, and the GitHub API is rate
#: limited. Keyed nowhere: there is only ever one answer per run.
_RELEASED_SPEC: dict[str, Any] = {}


async def released_openapi() -> tuple[str, dict[str, Any]]:
    """Coolify's openapi.json AT THE NEWEST PUBLISHED RELEASE, and that tag.

    NOT `main`. This is the whole lesson of the 2.5.6 tags regression: these
    whitelists are transcribed from upstream source, which makes the REF that
    source is read from part of the contract. Tag management sat on `main` from
    2026-07-07 while the newest release, v4.1.2, was from 2026-06-04 — so a
    canary reading `main` reported `tags` as a field we were missing, it was
    whitelisted in good faith, and every migration then died on an endpoint no
    operator could install. `main` is not a release.

    The tag is resolved dynamically rather than pinned to a constant, so the
    canary tracks what is installable without anyone remembering to bump it.
    ``/releases/latest`` excludes prereleases, which is what we want — Coolify
    publishes long beta chains and those are not what operators run.
    """
    if not _RELEASED_SPEC:
        import os

        import httpx

        headers = {"Accept": "application/vnd.github+json"}
        # Unauthenticated api.github.com allows 60 requests/hour per IP. A shared
        # CI runner can exhaust that, and a rate-limited canary fails looking
        # exactly like real drift — the one thing it must never do.
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            latest = await client.get(
                "https://api.github.com/repos/coollabsio/coolify/releases/latest",
                headers=headers,
            )
            latest.raise_for_status()
            tag = str(latest.json()["tag_name"])

            spec = await client.get(
                f"https://raw.githubusercontent.com/coollabsio/coolify/{tag}/openapi.json"
            )
            spec.raise_for_status()
            _RELEASED_SPEC["tag"] = tag
            _RELEASED_SPEC["spec"] = spec.json()

    return _RELEASED_SPEC["tag"], _RELEASED_SPEC["spec"]


#: ADJUDICATED against v4.2.0 (released 2026-07-21T16:08Z), controllers read AT
#: THAT TAG: `tags` is genuinely in the create $allowedFields — ServicesController
#: :358, ApplicationsController:1123 (all five routes share it), DatabasesController
#: :1762 plus each of the eight engine routes. It is NOT a schema artefact like
#: connect_to_docker_network; the enforcement arrays themselves carry it. It is
#: create-only — absent from the update lists at ServicesController:1173 and
#: ApplicationsController:2695.
#:
#: STILL not whitelisted, but as of 2026-08-16 for a different reason than before.
#:
#: The original reason was version skew: the fleet ran 4.1.2, where an unlisted
#: create field is a 422 on the whole resource, so whitelisting a 4.2 field would
#: have broken every migration we operate. That reason has expired — the fleet now
#: runs 4.3.2 (measured against paas.cloud) and accepts `tags` on create.
#:
#: It stays out because the RESOLUTION landed instead: tags are copied by
#: `api/resources.py::copy_tags` as a post-create POST /{collection}/{uuid}/tags,
#: exactly as this note originally prescribed. That shape is strictly better than
#: a create field, and the difference is not stylistic:
#:
#:   * it works on BOTH 4.1.2 (route absent -> degrades to no tags) and 4.3.x,
#:     where a create field works on one and destroys the resource on the other;
#:   * it is non-fatal, so cosmetic metadata never sits on the critical path of a
#:     cutover. A create field cannot be non-fatal — it takes the resource with it.
#:
#: THE LESSON, generalised and now paid off twice: the whitelist must match the
#: version that RUNS, not the newest that exists. Where a capability can be moved
#: OFF the critical path entirely, that beats tracking the version at all.
KNOWN_TAGS_GAP: frozenset[str] = frozenset({"tags"})


@pytest.mark.integration
async def test_whitelists_match_upstream_openapi() -> None:
    """Report drift between our whitelists and Coolify's RELEASED openapi.json.

    Marked `integration` because it needs network. A scheduled CI job runs it so
    that an upstream API change breaks our CI rather than a user's migration.

    NOTE: OpenAPI and `$allowedFields` genuinely disagree in places — the OA
    attributes are documentation, the arrays are the enforcement. So this test
    reports EXTRA fields we might be missing; it does not fail on fields we
    deliberately exclude (documented in api/fields.py).
    """
    tag, spec = await released_openapi()
    newest = tag_version(tag)

    def body_props(path: str, method: str) -> set[str]:
        op = spec.get("paths", {}).get(path, {}).get(method, {})
        schema = (
            op.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        return set(schema.get("properties", {}))

    drift: dict[str, list[str]] = {}

    documented = body_props("/services", "post")
    if documented:
        # openapi documents connect_to_docker_network for POST /services, but the
        # controller validates it out at line 332 (see SERVICE_CREATE_CUSTOM_COMPOSE).
        # The arrays are the enforcement; the schema is documentation. Where they
        # disagree the arrays win, and this is the recorded exception — verified
        # against the running instance by the e2e compose-service migration.
        openapi_only = {"connect_to_docker_network"}
        missing = (
            documented
            - cv.fields_for("services/compose", newest)
            - openapi_only
            - KNOWN_TAGS_GAP
            - KNOWN_SERVICE_GAP
        )
        if missing:
            drift["services/compose"] = [registry_line("services/compose", f, tag) for f in sorted(missing)]

    # Databases carry one create route per engine and they drifted together when
    # upstream added `tags` — checking only /services would have caught one of
    # nine routes. The engine-specific credential fields differ per route, so each
    # is diffed against its own database_allowed().
    for engine in sorted(DATABASE_ENGINE_FIELDS):
        path = f"/databases/{engine}"
        documented = body_props(path, "post")
        if not documented:
            continue
        missing = documented - database_allowed(engine, newest) - KNOWN_TAGS_GAP
        if missing:
            drift[f"databases/{engine}"] = [
                registry_line(f"databases/{engine}", f, tag) for f in sorted(missing)
            ]

    assert not drift, (
        f"Coolify {tag} documents fields the registry does not send: {drift}. "
        f"Upstream may have added fields; each is a row in domain/coolify_versions.py "
        f"(or a KNOWN_*_GAP here with the reason). Adjudicate each "
        f"against the controller's $allowedFields AT THIS TAG before whitelisting — "
        f"the schema documents fields the controller rejects (see "
        f"connect_to_docker_network), and reading the controller on `main` instead "
        f"of {tag} is what shipped the 2.5.6 tags regression."
    )


#: Fields POST /applications/* documents that APPLICATION_CREATE does not carry —
#: a log, extended release by release; each block below adjudicates one batch.
#:
#: ADJUDICATED against ApplicationsController — the first 17 are genuinely accepted on
#: create, none is a documentation artifact: 13 arrive via the
#: ``...self::APPLICATION_SETTING_FIELDS`` spread at the end of the create
#: $allowedFields (:1123, constant at :38), and autogenerate_domain,
#: force_domain_override, use_build_secrets and is_preview_deployments_enabled are
#: listed explicitly in that same array.
#:
#: They are NOT whitelisted anyway, because accepting them on write is only half
#: the problem. ``application_by_uuid`` eager-loads ``->with('settings')``, so the
#: 13 settings fields DO come back on the GET — but nested under a ``settings``
#: object, while the create body takes them flat. filter_body works on the flat
#: dict, so it never sees them. Whitelisting without flattening would change
#: nothing; flattening is a real change with per-field judgement attached, and at
#: least one field must NOT be copied blindly (create_application deliberately
#: forces autogenerate_domain=False when the source has no domains).
#:
#: DECIDED: not migrated, and that is fine. Coolify's defaults already enable the
#: ones that would hurt if they were off — submodules, LFS, shallow clone, force
#: https, gzip, strip prefixes all default ON — so an application lands on the
#: target configured the way the overwhelming majority of sources already are.
#: The only exposure is a source that deliberately turned one OFF coming back up
#: with it ON; that is a narrow edge case, not the silent loss `tags` was.
#: Revisit only if someone hits it.
#: EXTENDED 2026-08-16 for Coolify 4.3.x, which documents 11 further fields. Each
#: was adjudicated against the controller at v4.3.3 AND against 39 real
#: applications on the live 4.3.2 fleet, because "is it accepted?" and "does it
#: carry anything?" are different questions and only the second decides whether a
#: gap costs the operator something.
#:
#: EIGHT are the rest of APPLICATION_SETTING_FIELDS (custom_internal_name, the
#: five gpu_*, is_consistent_container_name_enabled, is_log_drain_enabled). They
#: fall under the reasoning above verbatim, now measured rather than assumed:
#: present under `settings` on 39 of 39 applications and at the top level on 0 of
#: 39. filter_body works on the flat dict, so whitelisting them alone would change
#: nothing at all.
#:
#: THREE are new top-level create fields, and they are NOT interchangeable:
#:
#:   * noindex_domains       — `[]` on all 39. Nothing to lose today.
#:   * max_restart_count     — uniform across all 39 (Coolify's own default).
#:   * preview_url_template  — the one with real content: several apps carry a
#:     customised template (`pr-{{pr_id}}.{{domain}}.staging…`). It is genuinely
#:     dropped, and this is the honest cost of the decision below.
#:
#: NOT whitelisted, and the reason is the 2.5.6 lesson rather than indifference:
#: all three landed after 4.1.2, which is the bottom of our validated range, and an
#: unlisted create field there is a 422 that destroys the whole resource. Trading a
#: preview-URL template for "every migration against a 4.1.x instance fails" is not
#: a trade worth making. The exposure is further bounded because this tool refuses
#: to migrate a stack that has preview deployments at all (quiesce.assert_previews_
#: absent), so the template governs something that cannot be present during a run.
#:
#: Revisit as a post-create PATCH (all three ARE in the 4.3.3 update list) if
#: someone actually loses a template they wanted — that needs version detection, so
#: it is a feature, not a whitelist entry.
#:
#: EXTENDED 2026-10-21 for Coolify 4.4.x: custom_container_name_prefix. It shipped
#: in v4.4.0 (2026-10-06, PR #11704), not in v4.4.7 — the canary has been red since
#: its first run after v4.4.0, and v4.4.6 -> v4.4.7 touched no API code. Read at
#: v4.4.7: one more member of APPLICATION_SETTING_FIELDS (const :44, field :66),
#: spread into the create array (:1251) and the update array (:3253); validated
#: `string|nullable|max:30` (api.php:216), slugified, and 422'd when another app on
#: the same SERVER already uses it as a uuid, prefix or custom_internal_name. Stored
#: on application_settings (migration 2026_09_08_214513), so the GET returns it only
#: under `settings`. Not backported: absent from v4.3.24, v4.2.1, v4.1.3 and v4.0.1.
#:
#: Not whitelisted, and the deciding reason is mechanical, not version skew:
#: filter_body reads the flat GET, the value lives under `settings`, so a whitelist
#: entry would carry nothing.
#:
#: The cost is real but cosmetic, and unlike the boolean settings above it is NOT
#: covered by "the defaults are what you want": it is operator-chosen text. A source
#: that set it comes up with `{uuid}-{YmdTHis}` container names instead of
#: `{prefix}-{YmdTHis}`. Nothing depends on that name: it changes on every deploy,
#: routing keys on the uuid, and discovery finds the container through the
#: `coolify.applicationUuid` label 4.4.0 introduced alongside it (locked by
#: TestCarriesUuid.test_matches_a_v44_prefixed_container_through_its_uuid_label).
#:
#: EXTENDED 2026-10-26 for Coolify 4.5.0 (the canary went red on its first run
#: after v4.5.0, 2026-10-23). Seven fields documented; all read in
#: ApplicationsController AT v4.5.0 (create array :1340, update :3363). Three are
#: WHITELISTED, not gapped — http_basic_auth_{bcrypt_cost, argon2id_memory_cost,
#: argon2id_time_cost} are top-level columns whose existence and acceptance ship
#: in the same release, so filter_body can never send them where they 422. The
#: other four stay out, each for its own reason:
#:
#:   * http_basic_auth_hash_algorithm — top-level, but `argon2id` 422s the WHOLE
#:     create unless the target server runs Caddy >= 2.11, which the planner
#:     cannot see. The credentials survive either way (bcrypt of the same
#:     plaintext); fidelity.application_warnings names an argon2id source.
#:   * is_permanent_redirect_enabled, traefik_certificate_resolver,
#:     redact_all_env_values_in_logs — application_settings columns, so they come
#:     back only under `settings` and filter_body could not carry them anyway.
#:     The resolver is also SERVER-scoped (a name defined in the source server's
#:     Traefik config), so copying it blindly could be as wrong as dropping it.
#:     The first two are warned about at plan time; redaction is not, because its
#:     default (true) is at least as protective as any source value.
#:
#: EXTENDED 2026-10-28 for Coolify 4.5.1 (the canary went red on the release
#: pipeline): four fields join the create arrays at v4.5.1 and are in NONE at
#: v4.5.0. They were gapped here until the release registry could gate them;
#: now they are CARRIED, from that release only (domain/coolify_versions.py;
#: pinned by test_v4_5_1_create_fields_are_carried_from_that_release).
KNOWN_APPLICATION_GAP: frozenset[str] = frozenset(
    {
        "autogenerate_domain",
        "custom_container_name_prefix",
        "custom_internal_name",
        "disable_build_cache",
        "docker_images_to_keep",
        "force_domain_override",
        "gpu_count",
        "gpu_device_ids",
        "gpu_driver",
        "gpu_options",
        "http_basic_auth_hash_algorithm",
        "include_source_commit_in_build",
        "inject_build_args_to_dockerfile",
        "is_consistent_container_name_enabled",
        "is_env_sorting_enabled",
        "is_git_lfs_enabled",
        "is_git_shallow_clone_enabled",
        "is_git_submodules_enabled",
        "is_gpu_enabled",
        "is_gzip_enabled",
        "is_log_drain_enabled",
        "is_permanent_redirect_enabled",
        "is_pr_deployments_public_enabled",
        "is_preview_deployments_enabled",
        "is_raw_compose_deployment_enabled",
        "is_stripprefix_enabled",
        "max_restart_count",
        "noindex_domains",
        "preview_url_template",
        "redact_all_env_values_in_logs",
        "stop_grace_period",
        "traefik_certificate_resolver",
        "use_build_secrets",
    }
)

#: Fields POST /services documents that SERVICE_CREATE does not carry.
#:
#: ADJUDICATED against ServicesController at v4.5.0: redact_all_env_values_in_logs
#: is in both create arrays and the update array, stored top-level on `services`
#: with DB default true for new and existing rows. Not carried: the target then
#: redacts every env value in Coolify's logs, which is at least as protective as
#: any source setting — a source that turned redaction OFF loses only log
#: verbosity, never protection. Gapped rather than whitelisted for symmetry with
#: applications (nested, uncarriable) and databases (no create route accepts it).
KNOWN_SERVICE_GAP: frozenset[str] = frozenset({"redact_all_env_values_in_logs"})


def test_known_application_gap_is_disjoint_from_the_whitelist() -> None:
    """A field is either sent or adjudicated as not sent — never both.

    The gap canary only runs on the weekly schedule, so without this a later
    whitelisting of a gap field (custom_container_name_prefix is the obvious
    candidate) would leave its "not migrated" rationale standing, unread, next to
    code that now migrates it. Offline, so every push checks it.
    """
    assert KNOWN_APPLICATION_GAP.isdisjoint(APPLICATION_CREATE)
    assert KNOWN_TAGS_GAP.isdisjoint(APPLICATION_CREATE)
    assert KNOWN_SERVICE_GAP.isdisjoint(SERVICE_CREATE)


@pytest.mark.integration
async def test_application_gap_does_not_grow() -> None:
    """The application whitelist gap is known; this fails if it WIDENS.

    Pinning the gap instead of excluding the whole route keeps the canary useful:
    a newly documented application field still breaks CI, but the ones already
    triaged do not cry every week.
    """
    tag, spec = await released_openapi()
    newest = tag_version(tag)

    new: dict[str, list[str]] = {}
    for route in cv.APPLICATION_ROUTES:
        op = spec.get("paths", {}).get(f"/applications/{route}", {}).get("post", {})
        documented = set(
            op.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("properties", {})
        )
        if not documented:
            continue
        sent = cv.fields_for(f"applications/{route}", newest)
        missing = documented - sent - KNOWN_APPLICATION_GAP - KNOWN_TAGS_GAP
        if missing:
            new[route] = [
                registry_line(f"applications/{route}", f, tag) for f in sorted(missing)
            ]

    assert not new, (
        f"NEW fields documented for POST /applications/* in Coolify {tag}: {new}. "
        f"Adjudicate against ApplicationsController's $allowedFields at that tag, "
        f"then either add the registry row (domain/coolify_versions.py; read_from="
        f'"settings" if the GET nests it) or add it to KNOWN_APPLICATION_GAP with a '
        f"reason."
    )


class TestDatabaseHealthCheckWarnings:
    """health_check_* is readable in every GET and settable through no endpoint.

    Dropping it from the request is forced — Coolify 422s the whole create
    otherwise. Dropping it *quietly* would not be: a source with a tuned health
    check would come up on defaults with nobody told.
    """

    def test_silent_on_coolify_defaults(self) -> None:
        """The overwhelmingly common case must not produce noise.

        A warning on every stock database is worse than none: it teaches
        operators that warnings from this tool are furniture.
        """
        source = dict(DATABASE_HEALTH_CHECK_DEFAULTS)
        assert database_health_check_warnings(source) == []

    def test_silent_when_the_source_says_nothing(self) -> None:
        assert database_health_check_warnings({"name": "pg", "image": "postgres:16"}) == []

    def test_reports_a_tuned_health_check(self) -> None:
        warnings = database_health_check_warnings(
            {**DATABASE_HEALTH_CHECK_DEFAULTS, "health_check_interval": 120}
        )
        assert len(warnings) == 1
        # The operator needs to know what to re-apply, not merely that something
        # was dropped.
        assert "health_check_interval=120" in warnings[0]
        assert "15" in warnings[0]

    def test_gathers_every_deviation_into_one_warning(self) -> None:
        warnings = database_health_check_warnings(
            {"health_check_enabled": False, "health_check_retries": 99}
        )
        assert len(warnings) == 1
        assert "health_check_enabled=False" in warnings[0]
        assert "health_check_retries=99" in warnings[0]

    def test_the_defaults_are_not_settable_anywhere(self) -> None:
        """The reason this module drops them at all.

        If a future version adds them to $allowedFields, this fails and someone
        gets to delete the warning instead of discovering it by 422.
        """
        for field in DATABASE_HEALTH_CHECK_DEFAULTS:
            assert field not in DATABASE_COMMON
            assert field not in database_allowed("postgresql")


class TestApplicationSettingsWarnings:
    """The naming settings ride under `settings` and never reach the create body.

    Silence is half the contract: a warning on every stock application would teach
    operators that this panel is furniture, exactly as for the health checks.
    """

    def test_silent_when_the_get_carries_no_settings(self) -> None:
        # 4.1.x does not eager-load settings at all.
        assert application_settings_warnings({"name": "api"}) == []

    def test_silent_when_settings_is_not_an_object(self) -> None:
        assert application_settings_warnings({"settings": None}) == []

    def test_silent_on_a_stock_application(self) -> None:
        stock = {
            "settings": {
                "custom_internal_name": None,
                "custom_container_name_prefix": None,
                "is_consistent_container_name_enabled": False,
                "is_static": False,
            }
        }
        assert application_settings_warnings(stock) == []

    def test_silent_on_empty_strings(self) -> None:
        # Coolify slugs '' to null on write, but older rows can still hold ''.
        blank = {"settings": {"custom_internal_name": "", "custom_container_name_prefix": ""}}
        assert application_settings_warnings(blank) == []

    def test_reports_an_internal_name_with_the_value_and_the_remedy(self) -> None:
        warnings = application_settings_warnings({"settings": {"custom_internal_name": "shop-db-proxy"}})
        assert len(warnings) == 1
        assert "'shop-db-proxy'" in warnings[0]
        # The operator has to act before cutover, so it must say what to do.
        assert "by hand" in warnings[0]
        assert "before switching traffic" in warnings[0]

    def test_reports_the_internal_name_whatever_the_consistent_naming_flag(self) -> None:
        """The flag's meaning flipped in 4.4, and the planner cannot tell which applies.

        Up to 4.3.x the name is used when consistent naming is OFF; from 4.4.0 only
        when it is ON. Conditioning on it would silence the warning on exactly one
        of the two lines, so both settings of the flag must report.
        """
        for flag in (True, False):
            source = {
                "settings": {
                    "custom_internal_name": "api",
                    "is_consistent_container_name_enabled": flag,
                }
            }
            assert len(application_settings_warnings(source)) == 1

    def test_reports_a_prefix_as_cosmetic(self) -> None:
        warnings = application_settings_warnings(
            {"settings": {"custom_container_name_prefix": "shop-api"}}
        )
        assert len(warnings) == 1
        assert "'shop-api'" in warnings[0]
        assert "Cosmetic" in warnings[0]

    def test_reports_both_separately(self) -> None:
        # Different consequences, different actions: one line each.
        source = {
            "settings": {"custom_internal_name": "api", "custom_container_name_prefix": "shop"}
        }
        assert len(application_settings_warnings(source)) == 2


class TestEnvKeyRejection:
    """Coolify >=4.2 tightened ENVIRONMENT_VARIABLE_KEY_PATTERN.

    Worth its own tests because the endpoint it guards validates and SAVES in the
    same loop: a key rejected halfway through a bulk upsert leaves the target with
    a partial environment, mid-cutover.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "DATABASE_URL",
            "_PRIVATE",
            "SERVICE_FQDN_APP",  # Coolify's own magic variables must pass
            "app.name",  # '.' is explicitly allowed
            "A",  # a single letter is a complete key
            "X" * 255,  # exactly at the limit
        ],
    )
    def test_accepts_what_coolify_accepts(self, key: str) -> None:
        assert env_key_rejection(key) is None

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("MY-VAR", "'-'"),
            ("2FA_SECRET", "starts with '2'"),
            ("FOO[BAR]", "'['"),
            ("a:b", "':'"),
            ("HAS SPACE", "' '"),
            ("", "is empty"),
        ],
    )
    def test_rejects_and_says_why(self, key: str, expected: str) -> None:
        reason = env_key_rejection(key)
        assert reason is not None
        # The operator has to fix the key by hand, so "invalid" alone is useless.
        assert expected in reason

    def test_length_is_checked_before_shape(self) -> None:
        """A 256-char key MATCHES the pattern and is still rejected by max:255.

        Upstream's rules are ['required','string','max:255','regex:...'] and Laravel
        applies all of them. Testing the regex first reported such a key as clean —
        this test exists because the first draft did exactly that.
        """
        reason = env_key_rejection("X" * 256)
        assert reason is not None
        assert "256 characters" in reason

    def test_reports_every_offending_character_once(self) -> None:
        reason = env_key_rejection("A-B-C+D")
        assert reason is not None
        assert "'-'" in reason and "'+'" in reason
        # Deduplicated: '-' appears twice in the key, once in the message.
        assert reason.count("'-'") == 1


class TestEnvKeyWarnings:
    def test_silent_on_ordinary_keys(self) -> None:
        """0 of 2751 real keys were flagged on our fleet; noise here would be fatal
        to the warning's credibility."""
        envs = [{"key": "DATABASE_URL"}, {"key": "REDIS_HOST"}, {"key": "_INTERNAL"}]
        assert env_key_warnings(envs) == []

    def test_empty_input_is_silent(self) -> None:
        assert env_key_warnings([]) == []

    def test_skips_entries_with_no_key(self) -> None:
        """build_env_entries drops keyless entries, so they never reach the API.

        Warning about something that cannot be sent is a false alarm.
        """
        assert env_key_warnings([{"value": "x"}, {"key": "", "value": "y"}]) == []

    def test_flags_a_legacy_key_with_the_name_and_the_remedy(self) -> None:
        warnings = env_key_warnings([{"key": "OK_ONE"}, {"key": "LEGACY-KEY"}])
        assert len(warnings) == 1
        assert "LEGACY-KEY" in warnings[0]
        # It must say what to DO, and why it is not merely cosmetic.
        assert "Rename it" in warnings[0]
        assert "PARTIAL" in warnings[0]

    def test_flags_every_offender_not_just_the_first(self) -> None:
        warnings = env_key_warnings([{"key": "A-B"}, {"key": "1C"}, {"key": "FINE"}])
        assert len(warnings) == 2


# ── the reverse canary ───────────────────────────────────────────────────────
#
# Everything above this line checks ONE direction: fields upstream documents that
# we do not send. That direction is a missed feature. The direction that breaks a
# migration is the opposite one — a field WE send that upstream does not accept,
# which is a 422 on the whole resource, so nothing is created at all.
#
# It cannot be checked against openapi.json. Measured 2026-08-16: openapi
# documents applications PER ROUTE while the controller enforces ONE shared array
# for all five, so a naive reverse diff reports 22 phantom fields for
# /applications/dockerfile alone. A canary with 22 false positives gets muted, and
# a muted canary is worse than none.
#
# So this reads the ENFORCEMENT arrays out of the controller source at the
# released tag — the same arrays api/fields.py was transcribed from, and the ones
# that actually decide the 422. Where the schema and the arrays disagree, the
# arrays win; this is that rule, automated.

_ALLOWED_FIELDS_RE = re.compile(r"\$allowedFields\s*=\s*\[(.*?)\];", re.S)
_PHP_STRING_RE = re.compile(r"'([a-z0-9_]+)'")
_SPREAD_RE = re.compile(r"\.\.\.self::([A-Z_]+)")

_CONTROLLERS = {
    "applications": "ApplicationsController",
    "services": "ServicesController",
    "databases": "DatabasesController",
}

_CONTROLLER_SOURCE: dict[str, str] = {}


async def released_controllers() -> tuple[str, dict[str, str]]:
    """The three API controllers' PHP source at the newest published release.

    Same tag resolution as :func:`released_openapi`, and for the same reason: a
    whitelist must match the version that RUNS, and `main` is not a release.
    """
    if not _CONTROLLER_SOURCE:
        import os

        import httpx

        headers = {"Accept": "application/vnd.github+json"}
        if token := os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            latest = await client.get(
                "https://api.github.com/repos/coollabsio/coolify/releases/latest",
                headers=headers,
            )
            latest.raise_for_status()
            tag = str(latest.json()["tag_name"])

            for key, name in _CONTROLLERS.items():
                resp = await client.get(
                    f"https://raw.githubusercontent.com/coollabsio/coolify/{tag}/"
                    f"app/Http/Controllers/Api/{name}.php"
                )
                resp.raise_for_status()
                _CONTROLLER_SOURCE[key] = resp.text
            _CONTROLLER_SOURCE["tag"] = tag

    tag = _CONTROLLER_SOURCE["tag"]
    return tag, {k: _CONTROLLER_SOURCE[k] for k in _CONTROLLERS}


def _resolve_spread(php: str, const_name: str) -> set[str]:
    """Expand a ``...self::CONST`` spread by reading the constant's own array."""
    match = re.search(rf"const\s+{const_name}\s*=\s*\[(.*?)\];", php, re.S)
    return set(_PHP_STRING_RE.findall(match.group(1))) if match else set()


def create_allowed_fields(php: str) -> list[set[str]]:
    """Every CREATE ``$allowedFields`` array in a controller, spreads expanded.

    A create array is identified by carrying both ``project_uuid`` and
    ``server_uuid`` — update arrays carry neither, and the storage/env/clone arrays
    are unrelated. This is structural rather than positional, so it survives
    upstream moving code around, which it does constantly.
    """
    out: list[set[str]] = []
    for body in _ALLOWED_FIELDS_RE.findall(php):
        fields = set(_PHP_STRING_RE.findall(body))
        for const_name in _SPREAD_RE.findall(body):
            fields |= _resolve_spread(php, const_name)
        if {"project_uuid", "server_uuid"} <= fields:
            out.append(fields)
    return out


#: Where the registry keeps the rows a canary may ask for, by controller.
_REGISTRY_HOME = {
    "applications": "_APPLICATION (shared by every applications/<route> endpoint)",
    "services": "_SERVICE (services/template and services/compose)",
    "databases": "_DATABASE_COMMON, or _DATABASE_ENGINE[<engine>] for one engine",
}


@pytest.mark.integration
async def test_we_never_send_a_field_upstream_rejects() -> None:
    """No registry row may send a field the newest released controller would 422.

    THE canary that matters. A field we are missing costs a feature; a field
    upstream removed costs the whole resource, because the extra-field check
    rejects the request wholesale before anything is created. Checked per
    endpoint for the NEWEST release, so a field that left a create array needs an
    ``until=`` on its row (as host_path has) rather than a deletion — the older
    releases still take it.
    """
    tag, sources = await released_controllers()
    newest = tag_version(tag)

    # All five application create routes share one array (ApplicationsController),
    # so there is exactly one create array to match against.
    app_arrays = create_allowed_fields(sources["applications"])
    assert app_arrays, f"found no application create $allowedFields at {tag}"
    app_allowed: set[str] = set().union(*app_arrays)

    # Services have two: the template branch and the compose branch. A field only
    # has to be accepted by the branch we actually use, so the union is correct.
    svc_arrays = create_allowed_fields(sources["services"])
    assert svc_arrays, f"found no service create $allowedFields at {tag}"
    svc_allowed: set[str] = set().union(*svc_arrays)

    db_arrays = create_allowed_fields(sources["databases"])
    assert db_arrays, f"found no database create $allowedFields at {tag}"
    db_allowed: set[str] = set().union(*db_arrays)

    endpoints = [
        *((f"applications/{route}", app_allowed) for route in cv.APPLICATION_ROUTES),
        ("services/template", svc_allowed),
        ("services/compose", svc_allowed),
        *((f"databases/{engine}", db_allowed) for engine in cv.DATABASE_ENGINES),
    ]
    rejected: dict[str, list[str]] = {}
    for endpoint, accepted in endpoints:
        if extra := sorted(cv.fields_for(endpoint, newest) - accepted):
            rejected[endpoint] = [
                f'Field("{f}", Gate(until={newest})) - or drop the row if no release '
                "ever took it"
                for f in extra
            ]

    assert not rejected, (
        f"Coolify {tag} would 422 these fields, which the registry still sends there: "
        f"{rejected}. An unlisted field is rejected wholesale — the resource is NOT "
        f"created. Close each row's gate in domain/coolify_versions.py, or confirm "
        f"against the controller at this tag that the extraction missed a branch."
    )


#: Fields a create array accepts that we leave out by DESIGN, not as a lost
#: setting — so they belong in neither a whitelist nor a gap.
#:
#:   * applications `type` — the ROUTE decides it (create_application($request,
#:     $type), ApplicationsController v4.5.0:1320); a body `type` is never read.
#:   * services `connect_to_docker_network` — carried by a follow-up PATCH, which
#:     works on every version; create accepts it only from v4.5.0.
#:   * databases `sqlite_databases` — SQLite (v4.4.0) is refused at plan time.
_NOT_SENT_BY_DESIGN = {
    "applications": frozenset({"type"}),
    "services": frozenset({"connect_to_docker_network"}),
    "databases": frozenset({"sqlite_databases"}),
}


@pytest.mark.integration
async def test_controller_arrays_are_adjudicated() -> None:
    """Every field a released create array accepts is either sent or adjudicated.

    The openapi canaries above only see what upstream DOCUMENTS. Measured
    2026-10-26: custom_network_aliases, health_check_type and health_check_command
    sat in the application create array from v4.1.2 on, documented by no schema,
    and every migration silently dropped them — the aliases other containers
    resolve the app by, and the CMD health check of every worker. Reading the
    enforcement arrays forward closes that blind spot: a field upstream accepts but
    never documents still fails here until someone decides about it.
    """
    tag, sources = await released_controllers()
    newest = tag_version(tag)

    def sent(*endpoints: str) -> frozenset[str]:
        return frozenset().union(*(cv.fields_for(e, newest) for e in endpoints))

    adjudicated = {
        "applications": sent(*(f"applications/{r}" for r in cv.APPLICATION_ROUTES))
        | KNOWN_APPLICATION_GAP
        | KNOWN_TAGS_GAP,
        "services": sent("services/template", "services/compose")
        | KNOWN_SERVICE_GAP
        | KNOWN_TAGS_GAP,
        "databases": sent(*(f"databases/{e}" for e in cv.DATABASE_ENGINES)) | KNOWN_TAGS_GAP,
    }
    undecided: dict[str, list[str]] = {}
    for key, php in sources.items():
        arrays = create_allowed_fields(php)
        assert arrays, f"found no {key} create $allowedFields at {tag}"
        accepted: set[str] = set().union(*arrays)
        # A field the array takes through the APPLICATION_SETTING_FIELDS spread is
        # an application_settings column: the GET returns it under `settings`.
        nested = _resolve_spread(php, "APPLICATION_SETTING_FIELDS")
        if extra := sorted(accepted - adjudicated[key] - _NOT_SENT_BY_DESIGN[key]):
            undecided[_REGISTRY_HOME[key]] = [
                registry_line(f"{key}/*", f, tag, nested="settings" if f in nested else None)
                for f in extra
            ]

    assert not undecided, (
        f"Coolify {tag} accepts these create fields that we neither send nor have "
        f"adjudicated: {undecided}. Read each at {tag}: where is it stored, does the "
        f"GET return it flat or under `settings`, and since which tag is it accepted? "
        f"Then add the row shown to domain/coolify_versions.py, or record it in a "
        f"KNOWN_*_GAP here with the reason. The openapi canary cannot see these — "
        f"upstream did not document them."
    )


def tag_version(tag: str) -> Version:
    """A release tag (``v4.5.1``) as the registry compares it."""
    version = parse_version(tag)
    assert version is not None, f"cannot parse the release tag {tag!r}"
    return version


def registry_line(endpoint: str, field: str, tag: str, *, nested: str | None = None) -> str:
    """The row a canary asks for: ``CREATE_FIELDS[...]: Field(...)``, ready to paste.

    ``since`` is the tag the canary ran against — the canary runs on every
    release, so a field it has not seen before is new in that tag. Check it
    against the previous tag before committing the row.
    """
    read_from = f', read_from="{nested}"' if nested else ""
    return (
        f'CREATE_FIELDS["{endpoint}"]: Field("{field}", Gate(since={tag_version(tag)})'
        f"{read_from})"
    )


class TestRegistryLine:
    """What a red canary tells the next person to add — tested, because it is the
    instruction a release upgrade follows."""

    def test_names_endpoint_field_and_tag(self) -> None:
        assert registry_line("backups", "foo", "v4.5.2") == (
            'CREATE_FIELDS["backups"]: Field("foo", Gate(since=(4, 5, 2)))'
        )

    def test_a_nested_field_says_where_the_get_keeps_it(self) -> None:
        line = registry_line("applications/*", "bar", "v4.6.0", nested="settings")
        assert line.endswith('Gate(since=(4, 6, 0)), read_from="settings")')

    def test_an_unparseable_tag_fails_loudly(self) -> None:
        with pytest.raises(AssertionError):
            tag_version("latest")


class TestCreateAllowedFieldsExtraction:
    """The reverse canary is only as good as its parser, so the parser is tested.

    A silently empty extraction would make the canary pass forever while checking
    nothing — the exact failure mode that makes canaries dangerous.
    """

    def test_extracts_a_create_array(self) -> None:
        php = "$allowedFields = ['project_uuid', 'server_uuid', 'name'];"
        assert create_allowed_fields(php) == [{"project_uuid", "server_uuid", "name"}]

    def test_ignores_update_arrays(self) -> None:
        """Update arrays carry no project_uuid/server_uuid and must not be merged in.

        Merging one would make the canary far too permissive: dockerfile_target_build
        is in the UPDATE list, and folding that in is exactly how it stayed
        undetected in APPLICATION_CREATE.
        """
        php = "$allowedFields = ['name', 'description', 'dockerfile_target_build'];"
        assert create_allowed_fields(php) == []

    def test_expands_a_self_spread(self) -> None:
        php = (
            "private const APPLICATION_SETTING_FIELDS = ['is_gzip_enabled', 'gpu_count'];\n"
            "$allowedFields = ['project_uuid', 'server_uuid', "
            "...self::APPLICATION_SETTING_FIELDS];"
        )
        assert create_allowed_fields(php) == [
            {"project_uuid", "server_uuid", "is_gzip_enabled", "gpu_count"}
        ]

    def test_unresolvable_spread_does_not_invent_fields(self) -> None:
        php = "$allowedFields = ['project_uuid', 'server_uuid', ...self::NOT_HERE];"
        assert create_allowed_fields(php) == [{"project_uuid", "server_uuid"}]
