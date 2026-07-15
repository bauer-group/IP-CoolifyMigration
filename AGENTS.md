# AGENTS.md

Instructions for an AI agent — or a human in a hurry — working on this repo.

## Fast path

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
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

Checked against `coollabsio/coolify@main`:

- `can_read_sensitive = token->can('root') || token->can('read:sensitive')`.
  Without it, `value`, `real_value`, `docker_compose_raw` **vanish** from
  responses — HTTP 200, no error, no marker.
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
- `GET /applications/{uuid}` does **not** return `settings` (no `$with`, no eager
  load). ~33 fields are settable-but-unreadable.
- Standalone DB mount path depends on the image tag (Postgres ≥18 moves to
  `/var/lib/postgresql`). Always pin `image`.

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
  invisible there**. Cross-check `/databases`, which merges all eight.
- **Only services carry `server_uuid`.** Applications and databases reach their
  server through `destination` (a morphTo). Use `planner.server_uuid_of`.
- **`is_reachable` lives under `settings`**, not at the top level.
- **`GET /version` returns a bare string**, not JSON.
- **The API is off instance-wide by default** — a 403 is not necessarily the
  token.
- **`health_check_*` is readable but in no `$allowedFields`.** Sending one 422s
  the whole request. Defaults are 15/5/5/5 (not 30/30/3/30).
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
- **Exit code 137 cannot be polled for**, for the same reason: both commands go
  out in one SSH invocation, so a SIGKILLed container exists for milliseconds.
  The guard that makes invariant 9 mean anything reads the **daemon event log**
  after the fact (`quiesce.killed_since`), where the exit code outlives the
  container. Do not replace it with a `docker ps` check that looks equivalent.

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
| Which volume maps to which? | `domain/naming.py` (`pair_by_mount_path`) |
| What are we copying, and why not that? | `domain/manifest.py` |
| Will the rebuild ship different code? | `domain/drift.py` |
| What happens on failure? | `domain/statemachine.py`, `engine/executor.py` |
| Is it safe to start the target? | `dns/gate.py` |
| Is the stack really stopped? | `discovery/quiesce.py` |
| Which fields may we send? | `api/fields.py` |
