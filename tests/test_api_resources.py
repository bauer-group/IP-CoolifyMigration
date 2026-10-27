"""Tests for resource re-creation.

The env-copying tests encode a decision that is easy to get backwards and
impossible to notice afterwards: copy `value`, not `real_value`.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.resources import (
    Placement,
    _strip_uuid_prefix,
    build_env_entries,
    copy_envs,
    copy_storages,
    copy_tags,
    create_resource,
    ensure_project,
    read_volume_endpoints,
    resolve_destination,
    retarget_fs_path,
    storage_endpoints,
)
from bg_coolify_migrate.domain.kinds import DatabaseEngine, GitAuth, ResourceKind
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from bg_coolify_migrate.errors import CoolifyApiError, UnsupportedResource

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


def _placement() -> Placement:
    return Placement(
        project_uuid="proj1", environment_name="production", server_uuid="srv2"
    )


def _placement_wc() -> Placement:
    """A placement whose servers both carry a wildcard base — the BAUER setup."""
    return Placement(
        project_uuid="proj1",
        environment_name="production",
        server_uuid="srv2",
        source_wildcard="app.0046-20.cloud.bauer-group.com",
        target_wildcard="app.0047-20.cloud.bauer-group.com",
    )


class TestBuildEnvEntries:
    def test_copies_value_not_real_value(self) -> None:
        # real_value is an ACCESSOR that RESOLVES Coolify's magic variables. A
        # SERVICE_FQDN_* would come back already expanded to the SOURCE's domain;
        # writing that to the target bakes the old hostname in permanently. The
        # raw value keeps the magic so Coolify re-resolves it for the new uuid.
        entries = build_env_entries(
            [{"key": "SERVICE_FQDN_APP", "value": "$SERVICE_FQDN_APP", "real_value": "https://old.example.com"}]
        )
        assert entries == [{"key": "SERVICE_FQDN_APP", "value": "$SERVICE_FQDN_APP"}]

    def test_drops_entries_with_no_value_key(self) -> None:
        # That state means the token lacked read:sensitive. We never guess a
        # secret; the client asserts scope long before this.
        assert build_env_entries([{"key": "SECRET"}]) == []

    def test_preserves_flags(self) -> None:
        entries = build_env_entries(
            [{"key": "A", "value": "1", "is_literal": True, "is_buildtime": True}]
        )
        assert entries[0]["is_literal"] is True
        assert entries[0]["is_buildtime"] is True

    def test_drops_unknown_fields(self) -> None:
        # $allowedFields is enforced; an unknown key would 422 on create.
        entries = build_env_entries([{"key": "A", "value": "1", "id": 5, "uuid": "x"}])
        assert set(entries[0]) == {"key", "value"}

    def test_drops_entries_without_a_key(self) -> None:
        assert build_env_entries([{"value": "orphan"}]) == []

    def test_empty_value_is_kept(self) -> None:
        # An intentionally-empty variable is not the same as an absent one.
        assert build_env_entries([{"key": "A", "value": ""}]) == [{"key": "A", "value": ""}]


class TestStorageEndpoints:
    def test_maps_persistent_storages(self) -> None:
        endpoints = storage_endpoints(
            {
                "persistent_storages": [
                    {"name": "u1-data", "mount_path": "/var/lib/postgresql/data"}
                ],
                "file_storages": [{"mount_path": "/app/config.yml", "content": "x"}],
            }
        )
        assert len(endpoints) == 1
        assert endpoints[0].name == "u1-data"
        assert endpoints[0].mount_path == "/var/lib/postgresql/data"

    def test_ignores_entries_without_a_name_or_path(self) -> None:
        assert storage_endpoints({"persistent_storages": [{"name": "x"}]}) == []

    def test_empty(self) -> None:
        assert storage_endpoints({}) == []


class TestStripUuidPrefix:
    def test_strips(self) -> None:
        # Upstream re-prefixes with the NEW uuid whatever we send, so we send the
        # bare name.
        assert _strip_uuid_prefix("olduuid-data", "olduuid") == "data"

    def test_leaves_unprefixed_names(self) -> None:
        assert _strip_uuid_prefix("data", "olduuid") == "data"


class TestEnsureProject:
    async def test_reuses_an_existing_project(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/projects").mock(
            return_value=httpx.Response(200, json=[{"uuid": "p1", "name": "shop"}])
        )
        assert await ensure_project(api, "shop") == "p1"

    async def test_creates_a_missing_project(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/projects").mock(return_value=httpx.Response(200, json=[]))
        route = respx_mock.post(f"{BASE}/projects").mock(
            return_value=httpx.Response(201, json={"uuid": "p2"})
        )
        assert await ensure_project(api, "shop") == "p2"
        assert route.called

    async def test_create_without_uuid_raises(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/projects").mock(return_value=httpx.Response(200, json=[]))
        respx_mock.post(f"{BASE}/projects").mock(return_value=httpx.Response(201, json={}))
        with pytest.raises(CoolifyApiError, match="no uuid"):
            await ensure_project(api, "shop")


class TestResolveDestination:
    """Destinations come from /servers/{uuid}/destinations, never from the server GET.

    They were read off ``GET /servers/{uuid}`` until 2026-08-16, where they have
    never been present on any Coolify — ``server_by_uuid`` loads only
    ``['settings']``. These tests passed anyway, because a mock will happily return
    a field the real API does not have. Verified against a live 4.3.2: both
    ``destinations`` and ``standalone_dockers`` come back ``null``.
    """

    async def test_single_destination_needs_no_choice(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/srv1/destinations").mock(
            return_value=httpx.Response(200, json=[{"uuid": "d1", "network": "coolify"}])
        )
        assert await resolve_destination(api, "srv1") is None

    async def test_multiple_destinations_prefers_the_coolify_network(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/srv1/destinations").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"uuid": "d1", "network": "other"},
                    {"uuid": "d2", "network": "coolify"},
                ],
            )
        )
        assert await resolve_destination(api, "srv1") == "d2"

    async def test_ambiguous_destinations_refuse_rather_than_guess(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/srv1/destinations").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"uuid": "d1", "network": "a"},
                    {"uuid": "d2", "network": "b"},
                ],
            )
        )
        with pytest.raises(UnsupportedResource, match="will not guess"):
            await resolve_destination(api, "srv1")

    async def test_older_coolify_without_the_route_lets_coolify_choose(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """Coolify <4.2 has no destinations route; a 404 must not fail the migration.

        The route arrived in 4.2 (zero destination routes in v4.1.2's api.php). On
        an older instance we cannot enumerate them, so we send no destination_uuid
        and let Coolify pick — which is correct for the single-destination case
        that covers this in practice.
        """
        respx_mock.get(f"{BASE}/servers/srv1/destinations").mock(
            return_value=httpx.Response(404, json={"message": "Not found."})
        )
        assert await resolve_destination(api, "srv1") is None

    async def test_a_real_error_is_not_swallowed_as_absent(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """Only 404 means "older Coolify". A 500 is a fault and must surface.

        Treating every failure as "route absent" would turn a broken instance into
        a silent wrong-destination placement.
        """
        respx_mock.get(f"{BASE}/servers/srv1/destinations").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        with pytest.raises(CoolifyApiError):
            await resolve_destination(api, "srv1")


class TestCopyTags:
    """Tags are cosmetic and run after the target exists, so failure must be free.

    The 2.5.6 outage was tags on the critical path: a 404 from /tags killed every
    migration. These tests pin the property that prevents a repeat — every failure
    path returns 0 rather than raising.
    """

    async def test_copies_names_through_the_dedicated_route(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/src1/tags").mock(
            return_value=httpx.Response(
                200, json=[{"uuid": "t1", "name": "prod"}, {"uuid": "t2", "name": "eu"}]
            )
        )
        post = respx_mock.post(f"{BASE}/applications/tgt1/tags").mock(
            return_value=httpx.Response(201, json=[])
        )
        count = await copy_tags(
            api, collection="applications", source_uuid="src1", target_uuid="tgt1"
        )
        assert count == 2
        assert json.loads(post.calls[0].request.read().decode()) == {"tag_names": ["prod", "eu"]}

    async def test_missing_route_is_not_an_error(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Coolify <4.2 has no tag routes at all.
        respx_mock.get(f"{BASE}/applications/src1/tags").mock(
            return_value=httpx.Response(404, json={"message": "Not found."})
        )
        assert (
            await copy_tags(api, collection="applications", source_uuid="src1", target_uuid="tgt1")
            == 0
        )

    async def test_a_read_failure_never_raises(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/src1/tags").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        assert (
            await copy_tags(api, collection="applications", source_uuid="src1", target_uuid="tgt1")
            == 0
        )

    async def test_a_write_failure_never_raises(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """The important one: the target already exists at this point.

        Raising here would abort a migration whose resource was created fine, over
        a label.
        """
        respx_mock.get(f"{BASE}/applications/src1/tags").mock(
            return_value=httpx.Response(200, json=[{"uuid": "t1", "name": "prod"}])
        )
        respx_mock.post(f"{BASE}/applications/tgt1/tags").mock(
            return_value=httpx.Response(422, json={"message": "nope"})
        )
        assert (
            await copy_tags(api, collection="applications", source_uuid="src1", target_uuid="tgt1")
            == 0
        )

    async def test_drops_names_upstream_would_reject(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """Upstream requires >=2 characters. One short name must not cost the rest."""
        respx_mock.get(f"{BASE}/applications/src1/tags").mock(
            return_value=httpx.Response(
                200, json=[{"uuid": "t1", "name": "x"}, {"uuid": "t2", "name": "prod"}]
            )
        )
        post = respx_mock.post(f"{BASE}/applications/tgt1/tags").mock(
            return_value=httpx.Response(201, json=[])
        )
        count = await copy_tags(
            api, collection="applications", source_uuid="src1", target_uuid="tgt1"
        )
        assert count == 1
        assert json.loads(post.calls[0].request.read().decode()) == {"tag_names": ["prod"]}

    async def test_no_tags_means_no_write_at_all(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/src1/tags").mock(
            return_value=httpx.Response(200, json=[])
        )
        post = respx_mock.post(f"{BASE}/applications/tgt1/tags").mock(
            return_value=httpx.Response(201, json=[])
        )
        assert (
            await copy_tags(api, collection="applications", source_uuid="src1", target_uuid="tgt1")
            == 0
        )
        assert not post.called


class TestCreateDatabase:
    def _snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            uuid="db1",
            name="postgres",
            collection="databases",
            kind=ResourceKind.DATABASE,
            engine=DatabaseEngine.POSTGRESQL,
            image="postgres:16",
        )

    async def test_pins_the_image(self, api: CoolifyClient, respx_mock: respx.Router) -> None:
        # The model's created hook parses the tag to choose the mount path
        # (Postgres >=18 moves to /var/lib/postgresql). Unpinned = wrong path.
        route = respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )
        await create_resource(
            api, self._snapshot(), _placement(), {"postgres_password": "s3cret", "image": "postgres:16"}
        )
        body = route.calls[0].request.read().decode()
        assert '"image":"postgres:16"' in body

    async def test_never_starts_the_target(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Nothing may run before the DNS gate has decided.
        route = respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )
        await create_resource(api, self._snapshot(), _placement(), {})
        assert '"instant_deploy":false' in route.calls[0].request.read().decode()

    async def test_does_not_expose_a_public_port(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )
        await create_resource(
            api, self._snapshot(), _placement(), {"public_port": 5432, "is_public": True}
        )
        body = route.calls[0].request.read().decode()
        assert "public_port" not in body
        assert '"is_public":false' in body

    async def test_never_sends_disallowed_fields(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # A GET response round-tripped into a POST is a 422 per extra field.
        route = respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )
        await create_resource(
            api,
            self._snapshot(),
            _placement(),
            {"id": 5, "uuid": "db1", "status": "running", "config_hash": "abc"},
        )
        body = route.calls[0].request.read().decode()
        for forbidden in ('"id"', '"status"', '"config_hash"'):
            assert forbidden not in body

    async def test_engineless_database_raises(self, api: CoolifyClient) -> None:
        snapshot = self._snapshot().model_copy(update={"engine": None})
        with pytest.raises(UnsupportedResource, match="no engine"):
            await create_resource(api, snapshot, _placement(), {})

    async def test_a_custom_config_is_sent_base64_encoded(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """REGRESSION: every DB create route has required base64 *_conf since 4.1.2.

        The GET returns the stored plaintext; sent verbatim it was a 422 "The
        postgres_conf should be base64 encoded.", so no database with a custom
        config could ever be migrated.
        """
        route = respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )
        conf = "max_connections = 200\nshared_buffers = 1GB\n"
        await create_resource(api, self._snapshot(), _placement(), {"postgres_conf": conf})
        body = json.loads(route.calls[0].request.read().decode())
        assert base64.b64decode(body["postgres_conf"]).decode() == conf

    async def test_an_empty_config_is_left_out(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.post(f"{BASE}/databases/postgresql").mock(
            return_value=httpx.Response(201, json={"uuid": "db2"})
        )
        await create_resource(api, self._snapshot(), _placement(), {"postgres_conf": ""})
        assert "postgres_conf" not in json.loads(route.calls[0].request.read().decode())


class TestCreateService:
    def _snapshot(self, **kw: object) -> ResourceSnapshot:
        base = {
            "uuid": "s1",
            "name": "minio",
            "collection": "services",
            "kind": ResourceKind.SERVICE_COMPOSE,
            "docker_compose_raw": "services:\n  minio:\n    image: minio/minio",
        }
        return ResourceSnapshot(**{**base, **kw})  # type: ignore[arg-type]

    async def test_compose_is_base64_encoded(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.post(f"{BASE}/services").mock(
            return_value=httpx.Response(201, json={"uuid": "s2"})
        )
        await create_resource(api, self._snapshot(), _placement(), {})
        body = route.calls[0].request.read().decode()
        expected = base64.b64encode(
            b"services:\n  minio:\n    image: minio/minio"
        ).decode()
        assert expected in body

    async def test_tags_are_never_sent(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # REGRESSION (2.5.6): `tags` is whitelisted on no create route in any
        # published Coolify, so a body carrying it 422s the whole resource. Even
        # if a caller hands one in, filter_body must drop it.
        route = respx_mock.post(f"{BASE}/services").mock(
            return_value=httpx.Response(201, json={"uuid": "s2"})
        )
        await create_resource(api, self._snapshot(), _placement(), {"tags": ["prod"]})
        assert "tags" not in json.loads(route.calls[0].request.read().decode())

    async def test_custom_compose_omits_type(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Sending BOTH type and docker_compose_raw is a 422 upstream.
        route = respx_mock.post(f"{BASE}/services").mock(
            return_value=httpx.Response(201, json={"uuid": "s2"})
        )
        await create_resource(api, self._snapshot(), _placement(), {"type": "minio"})
        assert '"type"' not in route.calls[0].request.read().decode()

    async def test_template_service_omits_compose(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.post(f"{BASE}/services").mock(
            return_value=httpx.Response(201, json={"uuid": "s2"})
        )
        snapshot = self._snapshot(
            kind=ResourceKind.SERVICE_TEMPLATE, service_type="minio", docker_compose_raw=None
        )
        await create_resource(api, snapshot, _placement(), {})
        body = route.calls[0].request.read().decode()
        assert '"type":"minio"' in body
        assert "docker_compose_raw" not in body

    async def test_missing_compose_explains_the_token_scope(self, api: CoolifyClient) -> None:
        snapshot = self._snapshot(docker_compose_raw=None)
        with pytest.raises(UnsupportedResource, match="read:sensitive"):
            await create_resource(api, snapshot, _placement(), {})


def _git_build_snapshot() -> ResourceSnapshot:
    return ResourceSnapshot(
        uuid="a1",
        name="web",
        collection="applications",
        kind=ResourceKind.APP_GIT_BUILD,
        git_repository="https://github.com/x/y",
        git_branch="main",
        git_auth=GitAuth.PUBLIC,
    )


class TestCreateApplication:
    async def test_an_image_app_never_sends_its_build_pack(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """Regression: the GET says build_pack 'dockerimage', which no create accepts.

        Every create validates a sent build_pack against BuildPackTypes, an enum
        without 'dockerimage' (helpers/api.php v4.1.2:95 .. v4.5.0:192), so the
        carried field 422'd every image app — found by the first e2e run that
        migrated one.
        """
        import json

        route = respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1", name="web", collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
        )
        await create_resource(
            api,
            snapshot,
            _placement(),
            {
                "build_pack": "dockerimage",
                "docker_registry_image_name": "nginx",
                "docker_registry_image_tag": "1.27-alpine",
                "ports_exposes": "80",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert "build_pack" not in body
        assert body["docker_registry_image_name"] == "nginx"

    async def test_github_app_snapshot_creates_via_the_private_route(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """Regression: a GitHub-App-backed app must NOT become public.

        The planner resolves source_id -> github_app_uuid; create must send it
        on the private-github-app route, or the target is created without
        credentials and its LoadComposeFile dies asking for a Username.
        """
        import json

        route = respx_mock.post(f"{BASE}/applications/private-github-app").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="wp",
            collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_repository="acme/wordpress-stack",
            git_branch="main",
            git_auth=GitAuth.GITHUB_APP,
            github_app_uuid="gh-uuid",
        )
        await create_resource(
            api,
            snapshot,
            _placement(),
            {
                "git_repository": "acme/wordpress-stack",
                "git_branch": "main",
                "build_pack": "dockercompose",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert body["github_app_uuid"] == "gh-uuid"
        # The short owner/repo form is what the private route accepts — no URL
        # expansion, which is public-route-only behaviour.
        assert body["git_repository"] == "acme/wordpress-stack"

    async def test_deploy_key_snapshot_sends_the_resolved_key_uuid(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        route = respx_mock.post(f"{BASE}/applications/private-deploy-key").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="web",
            collection="applications",
            kind=ResourceKind.APP_GIT_BUILD,
            git_repository="git@github.com:x/y.git",
            git_branch="main",
            git_auth=GitAuth.DEPLOY_KEY,
            private_key_uuid="key-uuid",
        )
        await create_resource(
            api,
            snapshot,
            _placement(),
            {
                "git_repository": "git@github.com:x/y.git",
                "git_branch": "main",
                "build_pack": "nixpacks",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert body["private_key_uuid"] == "key-uuid"

    async def test_rewrites_a_server_bound_url_onto_the_target_wildcard(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # A URL under the source server's wildcard is bound to that server and
        # cannot cut over; it is rewritten onto the target's wildcard so the app
        # keeps its subdomain on the new host.
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        await create_resource(
            api,
            _git_build_snapshot(),
            _placement_wc(),
            {
                "git_repository": "https://github.com/x/y",
                "git_branch": "main",
                "build_pack": "nixpacks",
                "fqdn": "https://pdf-tool.app.0046-20.cloud.bauer-group.com",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert body["domains"] == "https://pdf-tool.app.0047-20.cloud.bauer-group.com"
        # fqdn is Coolify's stored column, not the create input — never sent.
        assert "fqdn" not in body

    async def test_creates_without_a_custom_domain_the_source_still_holds(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Make-before-break: the source keeps its custom domain; the target claims
        # it after its gates (claim_application_domains), never in the create.
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        await create_resource(
            api,
            _git_build_snapshot(),
            _placement_wc(),
            {
                "git_repository": "https://github.com/x/y",
                "git_branch": "main",
                "build_pack": "nixpacks",
                "fqdn": "https://shop.example.com",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert "domains" not in body
        # Nothing to create with: no generated placeholder the claim would replace.
        assert body["autogenerate_domain"] is False

    async def test_without_wildcards_every_real_host_is_claimed_later(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # No wildcard configured on either side => nothing tells a server-bound
        # host from a custom one, so every real host is claimed after the create
        # (unchanged, never dropped or corrupted).
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        await create_resource(
            api,
            _git_build_snapshot(),
            _placement(),  # no wildcards
            {
                "git_repository": "https://github.com/x/y",
                "git_branch": "main",
                "build_pack": "nixpacks",
                "fqdn": "https://shop.example.com",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert "domains" not in body

    async def test_compose_app_sends_custom_labels_base64_encoded(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Regression: Coolify 422s a plaintext custom_labels ("should be base64
        # encoded"). The compose app (bauer-group/pair-drop) failed here.
        import base64
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="pair-drop",
            collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_repository="https://github.com/x/y",
            git_branch="main",
            git_auth=GitAuth.PUBLIC,
        )
        labels = "traefik.enable=true\ntraefik.http.routers.x.rule=Host(`a.com`)"
        await create_resource(
            api,
            snapshot,
            _placement_wc(),
            {
                "git_repository": "https://github.com/x/y",
                "git_branch": "main",
                "build_pack": "dockercompose",
                "custom_labels": labels,
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert base64.b64decode(body["custom_labels"]).decode() == labels

    async def test_compose_app_remaps_docker_compose_domains_to_target(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # A compose app's per-service domains must be rewritten onto the target's
        # wildcard and sent as the create ARRAY — not dropped (which left the
        # target with no URL) and not via `domains` (422 for dockercompose).
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="pair-drop",
            collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_repository="https://github.com/x/y",
            git_branch="main",
            git_auth=GitAuth.PUBLIC,
        )
        await create_resource(
            api,
            snapshot,
            _placement_wc(),
            {
                "git_repository": "https://github.com/x/y",
                "git_branch": "main",
                "build_pack": "dockercompose",
                "docker_compose_domains": json.dumps(
                    {"pairdrop": {"domain": "https://airdrop.app.0046-20.cloud.bauer-group.com"}}
                ),
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert body["docker_compose_domains"] == [
            {"name": "pairdrop", "domain": "https://airdrop.app.0047-20.cloud.bauer-group.com"}
        ]
        # `domains` must never be sent for a dockercompose app.
        assert "domains" not in body

    async def test_compose_carries_multiple_service_urls_and_an_empty_one(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The MinIO shape: two services with custom URLs, one with none.
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1", name="minio", collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_repository="https://github.com/x/y", git_branch="main", git_auth=GitAuth.PUBLIC,
        )
        await create_resource(
            api, snapshot, _placement_wc(),
            {
                "git_repository": "https://github.com/x/y", "git_branch": "main",
                "build_pack": "dockercompose",
                "docker_compose_domains": json.dumps(
                    {
                        "minio-server": {"domain": "https://assets.bauer-group.com"},
                        "admin-console": {"domain": "https://console.assets.bauer-group.com"},
                        "minio-init": {"domain": ""},
                    }
                ),
            },
        )
        body = json.loads(route.calls[0].request.read())
        # Every service is listed; the custom URLs are claimed after the compose
        # gate (a forced compose CREATE is ignored before 4.5), so none here.
        assert body["docker_compose_domains"] == [
            {"name": "minio-server", "domain": ""},
            {"name": "admin-console", "domain": ""},
            {"name": "minio-init", "domain": ""},
        ]
        assert body["autogenerate_domain"] is False

    async def test_domainless_compose_stack_suppresses_autogenerate(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # A stack reached only via a cloudflared tunnel has no domains and must
        # stay domain-less — Coolify must not auto-generate one.
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1", name="tunnel", collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_repository="https://github.com/x/y", git_branch="main", git_auth=GitAuth.PUBLIC,
        )
        await create_resource(
            api, snapshot, _placement_wc(),
            {
                "git_repository": "https://github.com/x/y", "git_branch": "main",
                "build_pack": "dockercompose",
                # no docker_compose_domains at all
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert body["autogenerate_domain"] is False
        assert "docker_compose_domains" not in body
        assert "domains" not in body

    async def test_domained_app_does_not_suppress_autogenerate(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        await create_resource(
            api, _git_build_snapshot(), _placement_wc(),
            {
                "git_repository": "https://github.com/x/y", "git_branch": "main",
                "build_pack": "nixpacks",
                "fqdn": "https://shop.example.com,https://pdf.app.0046-20.cloud.bauer-group.com",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert body["domains"] == "https://pdf.app.0047-20.cloud.bauer-group.com"
        assert "autogenerate_domain" not in body  # a domain is set; leave the default

    async def test_a_generated_host_is_dropped_and_regenerated(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # An sslip.io host names the SOURCE's IP: carrying it would route a host
        # that resolves to the source. The target generates its own.
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        await create_resource(
            api, _git_build_snapshot(), _placement_wc(),
            {
                "git_repository": "https://github.com/x/y", "git_branch": "main",
                "build_pack": "nixpacks", "fqdn": "http://a1.10.0.0.1.sslip.io",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert "domains" not in body
        assert "autogenerate_domain" not in body

    async def test_dropped_server_bound_url_still_allows_autogenerate_fallback(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Source HAD a server-bound URL, but the target has no wildcard to place it
        # on -> we drop it, and Coolify's sslip.io fallback should still be allowed
        # (this is NOT a genuinely domain-less stack).
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        placement = Placement(
            project_uuid="p", environment_name="production", server_uuid="s",
            source_wildcard="app.0046-20.cloud.bauer-group.com", target_wildcard=None,
        )
        await create_resource(
            api, _git_build_snapshot(), placement,
            {
                "git_repository": "https://github.com/x/y", "git_branch": "main",
                "build_pack": "nixpacks",
                "fqdn": "https://x.app.0046-20.cloud.bauer-group.com",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert "domains" not in body  # dropped: could not remap
        assert "autogenerate_domain" not in body  # source HAD a domain -> allow fallback

    async def test_missing_git_fields_raise_before_the_round_trip(
        self, api: CoolifyClient
    ) -> None:
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="web",
            collection="applications",
            kind=ResourceKind.APP_GIT_BUILD,
            git_auth=GitAuth.PUBLIC,
        )
        with pytest.raises(UnsupportedResource, match="missing required field"):
            await create_resource(api, snapshot, _placement(), {})

    async def test_public_short_repo_is_rebuilt_into_a_github_url(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Coolify stores a public app's repo as owner/repo, but the public create
        # route validates git_repository as a URL (422 otherwise). Reconstruct it.
        import json

        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="bentopdf",
            collection="applications",
            kind=ResourceKind.APP_GIT_BUILD,
            git_repository="alam00000/bentopdf",
            git_branch="main",
            git_auth=GitAuth.PUBLIC,
        )
        await create_resource(
            api,
            snapshot,
            _placement(),
            {
                "git_repository": "alam00000/bentopdf",
                "git_branch": "main",
                "build_pack": "nixpacks",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert body["git_repository"] == "https://github.com/alam00000/bentopdf"


class TestPublicGitUrl:
    def test_short_form_becomes_a_github_url(self) -> None:
        from bg_coolify_migrate.api.resources import public_git_url

        assert public_git_url("alam00000/bentopdf") == "https://github.com/alam00000/bentopdf"
        assert public_git_url("/owner/repo/") == "https://github.com/owner/repo"

    def test_a_real_url_is_left_untouched(self) -> None:
        from bg_coolify_migrate.api.resources import public_git_url

        for url in (
            "https://github.com/o/r",
            "http://example.com/o/r",
            "git://example.com/o/r",
            "git@github.com:o/r.git",
        ):
            assert public_git_url(url) == url


class TestRemapDomains:
    SRC = "app.0046-20.cloud.bauer-group.com"
    TGT = "app.0047-20.cloud.bauer-group.com"

    def _remap(self, fqdn: str | None) -> str:
        from bg_coolify_migrate.api.resources import _remap_domains

        return _remap_domains(fqdn, source_wildcard=self.SRC, target_wildcard=self.TGT)

    def test_server_bound_url_is_rewritten(self) -> None:
        assert (
            self._remap("https://pdf-tool.app.0046-20.cloud.bauer-group.com")
            == "https://pdf-tool.app.0047-20.cloud.bauer-group.com"
        )

    def test_custom_domain_is_kept_verbatim(self) -> None:
        assert self._remap("https://shop.example.com") == "https://shop.example.com"

    def test_a_server_bound_url_keeps_its_port_scheme_and_path(self) -> None:
        """REGRESSION: rebuilding from the bare host dropped `:8080`.

        Coolify routes a domain written with a port to that port (from 4.3.15 by
        moving it into a hidden override on save) and everything else to the first
        exposed port — so the target silently routed to the wrong one.
        """
        assert (
            self._remap("http://admin.app.0046-20.cloud.bauer-group.com:8080/panel/")
            == "http://admin.app.0047-20.cloud.bauer-group.com:8080/panel"
        )

    def test_mixed_list_rewrites_only_the_server_bound_one(self) -> None:
        out = self._remap(
            "https://pdf-tool.app.0046-20.cloud.bauer-group.com,https://shop.example.com"
        )
        assert out == (
            "https://pdf-tool.app.0047-20.cloud.bauer-group.com,https://shop.example.com"
        )

    def test_empty_is_empty(self) -> None:
        assert self._remap(None) == ""
        assert self._remap("") == ""

    def test_duplicate_hosts_collapse(self) -> None:
        out = self._remap("https://shop.example.com,https://shop.example.com")
        assert out == "https://shop.example.com"

    def test_server_bound_url_is_dropped_when_target_has_no_wildcard(self) -> None:
        from bg_coolify_migrate.api.resources import _remap_domains

        # Carrying the source-bound host would point the target at the SOURCE;
        # dropping it lets Coolify auto-generate a working URL instead.
        out = _remap_domains(
            "https://pdf-tool.app.0046-20.cloud.bauer-group.com",
            source_wildcard=self.SRC,
            target_wildcard=None,
        )
        assert out == ""

    def test_custom_domain_survives_even_without_a_target_wildcard(self) -> None:
        from bg_coolify_migrate.api.resources import _remap_domains

        out = _remap_domains(
            "https://shop.example.com", source_wildcard=self.SRC, target_wildcard=None
        )
        assert out == "https://shop.example.com"


class TestUnrewritableServerBound:
    SRC = "app.0046-20.cloud.bauer-group.com"
    TGT = "app.0047-20.cloud.bauer-group.com"

    def _detect(self, fqdn: str | None, *, tgt: str | None):
        from bg_coolify_migrate.api.resources import _unrewritable_server_bound

        return _unrewritable_server_bound(fqdn, source_wildcard=self.SRC, target_wildcard=tgt)

    def test_flags_server_bound_url_when_target_has_no_wildcard(self) -> None:
        assert self._detect(
            "https://pdf-tool.app.0046-20.cloud.bauer-group.com", tgt=None
        ) == ["pdf-tool.app.0046-20.cloud.bauer-group.com"]

    def test_nothing_flagged_when_the_target_has_a_wildcard(self) -> None:
        # It CAN be rewritten, so it is not "unrewritable".
        assert self._detect(
            "https://pdf-tool.app.0046-20.cloud.bauer-group.com", tgt=self.TGT
        ) == []

    def test_custom_domain_is_never_flagged(self) -> None:
        assert self._detect("https://shop.example.com", tgt=None) == []


class TestEncodeBase64Fields:
    def _apply(self, body: dict) -> dict:
        from bg_coolify_migrate.api.resources import _encode_base64_fields

        _encode_base64_fields(body)
        return body

    def test_plaintext_labels_are_base64_encoded(self) -> None:
        import base64

        labels = "traefik.enable=true\ntraefik.http.routers.x.rule=Host(`a.com`)"
        out = self._apply({"custom_labels": labels})
        assert base64.b64decode(out["custom_labels"]).decode() == labels

    def test_all_three_fields_are_encoded(self) -> None:
        import base64

        out = self._apply(
            {
                "custom_labels": "a=b",
                "custom_nginx_configuration": "server {}",
                "dockerfile": "FROM alpine",
            }
        )
        assert base64.b64decode(out["custom_labels"]).decode() == "a=b"
        assert base64.b64decode(out["custom_nginx_configuration"]).decode() == "server {}"
        assert base64.b64decode(out["dockerfile"]).decode() == "FROM alpine"

    def test_empty_and_null_are_dropped_not_sent(self) -> None:
        # Coolify decodes then runs a UTF-8 check that "" fails, and has()=true for
        # a null — both 422. Neither must reach the wire.
        out = self._apply({"custom_labels": "", "dockerfile": None, "keep": "x"})
        assert "custom_labels" not in out
        assert "dockerfile" not in out
        assert out["keep"] == "x"

    def test_absent_fields_are_left_absent(self) -> None:
        assert self._apply({"name": "web"}) == {"name": "web"}


class TestComposeDomains:
    SRC = "app.0046-20.cloud.bauer-group.com"
    TGT = "app.0047-20.cloud.bauer-group.com"

    def _raw(self) -> str:
        import json

        return json.dumps(
            {
                "pairdrop": {"domain": "https://airdrop.app.0046-20.cloud.bauer-group.com"},
                "config-generator": {"domain": ""},
            }
        )

    def test_remaps_server_bound_and_converts_to_create_array(self) -> None:
        from bg_coolify_migrate.api.resources import _compose_domains_body

        out = _compose_domains_body(self._raw(), source_wildcard=self.SRC, target_wildcard=self.TGT)
        # Stored dict {svc: {domain}} -> create array [{name, domain}], remapped.
        assert out == [
            {"name": "pairdrop", "domain": "https://airdrop.app.0047-20.cloud.bauer-group.com"},
            {"name": "config-generator", "domain": ""},
        ]

    def test_custom_domain_in_a_service_is_kept(self) -> None:
        import json

        from bg_coolify_migrate.api.resources import _compose_domains_body

        raw = json.dumps({"web": {"domain": "https://shop.example.com"}})
        out = _compose_domains_body(raw, source_wildcard=self.SRC, target_wildcard=self.TGT)
        assert out == [{"name": "web", "domain": "https://shop.example.com"}]

    def test_empty_or_unparseable_is_none(self) -> None:
        from bg_coolify_migrate.api.resources import _compose_domains_body

        assert _compose_domains_body(None, source_wildcard=self.SRC, target_wildcard=self.TGT) is None
        assert _compose_domains_body("{bad", source_wildcard=self.SRC, target_wildcard=self.TGT) is None

    def test_the_create_body_leaves_the_custom_hosts_out(self) -> None:
        import json

        from bg_coolify_migrate.api.resources import _compose_domains_body

        raw = json.dumps(
            {"web": {"domain": "https://shop.example.com,https://w.app.0046-20.cloud.bauer-group.com"}}
        )
        out = _compose_domains_body(
            raw, source_wildcard=self.SRC, target_wildcard=self.TGT, with_custom=False
        )
        assert out == [{"name": "web", "domain": "https://w.app.0047-20.cloud.bauer-group.com"}]


class TestReleaseApplicationDomains:
    SRC = "app.0046-20.cloud.bauer-group.com"

    def _snapshot(self, kind: ResourceKind = ResourceKind.APP_GIT_BUILD) -> ResourceSnapshot:
        return ResourceSnapshot(
            uuid="a1", name="shop", collection="applications", kind=kind, git_auth=GitAuth.PUBLIC
        )

    async def test_only_the_custom_hosts_go(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The target holds them; the source's own server-bound host collides with
        # nothing, and removing it would let Coolify delete its record.
        import json

        from bg_coolify_migrate.api.resources import release_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "a1", "fqdn": f"https://shop.example.com,https://x.{self.SRC}"}
            )
        )
        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        ok = await release_application_domains(api, self._snapshot(), source_wildcard=self.SRC)
        assert ok is True
        assert json.loads(route.calls[0].request.read()) == {"domains": f"https://x.{self.SRC}"}

    async def test_a_custom_only_app_is_left_without_domains(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.api.resources import release_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1", "fqdn": "https://shop.example.com"})
        )
        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        await release_application_domains(api, self._snapshot(), source_wildcard=self.SRC)
        assert json.loads(route.calls[0].request.read()) == {"domains": ""}

    async def test_compose_app_goes_through_docker_compose_domains(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The `domains` field 422s for dockercompose.
        import json

        from bg_coolify_migrate.api.resources import release_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "a1",
                    "docker_compose_domains": json.dumps(
                        {
                            "web": {"domain": "https://shop.example.com"},
                            "cfg": {"domain": f"https://cfg.{self.SRC}"},
                        }
                    ),
                },
            )
        )
        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        await release_application_domains(
            api, self._snapshot(ResourceKind.APP_GIT_COMPOSE), source_wildcard=self.SRC
        )
        body = json.loads(route.calls[0].request.read())
        assert body == {
            "docker_compose_domains": [
                {"name": "web", "domain": ""},
                {"name": "cfg", "domain": f"https://cfg.{self.SRC}"},
            ]
        }

    @pytest.mark.parametrize(
        "source",
        [
            {"fqdn": "https://x.app.0046-20.cloud.bauer-group.com"},
            {"fqdn": None},
        ],
    )
    async def test_nothing_custom_writes_nothing(
        self, api: CoolifyClient, respx_mock: respx.Router, source: dict[str, object]
    ) -> None:
        from bg_coolify_migrate.api.resources import release_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1", **source})
        )
        assert await release_application_domains(api, self._snapshot(), source_wildcard=self.SRC)
        assert [c.request.method for c in respx_mock.calls] == ["GET"]

    async def test_compose_nothing_custom_writes_nothing(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.api.resources import release_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(
                200,
                json={"docker_compose_domains": json.dumps({"cfg": {"domain": f"https://c.{self.SRC}"}})},
            )
        )
        assert await release_application_domains(
            api, self._snapshot(ResourceKind.APP_GIT_COMPOSE), source_wildcard=self.SRC
        )
        assert [c.request.method for c in respx_mock.calls] == ["GET"]

    async def test_a_failure_is_reported_never_raised(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import release_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(return_value=httpx.Response(500))
        assert (
            await release_application_domains(api, self._snapshot(), source_wildcard=self.SRC)
            is False
        )

    async def test_databases_have_nothing_to_release(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import release_source_domains

        snapshot = ResourceSnapshot(
            uuid="db1", name="pg", collection="databases", kind=ResourceKind.DATABASE
        )
        assert await release_source_domains(api, snapshot, source_wildcard=None)
        assert not respx_mock.calls


class TestTargetDomainValues:
    SRC = "app.0046-20.cloud.bauer-group.com"
    TGT = "app.0047-20.cloud.bauer-group.com"

    def test_create_leaves_custom_out_and_the_claim_carries_it(self) -> None:
        from bg_coolify_migrate.api.resources import target_domain_values

        value = (
            f"https://shop.example.com:8443,https://pdf.{self.SRC},http://a.10.0.0.1.sslip.io"
        )
        create, claim = target_domain_values(
            value, source_wildcard=self.SRC, target_wildcard=self.TGT
        )
        assert create == f"https://pdf.{self.TGT}"
        assert claim == f"https://shop.example.com:8443,https://pdf.{self.TGT}"

    def test_nothing_in_nothing_out(self) -> None:
        from bg_coolify_migrate.api.resources import target_domain_values

        assert target_domain_values(None, source_wildcard=None, target_wildcard=None) == ("", "")


def _conflict(*holders: str | None) -> httpx.Response:
    return httpx.Response(
        409,
        json={
            "message": "Domain conflicts detected. Use force_domain_override=true to proceed.",
            "conflicts": [
                {
                    "domain": "shop.example.com",
                    "resource_name": "shop",
                    "resource_uuid": h,
                    "resource_type": "application",
                }
                for h in holders
            ],
        },
    )


class TestClaimApplicationDomains:
    """Phase two of the claim: the target takes the custom hosts after its gates."""

    SRC = "app.0046-20.cloud.bauer-group.com"
    TGT = "app.0047-20.cloud.bauer-group.com"

    def _snapshot(self, kind: ResourceKind = ResourceKind.APP_GIT_BUILD) -> ResourceSnapshot:
        return ResourceSnapshot(
            uuid="a1", name="shop", collection="applications", kind=kind, git_auth=GitAuth.PUBLIC
        )

    def _placement(self) -> Placement:
        return Placement(
            project_uuid="p",
            environment_name="production",
            server_uuid="s",
            source_wildcard=self.SRC,
            target_wildcard=self.TGT,
        )

    async def _claim(
        self, api: CoolifyClient, source: dict[str, object], **kw: object
    ) -> list[str]:
        from bg_coolify_migrate.api.resources import claim_application_domains

        return await claim_application_domains(
            api,
            kw.pop("snapshot", self._snapshot()),  # type: ignore[arg-type]
            self._placement(),
            source,  # type: ignore[arg-type]
            "a2",
            own=kw.pop("own", frozenset({"a1", "a2"})),  # type: ignore[arg-type]
        )

    async def test_a_conflict_with_the_source_is_forced_then_read_back(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        route = respx_mock.patch(f"{BASE}/applications/a2").mock(
            side_effect=[_conflict("a1"), httpx.Response(200, json={})]
        )
        respx_mock.get(f"{BASE}/applications/a2").mock(
            return_value=httpx.Response(
                200, json={"uuid": "a2", "fqdn": f"https://shop.example.com,https://p.{self.TGT}"}
            )
        )
        source = {"fqdn": f"https://shop.example.com,https://p.{self.SRC}"}
        assert await self._claim(api, source) == ["shop.example.com"]
        bodies = [json.loads(c.request.read()) for c in route.calls]
        assert bodies == [
            {"domains": f"https://shop.example.com,https://p.{self.TGT}"},
            {"domains": f"https://shop.example.com,https://p.{self.TGT}", "force_domain_override": True},
        ]

    async def test_no_custom_host_means_no_claim(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        assert await self._claim(api, {"fqdn": f"https://p.{self.SRC}"}) == []
        assert not respx_mock.calls

    async def test_a_foreign_holder_is_never_forced(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.patch(f"{BASE}/applications/a2").mock(
            return_value=_conflict("a1", None)
        )
        with pytest.raises(CoolifyApiError, match="the Coolify instance itself") as exc:
            await self._claim(api, {"fqdn": "https://shop.example.com"})
        assert exc.value.status_code == 409
        assert "never stopped" in (exc.value.hint or "")
        assert route.call_count == 1

    async def test_other_errors_propagate(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.patch(f"{BASE}/applications/a2").mock(return_value=httpx.Response(422))
        with pytest.raises(CoolifyApiError) as exc:
            await self._claim(api, {"fqdn": "https://shop.example.com"})
        assert exc.value.status_code == 422

    async def test_a_target_that_did_not_keep_the_host_is_refused(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.errors import UnsupportedResource

        respx_mock.patch(f"{BASE}/applications/a2").mock(return_value=httpx.Response(200, json={}))
        respx_mock.get(f"{BASE}/applications/a2").mock(
            return_value=httpx.Response(200, json={"uuid": "a2", "fqdn": None})
        )
        with pytest.raises(UnsupportedResource, match=r"does not hold \['shop\.example\.com'\]"):
            await self._claim(api, {"fqdn": "https://shop.example.com"})

    async def test_a_compose_app_claims_through_docker_compose_domains(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The update honours the force at every tag; a forced compose CREATE is
        # ignored before 4.5 (the strip runs before the check).
        import json

        raw = json.dumps(
            {
                "globaleaks": {"domain": "https://speakup.bauer-group.com"},
                "config": {"domain": f"https://cfg.{self.SRC}"},
                "admin": {"domain": "https://admin.bauer-group.com"},
            }
        )
        route = respx_mock.patch(f"{BASE}/applications/a2").mock(
            return_value=httpx.Response(200, json={})
        )
        respx_mock.get(f"{BASE}/applications/a2").mock(
            return_value=httpx.Response(
                200,
                json={
                    "docker_compose_domains": json.dumps(
                        {
                            "globaleaks": {"domain": "https://speakup.bauer-group.com"},
                            "admin": {"domain": "https://admin.bauer-group.com"},
                        }
                    )
                },
            )
        )
        hosts = await self._claim(
            api,
            {"docker_compose_domains": raw},
            snapshot=self._snapshot(ResourceKind.APP_GIT_COMPOSE),
        )
        assert hosts == ["speakup.bauer-group.com", "admin.bauer-group.com"]
        assert json.loads(route.calls[0].request.read()) == {
            "docker_compose_domains": [
                {"name": "globaleaks", "domain": "https://speakup.bauer-group.com"},
                {"name": "config", "domain": f"https://cfg.{self.TGT}"},
                {"name": "admin", "domain": "https://admin.bauer-group.com"},
            ]
        }


class TestRestoreApplicationDomains:
    async def test_patches_only_a_difference_and_passes_force_on(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.api.resources import restore_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1", "fqdn": None})
        )
        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={})
        )
        body = {"domains": "https://shop.example.com", "force_domain_override": True}
        assert await restore_application_domains(api, "a1", body) is True
        assert json.loads(route.calls[0].request.read()) == body

    async def test_a_source_that_holds_them_is_not_written(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import restore_application_domains

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1", "fqdn": "https://shop.example.com"})
        )
        assert (
            await restore_application_domains(api, "a1", {"domains": "https://shop.example.com"})
            is False
        )
        assert [c.request.method for c in respx_mock.calls] == ["GET"]


class TestCopyEnvs:
    async def test_bulk_upsert_overwrites_generated_secrets(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Coolify GENERATES SERVICE_* passwords for a new service. The bulk
        # upsert matches by key and overwrites them with the source's - essential,
        # because the mirrored data belongs to the source's credentials.
        respx_mock.get(f"{BASE}/services/s1/envs").mock(
            return_value=httpx.Response(
                200, json=[{"key": "SERVICE_PASSWORD_MINIO", "value": "original-secret"}]
            )
        )
        route = respx_mock.patch(f"{BASE}/services/s2/envs/bulk").mock(
            return_value=httpx.Response(200, json={})
        )
        count = await copy_envs(api, collection="services", source_uuid="s1", target_uuid="s2")
        assert count == 1
        assert "original-secret" in route.calls[0].request.read().decode()

    async def test_no_envs_is_not_an_error(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/services/s1/envs").mock(return_value=httpx.Response(200, json=[]))
        assert await copy_envs(api, collection="services", source_uuid="s1", target_uuid="s2") == 0


class TestCopyStorages:
    @pytest.mark.parametrize(
        "kind",
        [
            ResourceKind.DATABASE,
            ResourceKind.APP_GIT_COMPOSE,
            ResourceKind.SERVICE_COMPOSE,
            ResourceKind.SERVICE_TEMPLATE,
        ],
    )
    async def test_skips_kinds_whose_volumes_coolify_materialises(
        self, api: CoolifyClient, kind: ResourceKind
    ) -> None:
        # DB volumes come from the model hook; compose volumes from the parser,
        # and shouldBeReadOnlyInUI makes them un-POSTable anyway. Posting them
        # would 422 at best and double-create at worst.
        count = await copy_storages(
            api, collection="databases", source_uuid="a", target_uuid="b", kind=kind
        )
        assert count == 0

    async def test_copies_user_defined_application_storages(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/applications/a1/storages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "persistent_storages": [{"name": "a1-data", "mount_path": "/app/storage"}],
                    "file_storages": [],
                },
            )
        )
        route = respx_mock.post(f"{BASE}/applications/a2/storages").mock(
            return_value=httpx.Response(201, json={})
        )
        count = await copy_storages(
            api,
            collection="applications",
            source_uuid="a1",
            target_uuid="a2",
            kind=ResourceKind.APP_GIT_BUILD,
        )
        assert count == 1
        body = route.calls[0].request.read().decode()
        # The bare name: upstream re-prefixes with the NEW uuid itself.
        assert '"name":"data"' in body

    async def test_skips_file_storages_the_api_cannot_round_trip(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # content caps at 5 MiB and comes back as a placeholder; the manifest
        # already warned and the path is rsynced instead.
        respx_mock.get(f"{BASE}/applications/a1/storages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "persistent_storages": [],
                    "file_storages": [
                        {"mount_path": "/app/blob.bin", "content": "[binary file]"},
                        {"mount_path": "/app/big.log", "content": "[file too large to display]"},
                    ],
                },
            )
        )
        count = await copy_storages(
            api,
            collection="applications",
            source_uuid="a1",
            target_uuid="a2",
            kind=ResourceKind.APP_GIT_BUILD,
        )
        assert count == 0

    async def test_a_blank_host_path_is_not_sent(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """A cleared form stores '' — and v4.3.22+ 422s the key whatever its value."""
        respx_mock.get(f"{BASE}/applications/a1/storages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "persistent_storages": [
                        {"name": "a1-data", "mount_path": "/app/storage", "host_path": ""}
                    ],
                    "file_storages": [],
                },
            )
        )
        route = respx_mock.post(f"{BASE}/applications/a2/storages").mock(
            return_value=httpx.Response(201, json={})
        )
        await copy_storages(
            api, collection="applications", source_uuid="a1", target_uuid="a2", kind=ResourceKind.APP_GIT_BUILD
        )
        assert "host_path" not in json.loads(route.calls[0].request.read().decode())

    async def test_a_host_file_mount_keeps_its_flag_and_sends_no_content(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        """Without the flag a 4.4+ target treats /etc/timezone as managed content.

        It then 422s on a symlink, or touches and chmods the target host's file.
        """
        respx_mock.get(f"{BASE}/applications/a1/storages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "persistent_storages": [],
                    "file_storages": [
                        {
                            "mount_path": "/etc/timezone",
                            "fs_path": "/etc/timezone",
                            "content": "",
                            "is_directory": False,
                            "is_host_file": True,
                        }
                    ],
                },
            )
        )
        route = respx_mock.post(f"{BASE}/applications/a2/storages").mock(
            return_value=httpx.Response(201, json={})
        )
        await copy_storages(
            api, collection="applications", source_uuid="a1", target_uuid="a2", kind=ResourceKind.APP_GIT_BUILD
        )
        body = json.loads(route.calls[0].request.read().decode())
        assert body["is_host_file"] is True
        assert body["fs_path"] == "/etc/timezone"
        # "Content is not valid for host file mounts." — even an empty one.
        assert "content" not in body

    async def test_a_managed_file_moves_into_the_targets_own_directory(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # 4.4+ honours an absolute fs_path verbatim; unchanged, the target's file
        # would live under the SOURCE uuid on the target host, forever.
        respx_mock.get(f"{BASE}/applications/a1/storages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "persistent_storages": [],
                    "file_storages": [
                        {
                            "mount_path": "/app/config.yml",
                            "fs_path": "/data/coolify/applications/a1/app/config.yml",
                            "content": "debug: false",
                            "is_directory": False,
                            "is_host_file": False,
                        }
                    ],
                },
            )
        )
        route = respx_mock.post(f"{BASE}/applications/a2/storages").mock(
            return_value=httpx.Response(201, json={})
        )
        await copy_storages(
            api, collection="applications", source_uuid="a1", target_uuid="a2", kind=ResourceKind.APP_GIT_BUILD
        )
        body = json.loads(route.calls[0].request.read().decode())
        assert body["fs_path"] == "/data/coolify/applications/a2/app/config.yml"
        assert body["content"] == "debug: false"


class TestRetargetFsPath:
    def test_rewrites_the_resource_directory(self) -> None:
        assert (
            retarget_fs_path(
                "/data/coolify/applications/a1/x/y.conf",
                kind=ResourceKind.APP_GIT_BUILD,
                source_uuid="a1",
                target_uuid="a2",
            )
            == "/data/coolify/applications/a2/x/y.conf"
        )

    def test_a_genuine_host_path_passes_through(self) -> None:
        assert (
            retarget_fs_path(
                "/srv/shared/config", kind=ResourceKind.APP_GIT_BUILD, source_uuid="a1", target_uuid="a2"
            )
            == "/srv/shared/config"
        )

    def test_a_sibling_whose_uuid_merely_starts_the_same_is_left_alone(self) -> None:
        # Prefix matching on the bare uuid would rewrite another resource's path.
        path = "/data/coolify/applications/a1b2/conf"
        assert (
            retarget_fs_path(path, kind=ResourceKind.APP_GIT_BUILD, source_uuid="a1", target_uuid="a2")
            == path
        )

    def test_absent_stays_absent(self) -> None:
        assert (
            retarget_fs_path(None, kind=ResourceKind.APP_GIT_BUILD, source_uuid="a1", target_uuid="a2")
            is None
        )


class TestReadVolumeEndpoints:
    async def test_reads_back_what_coolify_created(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # THE authority for target names. We never predict them.
        respx_mock.get(f"{BASE}/databases/db2/storages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "persistent_storages": [
                        {"name": "postgres-data-db2", "mount_path": "/var/lib/postgresql/data"}
                    ],
                    "file_storages": [],
                },
            )
        )
        endpoints = await read_volume_endpoints(api, collection="databases", uuid="db2")
        assert endpoints[0].name == "postgres-data-db2"


class TestMultiServiceDomains:
    """A compose app whose services each carry a different URL — remap and park
    must treat every service independently."""

    SRC = "app.0046-20.cloud.bauer-group.com"
    TGT = "app.0047-20.cloud.bauer-group.com"

    def _raw(self) -> str:
        import json

        return json.dumps(
            {
                "globaleaks": {"domain": "https://speakup.bauer-group.com"},  # custom
                "config": {"domain": "https://cfg.app.0046-20.cloud.bauer-group.com"},  # bound
                "admin": {"domain": "https://admin.bauer-group.com"},  # custom
            }
        )

    def test_remap_keeps_custom_and_rewrites_bound_per_service(self) -> None:
        from bg_coolify_migrate.api.resources import _compose_domains_body

        out = _compose_domains_body(self._raw(), source_wildcard=self.SRC, target_wildcard=self.TGT)
        assert out == [
            {"name": "globaleaks", "domain": "https://speakup.bauer-group.com"},
            {"name": "config", "domain": "https://cfg.app.0047-20.cloud.bauer-group.com"},
            {"name": "admin", "domain": "https://admin.bauer-group.com"},
        ]
