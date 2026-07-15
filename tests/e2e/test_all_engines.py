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

from .conftest import db_exec, ssh_to, wait_until_healthy

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
            await db_exec(source, db_uuid, engine.seed_argv, stdin=engine.seed_stdin)
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
