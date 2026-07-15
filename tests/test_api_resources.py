"""Tests for resource re-creation.

The env-copying tests encode a decision that is easy to get backwards and
impossible to notice afterwards: copy `value`, not `real_value`.
"""

from __future__ import annotations

import base64

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
    create_resource,
    ensure_project,
    read_volume_endpoints,
    resolve_destination,
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
    async def test_single_destination_needs_no_choice(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/srv1").mock(
            return_value=httpx.Response(200, json={"uuid": "srv1", "destinations": [{"uuid": "d1"}]})
        )
        assert await resolve_destination(api, "srv1") is None

    async def test_multiple_destinations_prefers_the_coolify_network(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/srv1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "srv1",
                    "destinations": [
                        {"uuid": "d1", "network": "other"},
                        {"uuid": "d2", "network": "coolify"},
                    ],
                },
            )
        )
        assert await resolve_destination(api, "srv1") == "d2"

    async def test_ambiguous_destinations_refuse_rather_than_guess(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        respx_mock.get(f"{BASE}/servers/srv1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "srv1",
                    "destinations": [
                        {"uuid": "d1", "network": "a"},
                        {"uuid": "d2", "network": "b"},
                    ],
                },
            )
        )
        with pytest.raises(UnsupportedResource, match="will not guess"):
            await resolve_destination(api, "srv1")


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


class TestCreateApplication:
    async def test_never_carries_the_fqdn_over(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # coolify-mover copies fqdn verbatim, producing two resources claiming
        # the same hostname. The DNS gate and finalize policy own domains here.
        route = respx_mock.post(f"{BASE}/applications/public").mock(
            return_value=httpx.Response(201, json={"uuid": "a2"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="web",
            collection="applications",
            kind=ResourceKind.APP_GIT_BUILD,
            git_repository="https://github.com/x/y",
            git_branch="main",
            git_auth=GitAuth.PUBLIC,
        )
        await create_resource(
            api,
            snapshot,
            _placement(),
            {
                "git_repository": "https://github.com/x/y",
                "git_branch": "main",
                "build_pack": "nixpacks",
                "fqdn": "https://shop.example.com",
                "domains": "https://shop.example.com",
            },
        )
        body = route.calls[0].request.read().decode()
        assert "shop.example.com" not in body

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
