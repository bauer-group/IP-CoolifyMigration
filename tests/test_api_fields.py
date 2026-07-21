"""Tests for the per-endpoint request whitelists.

These encode verified upstream `$allowedFields` arrays. The last test in this
file fetches Coolify's live openapi.json and reports drift — it is marked
`integration` so it never blocks a local run, and a scheduled CI job runs it so
that upstream drift breaks OUR ci rather than someone's production migration.
"""

from __future__ import annotations

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
    database_allowed,
    database_health_check_warnings,
    filter_body,
    missing_required,
    rejected_keys,
)
from bg_coolify_migrate.domain.kinds import DatabaseEngine


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


class TestEnvFields:
    def test_core_fields(self) -> None:
        assert {"key", "value"} <= ENV_FIELDS

    def test_is_shown_once_is_a_real_field(self) -> None:
        # It is UI-only and does NOT hide values from API reads, but it IS
        # settable and must round-trip.
        assert "is_shown_once" in ENV_FIELDS

    def test_build_and_runtime_flags(self) -> None:
        assert "is_runtime" in ENV_FIELDS
        assert "is_buildtime" in ENV_FIELDS


@pytest.mark.integration
async def test_whitelists_match_upstream_openapi() -> None:
    """Report drift between our whitelists and Coolify's published openapi.json.

    Marked `integration` because it needs network. A scheduled CI job runs it so
    that an upstream API change breaks our CI rather than a user's migration.

    NOTE: OpenAPI and `$allowedFields` genuinely disagree in places — the OA
    attributes are documentation, the arrays are the enforcement. So this test
    reports EXTRA fields we might be missing; it does not fail on fields we
    deliberately exclude (documented in api/fields.py).
    """
    import httpx

    url = "https://raw.githubusercontent.com/coollabsio/coolify/main/openapi.json"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        spec = response.json()

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
        missing = documented - SERVICE_CREATE_CUSTOM_COMPOSE - openapi_only
        if missing:
            drift["POST /services"] = sorted(missing)

    # Databases carry one create route per engine and they drifted together when
    # upstream added `tags` — checking only /services would have caught one of
    # nine routes. The engine-specific credential fields differ per route, so each
    # is diffed against its own database_allowed().
    for engine in sorted(DATABASE_ENGINE_FIELDS):
        path = f"/databases/{engine}"
        documented = body_props(path, "post")
        if not documented:
            continue
        missing = documented - database_allowed(engine)
        if missing:
            drift[f"POST {path}"] = sorted(missing)

    assert not drift, (
        f"openapi.json documents fields we do not whitelist: {drift}. "
        f"Upstream may have added fields; review api/fields.py. Adjudicate each "
        f"against the controller's $allowedFields before whitelisting — the schema "
        f"documents fields the controller rejects (see connect_to_docker_network)."
    )


#: POST /applications/* documents 17 fields APPLICATION_CREATE does not carry.
#:
#: ADJUDICATED against ApplicationsController — all 17 are genuinely accepted on
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
KNOWN_APPLICATION_GAP: frozenset[str] = frozenset(
    {
        "autogenerate_domain",
        "disable_build_cache",
        "docker_images_to_keep",
        "force_domain_override",
        "include_source_commit_in_build",
        "inject_build_args_to_dockerfile",
        "is_env_sorting_enabled",
        "is_git_lfs_enabled",
        "is_git_shallow_clone_enabled",
        "is_git_submodules_enabled",
        "is_gzip_enabled",
        "is_pr_deployments_public_enabled",
        "is_preview_deployments_enabled",
        "is_raw_compose_deployment_enabled",
        "is_stripprefix_enabled",
        "stop_grace_period",
        "use_build_secrets",
    }
)


@pytest.mark.integration
async def test_application_gap_does_not_grow() -> None:
    """The application whitelist gap is known; this fails if it WIDENS.

    Pinning the gap instead of excluding the whole route keeps the canary useful:
    a newly documented application field still breaks CI, but the 17 already
    triaged do not cry every week.
    """
    import httpx

    url = "https://raw.githubusercontent.com/coollabsio/coolify/main/openapi.json"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        spec = response.json()

    routes = ("public", "private-github-app", "private-deploy-key", "dockerfile", "dockerimage")
    new: dict[str, list[str]] = {}
    for route in routes:
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
        missing = documented - APPLICATION_CREATE - KNOWN_APPLICATION_GAP
        if missing:
            new[route] = sorted(missing)

    assert not new, (
        f"NEW fields documented for POST /applications/*: {new}. Adjudicate against "
        f"ApplicationsController's $allowedFields, then either whitelist in "
        f"APPLICATION_CREATE or add to KNOWN_APPLICATION_GAP with a reason."
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
