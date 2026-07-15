# E2E rig — a real Coolify, two real Docker daemons

The unit suite proves the pure cores are self-consistent. The integration rig
proves rsync moves bytes. Neither can prove we drive **Coolify** correctly —
that our request whitelists are accepted, that a created database really gets the
volume we predicted, that discovery finds anything at all.

Only a real Coolify can, so this rig runs one.

## The load-bearing detail

`server-a` and `server-b` run `docker:28-dind`, so each has its **own daemon and
its own volumes**. Mounting the host socket into both would be far simpler and
completely worthless: both would see the same volumes, and a "migration" between
them would move nothing while passing every assertion.

## Running it

```bash
python tests/e2e/prepare.py                                  # keys + /data/coolify/source/.env
docker compose -f tests/e2e/docker-compose.yml up -d --wait  # Coolify + 2 servers
python tests/e2e/bootstrap.py                                # root token, API on, register servers
docker compose -f tests/e2e/docker-compose.yml --profile test run --rm runner
```

The tests run **inside** the rig's network, not on Windows. Docker Desktop gives
the host no route to container IPs, and container IPs are exactly what Coolify
hands out as the servers' addresses — a test that reached the servers by some
other address would exercise a path the tool never takes.

`rig.json`, `keys/` and `coolify.env` are minted per run and gitignored. Every
one of them is a credential; the rig is reproducible from the two scripts.

### Resetting it

```bash
docker compose -f tests/e2e/docker-compose.yml down -v   # -v, or the old Coolify DB survives
rm -rf tests/e2e/keys tests/e2e/rig.json tests/e2e/coolify.env
```

Then start again from `prepare.py`. Do this rather than picking at a wedged rig:
it takes two minutes and leaves nothing to explain later.

Tests name their project uniquely per run and delete their databases (volumes
included) on the way out, so a crashed run leaves orphans that cannot make a
later run pass — only clutter. `down -v` clears them.

## What it found

Every item below was live in code that passed 923 green unit tests. They share a
shape: an assumption about someone else's system, checked only against itself.

| # | Assumption | Reality |
|---|---|---|
| 1 | `GET /version` returns JSON | Returns the bare string `4.1.2`. The unit test mocked it as JSON — an assumption checked against itself. |
| 2 | A 403 means the token lacks scope | The API is **off instance-wide by default**. Our hint blamed the operator's token for an instance setting. |
| 3 | The sensitive-probe is a yes/no | Three outcomes: confirmed, denied, **indeterminate** (no keys to probe against). Reporting "no" for "cannot tell" is a lie with consequences. |
| 4 | `is_reachable` is a top-level field | Lives under `settings.is_reachable`. `doctor` showed "unknown" for every server, always. |
| 5 | The environment endpoint has a `databases` key | It has **one key per engine** (`postgresqls`, `redis`, …) and no `databases`. Every database migration reported "no resources". |
| 6 | Those per-engine keys cover all engines | The controller eager-loads 7 of 10 relations. **keydb, dragonfly and clickhouse are invisible** — a project with a ClickHouse would migrate and leave it behind, silently. We cross-check `/databases`, which merges all eight. |
| 7 | A resource carries `server_uuid` | Only services do. Applications and databases hang their server off `destination` (a morphTo). The two kinds that matter most stopped at "could not determine the source server". |
| 8 | Discovery can filter `coolify.{kind}Id={id}` — Coolify does | Every controller calls `makeHidden(['id'])`. **The numeric id is never disclosed**, so the filter was `coolify.databaseId=`, which matches nothing and reports no error. Now filters on the slugified project/environment/resource-name triple, which every managed container carries. |
| 9 | Our `slugify` matches Laravel's `Str::slug` | It did not. Laravel **removes** stray characters then collapses; we replaced them with `-`. `a.b.c` → `abc` vs `a-b-c`. Invisible on names like `Straße & Co`, where both happen to agree. Now a ported implementation, checked against the running Laravel over 21 cases. |
| 10 | `health_check_*` round-trips | Readable in every GET, in **no** `$allowedFields` — create or update. Sending one 422s the whole request. Now excluded, and a deviating source is reported rather than silently flattened. |
| 11 | Coolify's health-check defaults are 30/30/3/30 | 15/5/5/5. Four of five guessed wrong — would have warned on every stock database and taught operators to skip warnings. Now read from the schema, and pinned by a test. |
| 12 | A 400 from `/stop` is a stop failure | Coolify decides "already stopped" from a **column a background job maintains**, so it lags the daemon and can say `exited` about a container serving traffic. Treating it as fatal aborted migrations over a stale row. Now tolerated — the daemon poll still has to see every container exited. |
| 13 | Containers survive a stop as `exited`, so discovery can run after it | **Coolify's stop removes them**: `docker stop` then `docker rm -f`, in all three stop actions. The design's "authoritative post-stop discovery" had nothing left to inspect, so it built an empty manifest and copied nothing. The saga ran green to `finalize` and the target came up as a brand-new empty database. QUIESCE now captures mounts before stopping; DISCOVER reconciles that with `volume ls` and `/storages`, which survive. |
| 14 | The SIGKILL guard protects the copy | It could never fire. Exit code 137 only exists on a container, and `docker stop`/`docker rm -f` leave in one SSH invocation — the record is gone within milliseconds. A database killed mid-write would have been mirrored byte-exactly as a torn snapshot, faithfully. Now read from the daemon's **event log**, where the exit code outlives the container. |
| 15 | Rollback restarts the source with `/start` | `/start` has the stop bug in reverse: `if status contains 'running' -> 400 "already running"`, dispatching nothing. QUIESCE removed the container but the column still read "running", so the rollback's restart step 400d **while the source was down** — `ROLLBACK_FAILED`, outage un-ended. Now uses `/restart`, which has no such guard. Found by the rollback test; the single postgres test never rolled back. |
| 16 | Service create allows `connect_to_docker_network` | It does not. `create_service` has two `$allowedFields` — line 296 (without it) and line 505 (with it) — but the rejection at line 332 validates **both** branches against 296, so a compose create carrying it 422s before 505 is reached. Our whitelist matched the wrong array. This broke the compose-service (90%) migration outright. Now dropped from create and carried via a follow-up PATCH. |

Findings 5–9 and 13 each cause the same failure: **a migration that reports
success and moves nothing**; 14 quietly permits the other one, a faithful copy
of a corrupted database; 15 leaves the source dead after a rollback that was
supposed to save it. That is the failure this tool exists to prevent, and it was
sitting in our own code.

The second wave (15, 16) came only from the **comprehensive** suite — every
database engine, a compose service, a multi-resource project, a real rollback.
The single postgres migration proved the happy path. It took migrating a stack
that *fails* to find that the recovery path was broken (15), and migrating the
compose shape the estate actually uses to find that its create was rejected
outright (16).

Finding 13 is the one worth remembering, because it is not a wrong constant or a
misread field — it is a correct-sounding sentence in the design (*"authoritative
post-stop discovery"*) resting on an assumption nobody thought to check. It also
leaves a permanent hazard: **an empty container list and a cleanly stopped stack
are the same observation.** Finding 8's broken label filter looked precisely like
a successful quiesce. Anywhere that reads "no containers" as "all stopped" is one
typo away from silently copying nothing.

## What the tests assert

- `test_real_migration.py` — deploys a Postgres on server-a, seeds rows whose
  text carries umlauts and an eszett, migrates to server-b, and reads the rows
  back **from server-b's daemon**. The fingerprint is count + sum + md5 over the
  names, so a byte-level encoding mangle fails it where a row count would not.
  A second test asserts `RENAME` leaves the source volume intact — checked
  against the daemon, because the API would only confirm a row still exists, and
  a row is not data.
- `test_label_contract.py` — asks the running Laravel what `Str::slug` returns
  and compares byte for byte, and pins the health-check defaults against the
  live schema. Both would be circular as unit tests: they would compare our code
  against our own idea of Coolify, which is the assumption under test.
- `test_all_engines.py` — the same round trip for **every** engine: postgres,
  mysql, mariadb, mongodb, redis, keydb, dragonfly, clickhouse. Each stores data
  differently (mysql bakes auth into the data dir, mongo uses two volumes, redis
  is in-memory with a persistence config, clickhouse is columnar), so "the target
  boots from the copied volume" is proven per engine, not assumed from postgres.
  Slow on purpose — eight deploys and eight migrations, serial.
- `test_drift_gate.py` — a floating-tag image (`eqalpha/keydb:latest`) must block
  at preflight without `--accept-drift` and complete with it. This is the user's
  own requirement, checked against a real `:latest` rather than a mocked one.
- `test_compose_service.py` — a Postgres inside a raw docker-compose **service**,
  through the `/services` path rather than `/databases`. Exercises the service
  volume separator (`_`, not `-`) and the pre-stop mount capture for a compose
  container.
- `test_multi_resource.py` — a project holding a Postgres AND a Redis, migrated
  as one plan, each with distinct data. Proves the n>1 case: migrating only the
  first, or crossing one resource's volume to another's target, fails a
  fingerprint.
- `test_rollback.py` — injects a fault after the volumes are copied and asserts
  the world is put back: target deleted from server-b, source still running on
  server-a with its data intact. This is what caught finding 15.
