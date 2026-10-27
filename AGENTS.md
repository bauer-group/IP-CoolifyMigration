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
  It is settable only on update. `create_service` sends it via a follow-up PATCH.
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
  root token (applications/services since 4.2, databases since 4.4). The target
  is created without it; a locked generated credential (`SERVICE_PASSWORD_*`,
  Redis `REDIS_PASSWORD`) would leave the target holding a different secret
  than the copied data, so the plan refuses those.
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
