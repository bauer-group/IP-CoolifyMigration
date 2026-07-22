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

    async def test_carries_a_custom_domain_to_the_target(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # A custom domain is server-independent: it moves with the app and is set
        # on the target verbatim (the DNS gate reasons about its cutover).
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
        assert body["domains"] == "https://shop.example.com"

    async def test_without_wildcards_a_server_bound_url_is_left_intact(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # No wildcard configured on either side => nothing to rewrite onto; the
        # host is carried unchanged rather than dropped or corrupted.
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
        assert body["domains"] == "https://shop.example.com"

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
        # Both custom URLs carried, the empty service preserved as blank.
        assert body["docker_compose_domains"] == [
            {"name": "minio-server", "domain": "https://assets.bauer-group.com"},
            {"name": "admin-console", "domain": "https://console.assets.bauer-group.com"},
            {"name": "minio-init", "domain": ""},
        ]
        # Domains were sent, so we must NOT suppress auto-generation.
        assert "autogenerate_domain" not in body

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
                "build_pack": "nixpacks", "fqdn": "https://shop.example.com",
            },
        )
        body = json.loads(route.calls[0].request.read())
        assert body["domains"] == "https://shop.example.com"
        assert "autogenerate_domain" not in body  # a domain is set; leave the default

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

    def test_blank_empties_every_service_domain(self) -> None:
        from bg_coolify_migrate.api.resources import _blank_compose_domains

        assert _blank_compose_domains(self._raw()) == [
            {"name": "pairdrop", "domain": ""},
            {"name": "config-generator", "domain": ""},
        ]


class TestReleaseFqdn:
    async def test_plain_app_clears_the_domains_field(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.api.resources import release_fqdn

        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        await release_fqdn(api, "applications", "a1", kind=ResourceKind.APP_GIT_BUILD)
        assert json.loads(route.calls[0].request.read()) == {"domains": ""}

    async def test_compose_app_blanks_docker_compose_domains_not_domains(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # The `domains` field 422s for dockercompose; release must go through
        # docker_compose_domains with blanked entries instead.
        import json

        from bg_coolify_migrate.api.resources import release_fqdn

        respx_mock.get(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "a1",
                    "docker_compose_domains": json.dumps(
                        {"pairdrop": {"domain": "https://airdrop.app.0046-20.cloud.bauer-group.com"}}
                    ),
                },
            )
        )
        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        await release_fqdn(api, "applications", "a1", kind=ResourceKind.APP_GIT_COMPOSE)
        body = json.loads(route.calls[0].request.read())
        assert body == {"docker_compose_domains": [{"name": "pairdrop", "domain": ""}]}
        assert "domains" not in body

    async def test_non_application_is_a_noop(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        # Databases/services have no fqdn to release this way.
        from bg_coolify_migrate.api.resources import release_fqdn

        await release_fqdn(api, "databases", "db1")
        assert not respx_mock.calls


class TestParkHosts:
    SRC = "app.0046-20.cloud.bauer-group.com"

    def _park(self, domains: str) -> str:
        from bg_coolify_migrate.api.resources import _park_hosts

        return _park_hosts(domains, source_wildcard=self.SRC, tag="t1")

    def test_custom_domain_is_marked_and_freed(self) -> None:
        assert (
            self._park("https://speakup.bauer-group.com")
            == "https://old-t1.speakup.bauer-group.com"
        )

    def test_server_bound_domain_is_left_alone(self) -> None:
        host = "https://x.app.0046-20.cloud.bauer-group.com"
        assert self._park(host) == host

    def test_only_the_custom_host_in_a_mixed_list_is_parked(self) -> None:
        out = self._park(
            "https://speakup.bauer-group.com,https://x.app.0046-20.cloud.bauer-group.com"
        )
        assert out == (
            "https://old-t1.speakup.bauer-group.com,https://x.app.0046-20.cloud.bauer-group.com"
        )


class TestParkSourceDomains:
    SRC = "app.0046-20.cloud.bauer-group.com"

    async def test_compose_parks_custom_domain_and_returns_restore_body(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.api.resources import park_source_domains

        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="wb",
            collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_auth=GitAuth.PUBLIC,
        )
        source = {
            "docker_compose_domains": json.dumps(
                {"globaleaks": {"domain": "https://speakup.bauer-group.com"}}
            )
        }
        restore = await park_source_domains(
            api, snapshot, source, source_wildcard=self.SRC, tag="t1"
        )
        # The source is PATCHed with the parked (freed) domain...
        assert json.loads(route.calls[0].request.read()) == {
            "docker_compose_domains": [
                {"name": "globaleaks", "domain": "https://old-t1.speakup.bauer-group.com"}
            ]
        }
        # ...and the restore body carries the original for rollback.
        assert restore == {
            "docker_compose_domains": [
                {"name": "globaleaks", "domain": "https://speakup.bauer-group.com"}
            ]
        }

    async def test_only_server_bound_makes_no_patch_but_still_records_restore(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.api.resources import park_source_domains

        snapshot = ResourceSnapshot(
            uuid="a1",
            name="wb",
            collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_auth=GitAuth.PUBLIC,
        )
        # Only a server-bound domain: remapped on the target, never collides, so
        # nothing is parked — but the original is still recorded so a rollback can
        # swing the URL back (finalize blanks it on success).
        source = {
            "docker_compose_domains": json.dumps(
                {"web": {"domain": "https://x.app.0046-20.cloud.bauer-group.com"}}
            )
        }
        restore = await park_source_domains(
            api, snapshot, source, source_wildcard=self.SRC, tag="t1"
        )
        assert restore == {
            "docker_compose_domains": [
                {"name": "web", "domain": "https://x.app.0046-20.cloud.bauer-group.com"}
            ]
        }
        assert not respx_mock.calls  # no PATCH: nothing was parked

    async def test_no_domains_at_all_returns_none(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        from bg_coolify_migrate.api.resources import park_source_domains

        snapshot = ResourceSnapshot(
            uuid="a1",
            name="web",
            collection="applications",
            kind=ResourceKind.APP_GIT_BUILD,
            git_auth=GitAuth.PUBLIC,
        )
        assert (
            await park_source_domains(api, snapshot, {}, source_wildcard=self.SRC, tag="t1")
            is None
        )
        assert not respx_mock.calls

    async def test_regular_app_parks_its_fqdn_via_domains(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.api.resources import park_source_domains

        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="web",
            collection="applications",
            kind=ResourceKind.APP_GIT_BUILD,
            git_auth=GitAuth.PUBLIC,
        )
        restore = await park_source_domains(
            api, snapshot, {"fqdn": "https://speakup.bauer-group.com"},
            source_wildcard=self.SRC, tag="t1",
        )
        assert json.loads(route.calls[0].request.read()) == {
            "domains": "https://old-t1.speakup.bauer-group.com"
        }
        assert restore == {"domains": "https://speakup.bauer-group.com"}


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

    async def test_park_frees_every_custom_service_and_records_full_original(
        self, api: CoolifyClient, respx_mock: respx.Router
    ) -> None:
        import json

        from bg_coolify_migrate.api.resources import park_source_domains

        route = respx_mock.patch(f"{BASE}/applications/a1").mock(
            return_value=httpx.Response(200, json={"uuid": "a1"})
        )
        snapshot = ResourceSnapshot(
            uuid="a1",
            name="wb",
            collection="applications",
            kind=ResourceKind.APP_GIT_COMPOSE,
            git_auth=GitAuth.PUBLIC,
        )
        restore = await park_source_domains(
            api, snapshot, {"docker_compose_domains": self._raw()},
            source_wildcard=self.SRC, tag="t1",
        )
        # Both custom services are parked; the bound one is left alone.
        assert json.loads(route.calls[0].request.read()) == {
            "docker_compose_domains": [
                {"name": "globaleaks", "domain": "https://old-t1.speakup.bauer-group.com"},
                {"name": "config", "domain": "https://cfg.app.0046-20.cloud.bauer-group.com"},
                {"name": "admin", "domain": "https://old-t1.admin.bauer-group.com"},
            ]
        }
        # The restore body carries every service's ORIGINAL domain.
        assert restore == {
            "docker_compose_domains": [
                {"name": "globaleaks", "domain": "https://speakup.bauer-group.com"},
                {"name": "config", "domain": "https://cfg.app.0046-20.cloud.bauer-group.com"},
                {"name": "admin", "domain": "https://admin.bauer-group.com"},
            ]
        }
