# AGENTS.md

Instructions for an AI agent — or a human in a hurry — working on this repo.

## Fast path

```bash
uv venv --python 3.14 && uv pip install -e ".[dev]"   # 3.12 is the floor, not the target
make all-checks          # ruff + mypy strict + pytest (>=80% coverage)
coolify-migrate doctor   # needs COOLIFY_URL + COOLIFY_TOKEN
```

## What this is

A tool that moves a Coolify **project with its data** between servers (F1), and
relocates a whole Coolify instance (F2).

It exists because Coolify deliberately will not move data: `VolumeCloneJob` and
`CloneMe`'s `cloneVolumeData` exist upstream but PR #4777 shipped them
**disabled**. The maintainer's four stated blockers — permission damage,
job-queue spam at 50+ resources, no progress tracking, large-volume failures —
are all consequences of running inside Coolify's Laravel queue. An external
orchestrator has none of them.

Two prior tools solve this badly, and their bugs are our requirements:

- **`mrcandev/coolify-mover`** — raw SQL through `docker exec coolify-db psql`
  with string interpolation into a double-quoted shell string (RCE as root); no
  transactions; `rsync -avz` with no `--numeric-ids`; silently loses every
  *service* volume; hot-copies live databases by default.
- **`Geczy/coolify-migration`** — architecturally right, operationally sloppy:
  `docker ps` without `-a` (stopped containers' volumes silently skipped), no
  verification of any kind, no resume, a fixed 1 GB disk check.

## Security invariants — NON-NEGOTIABLE (never weaken)

1. **Never `chown`.** Preserve uid/gid numerically (`--numeric-ids`). Coolify's
   own clone hardcodes `chown -R 1000:1000` and that is exactly what corrupts
   postgres/mysql/redis (uid 999) and clickhouse (uid 101) volumes.
2. **Never route volume data through the operator's workstation filesystem.**
   rsync runs *on* the Linux servers. The tunnel relays TCP only.
3. **Never disable host-key checking.** No `StrictHostKeyChecking=no`.
4. **Never write SQL to `coolify-db`.** The REST API is the only write path.
5. **Never log secrets** — env values, private keys, APP_KEY. Redaction is a
   structlog processor and is extended *additively*, never replaced.
6. **Never journal secrets.** `journal/store.py` raises rather than redacting —
   a redaction hides the caller's bug; an exception surfaces it.
7. **Never start a target whose FQDN still resolves to the source.** Doing so
   fails the ACME challenge and burns Let's Encrypt rate limits.
8. **Never delete the source before verification passes.** The source surviving
   until an explicit finalize is what makes rollback always available.
9. **Never swallow a stop failure, and never trust a stop endpoint.** Poll the
   Docker daemon by label, with `-a`, and require *every* container — previews
   included — to be `exited` and not SIGKILLed.
   A 400 "already stopped" is not a stop failure and is not a stop: Coolify
   reads it off a background-maintained column and dispatches nothing, so it
   refuses stacks that are serving traffic. `api.stop()` reports that as `False`
   and `_request_stop` re-asks until the daemon agrees. What stays forbidden is
   concluding anything about the stack from it — the gate below still has to see
   every container exited.
10. **Never string-replace a UUID to derive a volume name.** Pair by
    `mount_path`. This is coolify-mover's silent data-loss bug.

If a change would relax any of them, **stop and ask**.

## Architecture: pure cores, thin IO shells

This is the load-bearing design decision, not a stylistic one. Both predecessor
tools are broken *because* their logic is inseparable from their side effects, so
neither can be meaningfully tested.

| Layer | Purity | Rule |
|---|---|---|
| `domain/` | **PURE** | No IO, ever. Total functions over captured snapshots. |
| `api/` `discovery/` `transfer/` `dns/` `journal/` | IO shells | Gather facts and perform actions; decide nothing. |
| `engine/` | Executor | Performs what `domain/statemachine.py` decides. |
| `ui/` | Rendering | Surfaces the `reason` each domain object already carries. |

New logic goes in `domain/`. If you find yourself wanting a mock to test a
decision, the decision is in the wrong layer.

## Decision tree

- **Adding a Coolify API call?** Add its field whitelist to `api/fields.py` first.
  `$allowedFields` is enforced; unknown fields are a 422 per field. **Never**
  round-trip a GET response into a POST body.
- **Handling a new resource shape?** Extend `domain/kinds.py`. Remember "does it
  build?" is NOT a property of the kind — read it from the compose (`build:` vs
  `image:`) via `domain/compose.py`.
- **Tempted to block on drift?** Don't. We build the target exactly as the source
  is configured and report what could still differ; whether that is compatible is
  the operator's judgement about their stack. Blocking belongs to things that are
  not judgements (a refused volume, live previews, DNS pointing at the source).
- **Touching the transfer?** Every rsync flag in `transfer/rsync.py::BASE_FLAGS`
  is there for a reason documented in the module docstring. Removing one is a
  data-integrity change.
- **Touching quiesce?** It has no `--force` and must not grow one.
- **Leaving a volume unmounted across a step?** Pin it (`discovery/pins.py`)
  first. Unmounted is what Coolify's `docker volume prune -af` deletes. Never
  flip the server's cleanup setting instead: a crash leaves it flipped, and a
  manual API run overrides it anyway.

## Verified upstream facts (do not re-derive)

First checked against `coollabsio/coolify@main`; re-checked against the release
tags v4.1.2 through v4.5.0 (2026-10-26). Read upstream at a **release tag**, never
`main` — see the 2.5.6 tags regression.

- `can_read_sensitive = (token->can('root') || token->can('read:sensitive'))
  && owner->isAdminOfTeam()` — the admin clause since 4.2. Without it, `value`,
  `real_value`, `docker_compose_raw` **vanish** from responses — HTTP 200, no
  error, no marker.
- **`git_commit_sha` does not pin a deploy.** `check_git_if_build_needed()` runs
  `git ls-remote refs/heads/{branch}` and overwrites it
  (`ApplicationDeploymentJob.php:2329-2349`). The API never sets `rollback:`.
- `build_pack=dockercompose` runs `docker compose build --pull` unconditionally
  (`:761-764`), so even a same-commit rebuild is not byte-identical.
- `POST /applications/{uuid}/stop` does **not** stop preview containers
  (`StopApplication` filters `pullRequestId=0`).
- `StopService` finds containers from **DB records**, not labels — a compose
  container Coolify never parsed is never stopped.
- `POST /storages` forces `name = '{resource_uuid}-{name}'`. Volume names can
  never be preserved.
- Service compose volumes are `{svc_uuid}` **`_`** `{slug}`; application compose
  volumes are `{app_uuid}` **`-`** `{name}`. Never convert one into the other.
- `GET /applications/{uuid}` does **not** return `settings` on 4.1.x. From 4.2 it
  eager-loads them — but NESTED under `settings`, while create takes them flat, so
  `filter_body` still never carries one. ~33 fields are settable-but-uncarried.
- Standalone DB mount path depends on the image tag (Postgres ≥18 moves to
  `/var/lib/postgresql`). Always pin `image`.
- Every request body passes `TrimStrings` and **`ConvertEmptyStringsToNull`**
  (`Kernel.php:61-62`): an empty string we send arrives as `null`. From 4.5 an
  empty and a null env value render differently, so "send `""`" is never a fix.
- **Multi-server applications are refused on every release.** Every
  `GET /applications/{uuid}` carries `additional_servers_count` (a global
  `withCount` scope, Application.php v4.1.2:244-249 .. v4.5.0:306-311); the
  `additional_servers` rows ride along only via a lazy load, and on a root token
  they carry proxy config and server settings — read `name`/`uuid` only.
  `StopApplication` fans out to them but, up to 4.3.x, returns on the first
  non-functional server, and we can poll only the source's daemon (invariant 9).
  `/applications/{uuid}/destinations` exists from 4.3.0; its DELETE only
  detaches and never stops. The UI's removal stops first — except on an
  unreachable server, where it detaches anyway. CREATE re-checks the count
  (`steps._refuse_multi_server`) before parking anything.
- **Scheduled tasks** exist for applications and services only (no database
  route or column), readable and creatable from v4.1.2 with one six-field array
  (`ScheduledTasksController` v4.1.2:57 = v4.5.0:81). An absent `enabled`
  defaults to TRUE, so a disabled task is always sent `enabled: false`. From
  4.4.0 `command` is hidden without can_read_sensitive and `timeout` is
  `max:36000`; from 4.5.0 an ENABLED task needs `deploy`, and `container` also
  matches a full container name (it embeds the source uuid, so the plan warns
  instead of rewriting it, invariant 10).
  The scheduler skips a resource whose status does not contain 'running'
  (`ScheduledJobManager` v4.5.0:670-681), and a never-started target reads
  'exited'. That skip is the only thing keeping carried tasks inert before START.
  The UI's "execute now" and `artisan` RunScheduledJobsManually bypass it, and
  before 4.5 the job's app branch also sees stopped containers, so never call
  either. A template service or a git-compose app takes its sub-service names
  from the CURRENT catalog or git HEAD, so CREATE checks each task's container
  against the target. A POST retried on 502/504 can save a task twice, so CREATE
  re-reads and counts. Deleting the target deletes its tasks (model hooks).
- **Database backup schedules** (`/databases/{uuid}/backups`, every release)
  create through `$backupConfigFields` (v4.1.2:704, v4.5.0:1057).
  `missing_backup_notification_days` arrives with its column (4.3.18/4.4.0) and
  `s3_storage_uuids` in 4.5.0, so key presence gates both. The GET filters on
  `database_id` ONLY: another engine's rows with the same hidden id come back
  too, so filter on `database_type` (the model FQCN; there is no morph map). It
  also eager-loads every execution, whose `message` is raw dump output before
  4.5; `client.get_backups` drops it at the boundary. Only 4.5 names a
  schedule's S3 storages by uuid. Before that it is a hidden id no API maps, so
  the target's copy is created local-only and the plan says so. Redis, KeyDB
  and Dragonfly have no backup (create 422s from 4.2.0). DatabaseBackupJob skips
  a database whose status does not start with 'running', so a carried schedule
  is inert before START. `create_backup` retries 429 only: a re-POST after a
  proxy 502 would double the schedule.
- **No API reads two kinds of backup schedule.** A SERVICE database's schedules
  are not exposed (the backup routes resolve standalone databases only), so the
  plan warns for every backup-capable service database. Volume-backup schedules
  (4.3.0+, PUT/run/DELETE on `.../storages/{uuid}/backups`, no GET) are
  documented, not warned per resource.
- **Rollback deletes a target's backup ROWS, never its dump FILES.**
  DeleteResourceJob removes the rows (v4.5.0:167) and leaves the dumps. Never
  send `DELETE .../backups/{b}?delete_s3=true`: `rollback` also runs after a
  SUCCEEDED run (it has no is_finished guard), and then those dumps are the last
  copy of post-cutover data.
- **A domain PATCH only writes the database; the labels are baked at deploy.**
  `PATCH /applications/{uuid}` with `domains`/`docker_compose_domains` saves
  (and, read-only labels being the default, regenerates `custom_labels`) but
  deploys nothing without `instant_deploy` (v4.1.2:2660-2680, v4.5.0:3752-3793).
  Every deploy — `/restart` included — writes the stored labels into the new
  container (ApplicationDeploymentJob v4.1.2:3099-3113, v4.5.0:4118-4132). So a
  domain change must land BEFORE the deploy that should carry it: the rollback
  restores the source's domains, then restarts it (`statemachine._PRECEDENCE`).
- **The domain-conflict check is team-wide and excludes only the updated uuid.**
  Applications, service applications and the instance fqdn all count
  (domains.php v4.1.2:142-184, v4.5.0:250-316). The 409 names every holder in
  `conflicts[].resource_uuid` — a service's ServiceApplication uuid, null for the
  instance. DELETE dispatches a job on v4.1.2-v4.3.10 (the row holds its domains
  until it runs) and soft-deletes synchronously from v4.3.11. The restore forces
  (`force_domain_override`, in every update array) only over our own targets.
- **`noindex_domains` (4.3.0+) is dropped with the domains it flags.** The save
  hook's `syncNoindexDomains` keeps flags only for current domains
  (Application.php v4.3.0:280), so parking erases them; the update applies them
  after the fqdn (v4.3.0:3115-3120), so the journalled restore body carries them.

### Learned the hard way, from a real instance

Source-reading got the list above right. It got all of these wrong, and every one
shipped inside a green unit suite. `tests/e2e/README.md` has the full table.

- **The numeric `id` is never disclosed.** Every controller calls
  `makeHidden(['id', 'laravel_through_key'])`. So you cannot copy Coolify's own
  `--filter label=coolify.{kind}Id={id}` from outside — it filters on the empty
  string, matches nothing, and reports no error. Identify containers by the
  slugified `coolify.projectName` / `environmentName` / `resourceName` triple,
  which `defaultLabels()` and `defaultDatabaseLabels()` both write.
- **`slugify` must equal Laravel's `Str::slug` exactly**, because that discovery
  depends on it. It *removes* stray characters and only then collapses runs, so
  `a.b.c` → `abc`, and `@` → `-at-`. Locked by `test_slug_matches_laravel`
  against the live Laravel — never "improve" it against intuition.
- **The environment endpoint has no `databases` key.** It has one per engine
  (`postgresqls`, `redis`, `mongodbs`, `mysqls`, `mariadbs`) — and the controller
  eager-loads only 7 of 10 relations, so **keydb, dragonfly and clickhouse are
  invisible there** (and SQLite, 4.4+). Cross-check `/databases`, which merges
  them all.
- **Only services carry `server_uuid`.** Applications and databases reach their
  server through `destination` (a morphTo). Use `planner.server_uuid_of`.
- **`is_reachable` lives under `settings`**, not at the top level.
- **`GET /version` returns a bare string**, not JSON.
- **The API is off instance-wide by default** — a 403 is not necessarily the
  token.
- **`health_check_*` (databases) is readable but in no CREATE `$allowedFields`.**
  Sending one on create 422s the whole request; the UPDATE array has accepted them
  since 4.1.2. Defaults are 15/5/5/5 (not 30/30/3/30).
- **`connect_to_docker_network` is rejected on service *create*.** There are two
  `$allowedFields` in `create_service` — line 296 (without it) and line 505
  (with it) — but the extra-field rejection at line 332 runs against 296 for
  BOTH the templated and the compose branch, so 505 never applies to validation.
  It is settable only on update. `finish_service_target` sends it in the
  follow-up PATCH that carries the domains, after the target uuid is journaled.
  This broke the compose-service (90%) migration until the e2e suite hit it —
  a source-reading whitelist that matched the wrong one of two arrays. v4.5.0
  finally accepts it on create; 4.1.2-4.4.7 do not, so it stays a PATCH.
- **`/stop` 400s "already stopped" off a background-maintained column** and
  dispatches nothing. It lags the daemon, so it refuses to stop containers that
  are serving traffic. Re-ask until the daemon agrees; never conclude from it
  that a stack is down.
- **Coolify's stop REMOVES the containers.** `docker stop` then **`docker rm -f`**
  — StopDatabase.php:59, StopApplication.php:43, StopService.php:70. This
  refutes the design's "authoritative post-stop discovery": once a stack is
  quiesced there are no containers to inspect, and anonymous volumes and bind
  mounts are recorded nowhere else. QUIESCE therefore captures the mounts before
  it stops anything (`_capture_mounts`), and DISCOVER reconciles that capture
  with `docker volume ls` and `/storages`, both of which survive.
  Consequence to keep in mind everywhere: **an empty container list is
  indistinguishable from a cleanly stopped stack.** Both read as quiesced. The
  broken label filter above looked exactly like success for the same reason, so
  treat "no containers" as a question, never as an answer.
  One exception (4.4.0+): a ClickHouse whose data sits in an unregistered
  anonymous volume is only `docker stop`ped, and `/start`/`/restart` then 422
  until someone clicks "Keep current data". The manifest refuses such a volume at
  plan time, so a run never reaches it.
- **Exit code 137 cannot be polled for**, for the same reason: both commands go
  out in one SSH invocation, so a SIGKILLed container exists for milliseconds.
  The guard that makes invariant 9 mean anything reads the **daemon event log**
  after the fact (`quiesce.killed_since`), where the exit code outlives the
  container. Do not replace it with a `docker ps` check that looks equivalent.
- **`/start` has the stop bug in reverse, and here it is dangerous.** It guards
  on the same stale status column: `if status contains 'running' -> 400 "already
  running"`, dispatching nothing. After QUIESCE removed the container the column
  can still read "running", so the rollback's restart-the-source step gets a 400
  while the source is in fact down. Swallowing it would be the worst move — it
  would report the outage ended while leaving the source dead. The recovery path
  therefore uses **`/restart`** (`api.restart`), whose `action_restart` carries
  no guard and actually redeploys. Found by the e2e rollback test; the single
  postgres test never rolled back, so it never hit this.

### Learned from the v4.3.10 -> v4.5.0 audit (release tags, not yet a live run)

The drift canary went red on 8 documented fields; the release-tag audit found the
rest. Each is refused or reported at plan time by `domain/fidelity.py`, gated on
the release it starts in — read that module before adding a rule.

- **The openapi canary is blind to undocumented create fields.**
  `custom_network_aliases`, `health_check_type` and `health_check_command` sat in
  the application create array from 4.1.2 on, documented nowhere, and every
  migration dropped them. `test_controller_arrays_are_adjudicated` reads the
  enforcement arrays FORWARD now; a field accepted but not documented still fails.
- **A locked (`is_shown_once`) variable's value is in no API read** — even for a
  root token (applications/services since 4.2, databases since 4.4) — with ONE
  exception, below. `domain/locked_envs.py` reads it back from the file Coolify
  wrote on the source server instead (next section); only what that cannot read
  is created without, and a locked generated credential (`SERVICE_PASSWORD_*` of
  a service or a dockercompose app, Redis `REDIS_PASSWORD`) that stays unread is
  refused, because the target would hold a different secret than the copied data.
- **`host_path` left every storage create array in 4.3.22** (backported). Dropping
  it instead of refusing would mount an empty volume over the mirrored bind data,
  and VERIFY would pass.
- **A domain's explicit port moved into hidden `domain_port_overrides` (4.3.15).**
  The GET shows a portless fqdn; only the generated proxy labels still show the
  routing. Parking the source's domains erases the override on the source too.
- **Env rows saved before 4.5 keep the legacy escaping; every row we create is
  exact** (`uses_legacy_escaping`, hidden, unsettable). The row's `version` field
  tells which is which. Values with `\ " '`, a ` #` tail or quoted literals reach
  the container differently on the target.
- **A service is created from the INSTANCE's template catalog**, not the source's
  compose — 95 templates changed between 4.3.10 and 4.5.0. CREATE therefore
  rehearses the volume pairing for services while the source still serves, and
  services pair by compose volume key (`{uuid}_{key}`), like compose apps.
- **DELETE needs `write` AND `deploy` from 4.5** (rollback deletes targets);
  the token docs say root, or `read:sensitive` + `write` + `deploy`.
- **Standalone database TLS (`enable_ssl`, `ssl_mode`) is readable in every GET
  and carried by no create/update field and no dedicated route** (v4.1.2-v4.5.0);
  only `POST /databases/{uuid}/clone` (v4.3.0+) copies it, as a whole-row
  server-side copy. A target always starts plaintext (column default false) —
  `fidelity.database_tls_lost_warning`. Mongo rows that existed at beta.399 (or
  were created before beta.400) were switched to `enable_ssl=true` by migration;
  operators may have turned it off since. Re-enabled on the target, TLS gets a
  certificate from the TARGET server's CA for the new uuid and IP
  (`database_tls_identity_warning`, which carrying TLS must not silence). Dragonfly
  serves TLS on 6379 itself, Redis/KeyDB on an extra 6380. A carried `*_conf` that
  turns TLS on by itself is refused: the target gets the config, not the certs.

### Service domains (release tags v4.1.2 -> v4.5.0)

- **A service's domains live on its sub-applications** (`applications[].fqdn`);
  the service has none. The proxy labels are generated from that column
  (parsers.php v4.5.0:3389), so it is what CREATE carries — with ONE
  `PATCH /services/{target}` `{"urls": [{name, url}]}`, the only write path valid
  on every release (items are exactly `{name, url}`, ServicesController
  v4.1.2:971, v4.5.0:1259; the per-sub-application PATCH exists only from 4.2).
- **Read `url` before `fqdn`.** From 4.3.15 the port of a saved domain moves into
  the hidden `domain_port_overrides`; the model appends `url`, which re-composes
  it (ServiceApplication v4.5.0:18,215-242). Generated sslip hosts name the
  SOURCE's uuid and IP and are never carried.
- **Services are NOT parked.** Every `PATCH /services` re-parses the compose
  before it applies `urls` (v4.5.0:1338), and on 4.5 `applyServiceUrls` rewrites
  the SERVICE_URL_*/SERVICE_FQDN_* rows at once (`updateCompose`, v4.5.0:214) — so
  parking would rewrite a LIVE source and couple every rollback to restoring
  before `/restart`. Instead the target claims the hosts with
  `force_domain_override`, and only when every `conflicts[].resource_uuid` of the
  409 is one of the source's own sub-applications (that field is the holder's
  uuid, `null` for the instance's own domain: domains.php v4.5.0:250-335). Any
  other holder is refused before the stop. FINALIZE releases the source's custom
  hosts (url `""` -> null) under KEEP as well as RENAME: the park tag is the
  project/environment digest, not per run, and a kept source holding the target's
  host is a second router the moment someone restarts it.
- **The conflict check is port-blind against a portless holder (4.3.15+):** the
  request key keeps its port, the stored fqdn lost it, so a port-bearing URL never
  409s against the same host saved without one. Do not rely on a 409 to detect a
  double claim of a port-bearing URL.
- **From 4.4 a removed hostname queues `ReleaseRemovedDnsHostnamesJob`**, which
  deletes a Coolify-managed DNS record no live resource uses (v4.5.0
  ServicesController:125-145). Removing a host from a live source before the
  target holds it can delete its record; at FINALIZE the target already uses it.
- **A restore must not write a source that already holds its originals**
  (`restore_service_urls` GETs, compares, PATCHes only the difference): the same
  re-parse and `updateCompose` apply to it.
- **SPECIFIC_SERVICES images are routed from plain env rows, not from fqdn.**
  minio, logto, garage (and silo from 4.5) go through
  `generateServiceSpecificFqdns` (parsers.php v4.1.2:2527-2528, v4.5.0:3386-3389;
  docker.php v4.5.0:660-760): `MINIO_SERVER_URL`, `LOGTO_ENDPOINT`,
  `GARAGE_S3_API_URL` and friends (`dns/extract.py::ROUTE_ENV_KEYS`). The gate
  reads those, each sub-application's domain and a custom compose's own Host()
  labels — never the derived SERVICE_URL_*/SERVICE_FQDN_* rows.
- **`updateCompose` and the parser rebuild only DIRECTLY declared
  SERVICE_URL_*/SERVICE_FQDN_* rows.** A row that is only referenced (`${...}`) is
  `firstOrCreate`d and keeps whatever was copied into it for good (services.php
  v4.5.0:392-418). So `copy_envs` drops a service's URL row (and route key) only
  when it names the source — a generated host, a host under the source wildcard,
  or `old-<tag>.` — and copies an operator's custom value verbatim.
- **A TARGET host under the SOURCE's wildcard blocks the gate** (invariant 7, not
  a cutover: `--accept-dns` does not apply). It was not remapped and its wildcard
  binds it to the source. Derived `service_env` rows stay report-only.
- **One URL on two sub-applications rejects the WHOLE `urls` update** (422
  "conflicting URLs across containers", ServicesController v4.1.2:73-76,
  v4.5.0:166-169), before any conflict check — so no force retry helps, and the
  plan refuses it. The comparison lowercases scheme and host from 4.2.0
  (`normalizeApplicationDomains`); 4.1.x compares raw strings. A trailing slash,
  http vs https or a port difference only meet the per-item conflict check,
  which CREATE's own-conflict retry resolves or refuses before the stop — do not
  widen the plan refusal to them.
- **Coolify's URL validator accepts `user:password@host`.** The plan refuses
  such a sub-application URL (naming only the sub-application) so no credential
  reaches a request, a conflict message, a log line or the journal. Do NOT add a
  raising URL shape to `journal/store.py::_SECRET_SHAPES` instead: the executor
  journals `str(exc)` inside its except handler, and a raise there escapes
  before the rollback runs.

### Locked variables are read back from the source server (release tags)

`domain/locked_envs.py` + `discovery/envfiles.py`; read both before touching it.

- **Coolify writes every locked value to a file on the resource's server** and
  the stop deletes none of them: `/data/coolify/applications/{uuid}/.env`
  (runtime rows only — never build-only or NIXPACKS_/RAILPACK_ rows),
  `/data/coolify/services/{uuid}/.env` (every row), and the `environment:` list of
  `/data/coolify/databases/{uuid}/docker-compose.yml`. Never the container's
  environment: compose has already expanded `${SERVICE_FQDN_X}` to the SOURCE host
  there, and QUIESCE removes the containers.
- **Each line is `KEY=ENC(resolved)`** — exact escaping for rows stamped 4.5+,
  the legacy form otherwise. We decode, RE-ENCODE and require the line back byte
  for byte, so a wrong guess or a secret-manager line is "unrecoverable", never a
  different value (`test_round_trip_property_exact_or_nothing`). Indistinguishable
  secret-manager lines are carried as their resolved value — documented, accepted.
- **The file is what Coolify LAST WROTE, so it is checked first.** Every unlocked
  row in it must encode exactly as the API's `real_value` predicts (legacy rows:
  `real_value` IS the written form, v4.5.0 EnvironmentVariable.php:265-283);
  a mismatch means "redeploy/restart the source, then plan again".
- **A preview deploy overwrites the application's .env** (configuration_dir does
  not depend on the PR, v4.5.0 ADJ.php:282). `COOLIFY_CONTAINER_NAME=…-pr-N` or
  `SERVICE_NAME_*=…-pr-N` gives it away; recovery refuses that file.
- **The app .env and a database compose are written with `tee`, non-atomically**
  (ADJ.php:2063, StartPostgresql.php:213): read twice in one SSH command, and only
  two identical reads count.
- **The exception: a Redis `internal_db_url` carries the locked REDIS_PASSWORD.**
  It is appended and made visible to sensitive callers, and embeds
  `rawurlencode(connectionPassword())` (v4.5.0 StandaloneRedis.php:309-318,
  DatabasesController.php:213-234; same at 4.4.0). It is the primary source,
  cross-checked with the compose. Never log it or put it in a snapshot — `db_url`
  is a redaction fragment.
- **`is_shared` is strict only from 4.4.0** (`isSharedReference`); 4.1.2-4.3.x
  call any `{{x.y}}`-wrapped value shared. Shared locked rows are never recovered
  (the file holds the resolved value, not the reference).
- **A value-less bulk entry nulls the row on applications only**; services and
  databases `updateOrCreate` and keep the old value. Never send one either way.
  The application created hook copies a new row into an UNLOCKED preview twin,
  so a carried locked value is followed by a locked preview entry.
- **Values live only in `Secrets`** (repr: key names), never in a ResourcePlan,
  plan.json, the journal or a log line; CREATE re-reads before its first write.

### Coolify's Docker cleanup (release tags v4.1.2 through v4.5.0)

- **"Delete unused volumes" deletes every volume no container references.**
  `docker volume prune -af`, no filter (v4.1.2:CleanupDocker.php:61-63,
  v4.5.0:166-168). Off by default, but it runs on the server's cron or above
  its disk threshold, and the API's run endpoint (4.3.0+) takes it as a
  per-request override (v4.5.0:ServerDockerCleanupController.php:280-287) - so
  never rely on the setting. Coolify's stop removes the containers, so the
  stopped source's volumes, and the target's between COPY and START, are
  exactly what it deletes.
- **Its container cleanup removes only `coolify.managed=true` containers**
  (`docker container prune` up to 4.4.3, `docker ps -a ... | xargs docker rm`
  from 4.4.4; v4.1.2:51, v4.5.0:211-216), and the helper reaper lists only
  running ones. A created, never-started, unlabelled container is therefore a
  durable reference: that is a pin. `docker rmi` without `-f` keeps the image a
  pin uses; `--pull never` because a fresh import can be pruned before the
  create.
- **A pinned volume survives Coolify's delete.** `docker volume rm -f` refuses a
  referenced volume and apps/databases swallow it (v4.5.0:Application.php:575,
  StandalonePostgresql.php:180). Every pin is released BEFORE anything Coolify
  deletes - FINALIZE under DELETE, the target delete in a rollback.
- **The source unpin is the LAST compensation** (`RELEASE_SOURCE_PINS`, owed
  from CREATE_TARGET). It waits for the restarted source to mount its volumes,
  and `/restart` redeploys with proxy labels built at job time - waiting any
  earlier would hold back the parked-domain restore until the source had
  deployed under the parked names.
- **The plan reads the setting off `GET /servers/{uuid}`**, whose eager-loaded
  `settings` carry `delete_unused_volumes`, `docker_cleanup_frequency`,
  `force_docker_cleanup` and `docker_cleanup_threshold` at every release
  (v4.1.2:ServersController.php:166-171, v4.5.0:218-224). The dedicated
  `GET /servers/{uuid}/docker-cleanup` exists only from 4.3.0. A warning, never
  a refusal; under KEEP/RENAME it says the stopped source is no fallback copy.

## Verify + ship

```bash
make all-checks                  # must be green
python scripts/generate-docs.py  # if you touched docs/*.template.MD
```

Conventional Commits, **past-tense** subject (`added`, `fixed`, `updated`), max
50 chars. **No AI attribution in commits, ever.**

## Where to look

| Question | File |
|---|---|
| What kind of resource is this, and does it build? | `domain/kinds.py`, `domain/compose.py` |
| Which volume maps to which? | `domain/naming.py` (`pair_by_mount_path`, `pair_by_name_suffix`) |
| What are we copying, and why not that? | `domain/manifest.py` |
| Will the rebuild ship different code? | `domain/drift.py` |
| What happens on failure? | `domain/statemachine.py`, `engine/executor.py` |
| Is it safe to start the target? | `dns/gate.py` |
| Is the stack really stopped? | `discovery/quiesce.py` |
| Which fields may we send? | `api/fields.py` |
| What can the target NOT reproduce? | `domain/fidelity.py` |
| Are scheduled tasks and backup schedules carried? | `api/schedules.py`, `domain/schedules.py` |
| Can a locked variable be carried? | `domain/locked_envs.py` |
| Is the data safe from Coolify's cleanup? | `discovery/pins.py` |
