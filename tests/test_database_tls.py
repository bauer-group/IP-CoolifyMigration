"""Standalone database TLS: what the target cannot share, and what it cannot run.

enable_ssl / ssl_mode come back in every database GET and are accepted by no
create or update array and no route at any tag v4.1.2-v4.5.0, so a target always
starts plaintext. That is a WARNING (whether the clients need TLS is the
operator's call), split in two so that carrying TLS some day cannot silence the
certificate-identity half. A carried engine config that itself turns TLS on is a
REFUSAL: the target gets the config but not the certificate files, and fails
after the source is stopped.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.api.fields import DATABASE_ENGINE_FIELDS, database_allowed
from bg_coolify_migrate.api.resources import Placement, create_resource
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from tests.test_api_fields import released_controllers
from tests.test_engine_planner import _plan_one_app

E = DatabaseEngine

_ON = {"enable_ssl": True}


def _lost(source: dict[str, Any], engine: DatabaseEngine | None) -> str:
    line = fidelity.database_tls_lost_warning(source, engine=engine, carried=False)
    assert line is not None
    return line


def _identity(source: dict[str, Any], engine: DatabaseEngine | None) -> str:
    line = fidelity.database_tls_identity_warning(source, engine=engine)
    assert line is not None
    return line


# ── the warnings ─────────────────────────────────────────────────────────────


class TestTlsWarnings:
    @pytest.mark.parametrize("engine", [*E, None])
    @pytest.mark.parametrize("value", [False, 0, "0", "f", None, "absent"])
    def test_silent_when_tls_is_off(self, engine: DatabaseEngine | None, value: object) -> None:
        """A stock database is silent, and the reverse flip is never claimed."""
        source: dict[str, Any] = {} if value == "absent" else {"enable_ssl": value}
        assert fidelity.database_tls_lost_warning(source, engine=engine, carried=False) is None
        assert fidelity.database_tls_identity_warning(source, engine=engine) is None
        assert fidelity.database_warnings(source, engine=engine) == []

    @pytest.mark.parametrize("value", [True, 1, "1", "t", "true"])
    def test_string_forms_count_as_on(self, value: object) -> None:
        # No model casts enable_ssl, so the wire type is not guaranteed.
        assert "WITHOUT TLS" in _lost({"enable_ssl": value}, E.POSTGRESQL)

    def test_postgres_names_mode_client_effect_and_hostssl(self) -> None:
        source = {**_ON, "ssl_mode": "verify-full"}
        lost = _lost(source, E.POSTGRESQL)
        assert "ssl_mode='verify-full'" in lost
        assert "WITHOUT TLS" in lost
        assert "sslmode=require" in lost
        assert "hostssl" in lost
        assert "EVERY TCP client" in lost
        assert "TARGET server's CA" in _identity(source, E.POSTGRESQL)

    def test_the_remedy_toggles_the_stopped_target_before_its_first_start(self) -> None:
        lost = _lost({**_ON, "ssl_mode": "require"}, E.POSTGRESQL)
        assert "created stopped before the source is quiesced" in lost
        assert "first start already serves TLS" in lost
        assert "(same mode, 'require')" in lost

    def test_mysql_mentions_require_secure_transport(self) -> None:
        lost = _lost({**_ON, "ssl_mode": "REQUIRED"}, E.MYSQL)
        assert "require_secure_transport" in lost
        assert "VERIFY_CA" in lost
        assert "ssl_mode='REQUIRED'" in lost

    def test_mariadb_refuses_plaintext_like_mysql_but_has_no_mode(self) -> None:
        # StartMariadb v4.5.0:186-193 passes --require-secure-transport=1 too.
        lost = _lost({**_ON, "ssl_mode": "require"}, E.MARIADB)
        assert "require_secure_transport" in lost
        assert "ssl_mode=" not in lost, "mariadb has no ssl_mode column"
        assert "same mode" not in lost

    def test_mongo_explains_the_legacy_default_and_client_certs(self) -> None:
        require = {**_ON, "ssl_mode": "require"}
        assert "beta.399" in _lost(require, E.MONGODB)
        assert "client certificates" not in _lost(require, E.MONGODB)
        assert "client certificates" not in _identity(require, E.MONGODB)

        full = {**_ON, "ssl_mode": "verify-full"}
        assert "beta.399" in _lost(full, E.MONGODB)
        assert "client certificates" in _identity(full, E.MONGODB)

    @pytest.mark.parametrize("engine", [E.REDIS, E.KEYDB])
    def test_redis_and_keydb_mention_port_6380(self, engine: DatabaseEngine) -> None:
        lost = _lost({**_ON, "ssl_mode": "require"}, engine)
        assert "6380" in lost
        assert "ssl_mode=" not in lost, "these engines have no ssl_mode column"
        assert "same mode" not in lost

    def test_dragonfly_names_port_6379_tls_only(self) -> None:
        # StartDragonfly v4.5.0:204-213: --tls on the main port, no TLS port.
        lost = _lost(_ON, E.DRAGONFLY)
        assert "main port 6379" in lost
        assert "6380" not in lost

    @pytest.mark.parametrize("engine", [E.CLICKHOUSE, None])
    def test_an_engine_without_a_sentence_still_warns(self, engine: DatabaseEngine | None) -> None:
        assert "Clients that require TLS fail" in _lost(_ON, engine)

    def test_an_odd_mode_is_not_echoed(self) -> None:
        lost = _lost({**_ON, "ssl_mode": "require'; DROP"}, E.POSTGRESQL)
        assert "DROP" not in lost

    def test_carrying_tls_silences_the_loss_but_not_the_identity(self) -> None:
        """Carried TLS still gets a new CA, uuid and IP; that half must survive."""
        source = {**_ON, "ssl_mode": "verify-full"}
        assert fidelity.database_tls_lost_warning(source, engine=E.POSTGRESQL, carried=True) is None
        carried = fidelity.database_warnings(source, engine=E.POSTGRESQL, tls_carried=True)
        assert len(carried) == 1
        assert "TARGET server's CA" in carried[0]

    @pytest.mark.parametrize("engine", list(E))
    def test_never_echoes_a_secret(self, engine: DatabaseEngine) -> None:
        """The plan is persisted without the journal's guard; this test IS the guard."""
        pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
        secrets = {
            field: f"s3cret-{field}"
            for fields in DATABASE_ENGINE_FIELDS.values()
            for field in fields
            if "password" in field
        }
        source: dict[str, Any] = {
            **_ON,
            "ssl_mode": "verify-full",
            **secrets,
            "file_storages": [{"mount_path": "/etc/ssl/server.key", "content": pem}],
            "content": pem,
        }
        lines = fidelity.database_warnings(source, engine=engine)
        assert lines
        for line in lines:
            assert "s3cret" not in line
            assert "PRIVATE KEY" not in line
            assert "MIIEvQ" not in line

    def test_database_warnings_combines_with_default_variables(self) -> None:
        source = {"include_coolify_default_variables": False, "enable_ssl": True}
        assert len(fidelity.database_warnings(source, engine=E.POSTGRESQL)) == 3
        assert len(fidelity.database_warnings({"include_coolify_default_variables": False})) == 1


# ── the refusal: a carried config that enables TLS itself ────────────────────


class TestTlsConfigRefusals:
    @pytest.mark.parametrize(
        ("engine", "field", "config", "directive"),
        [
            (E.POSTGRESQL, "postgres_conf", "max_connections = 50\nssl = on\n", "ssl"),
            (E.POSTGRESQL, "postgres_conf", "ssl='true'", "ssl"),
            (E.MYSQL, "mysql_conf", "[mysqld]\nssl-cert=/etc/mysql/certs/server.crt\n", "ssl-cert"),
            (
                E.MYSQL,
                "mysql_conf",
                "[mysqld]\nrequire_secure_transport = ON\n",
                "require_secure_transport",
            ),
            (E.MARIADB, "mariadb_conf", "[mysqld]\nssl_key = /x/server.key\n", "ssl-key"),
            (
                E.MARIADB,
                "mariadb_conf",
                "[mariadb]\nrequire-secure-transport\n",
                "require_secure_transport",
            ),
            (E.MONGODB, "mongo_conf", "net:\n  tls:\n    mode: requireTLS\n", "net.tls.mode"),
            (
                E.MONGODB,
                "mongo_conf",
                "net:\n  tls:\n    certificateKeyFile: /x.pem\n",
                "certificateKeyFile",
            ),
            (E.REDIS, "redis_conf", "maxmemory 1gb\ntls-port 6380\n", "tls-port"),
            (E.KEYDB, "keydb_conf", "tls-cert-file /x/server.crt\n", "tls-cert-file"),
            # Each engine's own grammar, not just the canonical spelling: the
            # '=' is optional in postgresql.conf and booleans take prefixes,
            # Redis lowercases directive names, my.cnf has 'loose-', and
            # mongod.conf is YAML (flow style).
            (E.POSTGRESQL, "postgres_conf", "ssl on\n", "ssl"),
            (E.POSTGRESQL, "postgres_conf", "ssl 'on'\n", "ssl"),
            (E.POSTGRESQL, "postgres_conf", "ssl = t\n", "ssl"),
            (E.POSTGRESQL, "postgres_conf", "ssl = ye  # yes\n", "ssl"),
            (E.REDIS, "redis_conf", "TLS-PORT 6380\n", "tls-port"),
            (E.KEYDB, "keydb_conf", "Tls-Cert-File /x\n", "tls-cert-file"),
            (E.MYSQL, "mysql_conf", "[mysqld]\nloose-ssl-cert=/x\n", "ssl-cert"),
            (
                E.MONGODB,
                "mongo_conf",
                "net: {tls: {mode: requireTLS, certificateKeyFile: /x.pem}}\n",
                "net.tls.mode",
            ),
        ],
    )
    def test_refuses_and_names_the_directive(
        self, engine: DatabaseEngine, field: str, config: str, directive: str
    ) -> None:
        out = fidelity.database_tls_config_refusals({**_ON, field: config}, engine=engine)
        assert len(out) == 1
        assert f"{field} sets {directive}" in out[0]
        assert "fail to start" in out[0]

    def test_silent_when_tls_is_off(self) -> None:
        """A config enabling TLS against files in its own data volume is copied with it."""
        source = {"enable_ssl": False, "postgres_conf": "ssl = on\n"}
        assert fidelity.database_tls_config_refusals(source, engine=E.POSTGRESQL) == []

    @pytest.mark.parametrize(
        ("engine", "field", "config"),
        [
            (E.POSTGRESQL, "postgres_conf", "# ssl = on\nssl = off\n"),
            # 'o' is ambiguous between on and off; postgres rejects it.
            (E.POSTGRESQL, "postgres_conf", "ssl o\nssl_ca_file = '/x'\nssl f\n"),
            (E.MONGODB, "mongo_conf", "# certificateKeyFile: /x.pem\n"),
            (E.MYSQL, "mysql_conf", "[mysqld]\n# ssl-cert=/x\nrequire_secure_transport=OFF\n"),
            (E.REDIS, "redis_conf", "# tls-port 6380\ntls-port 0\n"),
            (E.MONGODB, "mongo_conf", "net:\n  tls:\n    mode: disabled\n"),
            (E.POSTGRESQL, "postgres_conf", ""),
            (E.POSTGRESQL, "postgres_conf", None),
        ],
    )
    def test_comments_and_explicit_off_are_silent(
        self, engine: DatabaseEngine, field: str, config: str | None
    ) -> None:
        assert fidelity.database_tls_config_refusals({**_ON, field: config}, engine=engine) == []

    @pytest.mark.parametrize("engine", [E.DRAGONFLY, E.CLICKHOUSE, None])
    def test_engines_without_a_config_field_are_silent(self, engine: DatabaseEngine | None) -> None:
        assert (
            fidelity.database_tls_config_refusals(
                {**_ON, "redis_conf": "tls-port 6380"}, engine=engine
            )
            == []
        )

    def test_reports_key_names_never_values(self) -> None:
        config = "requirepass hunter2-secret\ntls-port 6380\ntls-key-file /secret/path.key\n"
        out = fidelity.database_tls_config_refusals({**_ON, "redis_conf": config}, engine=E.REDIS)
        assert "tls-port, tls-key-file" in out[0]
        assert "hunter2" not in out[0]
        assert "/secret/path.key" not in out[0]


# ── the wiring, the create body and the whitelist ────────────────────────────


class TestPlannerWiring:
    async def test_a_tls_database_warns_and_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_one_app(
            monkeypatch,
            {"enable_ssl": True, "ssl_mode": "require"},
            snapshot={
                "collection": "databases",
                "kind": ResourceKind.DATABASE,
                "engine": DatabaseEngine.POSTGRESQL,
            },
        )
        assert not plan.is_blocked
        warnings = plan.resources[0].warnings
        assert any("WITHOUT TLS" in w and "sslmode=require" in w for w in warnings), (
            "the engine did not reach the rule"
        )
        assert any("TARGET server's CA" in w for w in warnings)
        assert any(w.startswith("api: TLS is on") for w in plan.warnings)

    async def test_a_tls_enabling_config_blocks_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_one_app(
            monkeypatch,
            {"enable_ssl": True, "postgres_conf": "ssl = on"},
            snapshot={
                "collection": "databases",
                "kind": ResourceKind.DATABASE,
                "engine": DatabaseEngine.POSTGRESQL,
            },
        )
        assert plan.is_blocked
        assert "postgres_conf sets ssl" in plan.resources[0].blocking_reasons[0]


HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    client = CoolifyClient(HOST, "tok", max_retries=0)
    yield client
    await client.aclose()


async def test_tls_fields_are_never_sent(api: CoolifyClient, respx_mock: respx.Router) -> None:
    """The obvious 'fix' — round-trip them — would 422 every create at every tag."""
    route = respx_mock.post(f"{BASE}/databases/postgresql").mock(
        return_value=httpx.Response(201, json={"uuid": "db2"})
    )
    snapshot = ResourceSnapshot(
        uuid="db1",
        name="postgres",
        collection="databases",
        kind=ResourceKind.DATABASE,
        engine=DatabaseEngine.POSTGRESQL,
        image="postgres:16",
    )
    await create_resource(
        api,
        snapshot,
        Placement(project_uuid="proj1", environment_name="production", server_uuid="srv2"),
        {"enable_ssl": True, "ssl_mode": "require"},
    )
    body = route.calls[0].request.read().decode()
    assert "enable_ssl" not in body
    assert "ssl_mode" not in body


@pytest.mark.parametrize("engine", sorted(DATABASE_ENGINE_FIELDS))
def test_tls_fields_are_not_whitelisted_on_any_engine(engine: str) -> None:
    assert fidelity.DATABASE_TLS_FIELDS.isdisjoint(database_allowed(engine))


# ── the canary ───────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_database_tls_is_still_unsettable() -> None:
    """Fails the day a released Coolify accepts enable_ssl/ssl_mode anywhere.

    A substring search, not the $allowedFields parser: DatabasesController already
    widens its update array with `array_merge($allowedFields, [...])` (v4.5.0:893),
    which that regex never sees. Routes are read too, for a dedicated SSL route.
    """
    tag, sources = await released_controllers()
    php = sources["databases"]
    # Positive control: an empty or truncated download must not pass.
    assert "'postgres_password'" in php, f"DatabasesController at {tag} looks empty"
    accepted = sorted(f for f in fidelity.DATABASE_TLS_FIELDS if f"'{f}'" in php or f'"{f}"' in php)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://raw.githubusercontent.com/coollabsio/coolify/{tag}/routes/api.php"
        )
        resp.raise_for_status()
    paths = re.findall(r"Route::\w+\(\s*'([^']*)'", resp.text)
    assert paths, f"found no routes in routes/api.php at {tag}"
    ssl_routes = sorted(p for p in paths if "ssl" in p.lower())

    assert not accepted and not ssl_routes, (
        f"Coolify {tag} now accepts {accepted or ssl_routes} — carry them (create field "
        "or follow-up PATCH, version-gated) and retire fidelity.database_tls_lost_warning."
    )
