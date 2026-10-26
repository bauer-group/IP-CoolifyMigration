"""The pure decisions in the planner, each one a thing a real Coolify taught us.

Both functions here were wrong in ways that reported no error and moved no data,
which is why they are worth table-driven tests rather than a passing glance.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from bg_coolify_migrate.engine.planner import resource_labels, server_uuid_of

# ── server_uuid_of ───────────────────────────────────────────────────────────
# The three kinds hang their server off different relations. The original read
# `server_uuid` only, which exists on services and on nothing else, so the two
# kinds that matter most died at "could not determine the source server".

SERVER_SHAPES: list[tuple[str, dict[str, Any], str | None]] = [
    (
        "service: a real server() belongsTo relation",
        {"server": {"uuid": "srv-1", "name": "prod"}},
        "srv-1",
    ),
    (
        "some endpoints flatten it to server_uuid",
        {"server_uuid": "srv-2"},
        "srv-2",
    ),
    (
        "application: only destination(), a morphTo",
        {"destination": {"server_id": 1, "server": {"uuid": "srv-3"}}},
        "srv-3",
    ),
    (
        "database: same as application — this is the common case",
        {
            "uuid": "db-1",
            "destination_type": "App\\Models\\StandaloneDocker",
            "destination": {"id": 1, "server_id": 1, "server": {"uuid": "srv-4", "ip": "10.0.0.4"}},
        },
        "srv-4",
    ),
    (
        "server_uuid wins when both are present",
        {"server_uuid": "srv-5", "destination": {"server": {"uuid": "other"}}},
        "srv-5",
    ),
    (
        "destination present but its server relation was not loaded",
        {"destination": {"server_id": 7}},
        None,
    ),
    ("no server information at all", {"uuid": "x"}, None),
    ("destination is null, as it is on an undeployed resource", {"destination": None}, None),
]


@pytest.mark.parametrize(
    ("shape", "expected"),
    [(shape, expected) for _, shape, expected in SERVER_SHAPES],
    ids=[name for name, _, _ in SERVER_SHAPES],
)
def test_reads_the_server_from_whichever_relation_carries_it(
    shape: dict[str, Any], expected: str | None
) -> None:
    assert server_uuid_of(shape) == expected


def test_returns_none_rather_than_guessing_from_server_id() -> None:
    """None means "ask /servers", not "there is no server".

    Resolving a numeric id needs an API round trip, so this stays pure and the
    caller does it. Inventing a uuid from the id would be a fabrication.
    """
    assert server_uuid_of({"destination": {"server_id": 3}}) is None


# ── resource_labels ──────────────────────────────────────────────────────────
# Coolify's own filter is `--filter label=coolify.{kind}Id={id}`, and copying it
# from outside is impossible: every controller calls makeHidden(['id']). These
# are the labels that are actually visible, and they go through Str::slug.


def test_labels_a_resource_by_what_is_visible() -> None:
    assert resource_labels(project="shop", environment="production", name="api") == {
        "coolify.projectName": "shop",
        "coolify.environmentName": "production",
        "coolify.resourceName": "api",
    }


def test_slugifies_every_part() -> None:
    """Coolify slugs all three when it writes them, so we must when we read.

    Not cosmetic: an unslugged filter matches no containers, and `docker ps`
    answers an empty list rather than an error — the stack then looks like it has
    no volumes and the migration moves nothing, successfully.
    """
    labels = resource_labels(project="Grüße GmbH", environment="Pre Prod", name="api.example.com")
    assert labels == {
        "coolify.projectName": "grusse-gmbh",
        "coolify.environmentName": "pre-prod",
        # Dots are stripped, not turned into separators — see Str::slug.
        "coolify.resourceName": "apiexamplecom",
    }


# ── server_ref ───────────────────────────────────────────────────────────────
# Coolify's localhost self-record carries user='' and port can be blank. Both
# must fall back, or F2 (and F1) SSH with an empty user / port 0.


class TestServerRef:
    def test_empty_user_falls_back_to_root(self) -> None:
        """The localhost self-record has user='' — get-default does not catch it.

        `get("user", "root")` returns "" because the key is present-but-empty, so
        F2 SSHed as `@host` and got Permission denied. Coolify's own DB column
        defaults to 'root'; the record just blanks it. Found by the F2 e2e run.
        """
        from bg_coolify_migrate.engine.planner import server_ref

        ref = server_ref({"uuid": "u", "name": "localhost", "ip": "127.0.0.1", "user": ""})
        assert ref.user == "root"

    def test_missing_user_falls_back_to_root(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        assert server_ref({"uuid": "u", "ip": "10.0.0.1"}).user == "root"

    def test_explicit_user_is_kept(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        assert server_ref({"uuid": "u", "ip": "10.0.0.1", "user": "deploy"}).user == "deploy"

    def test_blank_port_falls_back_to_22(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        assert server_ref({"uuid": "u", "ip": "10.0.0.1", "port": 0}).port == 22

    def test_reads_the_wildcard_from_the_settings_relation(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        ref = server_ref(
            {
                "uuid": "u",
                "ip": "10.0.0.1",
                "settings": {"wildcard_domain": "app.0046-20.cloud.bauer-group.com"},
            }
        )
        assert ref.wildcard_domain == "app.0046-20.cloud.bauer-group.com"

    def test_missing_settings_yields_an_empty_wildcard(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        # The LIST endpoint does not eager-load settings; a missing relation must
        # not crash, it just means "no wildcard known here".
        assert server_ref({"uuid": "u", "ip": "10.0.0.1"}).wildcard_domain == ""


# ── build_plan uses the project NAME for the discovery filter ─────────────────
# Regression for a silent data-loss bug: a resource-scoped run resolves to
# (project_uuid, ...), and build_plan passed that raw uuid to the container label
# filter. Coolify labels containers with coolify.projectName=Str::slug(NAME), so
# slug(uuid) matched nothing, the running stack looked empty, and its volume was
# left behind (found migrating GlobaLeaks by uuid).


class TestBuildPlanProjectName:
    async def test_discovery_uses_resolved_name_not_the_uuid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.engine import planner

        captured: dict[str, str] = {}

        class _Stop(Exception):
            pass

        async def fake_find_project(api: Any, project: str) -> dict[str, Any]:
            # `project` arrives as the UUID for a resource-scoped run.
            assert project == "PROJ-UUID"
            return {"uuid": "PROJ-UUID", "name": "01. BAUER GROUP - Prod"}

        async def fake_find_server(api: Any, name: str) -> dict[str, Any]:
            return {"uuid": "SRV", "name": name, "ip": "1.2.3.4"}

        async def fake_env_resources(
            api: Any, project_uuid: str, environment: str
        ) -> list[tuple[str, dict[str, Any]]]:
            return [("applications", {"uuid": "r1", "name": "app"})]

        async def spy_snapshot(
            api: Any, source_host: Any, *, collection: str, resource: Any,
            project: str, environment: str,
        ) -> None:
            captured["project"] = project
            captured["environment"] = environment
            raise _Stop()

        monkeypatch.setattr(planner, "find_project", fake_find_project)
        monkeypatch.setattr(planner, "find_server", fake_find_server)
        monkeypatch.setattr(planner, "environment_resources", fake_env_resources)
        monkeypatch.setattr(planner, "snapshot_resource", spy_snapshot)

        class FakeApi:
            async def get_server(self, uuid: str) -> dict[str, Any]:
                return {"uuid": "SRV", "name": "srv", "ip": "1.2.3.4", "settings": {}}

        with pytest.raises(_Stop):
            await planner.build_plan(
                FakeApi(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                project="PROJ-UUID",
                environment="production",
                target_server="srv",
            )

        # The label filter must slugify the NAME, never the uuid.
        assert captured["project"] == "01. BAUER GROUP - Prod"
        assert captured["environment"] == "production"


# ── _resolve_git_auth ────────────────────────────────────────────────────────
# The GET serialises the raw columns — source_type/source_id/private_key_id —
# and NEVER github_app_uuid, which is a create-request field. Reading the
# create field off the GET classified every GitHub-App-backed application as
# PUBLIC, so its target was created credential-less and LoadComposeFile died
# asking for a Username (2026-07-22, both runs).


class _Api:
    """Answers canned GETs; any other path is an assertion failure."""

    _PATHS: ClassVar[dict[str, str]] = {
        "github_apps": "/github-apps",
        "security_keys": "/security/keys",
    }

    def __init__(self, **routes: Any) -> None:
        self.routes = {self._PATHS[k]: v for k, v in routes.items()}
        self.calls: list[str] = []

    async def get(self, path: str) -> Any:
        self.calls.append(path)
        if path not in self.routes:
            raise AssertionError(f"unexpected GET {path}")
        return self.routes[path]


class TestResolveGitAuth:
    async def test_github_app_source_resolves_to_its_uuid(self) -> None:
        from bg_coolify_migrate.engine.planner import _resolve_git_auth

        api = _Api(github_apps=[{"id": 5, "uuid": "gh-uuid", "is_public": False}])
        gh, pk = await _resolve_git_auth(
            api,  # type: ignore[arg-type]
            {
                "name": "wp",
                "git_repository": "acme/wordpress-stack",
                "source_type": r"App\Models\GithubApp",
                "source_id": 5,
                "private_key_id": None,
            },
        )
        assert (gh, pk) == ("gh-uuid", None)

    async def test_the_seeded_public_source_stays_public(self) -> None:
        # Coolify's "Public GitHub" source is itself a GithubApp row; passing its
        # uuid to the private route would be pointless indirection.
        from bg_coolify_migrate.engine.planner import _resolve_git_auth

        api = _Api(github_apps=[{"id": 0, "uuid": "pub", "is_public": True}])
        gh, pk = await _resolve_git_auth(
            api,  # type: ignore[arg-type]
            {
                "git_repository": "x/y",
                "source_type": r"App\Models\GithubApp",
                "source_id": 0,
            },
        )
        assert (gh, pk) == (None, None)

    async def test_a_real_deploy_key_beats_the_source(self) -> None:
        # Transcribed from Application::deploymentType(): private_key_id > 0 wins.
        from bg_coolify_migrate.engine.planner import _resolve_git_auth

        api = _Api(security_keys=[{"id": 3, "uuid": "key-uuid"}])
        gh, pk = await _resolve_git_auth(
            api,  # type: ignore[arg-type]
            {
                "git_repository": "x/y",
                "source_type": r"App\Models\GithubApp",
                "source_id": 5,
                "private_key_id": 3,
            },
        )
        assert (gh, pk) == (None, "key-uuid")
        assert api.calls == ["/security/keys"]  # /github-apps never consulted

    async def test_an_invisible_github_app_refuses_rather_than_public(self) -> None:
        from bg_coolify_migrate.engine.planner import _resolve_git_auth
        from bg_coolify_migrate.errors import UnsupportedResource

        api = _Api(github_apps=[{"id": 9, "uuid": "other", "is_public": False}])
        with pytest.raises(UnsupportedResource, match="not visible"):
            await _resolve_git_auth(
                api,  # type: ignore[arg-type]
                {
                    "name": "wp",
                    "git_repository": "x/y",
                    "source_type": r"App\Models\GithubApp",
                    "source_id": 5,
                },
            )

    async def test_a_gitlab_source_refuses_rather_than_public(self) -> None:
        from bg_coolify_migrate.engine.planner import _resolve_git_auth
        from bg_coolify_migrate.errors import UnsupportedResource

        api = _Api()
        with pytest.raises(UnsupportedResource, match="cannot be recreated"):
            await _resolve_git_auth(
                api,  # type: ignore[arg-type]
                {
                    "git_repository": "x/y",
                    "source_type": r"App\Models\GitlabApp",
                    "source_id": 2,
                },
            )

    async def test_a_plain_public_app_needs_no_lookup(self) -> None:
        from bg_coolify_migrate.engine.planner import _resolve_git_auth

        api = _Api()
        gh, pk = await _resolve_git_auth(
            api,  # type: ignore[arg-type]
            {"git_repository": "x/y", "source_type": None, "source_id": None},
        )
        assert (gh, pk) == (None, None)
        assert api.calls == []

    async def test_non_git_resources_are_untouched(self) -> None:
        from bg_coolify_migrate.engine.planner import _resolve_git_auth

        api = _Api()
        assert await _resolve_git_auth(api, {"name": "db"}) == (None, None)  # type: ignore[arg-type]
        assert api.calls == []

    async def test_the_localhost_key_counts_only_without_a_source(self) -> None:
        from bg_coolify_migrate.engine.planner import _resolve_git_auth

        api = _Api(security_keys=[{"id": 0, "uuid": "localhost-key"}])
        gh, pk = await _resolve_git_auth(
            api,  # type: ignore[arg-type]
            {"git_repository": "x/y", "private_key_id": 0},
        )
        assert (gh, pk) == (None, "localhost-key")


# ── build_plan feeds what the API cannot carry into the plan ─────────────────
# domain/fidelity.py is tested on its own; these pin the WIRING, because a rule
# nobody calls protects nothing and reports nothing — the failure mode this whole
# suite exists for.


class _PlanApi:
    """Just enough Coolify for build_plan once snapshot/manifest are stubbed."""

    def __init__(
        self,
        *,
        version: str | Exception,
        envs: list[dict[str, Any]],
        storages: dict[str, Any] | None = None,
        server_role: str = "both",
    ) -> None:
        self._version = version
        self._envs = envs
        self._storages = storages or {"persistent_storages": [], "file_storages": []}
        self._role = server_role

    async def get_server(self, uuid: str) -> dict[str, Any]:
        return {
            "uuid": uuid,
            "name": uuid,
            "ip": "10.0.0.2",
            "settings": {"traefik_certificate_resolver": None, "server_role": self._role},
        }

    async def version(self) -> str:
        if isinstance(self._version, Exception):
            raise self._version
        return self._version

    async def get_storages(self, collection: str, uuid: str) -> dict[str, Any]:
        return self._storages

    async def get_envs(self, collection: str, uuid: str) -> list[dict[str, Any]]:
        return self._envs


async def _plan_one_app(
    monkeypatch: pytest.MonkeyPatch,
    full: dict[str, Any],
    *,
    version: str | Exception = "4.5.0",
    envs: list[dict[str, Any]] | None = None,
    storages: dict[str, Any] | None = None,
    server_role: str = "both",
    snapshot: dict[str, Any] | None = None,
) -> Any:
    from bg_coolify_migrate.domain.kinds import BuildPack, ResourceKind
    from bg_coolify_migrate.domain.manifest import VolumeManifest
    from bg_coolify_migrate.domain.plan import ResourceSnapshot
    from bg_coolify_migrate.engine import planner

    shape = snapshot or {
        "collection": "applications",
        "kind": ResourceKind.APP_DOCKERIMAGE,
        "build_pack": BuildPack.DOCKERIMAGE,
    }

    async def fake_find_project(api: Any, project: str) -> dict[str, Any]:
        return {"uuid": "p1", "name": "shop"}

    async def fake_find_server(api: Any, name: str) -> dict[str, Any]:
        return {"uuid": "s2", "name": name}

    async def fake_env_resources(api: Any, project_uuid: str, environment: str) -> Any:
        return [(shape["collection"], {"uuid": "a1", "name": "api"})]

    async def fake_snapshot(api: Any, host: Any, **kw: Any) -> Any:
        return ResourceSnapshot(uuid="a1", name="api", **shape), [], full

    async def fake_resolve_server(api: Any, resource: Any) -> dict[str, Any]:
        return {
            "uuid": "s1",
            "name": "old",
            "ip": "10.0.0.1",
            "settings": {"traefik_certificate_resolver": "letsencrypt"},
        }

    async def fake_manifest(host: Any, **kw: Any) -> VolumeManifest:
        return VolumeManifest()

    async def no_drift(host: Any, snapshot: Any) -> None:
        return None

    async def no_mounts(host: Any, containers: Any) -> list[Any]:
        return []

    monkeypatch.setattr(planner, "find_project", fake_find_project)
    monkeypatch.setattr(planner, "find_server", fake_find_server)
    monkeypatch.setattr(planner, "environment_resources", fake_env_resources)
    monkeypatch.setattr(planner, "snapshot_resource", fake_snapshot)
    monkeypatch.setattr(planner, "resolve_server", fake_resolve_server)
    monkeypatch.setattr(planner, "build_manifest", fake_manifest)
    monkeypatch.setattr(planner, "assess_drift", no_drift)
    monkeypatch.setattr(planner, "inspect_all_mounts", no_mounts)

    return await planner.build_plan(
        _PlanApi(  # type: ignore[arg-type]
            version=version, envs=envs or [], storages=storages, server_role=server_role
        ),
        object(),  # type: ignore[arg-type]
        project="shop",
        environment="production",
        target_server="new",
    )


class TestBuildPlanFidelity:
    async def test_a_create_coolify_would_reject_blocks_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_one_app(monkeypatch, {"manual_webhook_secret_github": "short"})
        assert plan.is_blocked
        assert "manual webhook secret" in plan.resources[0].blocking_reasons[0]

    async def test_the_rule_respects_the_instance_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 4.3.x still accepts a short webhook secret on create.
        plan = await _plan_one_app(
            monkeypatch, {"manual_webhook_secret_github": "short"}, version="4.3.10"
        )
        assert not plan.is_blocked

    async def test_an_unreadable_version_applies_every_rule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_one_app(
            monkeypatch,
            {"manual_webhook_secret_github": "short"},
            version=RuntimeError("version endpoint down"),
        )
        assert plan.is_blocked

    async def test_env_rules_see_the_source_envs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_one_app(
            monkeypatch,
            {},
            envs=[{"key": "DB_URL", "value": "{{vault.DB}}", "is_literal": False}],
        )
        assert any("secret manager" in r for r in plan.resources[0].refusals)

    async def test_server_level_differences_become_plan_notes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_one_app(monkeypatch, {})
        assert not plan.is_blocked
        assert any("letsencrypt" in n for n in plan.notes)

    async def test_compose_shapes_refuse_a_compose_backed_resource(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.domain.kinds import ResourceKind

        plan = await _plan_one_app(
            monkeypatch,
            {},
            snapshot={
                "collection": "services",
                "kind": ResourceKind.SERVICE_COMPOSE,
                "docker_compose_raw": (
                    "services:\n  a:\n    image: x\nvolumes:\n  d:\n    external: true\n"
                ),
            },
        )
        assert any("is external" in r for r in plan.resources[0].refusals)

    async def test_a_host_path_storage_refuses_a_plain_application(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_one_app(
            monkeypatch,
            {},
            storages={
                "persistent_storages": [{"name": "a1-d", "mount_path": "/d", "host_path": "/srv/d"}],
                "file_storages": [],
            },
        )
        assert any("host path" in r for r in plan.resources[0].refusals)

    async def test_an_engine_this_tool_cannot_recreate_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bg_coolify_migrate.domain.kinds import ResourceKind

        plan = await _plan_one_app(
            monkeypatch,
            {"type": "standalone-sqlite"},
            snapshot={"collection": "databases", "kind": ResourceKind.DATABASE},
        )
        assert any("SQLite" in r for r in plan.resources[0].refusals)

    async def test_a_build_server_target_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_one_app(monkeypatch, {}, server_role="build")
        assert any("build server" in r for r in plan.resources[0].refusals)
