"""Every database engine, migrated for real, with a data round trip.

The migration is engine-agnostic by design — a cleanly stopped stack makes a
volume just bytes, and we copy the bytes. But "by design" is a claim, and this
suite is where claims meet the product. Each engine stores its data differently
(mysql bakes auth into the data dir, mongo uses two volumes, redis is in-memory
with a persistence config, clickhouse is columnar), and "the target boots from
the copied volume" is only obviously true once you have watched it happen.

For each engine: deploy on server-a, write rows whose text carries umlauts and an
eszett, migrate to server-b, and read the rows back FROM server-b's daemon. The
fingerprint carries the text, so a byte-level encoding mangle fails it where a
row count would not.

This is the slow suite — eight deploys and eight migrations, serial, because the
rig is one shared instance. That cost is the point: it is the only way to know.
"""

from __future__ import annotations

import uuid as uuidlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy, Outcome
from bg_coolify_migrate.engine.planner import build_plan, server_uuid_of
from bg_coolify_migrate.engine.runner import run_migration
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.transfer.ssh import SshError

from .conftest import db_exec, db_exec_when_ready, ssh_to, tinker, wait_until_healthy

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

#: count=2, sum=134100. The customer text is where an encoding bug shows up.
SEED = [("Grüße GmbH", 129900), ("Ötztal AG", 4200)]


@dataclass(frozen=True)
class Engine:
    """How to create, seed and fingerprint one database engine."""

    path: str  # /databases/{path}
    creds: dict[str, str]
    seed_argv: str
    seed_stdin: str | None
    fingerprint_argv: str
    expect: str  # substring the fingerprint must contain — catches an empty seed
    volumes: int = 1  # persistent volumes Coolify creates (mongo has two)
    persist_argv: str | None = None  # force a flush to disk before we quiesce


def _sql_rows() -> str:
    return ",".join(f"('{c}',{n})" for c, n in SEED)


def _clickhouse_rows() -> str:
    return ",".join(f"({i + 1},'{c}',{n})" for i, (c, n) in enumerate(SEED))


ENGINES: list[Engine] = [
    Engine(
        path="postgresql",
        creds={"postgres_user": "shop", "postgres_password": "secret", "postgres_db": "shopdb"},
        seed_argv="psql -U shop -d shopdb -tA -v ON_ERROR_STOP=1",
        seed_stdin="CREATE TABLE orders(id serial primary key, customer text, cents int);"
        f"INSERT INTO orders(customer,cents) VALUES {_sql_rows()};",
        fingerprint_argv=(
            "psql -U shop -d shopdb -tA -c "
            "\"SELECT count(*)||':'||sum(cents)||':'||md5(string_agg(customer,'|' ORDER BY id)) "
            'FROM orders"'
        ),
        expect="2:134100:",
    ),
    Engine(
        path="mysql",
        creds={
            "mysql_root_password": "rootpw",
            "mysql_user": "shop",
            "mysql_password": "secret",
            "mysql_database": "shopdb",
        },
        seed_argv="mysql -uroot -prootpw shopdb",
        seed_stdin="CREATE TABLE orders(id int auto_increment primary key, customer varchar(64), "
        f"cents int); INSERT INTO orders(customer,cents) VALUES {_sql_rows()};",
        fingerprint_argv=(
            "mysql -uroot -prootpw shopdb -N -e "
            "\"SELECT concat(count(*),':',sum(cents),':',md5(group_concat(customer ORDER BY id))) "
            'FROM orders"'
        ),
        expect="2:134100:",
    ),
    Engine(
        path="mariadb",
        creds={
            "mariadb_root_password": "rootpw",
            "mariadb_user": "shop",
            "mariadb_password": "secret",
            "mariadb_database": "shopdb",
        },
        seed_argv="mariadb -uroot -prootpw shopdb",
        seed_stdin="CREATE TABLE orders(id int auto_increment primary key, customer varchar(64), "
        f"cents int); INSERT INTO orders(customer,cents) VALUES {_sql_rows()};",
        fingerprint_argv=(
            "mariadb -uroot -prootpw shopdb -N -e "
            "\"SELECT concat(count(*),':',sum(cents),':',md5(group_concat(customer ORDER BY id))) "
            'FROM orders"'
        ),
        expect="2:134100:",
    ),
    Engine(
        path="mongodb",
        creds={
            "mongo_initdb_root_username": "root",
            "mongo_initdb_root_password": "rootpw",
            "mongo_initdb_database": "shopdb",
        },
        seed_argv=(
            'mongosh "mongodb://root:rootpw@localhost/shopdb?authSource=admin" --quiet --eval '
            "'db.orders.insertMany(["
            '{_id:1,c:"Grüße GmbH",n:129900},{_id:2,c:"Ötztal AG",n:4200}])\''
        ),
        seed_stdin=None,
        fingerprint_argv=(
            'mongosh "mongodb://root:rootpw@localhost/shopdb?authSource=admin" --quiet --eval '
            "'const a=db.orders.find().sort({_id:1}).toArray();"
            'print(a.length+":"+a.reduce((s,d)=>s+d.n,0)+":"+a.map(d=>d.c).join("|"))\''
        ),
        expect="2:134100:Grüße GmbH|Ötztal AG",
        volumes=2,
    ),
    Engine(
        path="redis",
        creds={"redis_password": "rootpw"},
        seed_argv='redis-cli -a rootpw MSET greeting "Grüße GmbH" amount 134100',
        seed_stdin=None,
        fingerprint_argv="redis-cli -a rootpw --no-raw MGET greeting amount",
        expect="Gr",  # the escaped umlaut bytes appear; identical both sides
        persist_argv="redis-cli -a rootpw SAVE",
    ),
    Engine(
        path="keydb",
        creds={"keydb_password": "rootpw"},
        seed_argv='keydb-cli -a rootpw MSET greeting "Grüße GmbH" amount 134100',
        seed_stdin=None,
        fingerprint_argv="keydb-cli -a rootpw --no-raw MGET greeting amount",
        expect="Gr",
        persist_argv="keydb-cli -a rootpw SAVE",
    ),
    Engine(
        path="dragonfly",
        creds={"dragonfly_password": "rootpw"},
        seed_argv='redis-cli -a rootpw MSET greeting "Grüße GmbH" amount 134100',
        seed_stdin=None,
        fingerprint_argv="redis-cli -a rootpw --no-raw MGET greeting amount",
        expect="Gr",
        persist_argv="redis-cli -a rootpw SAVE",
    ),
    Engine(
        path="clickhouse",
        creds={"clickhouse_admin_user": "shop", "clickhouse_admin_password": "rootpw"},
        seed_argv=(
            "clickhouse-client -u shop --password rootpw -q "
            '"CREATE TABLE orders(id Int32, customer String, cents Int32) '
            'ENGINE=MergeTree ORDER BY id"'
        ),
        seed_stdin=None,
        fingerprint_argv=(
            "clickhouse-client -u shop --password rootpw -q "
            "\"SELECT concat(toString(count()),':',toString(sum(cents)),':',"
            'lower(hex(MD5(arrayStringConcat(arraySort(groupArray(customer)),\'|\'))))) FROM orders"'
        ),
        expect="2:134100:",
        # clickhouse INSERT is a second statement; run it as the persist step so
        # the seed stays a single create.
        persist_argv=(
            "clickhouse-client -u shop --password rootpw -q "
            f'"INSERT INTO orders VALUES {_clickhouse_rows()}"'
        ),
    ),
]


async def _deploy(
    api: CoolifyClient, rig: dict[str, Any], engine: Engine, *, project_uuid: str, name: str
) -> str:
    created = await api.post(
        f"/databases/{engine.path}",
        {
            "name": name,
            "project_uuid": project_uuid,
            "environment_name": "production",
            "server_uuid": rig["server_a"]["uuid"],
            "instant_deploy": True,
            **engine.creds,
        },
    )
    return str(created["uuid"])


async def _seed_once_ready(source: Any, container: str, engine: Engine, *, timeout: float = 180) -> None:
    """Seed, retrying while the image is still initialising its data directory.

    "healthy" is not "ready" on first boot: see conftest.db_exec_when_ready for
    what each engine says in that window (mysql:8's temporary init server,
    clickhouse's native port opening after its health check passes).
    """
    await db_exec_when_ready(
        source, container, engine.seed_argv, stdin=engine.seed_stdin, timeout=timeout
    )


@pytest_asyncio.fixture
async def engine_stack(
    request: pytest.FixtureRequest,
    api: CoolifyClient,
    settings: Settings,
    rig: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """A deployed, seeded database of one engine, with a known fingerprint."""
    engine: Engine = request.param
    suffix = uuidlib.uuid4().hex[:8]
    project_name = f"eng-{engine.path}-{suffix}"

    project = await api.post("/projects", {"name": project_name, "description": "bgcm engines"})
    project_uuid = str(project["uuid"])

    try:
        db_name = f"{engine.path}-{suffix}"
        db_uuid = await _deploy(api, rig, engine, project_uuid=project_uuid, name=db_name)
        async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
            await wait_until_healthy(source, db_uuid)
            await _seed_once_ready(source, db_uuid, engine)
            if engine.persist_argv:
                await db_exec(source, db_uuid, engine.persist_argv)
            fingerprint = await db_exec(source, db_uuid, engine.fingerprint_argv)

        assert engine.expect in fingerprint, (
            f"{engine.path}: seed did not take (fingerprint {fingerprint!r})"
        )
        yield {
            "engine": engine,
            "project": project_name,
            "project_uuid": project_uuid,
            "db_uuid": db_uuid,
            "db_name": db_name,
            "fingerprint": fingerprint,
        }
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            for db in await api.get("/databases") or []:
                if isinstance(db, dict) and suffix in str(db.get("name", "")):
                    with contextlib.suppress(Exception):
                        await api.delete_resource(
                            "databases", str(db["uuid"]), delete_volumes=True
                        )
        with contextlib.suppress(Exception):
            await api.delete(f"/projects/{project_uuid}")


@pytest.mark.parametrize("engine_stack", ENGINES, ids=[e.path for e in ENGINES], indirect=True)
async def test_engine_data_survives_migration(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], engine_stack: dict[str, Any]
) -> None:
    """server-a -> server-b, and the rows arrive intact for this engine."""
    engine: Engine = engine_stack["engine"]

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=engine_stack["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    # The planner must have found every volume this engine declares — mongo has
    # two, and dropping one is exactly the silent loss this proves against.
    volumes = [item for r in plan.resources for item in r.manifest.items]
    assert len(volumes) >= engine.volumes, (
        f"{engine.path}: expected >={engine.volumes} volume(s), planner found "
        f"{[v.source_name for v in volumes]}"
    )

    # accept_drift=True is the operator saying "yes, migrate despite the tag".
    # keydb and dragonfly ship floating tags (:latest / no version), and the
    # drift gate correctly blocks those without explicit consent — see
    # test_drift_gate for that path. Here we are proving the data round trip.
    result = await run_migration(api, settings, plan, accept_drift=True)
    assert result.outcome is Outcome.SUCCEEDED, f"{engine.path}: {result}"

    target_uuid = await _target_db(api, engine_stack["db_name"], str(rig["server_b"]["uuid"]))
    assert target_uuid, f"{engine.path}: no database landed on server-b"

    async with ssh_to(api, str(rig["server_b"]["uuid"]), settings) as target_host:
        await wait_until_healthy(target_host, target_uuid)
        arrived = await db_exec(target_host, target_uuid, engine.fingerprint_argv)

    assert arrived == engine_stack["fingerprint"], (
        f"{engine.path}: data did not survive the move\n"
        f"  source: {engine_stack['fingerprint']}\n  target: {arrived}"
    )


async def _target_db(api: CoolifyClient, original_name: str, target_server: str) -> str | None:
    """The migrated copy on server-b: the original name, on the target server.

    Looks up by the name captured BEFORE the migration — RENAME finalize renames
    the source, so reading the source's name afterwards would find the wrong one.
    """
    for db in await api.get("/databases") or []:
        if not isinstance(db, dict) or db.get("name") != original_name:
            continue
        full = await api.get_resource("databases", str(db["uuid"]))
        if server_uuid_of(full) == target_server:
            return str(db["uuid"])
    return None


# ── database TLS ─────────────────────────────────────────────────────────────
# enable_ssl/ssl_mode are settable through no API route, so a target always
# starts plaintext (fidelity.database_tls_lost_warning). Postgres only: the
# other engines differ in the warning's text, which the unit tests cover. This is
# also the first TLS source through QUIESCE/DISCOVER, where its certificate bind
# mounts and file-storage rows could have been refused or half-copied.

TLS_ENGINES = [e for e in ENGINES if e.path == "postgresql"]


async def _enable_tls(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], db_uuid: str, *, restart: bool
) -> None:
    """Switch TLS on for a deployed Postgres, the way the UI's toggle does.

    instantSaveSSL only saves the column (HasDatabaseStatusInfo v4.5.0:105-118)
    and no API accepts it, so this is a tinker write — test setup, like
    bootstrap.py's API switch, never tool behaviour.
    """
    assert db_uuid.isalnum(), db_uuid  # it is interpolated into PHP source
    out = tinker(
        f"\\App\\Models\\StandalonePostgresql::where('uuid','{db_uuid}')->firstOrFail()"
        "->update(['enable_ssl'=>true,'ssl_mode'=>'require']); echo 'TLS-ON';"
    )
    assert "TLS-ON" in out, out[-300:]
    full = await api.get_resource("databases", db_uuid)
    assert full.get("enable_ssl") is True, "the GET's wire type is what the rule reads"
    if not restart:
        return

    import asyncio

    # /restart only queues the job, so the OLD container answers for a while:
    # poll for the new one's `ssl`, not for health.
    await api.restart("databases", db_uuid)
    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source:
        deadline = asyncio.get_running_loop().time() + 240
        ssl = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                ssl = await db_exec(source, db_uuid, "psql -U shop -d shopdb -tAc 'SHOW ssl'")
            except SshError:
                ssl = ""  # between the stop and the start
            if ssl == "on":
                break
            await asyncio.sleep(4)
        assert ssl == "on", "the source never came back serving TLS"
        await wait_until_healthy(source, db_uuid)


@pytest.mark.parametrize("engine_stack", TLS_ENGINES, ids=["postgresql"], indirect=True)
async def test_tls_database_warns_and_migrates_plaintext(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], engine_stack: dict[str, Any]
) -> None:
    """A TLS source warns, migrates, and its target serves plaintext."""
    from bg_coolify_migrate.domain.manifest import Decision
    from bg_coolify_migrate.engine.runner import make_migration_id
    from bg_coolify_migrate.journal.store import Journal

    db_uuid = engine_stack["db_uuid"]
    await _enable_tls(api, settings, rig, db_uuid, restart=True)

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=engine_stack["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    resource = plan.resources[0]
    assert not plan.is_blocked, resource.blocking_reasons
    lost = [w for w in resource.warnings if "WITHOUT TLS" in w]
    assert len(lost) == 1, resource.warnings
    assert "ssl_mode='require'" in lost[0]
    assert any("TARGET server's CA" in w for w in resource.warnings), resource.warnings

    # The certificate binds are Coolify's derived config: skipped, never copied.
    items = resource.manifest.items
    coolify_dirs = [i for i in items if i.source_path.startswith("/data/coolify/")]
    assert any("ssl" in i.source_path for i in coolify_dirs), [i.source_path for i in items]
    assert all(i.decision is Decision.SKIP for i in coolify_dirs), [
        (i.source_path, i.decision) for i in coolify_dirs
    ]
    migrate = [i.source_name for i in resource.manifest.to_migrate]
    assert len(migrate) == 1 and str(migrate[0]).startswith("postgres-data-"), migrate

    mid = make_migration_id(plan.project, plan.environment)
    result = await run_migration(api, settings, plan, migration_id=mid, accept_drift=True)
    assert result.outcome is Outcome.SUCCEEDED, str(result)

    # DISCOVER paired the data volume and nothing else.
    discovered = Journal.open(settings.resolved_state_dir(), mid).undo_info("discover")
    paired = discovered["volume_pairs"][db_uuid]
    assert len(paired) == 1 and str(paired[0]["source"]).startswith("postgres-data-"), paired

    target_uuid = await _target_db(api, engine_stack["db_name"], str(rig["server_b"]["uuid"]))
    assert target_uuid, "no database landed on server-b"
    target = await api.get_resource("databases", target_uuid)
    assert target.get("enable_ssl") in (False, 0), (
        "Coolify carried enable_ssl to the target — retire fidelity.database_tls_lost_warning"
    )

    async with ssh_to(api, str(rig["server_b"]["uuid"]), settings) as target_host:
        await wait_until_healthy(target_host, target_uuid)
        ssl = await db_exec(target_host, target_uuid, "psql -U shop -d shopdb -tAc 'SHOW ssl'")
        arrived = await db_exec(target_host, target_uuid, TLS_ENGINES[0].fingerprint_argv)
    assert ssl == "off"
    assert arrived == engine_stack["fingerprint"]


@pytest.mark.parametrize("engine_stack", TLS_ENGINES, ids=["postgresql"], indirect=True)
async def test_a_tls_enabling_config_blocks_the_plan(
    api: CoolifyClient, settings: Settings, rig: dict[str, Any], engine_stack: dict[str, Any]
) -> None:
    """A carried postgres_conf with `ssl = on` would fail the target: refused.

    Plan only — no restart, no migration. postgres_conf goes through the API's
    own update route (base64, DatabasesController v4.5.0:708-726).
    """
    import base64

    db_uuid = engine_stack["db_uuid"]
    await _enable_tls(api, settings, rig, db_uuid, restart=False)
    await api.patch(
        f"/databases/{db_uuid}",
        {"postgres_conf": base64.b64encode(b"ssl = on\n").decode("ascii")},
    )

    async with ssh_to(api, str(rig["server_a"]["uuid"]), settings) as source_host:
        plan = await build_plan(
            api,
            source_host,
            project=engine_stack["project"],
            environment="production",
            target_server="e2e-server-b",
            finalize_policy=FinalizePolicy.RENAME,
            transfer_mode=TransferMode.DIRECT,
        )

    assert plan.is_blocked
    assert any("postgres_conf sets ssl" in r for r in plan.resources[0].blocking_reasons), (
        plan.resources[0].blocking_reasons
    )
