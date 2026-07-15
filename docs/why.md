# Why this exists

Coolify can clone a resource to another server. It will not move the data. That
is deliberate, and understanding why is the fastest way to understand this tool.

## The gap is upstream policy, not an oversight

Coolify has all the machinery: `app/Jobs/VolumeCloneJob.php` implements
cross-server volume transfer, and `app/Livewire/Project/CloneMe.php` has a
`cloneVolumeData` flag wired through to it. PR #4777 (merged January 2025)
shipped it **disabled and unexposed in the UI**.

The maintainer's stated reasons:

> There are a lot of permission issues - in and outside the container / on the
> server and the target server (and on macOS this is even more challenging)

> if you have a lot of resources like 50+ and all of these have 1 or more volumes
> each, then there will be such a huge amount of job spam that your job server
> will crash

plus no progress tracking, and failures on large volumes.

Every one of those four is a consequence of running **inside Coolify's Laravel
job queue**:

| Blocker | Root cause | How an external orchestrator avoids it |
| --- | --- | --- |
| Permission damage | The job must guess ownership, so it hardcodes `chown -R 1000:1000` | We never chown; `rsync --numeric-ids` preserves uid/gid exactly |
| Job-queue spam | One queued job per volume, times 50+ resources | We are one process with a bounded worker pool |
| No progress | A queue worker has nowhere to render to | We own a terminal, and parse `rsync --info=progress2` |
| Large-volume failure | `tar` to a temp dir → `scp` → untar needs 2× disk and cannot resume | We stream with rsync, `--partial`, resumable, chunked |

The permission one deserves its own note, because it is the whole ballgame.
`VolumeCloneJob` does:

```php
"docker run --rm -v {$srcVol}:/source -v {$tgtVol}:/target alpine sh -c
 'cp -a /source/. /target/ && chown -R 1000:1000 /target'"
```

Postgres runs as uid 999. MySQL 999. Redis 999. ClickHouse 101. Blanket-chowning
a database volume to 1000 corrupts it. That single line is why the feature is
off, and avoiding it is the first of our
[security invariants](safety.md#security-invariants).

## The community tools, and what they taught us

Both were studied at source. Their bugs are this tool's requirements.

### `mrcandev/coolify-mover`

23 commits, 21 of them in a two-day burst. It bypasses the API and writes
resource rows with raw SQL.

- **Remote code execution as root.** It builds SQL by textually substituting
  `$1`, then embeds the result in a *double-quoted shell string* passed to
  `execSync("docker exec coolify-db psql -c \"...\"")`. Inside double quotes `$`
  and backticks stay live. A resource name — user-controlled from the Coolify
  dashboard — containing `$(...)` executes on the Coolify host as root.
- **No transactions anywhere.** `cloneService` issues 10+ independent INSERTs
  across 5 tables. A failure at statement 7 leaves a half-built service that
  nothing will ever clean up, and re-running creates a *second* clone.
- **It silently loses every service volume.** Coolify names a service's compose
  volume `{parent_service_uuid}_{slug}`, but the tool rewrites using the
  *sub-application's* uuid. The replace matches nothing, so the DB row keeps the
  old name while the data is copied to a new one. Docker then auto-creates an
  empty volume at deploy. **No error is raised at any point.**
- **It hot-copies live databases by default.** `--stop-source` is opt-in, its
  failure is *swallowed* (`logger.warn` then continue), and there is no wait for
  `exited`.
- `rsync -avz` only: no `-H`, no `-A/-X`, and critically **no `--numeric-ids`**,
  routed source → operator's laptop → target through `/tmp` (often tmpfs).

### `Geczy/coolify-migration`

210 stars, and **architecturally right**: it copies `/data/coolify` wholesale
(so `source/.env` with `APP_KEY` rides along), extracts **before** running
`install.sh`, and never mutates the source — which gives it an implicit rollback.
We kept all three.

Its problems are operational hygiene:

- Volumes discovered from `docker ps` — **running containers only**. A stopped
  container's volume is silently skipped and never even reported. Coolify's own
  code uses `docker ps -a`; so do we.
- Bind mounts silently dropped (`docker inspect .Name` is empty for them).
- Stopping Docker is a *prompt*, and `tar --warning=no-file-changed` deliberately
  tolerates files changing underneath it. Answering "n" tars a live Postgres data
  directory. That is one keystroke from a torn snapshot.
- The disk check is a fixed 1 GB floor. It computes the real total, prints it,
  and never compares against it.
- **Zero verification.** Success means `ssh` returned 0.

## What we do differently

Nothing here is clever. It is the boring version, done carefully:

1. **Application-unaware.** A cleanly stopped stack makes a volume just bytes.
   No engine allowlist, no `pg_dump`, no special cases. Your ClickHouse and the
   service nobody has heard of get identical treatment.
2. **The REST API is the only write path.** No SQL against `coolify-db`, so a
   Coolify upgrade cannot silently break us — and a 422 tells us exactly which
   field drifted.
3. **Trust the daemon, not the endpoint.** Every Coolify stop is a `dispatch()`
   that returns before anything stops, does not touch preview containers, and for
   services works from DB records rather than labels. We poll `docker ps -a` by
   label until every container — previews included — is `exited` and not
   SIGKILLed.
4. **Verify both halves.** Content *and* metadata. A content-only hash cannot see
   the wrong `chown`, which is the exact corruption we are guarding against.
5. **Journal everything, compensate in reverse.** The source is never destroyed
   until an explicit finalize, so rollback is always available.

## What we could not fix

Honesty is part of the design. See [Safety model](safety.md#accepted-risks).

- **A rebuild is never byte-identical.** `git_commit_sha` does not pin a deploy
  (the job overwrites it from `git ls-remote`), and `docker build --pull` is
  forced, so unpinned `FROM` tags refresh every build. We detect and gate the
  drift; we cannot remove it.
- **~25 `ApplicationSetting` fields are unreadable over the API.** We recover
  what Docker can prove and ask for the rest. We never guess.
