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
   Stopped once is not stopped for good: from COPY to FINALIZE the containment
   checkpoints (`engine/containment.py`) ask both daemons again, and a breach is
   never a warning and has no flag — before START it rolls back, after it blocks.
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

- **Adding a Coolify API call?** Add its fields to the release registry,
  `domain/coolify_versions.py` (`CREATE_FIELDS` / `UPDATE_FIELDS`), first — each
  with the `Gate` of releases that accept it — and build the body with
  `api/fields.filter_for(source, endpoint, version)`. `$allowedFields` is
  enforced; unknown fields are a 422 per field. **Never** round-trip a GET
  response into a POST body.
- **A rule that holds only from (or until) some release?** Ask
  `coolify_versions.supports("name", version)` and add the name to `FEATURES`
  with its tag evidence. Never compare a version anywhere else. The version is
  the plan's (`MigrationPlan.coolify_version`, `ctx.coolify_version` at run
  time): read ONCE, at PLAN, so the plan's verdicts and CREATE's bodies always
  speak about the same release.
- **Handling a new resource shape?** Extend `domain/kinds.py`. Remember "does it
  build?" is NOT a property of the kind — read it from the compose (`build:` vs
  `image:`) via `domain/compose.py`.
- **Tempted to block on drift?** Don't. We build the target exactly as the source
  is configured and report what could still differ; whether that is compatible is
  the operator's judgement about their stack. Blocking belongs to things that are
  not judgements (a refused volume, live previews, DNS pointing at the source,
  or at something DNS cannot place).
- **A refusal that holds only under one finalize policy?** Put it on
  `ResourcePlan.kept_source_refusals` (warnings: `kept_source_warnings`), never
  in `refusals`: `run` and `plan --finalize` swap the policy onto a built plan
  (`model_copy`), so the plan derives these lines on every read
  (`MigrationPlan.blocking_reasons_of`). A refusal of the move as a whole goes in
  `MigrationPlan.plan_refusals`. `is_blocked`, the report and PREFLIGHT read
  `MigrationPlan.blocking_reasons`; never judge a plan by `ResourcePlan.is_blocked`.
- **Touching the transfer?** Every rsync flag in `transfer/rsync.py::BASE_FLAGS`
  is there for a reason documented in the module docstring. Removing one is a
  data-integrity change.
- **Touching quiesce?** It has no `--force` and must not grow one.
- **Touching QUIESCE's order?** The sources are disarmed and their queued or
  running deployments waited out FIRST, the deployment record read right after
  the wait (`containment.deployments_before_stop`), then the preview delete, the
  mount capture, the pins and the stop. A deployment that ends replaces the
  containers (facts below), so anything that names containers reads them after
  the wait, or the SIGKILL guard checks names that no longer exist — and after
  the record: one that ended between them would be recorded ENDED, reported by
  no checkpoint, while the capture and the pins name the container it removed.
- **Touching what can deploy a source or a target?** `domain/deploy_triggers.py`
  decides, `engine/deploy_triggers.py` performs. A target is CREATED with
  auto-deploy off (`TARGET_CREATE`) and without tags — CREATE only reads the
  source's tag names (`source_tags`), while every source still exists — and
  FINALIZE arms it, never fatally, with the source's original and attaches the
  tags. Never move either back into CREATE: a push or a tag deploy would start
  the target before the DNS gate (invariant 7). QUIESCE switches every source
  application's auto-deploy off (previews too, where read on), journalling the
  originals FIRST (`deploy_triggers`), and refuses before the stop
  (`SourceDeploymentPending`) when a source deployment does not end within
  `deploy_timeout`; it never cancels one. A kept or renamed source stays
  disarmed (`kept_source_notice`). `POST /deploy` by uuid or tag, manual, UI and
  MCP deploys have no switch; the plan says so (`run_note`).
- **Turning a switch on (or off) on a resource during the run?** Journal it
  BEFORE the write, and undo exactly that write. FINALIZE journals each target's
  uuid (`arm_requested`) and tag names (`tags_given`) before the request, and
  either marker makes a rollback owe FINALIZE's compensations even when FINALIZE
  never completed (`statemachine.targets_armed`). DISARM_TARGETS leads that
  tuple, ahead of STOP_TARGET: auto-deploy off (read first, a 404 is done) and
  exactly the journalled names detached (`deploy_triggers.tags_to_detach`), never
  a tag someone gave the target since. Switching back ON happens once per
  disarm: RESTORE_SOURCE_DEPLOY_TRIGGERS (owed to CREATE_TARGET, `_PRECEDENCE`:
  after RESTORE_SOURCE_FQDN, before RESTART_SOURCE) gives back only what the
  disarm took, journals `deploy_triggers_restored` per source and skips it on a
  replay until QUIESCE journals a new disarm record (`restored_since_disarm`) —
  RESTART_SOURCE's once-per-stop rule. `disarm_sources` journals that record
  again whenever it switches a source off again.
- **Adding or moving a step between QUIESCE and FINALIZE?** Two stacks must stay
  still while it runs; keep the `containment.assert_contained` checkpoints: the
  start of COPY (target), its end (both; the source alone when there is nothing
  to copy), the end of VERIFY, the DNS gate on every attempt, START before the
  first `/start`, and FINALIZE (source) BEFORE its policy is journalled — after
  it, `rollback_refusal` would answer a revived source under DELETE with "the
  source is deleted". The rules live in `domain/containment.py`. A check that
  cannot be answered (any daemon read over SSH — a listing, an inspect, the
  event log — or a deployment history) never passes. Ask every read through
  `engine/containment._Reads`, which notes a failure and lets the check go on:
  a read that raises past it is a rollback after START.
- **A breach?** Before START asked for any target the run rolls back
  (`SourceRevived`, `TargetStartedEarly`; a target found started is journalled
  `started_early`, so STOP_TARGET is owed, `statemachine.target_may_run`), and a
  read that got no answer raises its own error. Once START asked
  (`start_requested`, or START completed), a revived source BLOCKS
  (`SourceRevivedAfterStart`) and so does a check that could not be finished —
  an unread deployment history, a daemon read that failed, on either side
  (`SourceUncheckedAfterStart`, nothing journalled): the executor rolls nothing
  back, because the target may hold users' writes. A breach is raised first,
  with what could not be read beside it, and journals the revival clock only
  when every source read answered. Judge revivals from `stopped_clock`, the stop
  gate's proof (`containment.judged_from`), never from the stop request.
- **Stopping a target in a rollback?** End its deployments first
  (`compensations._settle_target_deployments`): an application's `/stop` cancels
  none, and DeleteResourceJob cancels only a preview's.
- **Touching a domain of a source or a target?** Make-before-break, always: a
  custom hostname must have a live holder of ours at every instant. The target
  claims it first (scoped `force_domain_override`, `domains.only_own_holders` —
  the ONE predicate), the source lets go only at FINALIZE as its last mutation,
  and a rollback restores the source BEFORE it deletes a target holding the host
  — deleting the last holder is what makes Coolify (4.4+) delete a managed DNS
  record. Never park, blank or delete first.
- **Leaving a volume unmounted across a step?** Pin it (`discovery/pins.py`)
  first. Unmounted is what Coolify's `docker volume prune -af` deletes. Never
  flip the server's cleanup setting instead: a crash leaves it flipped, and a
  manual API run overrides it anyway.
- **Touching a bind mount?** `domain/binds.py` decides; `transfer/binds.py` only
  asks the servers and writes through fixed scripts that take the path as a
  quoted argument. A bind is copied to the host path the TARGET's own storage
  row names at the same mount path, read back after CREATE (invariant 10).
  Never skip a directory in the resource's own
  `/data/coolify/{applications|services|databases}/{uuid}` as "regenerated":
  Coolify only `mkdir -p`s it. Every pair COPY writes first
  passes `binds.pair_problem` — a journal from 2.14.0 or earlier paired writable
  `/etc/...` path-to-path, and a resumed COPY reloads it — and only into a target
  path that is absent or empty. COPY journals each target (`bind_target`) BEFORE
  it touches it; a rollback deletes only what that record says this run
  created, only where `binds.removable` allows, each removal journalled the
  moment it is gone (`binds_removed`). The record also makes COPY owed when COPY
  failed (`statemachine.owed_states`). A resumed COPY or VERIFY reads the pairs
  from DISCOVER's journal (`steps._bind_pairs`): an empty `ctx.bind_pairs` is
  never "no binds". "Nobody else mounts it" must be a fact: `host_binds` fails
  closed except on containers gone between `docker ps` and the inspect
  (`vanished_only`). A writable bind of `/` is a host agent and only a warning
  (`binds.host_wide_writers`); every other writable parent refuses. Two
  fail-closed limits are pinned in `tests/test_binds_limitations.py`; loosening
  either is a decision.

## Verified upstream facts (do not re-derive)

First checked against `coollabsio/coolify@main`; re-checked against the release
tags v4.1.2 through v4.5.0 (2026-10-26), v4.5.1 against v4.5.0 (2026-10-28),
v4.6.0 against v4.5.2 (2026-11-05), v4.7.0 through v4.7.2 against v4.6.0
(2026-11-09), and v4.7.3 through v4.7.5 against v4.7.2 (2026-11-10).
Read upstream at a **release tag**, never `main` — see the 2.5.6 tags regression.
What differs between releases lives in `domain/coolify_versions.py`, not here.

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
  (Kernel.php v4.5.0:61-62, v4.7.0:65-66): an empty string we send arrives as
  `null`. From 4.5 an empty and a null env value render differently, so "send
  `""`" is never a fix. From 4.7.0 an env `value` / `data.*.value` is exempt from
  TrimStrings (TrimStrings v4.7.0:18-20) and the model trims instead: a
  single-line value loses its edge whitespace, a multi-line one is kept
  byte-exact (`withoutEdgeWhitespace`, EnvironmentVariable v4.7.0:889-899). Every
  other string, task names included, is still trimmed.
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
  (`steps._refuse_multi_server`) before creating anything.
- **Scheduled tasks** exist for applications and services only (no database
  route or column), readable and creatable from v4.1.2 with one six-field array
  (`ScheduledTasksController` v4.1.2:57 = v4.5.0:81) through 4.6.x; `use_shell`
  joins it in 4.7.0 together with its column (v4.7.0:81,89,128). An absent
  `enabled` or `use_shell` defaults to TRUE, so a disabled task is always sent
  `enabled: false` and a task without a shell `use_shell: false`. From
  4.4.0 `command` is hidden without can_read_sensitive and `timeout` is
  `max:36000`; from 4.5.0 an ENABLED task needs `deploy`, and `container` also
  matches a full container name (it embeds the source uuid, so the plan warns
  instead of rewriting it, invariant 10).
  The scheduler skips a resource whose status does not contain 'running'
  (`ScheduledJobManager` v4.5.0:670-681), and a never-started target reads
  'exited'. That skip is the only thing keeping carried tasks inert before START.
  The UI's "execute now" and `artisan` RunScheduledJobsManually bypass it, and
  before 4.5 the job's app branch also sees stopped containers, so never call
  either. A git-compose app takes its sub-service names from git HEAD (a template
  service now runs the source's compose), so CREATE checks each task's container
  against the target. A POST retried on 502/504 can save a task twice, so CREATE
  re-reads and counts the uuids the creates returned, never names: the API trims
  every string (TrimStrings) while Livewire skips it, so a UI-typed "nightly "
  arrives as "nightly". Deleting the target deletes its tasks (model hooks).
- **Database backup schedules** (`/databases/{uuid}/backups`, every release)
  create through `$backupConfigFields` (v4.1.2:704, v4.5.0:1057).
  `missing_backup_notification_days` arrives with its column (4.3.18/4.4.0) and
  `s3_storage_uuids` in 4.5.0, so key presence gates both. Up to v4.5.0 the GET
  filters on `database_id` only (v4.5.0:447): another engine's rows with the same
  hidden id come back too. From v4.5.1 every backup route also filters on
  `database_type` (fdb77415a, not backported). We filter on `database_type`
  ourselves on every release (the model FQCN; there is no morph map). It
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
  (4.3.0+, PUT/run/DELETE on `.../storages/{uuid}/backups`, no GET) get one plan
  note naming every resource with a volume, gated on 4.3.0
  (`MigrationPlan.volume_backups_possible`).
- **Rollback deletes a target's backup ROWS, never its dump FILES.**
  DeleteResourceJob removes the rows (v4.5.0:167) and leaves the dumps. Never
  send `DELETE .../backups/{b}?delete_s3=true`: `rollback` also runs after a
  SUCCEEDED run (it has no is_finished guard), and then those dumps are the last
  copy of post-cutover data. That holds for DATABASE dumps only: the same job
  deletes every VOLUME-backup archive, local and on every backup storage it was
  uploaded to (S3; from 4.7.0 also SFTP, FTPS and WebDAV, which share the
  s3_storages table: HasS3Replicas v4.7.0:26-47), of the resource it deletes
  (`deleteScheduledVolumeBackups`, v4.3.0:49,123-137, v4.5.0:125,188-209). So
  FINALIZE DELETE destroys the source's volume-backup history; the plan says so
  under a DELETE policy and the typed confirmation repeats it.
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
  (Application.php v4.3.0:280), so FINALIZE's release erases them; the update
  applies them after the fqdn (v4.3.0:3115-3120), so the journalled restore body
  carries them.

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
  And because the containers are gone, the source is DOWN as soon as a stop was
  requested — even if QUIESCE then fails (SIGKILL refusal, gate timeout) and never
  completes. QUIESCE journals `stop_requested` before the request; the rollback
  owes RESTART_SOURCE from it (`statemachine.source_was_stopped` — reuse it, never
  `"quiesce" in completed`).
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
  routing. Releasing the source's domains at FINALIZE erases the override on the
  source too, so a rollback after FINALIZE restores the portless value.
  **Compose domains followed on the 4.3 line, not in 4.4.0**: a UI save from
  v4.3.15 (Livewire Application/Domains.php:1995-2011), an API save from v4.3.18
  (`normalizeDockerComposeDomainPorts`, ApplicationsController v4.3.18:1459/3379,
  v4.4.0:6372, v4.5.0:6535-6551). Tags are not linear — v4.3.24 shipped after
  v4.4.0 — so gate on the release that introduced a thing, never on "4.4". The
  parsed `docker_compose` labels are the only witness, and
  `fidelity.compose_hidden_port_overrides` reads them; where a portless domain
  falls back moved twice (`ports_exposes` on 4.3.15, the service's first port of
  any protocol on 4.3.16-4.3.17, its first tcp port from 4.3.18), so the check
  takes the version. A port that is not that fallback is refused.
- **The parking tag is stable per project/environment** (`domains.parking_tag`,
  the migration id's digest). Versions up to 2.9 parked a source's custom hosts
  under `old-<tag>.` at CREATE; a source still on such hosts — an un-park that
  failed — is refused at plan time: a new run would carry the parked host to
  the target and journal it as the one to restore.
- **Env rows saved before 4.5 keep the legacy escaping; every row we create is
  exact** (`uses_legacy_escaping`, hidden, unsettable). The row's `version` field
  tells which is which. Values with `\ " '`, a ` #` tail or quoted literals reach
  the container differently on the target.
- **A template service's target runs the SOURCE's compose.** The `type` create
  builds it from the instance's CURRENT catalog, which the CDN bundle changes for
  every instance whatever its release (config/constants.php v4.7.0:46) — 95
  templates changed between 4.3.10 and 4.5.0, and v4.7.0's moved getoutline from
  postgres:12 to 16 on the same key and mount, so pairing, copy and verify passed
  and the target refused the data after the stop. So once its uuid is journaled,
  and before anything reads its part names, env rows or volumes, one
  `PATCH /services/{target}` carries only the source's `docker_compose_raw`
  (`resources.carry_source_compose`); the update re-parses on every release
  (ServicesController v4.1.2:996-1046 .. v4.7.0:1287-1343), a same-named part keeps
  its row and file contents, and without `urls` no updateCompose runs. CREATE still
  rehearses the volume pairing while the source serves, as the read-back proof;
  services pair by compose volume key (`{uuid}_{key}`), like compose apps. PLAN
  refuses a service of either kind whose GET returned no compose. Not carried: an
  operator's edited FILE content in a template part (the PATCH keeps the catalog's
  content found by mount path, and the source's raw compose has `content:` stripped).
- **DELETE needs `write` AND `deploy` on 4.5, and its own `delete` from 4.6**
  (rollback deletes targets); the token docs say root, or `read` +
  `read:sensitive` + `write` + `deploy` + `delete`. `read:sensitive` does not imply
  `read` (`tokenCan` matches exactly, and 110 routes need `read`, routes/api.php
  v4.6.0).
- **Before 4.3.11 a DELETE only queues DeleteResourceJob** (`delete_by_uuid`,
  v4.1.2-v4.3.10): the row — and every host it holds — lives on until the job's
  SSH cleanup is done, so a claim seconds later 409s on it. From 4.3.11 the row
  is soft-deleted first. Hence a resumed CREATE counts the targets it just
  deleted as its own (`steps._undo_interrupted_attempt` returns them).
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

### Learned from the v4.6.0 -> v4.7.2 audit (release tags; e2e green on a live 4.7.2)

The canaries went red on five application create fields and one task field; the
release-tag audit (276 changed files in the paths this tool reads, most of them
hidden in two squash commits, #12478 and #12460) found the rest. v4.7.1 differs
from v4.7.0 in 24 files (maintenance-mode routing, image-scan fixes), v4.7.2 from
v4.7.1 in 27 (a scanner memory setting, UI destination actions limited to the
application's own servers, the root setup window): neither touches this tool.
Everything below starts in v4.7.0 unless it says otherwise.

- **Four application settings join APPLICATION_SETTING_FIELDS** (ApplicationsController
  v4.7.0:55-56,60,83 -> create :1613, update :3662; nested under `settings` in the
  GET): include_network_hosts_in_build, is_auto_cancel_{queued,building}_deployments_enabled,
  is_image_scan_enabled. Carried with `read_from="settings"`, `column_gated`. The last
  is not cosmetic: it decides whether the deploy scan gate applies (below), so a
  source's opt-out must never be gapped as "the default is as protective".
- **Gitea App sources stay refused** (`planner._resolve_git_auth`). 4.7.0 adds
  POST /applications/private-gitea-app (routes/api.php v4.7.0:266); `gitea_app_uuid`
  is unset before the fill (api.php :522), so no GET carries it (KNOWN_APPLICATION_GAP).
  If ever wired: GET /gitea-apps returns client_secret, access/refresh tokens and the
  webhook secret in clear (GiteaController v4.7.0:14-24) — read id/uuid only.
- **Names and descriptions pass the UI's ValidationPatterns on every API create and
  update** (32541a32c; ApplicationsController v4.7.0:1616-1617/:3665/:3669,
  ServicesController :421-422/:647-648/:1253-1254, DatabasesController :2252-2253/:664-665,
  ValidationPatterns :13,:18,:496-532; `string|max:255` up to 4.6.0). The rule sees
  the value after `Str::trim` (wider than PHP's trim: `\s` under UCP, 56 invisible
  characters, NUL) and PHP's `/u` sets UTF **and** UCP. `fidelity.name_rule_problem`
  mirrors it; a bad NAME refuses at plan for every kind (`api_name_rules`, unknown
  refuses) and is never sanitised — PREFLIGHT, HEALTHCHECK and discovery find the
  target by `slugify(source name)`. A bad DESCRIPTION is left out and warned about.
  Livewire skips TrimStrings, so a UI-saved `"ab "` passes the UI and fails the API.
  FINALIZE RENAME starts from the name trimmed at both ends and is cut to 255
  (`fidelity.renamed_source_name`) — a 245+ character name 422'd the rename after
  the cutover on every release.
- **An accepted application `/start` or `/restart` is a queued deployment, not a
  deploy** — 200 with a `deployment_uuid` even when the queue was full and nothing
  was queued (applications.php v4.1.2:40-44, v4.7.0:58-63). `GET /deployments/{uuid}`
  (`read`) and its five statuses are identical v4.1.2-v4.7.2, so the watch is
  UNGATED and a 404 means "no such row". START journals each uuid; HEALTHCHECK fails
  on a failed/cancelled/missing deployment, but only once every other target's start
  has landed (`steps._every_other_start_landed`): the rollback's `/stop` cancels no
  deployment and runs on the 'high' queue while starts wait on deployment_queue(), so
  an early stop is overtaken and the sibling comes up routing the claimed hosts.
  RESTART_SOURCE counts an application as restarted only when its deployment
  FINISHED; failed/cancelled/missing raises (ROLLBACK_FAILED, pins kept), pending at
  deploy_timeout keeps the old answer with a notice, and the uuid is journalled on
  accept so a resumed rollback watches it instead of redeploying
  (`compensations._restart_stands`). `client.get_deployment` keeps `status` and
  `scan_gate_result` only — `logs` is the full deployment log (`deployments.read_deployment`).
- **Every application deploy passes Coolify's image-scan gate** — the target's first
  `/start` and the rollback's `/restart` alike (ApplicationDeploymentJob
  v4.7.0:3040-3043, :994; DeploymentImageScan :890-892). Off by default (instance
  policy seeded off/deploy, migration 2026_10_29_100000:35-44); the mode resolves
  instance -> team -> server -> app and no API reads it. A new target has no
  baseline, so under On every exploit-signal finding is new (SecurityGate
  v4.7.0:15-19); Strict, or "If the scan cannot run: Block", can block the source's
  restart too. A self-hosted v4.7.0 cannot scan at all (fixed in v4.7.1). The plan
  warns (`deployments.image_scan_gate_warnings`, `deploy_image_scan_gate`); never
  write the policy or scanning. "Deploy anyway" is UI-only and queues a NEW
  deployment: after it, `coolify-migrate unpin`, never a second `rollback`.
- **Scheduled tasks: `use_shell`, and Coolify disables failing tasks.** See the
  scheduled-task fact for `use_shell`. After 10 failed runs with the first >= 24h old
  Coolify sets enabled=false and `disabled_reason` (ScheduledTask v4.7.0:99-114),
  which no array takes; the carried enabled=false is exact and the plan names such
  tasks.
- **A template service's target runs the SOURCE's compose** (next fact). Its `type`
  create still writes the CURRENT catalog's env rows, and the re-parse deletes only
  keys starting with `$` or ending with `}` (parsers.php v4.7.0:2466-2475), while
  every row reaches every container through `env_file: .env` (:3664-3670) — v4.7.0's
  litellm added `LITELLM_SALT_KEY`, which would stop the source's stored credentials
  decrypting. CREATE deletes each target row whose key the source lacks
  (`fidelity.leftover_template_envs`), right after the compose PATCH.
  Before 4.7.0 a catalog-only PART stays too, unflagged and with no container, so the
  started target reads degraded and the scheduler skips every task of it
  (ScheduledJobManager v4.6.0:677-678): CREATE refuses when the source has a task
  Coolify would run (`schedules.armed_task_names`), else a run notice names the
  parts. Never PATCH such a part (e.g. exclude_from_status): UpdateServiceApplicationFromApi
  ends in updateCompose, which writes a phantom `services.{name}.image`.
- **Parts that leave a service's compose are flagged, not deleted**
  (`is_removed_from_compose`, parsers.php v4.7.0:2425-2438, e29dcf70d). They keep
  their fqdn and storages, stay in every GET and every domain holder set, and have no
  container. Read by key presence (`kinds.removed_from_compose`); never carried in
  `urls`; PLAN refuses one holding a custom host or a migrated volume no live part
  declares (`fidelity.removed_part_refusals`).
- **4.5.0-4.6.x split a dotted compose service name on every URL update**:
  updateCompose writes `services.{$name}.image` through a dotted path (services.php
  v4.6.0:397-399,:411) and its Stringable `!==` always runs; the urls PATCH reaches it
  from 4.5.0. The target then fails at START, after the stop. Fixed in 4.7.0
  (05e52fc41). `service_url_patch_splits_dotted_names` Gate(4.5.0, 4.7.0) refuses a
  dotted sub-application holding a non-generated host; a rename alone is no way out
  (the old part keeps its domain).
- **Env values skip TrimStrings** (see the TrimStrings fact): `" "` arrives and is
  stored as `""`, so `build_env_entries` sends it for an exactly empty value on a
  KNOWN 4.7+ (`env_value_untrimmed`) and the empty-value warning stops there; a
  preserved null still warns. A multi-line value keeps its edge whitespace.
- **Legacy env rows written exact** (multi-line JSON from 4.7.0, HasSecretManager
  v4.7.0:179-192; quoted literals from 4.6.0): `locked_envs.written_forms` accepts the
  exact form for every legacy dotenv row, ungated (no legacy form starts with an
  unescaped `"`), and the plan's old-escaping warning stops for them
  (`legacy_unreadable_line_exact`, `legacy_json_multiline_exact`).
- **A lock is permanent** (EnvironmentVariable v4.7.0:182-186). Every remedy says
  "delete and re-add it unlocked with the same value", never "unlock".
- **`{{server.X}}` reads the shared variables of the server the resource runs on** —
  in env rows since 4.1.2 (EnvironmentVariable v4.1.2:323-330), in every compose
  environment/build.args value from 4.7.0 (shared.php v4.7.0:6011-6012). We copy the
  raw value, so the target reads the TARGET server's X; the plan warns with names
  only (`compose_resolves_server_shared_vars`). GET /servers/{uuid}/envs is not read:
  its rows carry values.
- **Plan warnings for state no API carries**: a dockercompose app's security-update
  `docker_compose_image_overrides` (applied while the compose still names `from`,
  parsers.php v4.7.0:2305-2308); `git_ssh_known_hosts` (git host keys pinned per app,
  trusted on first use; the target trusts on first contact — host, type and SHA256
  only, never the key); a maintenance route, a proxy file neither the stop nor the
  domain release removes.
- **The shared image-name check skips restarts** for nixpacks, static and railpack
  (ApplicationDeploymentJob v4.7.0:2730-2741, :320-323), so `shared_image_refusals`
  refuses only a dockerfile source from 4.7.0 (`registry_image_check_skips_restarts`).
- **A token is blocked for 60/300/900 s after 30 counted failures in a minute**, by
  ANY client using it (ThrottleFailedApiRequests v4.7.0; 401/403, and 404 only without
  a token). Every request then gets 429 "Too many failed API requests." before the
  controller. `api/client.py` logs `api.token_blocked` and keeps waiting out
  Retry-After — a rollback needs the same token.
- **Coolify edits authorized_keys under its own lock** (AuthorizedKeys::runLocked
  v4.7.0:164-182, `$HOME/.ssh/.coolify-authorized-keys.lock`); `engine/keys.py` takes
  the same lock around the ephemeral-key append and revoke, ungated.
- **Coolify pins every server's SSH host key, its own included** (learnHostKey
  v4.7.0:731-783; `ssh_host_key_pinning`). F2 moves the pin, not the key: the F2 plan
  warns (`domain/host_keys.py`). Under enforce the relocated instance cannot reach its
  own host until the pending key is trusted. Never POST `/host-key/reset` empty: that
  drops the pin and Coolify trusts the next key it sees.

- **API creates queue DNS records for the target's OWN hosts** (see the managed-DNS
  facts). The record points at the target server's first public address
  (ResourceDnsRecords v4.7.0:50-53,158-181), so a DNS-only record reads READY; with
  the token's opt-in proxied DNS (CloudflareDnsProvider v4.7.0:239) it resolves to
  Cloudflare and the gate reads ELSEWHERE after the stop. The plan warns when the
  target's wildcard base lies in exactly one team zone (`dns/managed.target_records_note`,
  zones from GET /dns/zones, v4.6.0+, whose rows carry no proxied flag).
- **Registry logins are per server, and no API copies them** (pre-existing, found
  by this audit). An application's deploy mounts only the SSH user's
  ~/.docker/config.json into its helper (ApplicationDeploymentJob v4.7.0:3392-3418),
  so a login made with sudo does not count; host-side pulls use
  serverDockerConfigPrefix from 4.5.1. CREATE starts nothing, so a private image's
  first pull on the target is START, after the stop. From 4.4.0 GET
  /servers/{uuid}/registries (read:sensitive + team admin) lists the logins; the plan
  compares both servers (`server_registry_logins`, `fidelity.registry_login_warnings`,
  names only — `client.get_server_registries` drops username, source and used_by).
  A warning, never a refusal: a public image needs no login.

Unchanged, and re-read at the tags because everything here depends on it: the
three Stop actions still `docker stop` + `docker rm -f` (they only add
`NotifyResourceStoppedJob::expectStop`), StopApplication still filters
`pullRequestId=0`, StopService still names containers from DB rows (removed parts
included); `defaultLabels`/`defaultDatabaseLabels` and `Str::slug` (Laravel
12.70.1 at all three tags); volume names, the Postgres >= 18 mount rule and
POST /storages naming; the `/start` "already running" guard and `/restart`'s lack
of one (`running:starting` still contains and starts with `running`);
DeleteResourceJob; CleanupDocker (`volume prune -af` unfiltered, container cleanup
only for `coolify.managed=true`); the ScheduledJobManager and DatabaseBackupJob
`running` skips; the DNS release path (ManagedDnsRecordCleanup, holders check);
the env-file encoders and GENERATED_MAGIC_VARIABLE_COMMANDS; the domain PATCH /
deploy-bakes-labels split.

Nothing to answer:

- **New default database images** (postgres:18-alpine, valkey/valkey:9-alpine for
  Redis, mongo:9, mysql:9.7, mariadb:12.3, clickhouse 26.8; migrations
  2026_11_06_083240, 2026_11_06_113843) apply only to a create without `image`.
  CREATE always pins the source's image, which every GET carries. Valkey is an
  image of the redis engine (StandaloneRedis), not a new engine.
- **The Redis health check pings with the password** (878d77bd6), so the
  password also sits in the database compose's `healthcheck.test`; the
  `environment:` list locked recovery reads is unchanged.
- **Superseded-deployment cancellation** applies to webhook deployments only;
  API deploys, `/start` and `/restart` cancel nothing (applications.php
  v4.7.0:196-203).
- **Access hardening** (placeResource, useTeamCredentials, nested server secrets
  limited to team-wide tokens, ValidGitRepositoryUrl on a changed repo, rollback
  and preview restrictions, deploy tag validation) all pass for the team-admin,
  team-wide token this tool already requires; no PATCH we send carries
  `git_repository`.
- **Servers may share an IP with different SSH ports**
  (Server v4.7.0:2097-2104, ServersController :637-639, :836-842). Nothing
  assumes a unique IP: known_hosts, the probe and the tunnel use ip+port, and
  identical address sets fail closed through the shared-address rule
  (`dns/gate.classify`, every custom host ELSEWHERE).
- **`RejectPublishedAppKey`** (a global 503 on a published APP_KEY) was added and
  removed again before the v4.7.0 tag (c75e7078b, 61b10664c): in no release.
- **A deployment can outlive `deploy_timeout`** on every release
  (`dynamic_timeout` 3600 s against our 900 s), and a rollback never cancels it;
  4.7's opt-in scan gate is one more source of delay, not a new class.
- **`GET /servers/{uuid}/validate` answers 405** ("changed to a POST request")
  since at least 4.6.0 (routes/api.php v4.6.0:199-200). Only the e2e rig called
  it, now with POST.

### v4.7.2 -> v4.7.5 (release tags): nothing to answer

Sixteen commits, 34 files in the paths this tool reads; no API controller, route,
Stop/Start action, label, volume name or DeleteResourceJob change. The deploy
job's environment changes sit in its pull-request branch only
(ApplicationDeploymentJob v4.7.5:2185-2205, `previewGetsProductionRuntimeFallback`):
production's `.env`, which locked-variable recovery reads, is written as before.
The rest is image-scanner limits (new `server_settings` columns, ServerSetting
v4.7.5:117-127, never carried), the instance database hidden from
`queryDatabaseByUuidWithinTeam` (shared.php v4.7.5:1834-1847), failed backup
executions pruned after the retention days (CleanupDatabase v4.7.5:51-53; the
client drops executions anyway), IPv6 SFTP hosts and the token page. Canaries
green against v4.7.5. `VALIDATED_MAX` = 4.7.5.

### Learned from the v4.5.2 -> v4.6.0 audit (release tags; e2e green on a live 4.6.0)

The canary went red on two create fields. Reading the release found what it cannot
see. v4.5.3 changed nothing this tool depends on. Stop/start/restart, labels, volume
names, DeleteResourceJob and the release of DNS records are unchanged.
`tests/e2e/test_v4_6_contract.py` asks the real thing about the image name, the DNS
route and the hidden password.

- **A building application's `docker_registry_image_name` is unique in the team**
  (`otherApplicationBuildingSameImage`, Application v4.6.0:1123-1137; dockerimage
  and dockercompose are exempt). Every create 422s on it (ApplicationsController
  :1699,1962,2194,2345) and every deploy fails, `/restart` included up to 4.6.x
  (ApplicationDeploymentJob :380,:2588-2594; from 4.7.0 a nixpacks, static or
  railpack restart skips the check, see the 4.7 audit). The source still holds the name, so
  CREATE leaves it and the tag out and the plan reports that: the target builds
  locally and pushes nothing. Two cases are refused, because they cannot deploy: a
  deployment-only target (`server_refusals`, it needs a name), and a source whose
  name another application already builds (`shared_image_refusals`: a rollback's
  `/restart` would be accepted and leave the source down).
- **`git_https_username`/`git_https_password` cannot be carried.** The password
  is hidden from the resource GET (`removeSensitiveData` :128-143 does not make it
  visible), and the username 422s without it (:1687). A username refuses, because
  the deploy fails after the stop; a custom-host clone without one warns, because
  a password alone is allowed and invisible. Do NOT read the password off
  `/resources` or `/projects/{uuid}/{env}`, though both return it in clear to a token
  that can read sensitive data (seen on a live 4.6.0): `exposeSensitiveFields` makes
  every hidden attribute visible there, on purpose (api.php :173-177), against the
  field's own "never returned". Such a token reads every env value anyway, so this
  is no hole, and no contract to build on either. `environment_resources` reads that second route,
  so its rows are used for uuid, name and type only and never kept; the resource
  itself always comes from `GET /{collection}/{uuid}`.
- **An API domain save can now CREATE a DNS record** (next section).
- **A legacy env row can be written exact** (`useExactEscaping`, HasSecretManager
  :153-171): a compose reference like `FOO=${FOO:-}`, or a literal holding a quote.
  Its `real_value` is then the resolved value (EnvironmentVariable :310-314), and
  the file holds `"value"`. `locked_envs._stale` accepts that form, and from the 4.7
  work `locked_envs.written_forms` accepts it for a LOCKED legacy dotenv row too, so
  such a row goes through the same byte-for-byte round trip and is read back.
- **DELETE routes need `api.ability:delete`** (routes/api.php). The migration
  2026_10_25_100000 gives it to every token that has `write`; a NEW token must
  select it. Stop, start, restart and the writes this tool uses keep their abilities.
- **Tokens can be limited to projects** (`scopes`, ApiAbility :11-27): such a token
  gets a 403 on every team-level route, `/servers` and `/security/keys` included,
  so this tool cannot run with one, and the 403 hint says so. A conflict with a
  resource it cannot see has `resource_uuid: null`, which is never "ours".
- **Nothing to answer:** `ports_mappings` validation only got wider; `is_preview`
  left the service env docs, not the arrays; a new service's null row takes its
  compose default when a bare reference exists (`fillDockerComposeDefaultsForBareReferences`),
  and `copy_envs` overwrites any row with a value.

### v4.5.1 -> v4.5.2 (release tags): nothing to answer

The free-form command and configuration attributes of applications and
databases (build/start/health-check commands, `custom_docker_run_options`,
`custom_nginx_configuration`, `*_conf`) are readable only with `read:sensitive`
from 4.5.2 — like `value` before them, they would VANISH (HTTP 200) for a token
without it, and filter_body would silently drop them. The client asserts the
scope at startup, so a migrating token always sees them. Everything else in the
release (proxy retry labels removed, preview production values, webhook secret
permission) does not touch this tool. `VALIDATED_MAX` = 4.5.2.

### Learned from the v4.5.0 -> v4.5.1 audit (release tags, not yet a live run)

Every item is a registry row or FEATURE gated on 4.5.1 (`domain/coolify_versions.py`);
older releases get exactly what they got before.

- **Four create fields, one backup field.** `dockerfile_target_build` joins the
  shared application create array (carried on every route that builds a
  Dockerfile); `include_coolify_default_variables`,
  `is_container_label_readonly_enabled`, `rolling_update_drain_seconds` join
  APPLICATION_SETTING_FIELDS — the GET nests them under `settings`, the create
  takes them flat (`read_from="settings"`); `disable_local_backup` joins the
  backup create array but 422s without `save_s3`.
- **Readonly labels OFF keeps `custom_labels` verbatim** — create and update
  stop regenerating them, and the deploy uses them whenever set. Carried only when
  they route nothing but the app's own custom domains
  (`fidelity.labels_route_beyond_domains`): a server-bound or generated host in
  them would make the target route, and request a certificate for, a host that
  still resolves to the source (invariant 7).
- **Settings no create takes ride follow-up PATCHes**: a service's and a
  database's `include_coolify_default_variables`, a database's
  `is_log_drain_enabled` (422 unless the server has a log drain), and a service
  sub-application's `redirect` (422 for www/non-www unless the sub-application
  already holds both hosts — so after the URLs).
- **A locked credential's connection URL is NULL** (DatabasesController
  :262-270): a locked Redis REDIS_PASSWORD comes from the compose file alone.
- **The parsed `docker_compose` is redacted**: every locked value of 4+ characters
  becomes `<locked>`, labels included. A domain'd service's labels, expose or
  ports carrying it make the hidden-port check unknown (a warning), never clean.
  `docker_compose_raw` is not redacted.
- **An enabled Coolify health check replaces the Dockerfile's HEALTHCHECK** on
  every deploy — the source's next deploy and a rollback's /restart included.
  `custom_healthcheck_found` is in no create array: warned about on every release.
- **No preview twin for a dockercompose app's generated magic variables**, and
  twins copy `is_shown_once`. The locked twin CREATE sent ahead of a recovered
  secret is skipped there (only for a KNOWN 4.5.1).
- **Upstream's list of generated magic variables** (GENERATED_MAGIC_VARIABLE_COMMANDS)
  is the same switch every release since 4.1.2 runs; `target_regenerates` mirrors it.

### Service domains (release tags v4.1.2 -> v4.5.0)

- **A service's domains live on its sub-applications** (`applications[].fqdn`);
  the service has none. The proxy labels are generated from that column
  (parsers.php v4.5.0:3389), so it is what CREATE carries — with ONE
  `PATCH /services/{target}` `{"urls": [{name, url}]}`, the only write path valid
  on every release (items are exactly `{name, url}`, ServicesController
  v4.1.2:971, v4.5.0:1259; the per-sub-application PATCH exists only from 4.2).
- **Read `url` and `fqdn` per comma part, never one wholesale.** From 4.3.15
  the port of a saved domain moves into the hidden `domain_port_overrides`; the
  model appends `url`, which re-composes it (ServiceApplication
  v4.5.0:18,215-242). But on 4.3.15-4.3.17 that accessor returned the
  port-STRIPPED canonical for a domain without an override entry — a row saved
  before 4.3.15 keeps its port inline in `fqdn` (no data migration; the proxy
  keeps routing to it) — fixed in 4.3.18 (`return $url`). So the `fqdn` part
  wins when it names a port, the `url` part otherwise
  (`resources._routed_url`). Generated sslip hosts name the SOURCE's uuid and
  IP and are never carried.
- **Nothing is parked — neither services nor applications.** Every `PATCH /services`
  re-parses the compose before it applies `urls` (v4.5.0:1338), and on 4.5
  `applyServiceUrls` rewrites the SERVICE_URL_*/SERVICE_FQDN_* rows at once
  (`updateCompose`, v4.5.0:214) — so parking would rewrite a LIVE source. Instead
  the target claims the hosts with `force_domain_override`, and only when every
  `conflicts[].resource_uuid` of the 409 is ours: the plan's sources, their
  sub-applications, this run's targets and theirs (that field is the holder's
  uuid, `null` for the instance's own domain: domains.php v4.5.0:250-335). Any
  other holder is refused before the stop, named by type and uuid (an orphaned
  target carries the source's NAME). The target is read back and must hold every
  custom host. FINALIZE releases the source's custom hosts (url `""` -> null)
  under KEEP as well as RENAME, as its last mutation: a kept source holding the
  target's host is a second router the moment someone restarts it.
- **The conflict check is port-blind against a portless holder on 4.3.15-4.6.x:**
  the request key keeps its port, the stored fqdn lost it, so a port-bearing URL
  never 409s against the same host saved without one. From v4.7.0
  `domainConflictKey` drops the port on BOTH sides (domains.php v4.7.0:76-86,
  53e21abe9), so the same host and path on any port, or none, 409s; paths still
  differ. Every claim and restore already handles a 409 through
  `only_own_holders`, so an outside holder on another port is now refused at
  CREATE, before the stop, like an exact-URL holder always was. Do not rely on a
  409, or on its absence, either way: the read-back is what proves a claim.
- **From 4.4 a removed hostname queues `ReleaseRemovedDnsHostnamesJob`**, which
  deletes a Coolify-managed DNS record no live resource uses (v4.5.0
  ServicesController:125-145). Removing a host from a live source before the
  target holds it can delete its record; at FINALIZE the target already uses it
  (next section).
- **A restore must not write a source that already holds its originals**
  (`restore_service_urls` GETs, compares, PATCHes only the difference): the same
  re-parse and `updateCompose` apply to it.
- **SPECIFIC_SERVICES images are routed from plain env rows, not from fqdn.**
  minio, logto, garage (and silo from 4.5) go through
  `generateServiceSpecificFqdns` (parsers.php v4.1.2:2527-2528, v4.5.0:3386-3389;
  docker.php v4.5.0:660-760): `MINIO_SERVER_URL`, `LOGTO_ENDPOINT`,
  `GARAGE_S3_API_URL` and friends (`dns/extract.py::SPECIFIC_SERVICE_ROUTE_KEYS`)
  — but ONLY when the sub-application's image, up to its first `:`, is in
  `SPECIFIC_SERVICES` (`ServiceApplication::serviceType`, v4.5.0:171-181,
  constants.php :102-111). For any other image a `LOGTO_ENDPOINT` is a client's
  setting: never gated, never dropped from the copy (`extract.route_env_keys`).
  The gate reads those keys, each sub-application's domain and a custom
  compose's own Host() labels — never the derived SERVICE_URL_*/SERVICE_FQDN_*
  rows. FINALIZE blanks a route row holding a custom host (Coolify regenerates
  an empty one, docker.php v4.5.0:680-688), and the restore body journals it
  as `route_envs: [{key, url}]` — `url`, because the journal refuses `value`.
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
- **The round trip cannot see a legacy value cut at its own newline** — the cut
  first line re-encodes to itself. Only the NEXT line proves where a record not
  closed by `"` ends: the end of the file, or a line Coolify certainly wrote
  (a row; HOST=0.0.0.0 without a HOST row; PORT=digits once `ports_exposes` is
  known). PORT/HOST/COOLIFY_*/SERVICE_* are written only sometimes (build pack,
  compose parsing version, settings, release), so a record followed by one of
  those is refused, never trusted (`_open_ended`). Do not "repair" such a record
  by joining lines: that carries another row's line as part of the value.
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
- **The exception, up to 4.5.0: a Redis `internal_db_url` carries the locked
  REDIS_PASSWORD.** It is appended and made visible to sensitive callers, and
  embeds `rawurlencode(connectionPassword())` (v4.5.0 StandaloneRedis.php:309-318,
  DatabasesController.php:213-234; same at 4.4.0). It is the primary source,
  cross-checked with the compose. From 4.5.1 both URLs are NULL for a locked
  credential (below) and the compose is the only source. Never log it or put it
  in a snapshot — `db_url` is a redaction fragment.
- **`is_shared` is strict only from 4.4.0** (`isSharedReference`); 4.1.2-4.3.x
  call any `{{x.y}}`-wrapped value shared. Shared locked rows are never recovered
  (the file holds the resolved value, not the reference).
- **A value-less bulk entry nulls the row on applications only**; services and
  databases `updateOrCreate` and keep the old value. Never send one by accident;
  the one deliberate use is the blank locked preview row below.
- **The application created hook copies a new production row into an UNLOCKED
  preview twin — unless a preview row with that key already exists.** So every
  preview entry of an application's bulk goes BEFORE every production entry
  (`build_env_entries`): a carried locked value gets a locked preview copy (no
  source preview row) and a source preview row that is locked gets a BLANK
  locked one — never the production value, the plan says PR deploys run
  without it. All previews first, not each before its own row: up to 4.3.x the
  bulk caches the preview rows at its first preview item (v4.1.2:3127), and a
  later one would create a second row beside a twin made after that.
- **Values live only in `Secrets`** (repr: key names), never in a ResourcePlan,
  plan.json, the journal or a log line; CREATE re-reads before its first write.
  Recovery is never persisted, so a resumed CREATE re-runs it from the files.
  That says nothing about the targets an interrupted CREATE already made (with
  recovered values in their env rows): undoing those is the rollback's job.

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
  StandalonePostgresql.php:180). Every pin of OURS is released BEFORE anything
  Coolify deletes - FINALIZE under DELETE, the target delete in a rollback - and
  a DELETE refuses (PREFLIGHT, then FINALIZE) while any other container, above
  all another migration's kept pin, still mounts a source volume.
- **Only a running container is a reference worth unpinning for**
  (`pins.is_reference`), and only one that stays across a settle window. A
  restarted container that fails Coolify's rolling-update health check is
  removed, a queued stop lands late, and before 4.4.4 the cleanup prunes stopped
  resource containers right before the volumes (v4.4.3:CleanupDocker.php:164,
  173). Volumes nothing mounted before the stop get an `idle` pin, released
  without a wait - awaiting a remount that never comes kept every pin forever.
- **The source unpin is the LAST compensation** (`RELEASE_SOURCE_PINS`, owed
  from CREATE_TARGET): ..., RESTORE_SOURCE_FQDN, RESTORE_SOURCE_DEPLOY_TRIGGERS,
  RESTART_SOURCE, DELETE_TARGET_RESOURCE, RELEASE_SOURCE_PINS. It waits for the
  restarted source to keep its volumes mounted, so nothing may wait behind it;
  the domain restore runs before the restart (`/restart` bakes the labels at job
  time). When a stop
  may have been requested (`statemachine.source_was_stopped`) and the restart
  was not accepted, it keeps the pins without a look. A kept pin's way out is
  `coolify-migrate unpin <id>` - never a second `rollback`, which redeploys a
  source that is serving by then.
- **`rollback <id>` again replays EVERY compensation** (completed states never
  leave the journal), and several messages ask for it (a KEPT target, a failed
  restart). RESTART_SOURCE is therefore once per stop and per resource
  (`compensations._restarted_since_stop`, a `restarted` uuid journalled per
  restart): `/restart` is `docker rm -f` plus start for a database and a
  redeploy for an image app, and the source is serving since the first one.
  A remedy that has the operator start the source by hand must not lead into a
  rollback that still owes its restart (the kept-pin notice says `coolify-migrate unpin`).
- **After FINALIZE(keep/rename) the source is no proven copy.** Its pins are
  gone, its containers were removed at QUIESCE, so a pruning server deletes its
  volumes; an operator may delete it. `rollback_refusal` refuses DELETE as a
  policy; for keep/rename the saga's `guard`
  (`compensations.source_data_refusal`, gated by
  `statemachine.source_copy_unproven`) LOOKS before any compensation: every
  source resource GETs, every copied volume is in `docker volume ls` with the
  CreatedAt COPY journalled (`source_volumes_created`: one pruned and recreated
  under its name is new), and every copied bind source is still what was copied
  (`binds.source_lost`: present, same kind, not empty where DISCOVER saw data),
  else refuse — STOP_TARGET and DROP_TARGET_VOLUMES run before RESTART_SOURCE,
  compose recreates a missing volume EMPTY, and DELETE_TARGET_RESOURCE `rm -rf`s
  the target's directory, where a relative bind's copy lives.
  DROP_TARGET_VOLUMES' own bind check is a second line only: the saga goes on
  after a failed compensation. Present volumes are re-pinned there,
  so no cleanup lands between the look and the restart. A guard that cannot
  answer refuses.
- **The plan reads the setting off `GET /servers/{uuid}`**, whose eager-loaded
  `settings` carry `delete_unused_volumes`, `docker_cleanup_frequency`,
  `force_docker_cleanup` and `docker_cleanup_threshold` at every release
  (v4.1.2:ServersController.php:166-171, v4.5.0:218-224). The dedicated
  `GET /servers/{uuid}/docker-cleanup` exists only from 4.3.0. A warning, never
  a refusal; under KEEP/RENAME it says the stopped source is no fallback copy.

### Managed DNS and the domain hand-over (release tags v4.4.0 -> v4.5.0)

Read at the tags only (managed DNS is absent at v4.3.24 and v4.4-rc.1). This is
why `domain/domains.py` hands every hostname over make-before-break.

- **Coolify manages DNS records for Cloudflare only, from v4.4.0.** Zones come
  from the team's Cloudflare integration tokens (CloudflareDnsProvider
  ::zonesForTeam, v4.4.0:73-76, v4.5.0:80-83). Records are created or replaced
  only from Livewire UI flows (InteractsWithDnsProviders v4.5.0:47 create, :164
  replace); no API controller, deploy, start or stop calls them. Created records
  carry ttl 1 and `proxied` false (v4.4.0:156); from 4.4.4 `proxied` follows the
  token's setting (v4.5.0:190). From v4.6.0 the API creates them too (below).
- **Up to 4.5.x the REST API shows nothing of it.** No zone/record route, and the
  resources' `domain_dns_statuses` is hidden. Only `GET /audit-events`
  (admin-only, 90 days) names managed hostnames — so safety must never depend
  on detecting a managed record. From 4.6.0 `GET /{applications,services}/{uuid}/dns`
  reads what Coolify tracks (the database, not Cloudflare; ResourceDnsRecords
  v4.6.0:60-86), which still says nothing about a record that lives only at
  Cloudflare. `GET /dns/zones` carries no `automatic_dns`.
- **From 4.6.0 a domain save can CREATE a record.** Any PATCH that adds a hostname
  queues ConfigureDnsRecordJob with the resource's server address, when exactly one
  team zone holds the host and its token has automatic DNS on, the default
  (ResourceDnsRecords :158-184, IntegrationToken :129-132; ApplicationsController
  :3871, ServicesController :1341; up to 4.6.x a create does not). So OUR CLAIM
  triggers it. From v4.7.0 every API create queues records too, for every
  hostname of the new resource, with the target server's address (b7defddc3;
  ApplicationsController v4.7.0:2003,2243,2477,2617,2753, ServicesController
  :617,:809, clones, UpdateServiceApplicationFromApi :140). Harmless here only
  because CREATE never carries a custom host (`resources.target_domain_values`,
  `_compose_domains_body(with_custom=False)`, a service create sends no urls):
  never move a custom host into a create body. With the token's opt-in proxied
  DNS those target-own records are proxied, which the DNS gate reads as
  ELSEWHERE; the plan warns (`dns/managed`, `automatic_dns_on_api_create`).
  A host with its own record is left alone: conflict, nothing written
  (CloudflareDnsProvider :183-185), which is why this does not re-point a live
  record. A host that resolves only through a WILDCARD record gets a new record
  pointing at the not-yet-running target, and a rollback leaves it (the source holds
  the host, so the release only re-references it). The end-of-CREATE re-check may
  catch the move, and the rollback's flip-back list names the host; the plan warns
  (`dns/managed.automatic_dns_note`). A warning, because the API cannot tell a wildcard
  from a record of its own. Never flip the token's setting from here, for the
  reason the Docker cleanup setting stays unflipped.
- **Up to 4.5.x, Coolify's only provider write we can trigger is a DELETE.** Removing a
  hostname from a resource (any domain PATCH: ApplicationsController
  v4.4.0:3252/3595, v4.5.0:3361/3784) or deleting a resource that references a
  record (Traits/ReleasesManagedDnsRecords v4.5.0:21-27, afterCommit) queues a
  release. The release (ManagedDnsRecordCleanup::releaseLocked v4.4.0:229-285,
  v4.5.0:294-350) keeps the record when another live reference exists, or when
  any live application, preview or service application HOLDS the exact hostname
  in `fqdn`/`docker_compose_domains` — it then only re-references it
  (v4.5.0:322-333; holders :358-398, soft-deleted excluded). Otherwise it
  DELETES an owned record at Cloudflare (:342), unless its value was changed
  there (ChangedExternally, CloudflareDnsProvider v4.5.0:343: forgotten, kept).
  A deleted record is never recreated.
- **So holding the hostname is what protects a record.** CREATE never writes
  the source; the target claims the host while the source still holds it;
  FINALIZE releases the source last, and only once the target is READ to hold
  every one of those hosts right then (`steps._target_still_holds`): every deploy
  of a dockercompose app reloads its compose from the branch HEAD and drops the
  domain of a service renamed there since CREATE (reconcileDockerComposeDomains,
  v4.5.0:2409-2431), and HEALTHCHECK sees containers, not domains. A rollback restores
  the source first and deletes a host-holding target only once the source holds
  its hosts again (`compensations._source_lacks_its_hosts`), keeping the target
  — stopped, harmless — otherwise. Up to 2.9 CREATE parked the source first,
  racing the release job against the target's create (a record of a LIVE site
  could be deleted before QUIESCE), and a rollback deleted the target before the
  restore. Both orders are races, not certainties — never write a test that
  expects a deletion from them.
- **Coolify re-points a record by itself only when nobody holds its hostname.**
  From v4.7.0 an automatic create REPLACES a conflicting record when Coolify
  created it (owned, ownership comment intact), no live resource but the claimer
  references it, and no live application, preview or service application holds
  the hostname (ConfigureDnsRecordJob v4.7.0:59-76,100-119, c3a21ecee;
  ManagedDnsRecordCleanup v4.7.0:358-373; a soft-deleted claimer never qualifies,
  :149-157). Our hand-over keeps the source holding every claimed host until
  FINALIZE, so a claim still never re-points a live record. Otherwise its value is
  written only on create or a confirmed replace (from 4.6.0 also `POST .../dns/replace`, which
  needs the record id and current content from a conflict and points only at the
  resource's own server). The UI offers no create/replace for a hostname that
  already has a managed row ("Managed by Coolify" badge only,
  dns-provider-management.blade.php v4.4.0:28-29); for a Cloudflare record it
  does not manage, the target's Domains -> Configure DNS -> "Replace record"
  re-points it and tracks it as not owned (blade :33-36, InteractsWithDnsProviders
  v4.5.0:164-201). The DNS cutover is therefore the operator's (or, later, this
  tool's own Cloudflare API call — not built: phase 2 is a user decision).
- **The claim is two-phase on every release.** Before v4.5.0 a dockercompose
  CREATE strips `force_domain_override` before its conflict check reads it
  (ApplicationsController v4.4.0:1464 strip, :1518 check; fixed
  v4.5.0:1329-1330), while the UPDATE checks before it strips at every tag
  (v4.1.2:2522/2591 vs :2657, v4.4.0:3409/3478 vs :3569, v4.5.0:3589/3658 vs
  :3749). The target is created without the contested hosts, passes its gates,
  then one PATCH claims them. The update ignores `domains` when the target
  server's proxy should not run (v4.5.0:3566) and keeps compose entries only for
  services in the loaded compose (:3667-3682) — hence the read-back.
- **The DNS gate decides per host, and only a cutover is accepted globally.**
  ELSEWHERE (neither server — a CDN, proxy or load balancer; a proxied
  Cloudflare record looks exactly like this) BLOCKS for every provider: its
  origin may still be the source. VANISHED (resolved at PLAN, a definitive
  NXDOMAIN or empty answer now) blocks too, and so does UNANSWERED (resolved at
  PLAN, the lookup times out or SERVFAILs now): a failed lookup is no deletion,
  but the source is stopped by then and nothing shows where the host points.
  All three are accepted only by name (`--accept-dns-host`,
  `ctx.accepted_hosts`); `--accept-dns` stays the answer to CUTOVER_NEEDED alone.
  An address BOTH servers' sets hold (a wildcard base they share, one CDN in
  front of both) proves neither: `gate.classify` matches each side on what only
  it answers as, so a host on shared addresses alone is ELSEWHERE. Counted for
  both, it read as READY and a host still on the source passed.
  Cloudflare/NS/CNAME classification (`dns/managed.py`) only words messages and
  plan warnings — never gate on it (vanity nameservers, partial setups and every
  other CDN would slip through). Each server's address set includes its wildcard
  base (and a probe label under it), the public address custom records point at.
- **Four plan warnings, never refusals.** A custom host whose zone is on
  Cloudflare (authoritative NS `*.ns.cloudflare.com` or a CNAME into
  `*.cdn.cloudflare.net`) from 4.4.0 (`dns/managed.cloudflare_note`), a claim
  that can create a record from 4.6.0 (`dns/managed.automatic_dns_note`), the
  target's own hosts under a wildcard in a team zone from 4.7.0, or 4.6.0 for a
  service (`dns/managed.target_records_note`), and a
  hostname also held by a resource outside the plan
  (`domains.outside_holders`). REST only: `GET /applications` carries every
  application's `fqdn`/`docker_compose_domains`, but `GET /services` does not
  load sub-applications (ServicesController v4.5.0:270-283), so each other
  service is read once; previews are invisible to both.
- **The DNS baseline is taken at PLAN and journalled** (`plan.dns_baseline`,
  PLAN's step detail; `context.dns_baseline_of` reads the journal first). It is
  re-checked at the gate (VANISHED), at the end of CREATE while the source still
  serves (a custom host that left the source refuses with zero downtime — on a
  DEFINITIVE answer only, `gate.is_definitive`: a timeout proves nothing), and at
  the end of every rollback (the flip-back list, `domains.dns_rollback_note`).
  Unit tests never touch the real resolver: `tests/conftest.py::_offline_dns`
  answers "offline" (not definitive) unless a test patches resolution itself.

### Deploy triggers, containment, kept-source routers and bind mounts (release tags v4.1.2 -> v4.7.5)

Read at the tags (moby v27.5.1 and v28.3.3 for the daemon). The modules named
carry the rest of the evidence.

- **Nothing in Coolify asks whether a migration runs before it deploys.** A push
  deploys every application with `is_auto_deploy_enabled` on — all isDeployable
  checks (Application v4.1.2:1106-1113, v4.7.2:1506-1513; column default true,
  migration 2023_03_27_081717:19) — and a pull request a preview wherever
  previews are on (isPRDeployable :1115/:1515, default false). A target made
  from a GitHub App carries the source's repository id, one with manual webhooks
  its secrets (`??=`, Application v4.1.2:239-242, v4.7.2:334-337): one push
  deploys both. `POST /deploy` by uuid or tag checks neither flag nor status
  (DeployController by_tags v4.1.2:444, v4.7.2:488; deploy_resource :496/:540).
  The stop cancels no deployment (StopApplication v4.7.2:19-102), and
  queue_next_deployment starts what waits.
- **The switches.** The shared create array takes `is_auto_deploy_enabled` at
  every tag (v4.1.2:914 .. v4.7.2:1613); the git routes store it at every tag,
  dockerfile and dockerimage only from v4.5.1 (:2331/:2466). The update array
  takes it at every tag (v4.1.2:2368, v4.7.2:3662), `is_preview_deployments_enabled`
  only from v4.2.0 (:2695), and both are readable only from 4.2 (`settings`,
  v4.2.0:2277, v4.7.2:2848). A settings-only PATCH deploys nothing, rebuilds no
  labels and queues no DNS job (v4.1.2:2668/2680, v4.7.2:4107/4130;
  ResourceDnsRecords v4.7.2:158-163).
- **The queue.** `GET /deployments` (every tag, `read`) lists the team's queued
  and in_progress rows — a JSON object once `->sortBy('id')` moved one. Match a
  row by the application uuid in `deployment_url` (applications.php
  v4.1.2:19-20, routes/web.php :217-218), never by the hidden `application_id`;
  drop `logs` in the client. A deployment that ends replaces the containers: a
  rolling update starts `{uuid}-{Ymd\THis}` (docker.php v4.7.2:670-686) and
  removes the old one (ApplicationDeploymentJob v4.7.2:3084-3090) before the row
  reads finished (:718-725).
- **Tags.** No tag routes on 4.1.x (routes/api.php v4.2.0:158-160). A deleted
  source has none to read: the routes skip soft-deleted rows (HandlesTagsApi
  v4.7.2:23-38) and DeleteResourceJob force-deletes (v4.3.10:130, v4.7.2:171).
  Adding is idempotent (createOrFirst + syncWithoutDetaching, v4.2.0:142), names
  are stored lowercased and trimmed (normalizeTagNames v4.2.0 .. v4.7.5).
  `DELETE /{collection}/{uuid}/tags/{tag_uuid}` only detaches (routes/api.php
  v4.2.0:160, v4.7.5:313); up to 4.4.7 a tag left on nothing is then deleted
  (deleteIfOrphaned, gone in v4.5.0).
- **A service's or database's `/start` 400s "already running" off the status
  column** (ServicesController v4.1.2:1724-1726 .. v4.7.2:2183-2185,
  DatabasesController v4.1.2:2762-2764 .. v4.7.2:3588-3590). A target this run
  never started reads 'exited', so for it the 400 means someone else started it:
  `client.start` raises `AlreadyRunning`, START `TargetStartedEarly` unless its
  own `start_requested` shows the ask. From 4.6.0 a database `/start` can 409
  while an operation runs (v4.6.0:3583-3586): a plain API error.
- **An application `/start` is skipped while a deployment of the same commit is
  queued or running**: 200, a message, no `deployment_uuid` (applications.php
  v4.1.2:47-64, v4.7.2:65-78; ApplicationsController v4.1.2:3597-3604,
  v4.7.2:4970-4977). For a target that is someone else's deploy: refused. And
  HEALTHCHECK counts a target's containers only once START's own deployment
  finished: a container someone else deployed is up just as well.
- **Stopping a never-started target is harmless** (StopApplication
  v4.1.2:35-36, v4.7.2:43-45; services and databases answer "already stopped"),
  but an application stop dispatches CleanupDocker on that server
  (`docker_cleanup` defaults to true, ApplicationsController v4.1.2:3700,
  v4.7.2:5073). It holds back no deployment: StopApplication runs on 'high' and
  cancels none (v4.7.2:17), DeleteResourceJob cancels only a preview's
  (v4.7.2:234-278). The cancel and per-application deployment routes exist on
  every release (routes/api.php v4.1.2:81-82, v4.7.2:165-166). Not closed: a
  service's StartService waits on the default queue, and no API cancels a queued
  job (ServicesController v4.7.2:2186/2283, StopService:18).
- **Coolify's stop is no single act.** StopApplication stops a compose app's
  containers one SSH call and one grace period at a time (v4.1.2:38-42,
  v4.7.2:47-58), QUIESCE stops the resources one after another, and every
  container runs `restart: unless-stopped` (constants.php v4.1.2:37, v4.7.2:62;
  parsers.php v4.7.2:1508,2918). So QUIESCE journals the source's clock twice:
  `stop_clock` before the request (`quiesce.killed_since`'s window) and
  `stopped_clock` once the gate passed (the revival window).
- **The daemon's event log is no witness of the stop window.** It keeps its last
  256 events in memory and none across a restart (daemon/events/events.go
  v27.5.1:12,26, v28.3.3:13,27); every health probe logs three. The checkpoints
  warn `containment.event_log_gap` (an empty buffer is a gap) and judge what
  still exists by its state and `State.StartedAt`. A container that came and
  went before the window cannot be ruled out: say so, never claim it.
- **An application's deployment history is the durable witness**
  (`GET /deployments/applications/{uuid}`; rows kept 60 days, CleanupDatabase
  v4.1.2:23-26,52-57, v4.7.2:29-31,47; a failed deploy leaves its row).
  QUIESCE journals each source application's ENDED rows (`source_deployments`)
  and refuses the stop over one still pending. A checkpoint reports every row
  queued, running or unread, and every row the record lacks that no recorded row
  sorts above — by uuid and Coolify's order (created_at desc; the id tiebreak
  only from v4.7.2:1489-1490), never by time. Coolify's own 404 "Application
  not found" (DeployController v4.1.2:692-695 .. v4.7.2:754-757) is a source
  gone, any other 404 an unread history. A block after START journals what it
  showed (`containment.block_record`: ended rows in `revival_seen_deployments`,
  pending ones in `revival_pending_deployments`, reported once more after they
  end) and the source's clock (`revival_seen_clock`). Services and databases
  have no deployment rows.
- **Coolify's scheduled volume backup reads a stopped source's volumes**
  (4.3.0+): `docker run --rm --name volume-backup-<uuid> -v <volume>:/volume:ro`
  whatever the status — no 'running' skip (VolumeBackupJob v4.3.0:78-87,
  v4.7.2:101-123; ScheduledJobManager v4.3.0:382-430, v4.7.2:531-600). It has no
  label and no uuid, so only the migrated-volume probe finds it, and such a
  container counts only when it mounts a migrated volume writable
  (`containment.volume_only`, `mounts_writable`; untellable counts). Never extend
  that to a labelled or uuid-carrying container. Its `stop_during_backup`
  restarts running containers only (`docker ps -q`, v4.7.2:383-419).
- **One machine can be two Coolify servers.** One SSH endpoint is one record
  (ServersController v4.7.2:637, `sshEndpointInUse`), but localhost and the
  public IP, or two SSH ports, are two, and the source checkpoints would take the
  running target for the source started again. PREFLIGHT compares moby's engine
  ID (`docker info --format '{{.ID}}'`, written once into the data root,
  daemon/id.go v27.5.1:12-35) and refuses equal ones
  (`containment.shared_daemon_refusal`), nothing when a probe goes unanswered. A
  machine cloned with its `/var/lib/docker` shares the ID.
- **A local volume's CreatedAt is the ctime of `/var/lib/docker/volumes/<name>`**
  (volume/local/local_unix.go v27.5.1:229-236), so a volume pruned and recreated
  under its name (`external: false`, ApplicationDeploymentJob v4.7.2:5011-5030)
  reads new. COPY journals `source_volumes_created` once its end checkpoint
  passed.
- **Readonly labels off: FINALIZE's release stops nothing the proxy reads.** The
  domain PATCH rebuilds `custom_labels` only while
  `is_container_label_readonly_enabled` is on (ApplicationsController
  v4.1.2:2668 .. v4.7.2:4107), and every non-compose deploy writes the stored
  labels (ApplicationDeploymentJob v4.1.2:3099-3115, v4.7.2:4740-4763). A
  dockercompose app's come from `docker_compose_domains` at every parse (its
  deploy, v4.7.2:876, never reads custom_labels); services have no such column
  (migration 2023_10_18_072519). So keep and rename are refused for such an
  application (`fidelity.kept_source_label_refusals`; 4.1.x shows no settings
  and warns). Switching label management back regenerates the labels (Livewire
  General v4.1.2:490-491, v4.7.2:567-568).
- **A compose's own labels outlive every domain write.** Both parsers start a
  service's labels from its compose `labels:` and merge the generated ones on
  top, never reading the readonly setting (applicationParser v4.1.2:1311,
  v4.5.0:2130, v4.7.2:2170; serviceParser v4.1.2:2588, v4.5.0:3447,
  v4.7.2:3518). Where the proxy serves them they route after the release:
  Coolify's Traefik runs `--providers.docker.exposedbydefault=false` (proxy.php
  v4.1.2:351, v4.7.2:809), Coolify writes `traefik.enable=true` only for a
  compose service with a domain (fqdnLabelsForTraefik, docker.php v4.7.2:1048;
  parsers v4.7.2:2179,3527), and a Caddy site label always routes. Refused under
  keep/rename (`fidelity.compose_hosts_routed_after_release`, the one predicate;
  a service that keeps a domain is `_traefik_serves_after_release`, gated on
  `compose_blank_domain_regenerated`). What the GET cannot show is a warning
  (`compose_routes_unexposed`, `compose_routes_unseen`): a Traefik router on a
  service that neither keeps a domain nor sets `traefik.enable`, a host built
  from a variable while label escaping is off or unknown (escapeDollarSign,
  v4.1.2:1313-1316/2590-2593, v4.7.2:2172-2175/3520-3523), a SPECIFIC_SERVICES
  part's env-row routes. A refusal and a warning never name the same host.
- **FINALIZE asks both predicates of the GET its release is built from**, against
  the hosts the source held BEFORE the release (`parked_domains`,
  `api_resources.held_before_release`, so a resumed FINALIZE still sees them),
  and never lists such a source as released (`DomainRelease.still_routed`,
  `compose_routed`).
- **One server per side.** Source and target are never one server record
  (`plan.same_server_refusal`); an environment migrates from its first
  resource's server, so `ResourceSnapshot.server_uuid` and
  `plan.foreign_server_refusal` refuse every resource elsewhere, and the planner
  one whose server it cannot read. Both derived on every read: a saved plan.json
  is judged too.
- **Every compose bind is a file-storage row** for each local source but
  `/var/run/docker.sock` and `/tmp`, keyed on `(mount_path, resource)`
  (parsers.php v4.1.2:774-812/2161-2193 .. v4.7.2:1613-1657/3062-3098). `./x`
  resolves under the resource's directory (resolveComposeBindSource, shared.php
  v4.7.2:2124-2132), under `applications/{uuid}` for a service on compose
  parsing version 3 (parsers.php v4.7.2:138-145).
- **Only a row WITH content is regenerated.** A directory row is only ever
  `mkdir -p`'d (LocalFileVolume v4.7.2:333-405), a deploy writes only
  non-directory rows (ApplicationDeploymentJob v4.7.2:1546-1550), a service start
  only its compose, `.env` and missing content files (EnsureContentFilesOnServer
  v4.7.2:51): a new target starts with every such directory EMPTY. A content row
  is written wherever the compose put it (LocalFileVolume v4.7.2:319-330), so it
  is judged before the system-path rule.
- **Which target gets that content** (`binds.StorageCarry`): a plain app's rows
  are POSTed (`copy_storages`, which skips placeholder content — a binary or
  over-5-MiB file mount is refused); a git compose app re-reads its compose from
  git (into `/tmp/{random}`, Application v4.7.2:2772-2810: its directory stays
  empty until START); a template service gets the catalog's; a raw-compose
  service's stored `docker_compose_raw` has no `content:`/`isDirectory`
  (parsers.php v4.1.2:2740, v4.5.0:3638, v4.7.2:3701-3718), so its row is a
  contentless directory (v4.7.2:3051-3058) and the file is mirrored. A database
  carries no storage row: its host-path, directory and content mounts are
  refused, and what Coolify writes from its columns at every start (StartPostgresql
  v4.7.2:163-190) is skipped. The rest is read back at the end of CREATE
  (`binds.content_problem`): a catalog or a branch HEAD can drop a file.
- **A database's TLS certificate files are content rows, and skipped.** With
  TLS on, every start writes `{dir}/ssl/server.{crt,key,pem}` and keeps them as
  FILE STORAGE rows with the content (SslHelper::generateSslCertificate
  v4.7.5:176-223, mountPath per engine the same at v4.1.2/v4.5.0/v4.7.5); a start
  without TLS deletes them (StartPostgresql v4.7.5:47-63). The target starts
  without TLS, so `binds.is_database_tls_certificate` skips exactly those files
  and CREATE reads no target row back for them. Refusing them as "a database's
  content file" refused every TLS database (found by the e2e run). The rows hold
  the private key: the plan keeps only `has_content` (`planner`, ApiStorage), and
  a database's rows are never POSTed anywhere.
- **Deleting a resource removes its own directory only** (deleteConfigurations,
  Application v4.7.2:587-594, Service v4.7.2:226-233 via DeleteService
  v4.7.2:30-32; LocalFileVolume v4.7.2:269-271). A target delete takes a
  relative bind's copy, never an absolute one's — COPY's rollback removes that —
  and FINALIZE `delete` leaves the source's absolute bind paths on its server.

## Supporting a new Coolify release

A release that only ADDS fields is a data change in `domain/coolify_versions.py`
and nothing else. The steps, in order:

1. **Run the canaries** — they read the NEWEST release tag, never `main`:
   `GITHUB_TOKEN=$(gh auth token) pytest -m integration tests/test_api_fields.py
   tests/test_backup_schedules.py tests/test_database_tls.py
   tests/test_scheduled_tasks.py`. A red one prints the exact registry row to
   add (`CREATE_FIELDS["..."]: Field("x", Gate(since=(4, 5, 2)),
   read_from="settings")`). `since` is the tag the canary ran against: confirm
   at the previous tag that the field really is new there.
2. **Read every new field at the release tag** (blobless clone, `git show
   vX:path`): where it is stored, whether the GET returns it flat or nested,
   and what its validation refuses. Then add the `Field` row with that tag
   evidence in a comment — or record it in a `KNOWN_*_GAP` in the tests with
   the reason it is not carried. A field that LEFT an array gets `until=`, not
   a deletion: older releases still take it.
3. **Behaviour changes** (a validator, a hidden value, a new guard): add a
   `FEATURES` row with the tag and file:line evidence, and gate the rule on
   `supports(...)`. Where a field is now carried, the plan warning about it
   must stop firing from that release on (`carries(endpoint, field, version)`).
4. **Diff the release against the previous one** for the controllers,
   `bootstrap/helpers/api.php`, the models the tool reads and the jobs it relies
   on (stop/start/restart guards, labels, volume names, mount paths). Record
   what you found in a "Learned from the vA -> vB audit" section below.
5. **Raise `VALIDATED_MAX`** in `domain/coolify_versions.py` with an audit note
   like the existing ones, and the boundary test in `tests/test_cli.py`.
6. **Run the e2e rig** (`make e2e`; it pulls `coolify:latest`) before
   releasing.

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
| Which fields may we send, on which release? | `domain/coolify_versions.py` (views: `api/fields.py`) |
| Which release does this behaviour start in? | `domain/coolify_versions.py` (`FEATURES`) |
| What can the target NOT reproduce? | `domain/fidelity.py` |
| Who holds a domain, and when may we force it? | `domain/domains.py` |
| Could Coolify manage this DNS record? | `dns/managed.py` (wording only, never a gate) |
| Are scheduled tasks and backup schedules carried? | `api/schedules.py`, `domain/schedules.py` |
| Can a locked variable be carried? | `domain/locked_envs.py` |
| Is the data safe from Coolify's cleanup? | `discovery/pins.py` |
| Will Coolify (4.7+) accept this name or description? | `domain/fidelity.py` (names and descriptions) |
| Did the deploy behind a start or restart actually run? | `domain/deployments.py` |
| Does Coolify pin its own host's SSH key (F2)? | `domain/host_keys.py` |
| What can deploy a source or a target, and what does a rollback switch back? | `domain/deploy_triggers.py` (IO: `engine/deploy_triggers.py`) |
| Did anything start the stopped source or an unstarted target? | `domain/containment.py` (rules), `engine/containment.py` (checkpoints, the stop's deployment record) |
| Are the source and target one server, or one machine? | `domain/plan.py` (`same_server_refusal`), `domain/containment.py` (`shared_daemon_refusal`) |
| Which refusals depend on the finalize policy, or on the plan as a whole? | `domain/plan.py` (`MigrationPlan.blocking_reasons_of`, `plan_refusals`) |
| Does a kept source keep routing its moved hosts? | `domain/fidelity.py` (`hosts_routed_after_release`, `compose_hosts_routed_after_release`) |
| May a bind mount be copied, shared or removed, and where does it land? | `domain/binds.py` (IO: `transfer/binds.py`) |
