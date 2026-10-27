# Troubleshooting

## `doctor` says the token cannot read sensitive data (exit 10)

Create a new token in Coolify under **Keys & Tokens > API tokens** with `root`, or
`read:sensitive` + `write` + `deploy` — as an admin or owner of the team. Since
4.2 a token held by a plain member is treated as unable to read sensitive data
whatever its abilities, and its 403 says `Missing required team role.`

This is not a nicety. Coolify's `ApiSensitiveData` middleware omits `value`,
`real_value` and `docker_compose_raw` for tokens without that scope — **HTTP 200,
no error, no redaction marker, the keys are simply absent**. A migration would
recreate every environment variable empty and every service without its compose.

## `403 API is disabled.` on every call

Coolify ships with its REST API **off instance-wide**, separately from your
token. Turn it on under **Settings > API**. (On 4.2-4.4 a root token could also
`POST /api/v1/enable`; Coolify 4.5 removed that route.)

This reads exactly like a token or permission problem and is neither — your token
is fine, the instance switch just has not been flipped. It is off by default and
has to be enabled once.

## The DNS gate blocked me (exit 3)

Working as designed. It is a resumable stop, not a failure — the target is created
and its data is verified. The gate checks the domains the **target** will serve
(the source's may have been parked to free a custom domain for the target) and
compares them against the servers' **resolved** addresses (Coolify's `ip` is often
a hostname). It fires for a **custom domain** (`shop.example.com`) that still
points at the source. Server-bound URLs under a server's wildcard
(`pdf-tool.app.0046-20…`) are rewritten onto the target's wildcard automatically
and never gate — see *Domains & DNS* in the CLI reference — unless one was NOT
rewritten and the target would serve it on the source's wildcard (below).

```bash
# Apply the printed cutover checklist to your DNS, wait out the TTL, then:
coolify-migrate resume <id>
```

Blocking by default is deliberate, because starting early is *actively harmful*:
Traefik on the new host requests an ACME certificate, the HTTP-01 challenge is
routed by DNS to the **old** host, the challenge fails, and Let's Encrypt
rate-limits 5 failed validations per hostname per hour. A retry loop burns that
budget, so even a correct cutover an hour later cannot get a certificate.

If you accept that risk — you want to finalize now and repoint DNS in parallel,
knowing propagation lags by the record's TTL — confirm interactively or pass
`--accept-dns` (on `run` or `resume`). The gate then warns instead of blocking.

### "the target would serve hostname(s) bound to the SOURCE server's wildcard"

A host the target is configured to route still sits under the **source**
server's wildcard: it was not rewritten onto the target's, usually because the
target has no wildcard configured. The wildcard's DNS record binds it to the
source for good, so no DNS change moves it and the target could never obtain a
certificate for it. This is not a cutover, so `--accept-dns` does not apply.
Change the target's domain in Coolify to a host under the target's wildcard (or
a custom domain), then `coolify-migrate resume <id>`. For a service, the gate
reads each sub-application's domain, the routing variables of a minio, logto,
garage or silo image (`MINIO_SERVER_URL` and friends) and Host() labels written
into a custom compose — not the derived `SERVICE_URL_*` variables.

### "resolves to neither source nor target"

A CDN or reverse proxy (typically Cloudflare's orange cloud) is in front. DNS
then tells us nothing about where the origin points, so we surface it rather than
guess. Check the CDN's origin setting before starting the target.

## Drift needs my decision (exit 4)

Not a failure, and not a refusal. **We build the target exactly as the source is
configured** — same image reference, same branch. But a tag is a pointer and a
branch moves, so "the same configuration" can still produce something different.
Whether that is compatible is a judgement about *your* stack, so we ask.

You only see exit 4 when we could not ask, i.e. `--yes` in a pipe or CI. Either:

1. Re-run interactively — you get the detail and a prompt; or
2. Answer in advance: `--accept-drift`.

Nothing has been changed when this fires.

### "postgres:latest is a moving tag"

The one worth pausing on. We copy the data directory byte-exactly and then start
whatever `latest` resolves to *now*. If that crossed a major version, the engine
will refuse to read the data — "database files are incompatible with server".

Your source is untouched either way, so the worst case is a failed healthcheck
and a rollback. But it is cheaper to know first. Pin the tag (`postgres:16`) if
you want the question to go away permanently.

### "postgres:16 may resolve to a newer image"

A notice, not a question. `16` picks up minor releases, which share an on-disk
format. Routine.

### "Coolify cannot read a version out of postgres:latest"

Coolify's trap, not ours. Its `created` hook picks the volume mount path by
regexing the tag for a number, defaulting to the pre-18 path when it finds none —
so an unversioned tag that actually resolves to 18+ gets the wrong path. Pin the
tag to a version to remove the guess.

### "branch HEAD has moved"

The target rebuilds from HEAD, not from the commit currently running. You cannot
pin it: `git_commit_sha` is settable, but the deploy job resolves
`git ls-remote refs/heads/{branch}` and overwrites it. We report it rather than
pretend to prevent it.

Usually fine. Worth a thought if the delta contains schema migrations, since the
data is byte-exact and the code is not. If you want them to agree, deploy the
source from current HEAD first, then migrate.

### "the compose in git differs from the one the stack is running"

Advisory. A volume **renamed** in git still maps correctly — we pair by mount
path, not by name. One that was genuinely added, removed or re-pathed stops the
migration at DISCOVER with a `VolumePairingError` rather than guessing, which is
the precise check and the one that matters.

## Quiesce failed: SIGKILL (exit 5)

A container exited 137 — the stop timeout was hit and Docker killed it — **and
it was one of the containers mounting a volume this migration copies**. A killed
database has not flushed, so its volume is a torn snapshot.

Raise the resource's stop grace period in Coolify, or raise `STOP_TIMEOUT`, and
retry. This is fatal by design: mirroring an unflushed data directory
byte-exactly just gives you a faithful copy of corruption.

A kill of a container that mounts none of the migrated volumes — an app server,
a worker, a sidecar — is logged as `quiesce.killed_without_data` and does not
stop the run. Nothing it held is being copied. Fix its shutdown path anyway.

## Quiesce failed: preview deployments present

Coolify's stop endpoint does **not** stop preview containers
(`StopApplication` filters `pullRequestId=0`), so they would keep writing while
volumes are mirrored.

Delete them first — they are rebuilt from the PR, so nothing of value is lost:

```bash
coolify-migrate run ... --delete-previews
# or manually: DELETE /v1/applications/{uuid}/previews/{pr_id}
```

## "anonymous volume: its id cannot be reproduced"

Your compose has a mount like `- /data` with no source. Docker names it with a
random 64-hex id that cannot be recreated on the target, and there is no stable
key to pair it by.

Give it a name in the compose:

```yaml
volumes:
  - app-data:/data     # instead of: - /data
```

Refusing beats silently starting with an empty volume.

## "this application also deploys to N additional server(s)"

Refused, on every Coolify release, and no flag overrides it. The target is
created on the target server only, so the additional deployments would be
dropped — and while Coolify's stop of the source reaches them, this tool can only
check the source server's daemon, so a copy left running there would keep serving
the domain next to the new target.

1. **Remove each additional server on the source** in the application's
   **Servers** tab (Configuration), **while that server is reachable**. Coolify
   stops the application there before detaching it — but on an unreachable
   server it skips the stop and detaches anyway, leaving the containers running
   unmanaged. Do **not** use the API's
   `DELETE /applications/{uuid}/destinations/{destination_uuid}`: it never stops
   anything, it only detaches.
2. **Migrate.** The next plan sees no additional servers and proceeds normally.
3. **Add the servers back to the target** in its Servers tab (or, on 4.3+, with
   `POST /applications/{uuid}/destinations`) — **except the migration's target
   server**: it is the primary now, and Coolify refuses an additional destination
   on the primary's own server.

If the application has persistent storage, each additional server holds its
**own** copy of the volumes. Only the primary server's copy is migrated, and
removing a server leaves its copy behind on that server.

On Coolify >= 4.4 step 1 is **one-way** for an application with persistent
storage or built from docker compose: Coolify will not add servers back to such
an application. Deploying to several servers also needs a Docker image set in the
General tab.

The same check runs again when the target is created, so a server attached
between plan and run stops the migration before anything of that application is
parked or created; whatever the run created before it is deleted again.

## "TLS is on for this database … the target starts WITHOUT TLS"

A warning, not a refusal. Coolify reports a database's SSL setting in every read
but accepts it on no create or update call, so the target is created with TLS
off. The database runs fine either way; whether its clients need TLS is your
call. The line says what those clients will see — e.g. `sslmode=require`
clients fail against Postgres, `rediss://…:6380` clients get connection refused.

The target is created **stopped**, before the source is quiesced. Toggle SSL on
it in the UI (the database's SSL settings, same mode) as soon as it appears in
the target environment, and its first start already serves TLS. Missed that
window? Toggle it and restart the target right after cutover.

Postgres with `hostssl` lines in `pg_hba.conf` is the sharp edge: that file lives
in the copied data directory, so a plaintext target refuses **every** TCP client
while its health check (over the local socket) still passes. Toggle SSL before
the first start.

### "once TLS is on for the target, its certificate is issued by the TARGET server's CA"

Also a warning, and it stays even once TLS is back on. Each server has its own
CA (`/data/coolify/ssl/coolify-ca.crt` on that server), and the certificate names
the database's uuid and its server's IP — all three change. Clients that verify
the server (`verify-ca`/`verify-full`, `ssl-ca`, `tlsCAFile`) or pin the source
server's CA need the target server's CA; a MongoDB with `verify-full` also needs
new client certificates signed by it.

## "its custom configuration turns TLS on by itself"

Refused. The source has TLS on and its custom engine config (`postgres_conf`,
`mysql_conf`, `mariadb_conf`, `mongo_conf`, `redis_conf`, `keydb_conf`) enables
TLS itself — `ssl = on`, `ssl-cert`, `require_secure_transport`, `tls-port`,
`net.tls.mode`, … The config is carried verbatim, but the certificate files
Coolify's SSL setting mounts are not, so the target would fail to start (or refuse
every client) after the source is stopped.

Remove the TLS lines from the source's custom configuration — Coolify's SSL
setting applies TLS on its own — then migrate, and re-enable SSL on the target as
described above. Or migrate that database by hand.

## Verification failed (exit 7)

The copy does not match. The target was **not** started and the migration rolled
back; your source is untouched.

The report names every difference. `metadata_differs` with different uid/gid is
the important one — it means ownership changed, which is exactly what stops a
database from starting, and exactly what a content-only checksum would have
missed.

## Rollback failed (exit 8)

A compensating action could not run, so the system is in a state only you can
adjudicate. The error names the journal path.

**Your source has not been deleted** — that only ever happens at an explicit,
confirmed finalize. Inspect with:

```bash
coolify-migrate status <id>
```

## Scheduled tasks

An application's or service's scheduled tasks are recreated on the target during
CREATE, with each task's enabled/disabled state kept. They cannot run before the
target is started: Coolify's scheduler skips a resource that is not running. The
source keeps its own tasks, and they cannot run while the source is stopped
either. If you ever restart the old stack next to the target, both run every cron.

### "scheduled task(s) … run with a timeout above 36000s"

A refusal. Coolify 4.4+ refuses a timeout above 36000 seconds on create. Only a task
created through the API before 4.4 can have one. Lower it on the source (the UI's
own limit is 36000), then plan again.

### "scheduled task … its command is not readable" (exit 10)

From 4.4, Coolify returns a task's command only to a token that can read sensitive
data. Fix the token as in the first section above. Nothing was stopped.

### "… name their container by its full name" / "… fire on the server's clock"

Warnings, not blocks. A full container name contains the source's uuid and stops
matching once the target runs more than one container: set the task's container
to the compose service name (`web`, not `web-<uuid>`). A different
`server_timezone` on the target server means the same cron fires at a different
local time.

### `api.scheduled_tasks.container_missing` in the run log

The target is created from the instance's current service template, or from git
HEAD for a compose app, and that compose no longer has a service with this task's
container name. The task was still created. Point its container at one of the
`available` names on the target.

### "the target's scheduled tasks do not match what was created"

A create timed out at a proxy (502/504) after Coolify had already saved it, and
the retry saved it a second time. CREATE refuses a doubled cron, deletes the
target and leaves the source running. Re-run the migration.

## Database backup schedules

A standalone database's backup schedules are recreated on the target during
CREATE, with their frequency, retention, timeout and enabled state. The target
makes no dumps before it is started: Coolify skips a database that is not
running. These cases come up as plan warnings, never as blocks:

- **"… upload to S3 … created LOCAL-ONLY"**: below Coolify 4.5 the API does not
  say which S3 storage a schedule uses. Select it in the target database's
  **Backups** tab after the move, or off-site backups stop.
- **"the source has N backup run(s) on record"**: history is not migrated. The
  old dumps stay on the source server under the directory named in the warning,
  and in S3 if they were uploaded. Nothing deletes them, and no retention prunes
  them once the source is stopped. Remove them by hand when you no longer need
  them.
- **"keep no local dumps on the source (disable_local_backup)"**: no API can set
  that, so the target keeps local dumps. Switch it off on the target if disk
  matters.
- **"service database(s) … can have backup schedules"**: no Coolify API exposes a
  service database's schedules. Check each one's Backups tab on the source and
  recreate them on the target by hand.
- **"… not carried: Coolify has no backup for redis"** (also KeyDB, Dragonfly):
  those schedules never produced a backup on the source either.

**Volume backups** (Coolify 4.3+, a volume's **Backups** setting) are not carried.
The API can create them but cannot read them, so the plan cannot see them. If you
use them, set them up on the target again.

### "the target database holds N backup schedule(s) where M were created"

The target did not hold what CREATE made: a schedule saved twice, or one not
reported back. A doubled schedule dumps twice per run, so CREATE deletes the
target and leaves the source running. Re-run the migration.

### Rolled back, and the target had already made backups

A rollback deletes the target database and its schedules, but **not its dump
files**. A rollback can also be run after a successful migration, and then those
dumps are the only copy of everything written since the cutover. The log line
`compensate.target_backup_files_kept` names where they are. Delete them yourself
once you are sure.
## CREATE refused a service's domains

Both refusals come before the source is stopped: the created target is deleted
and the source keeps serving.

- **"the target has no sub-application(s) [...] to carry the source's domains
  to"** — a service is created from this instance's *current* template catalog,
  and the template now names those containers differently, so their domains
  would be served by nothing. Migrate the service by hand, or re-create the
  source from the current template first.
- **"the target cannot claim its domain(s): Domain ... is already in use by
  ..."** — another resource of the team, or the Coolify instance itself, holds a
  hostname the service serves. The tool forces a domain away only from the
  source service itself, never from anything else. Free it on the resource the
  message names, then run again.

## `422 This field is not allowed`

Our request whitelist has drifted from upstream Coolify. Coolify names the
offending field in the response.

Please open an issue with that field name. A scheduled CI job checks for this
weekly precisely so it breaks our CI rather than your migration — but Coolify may
have changed since the last run.

## Host key is not known

```bash
# Verify the fingerprint out of band first, then:
coolify-migrate run ... --trust-host-key
```

We never disable host-key checking. A *changed* key is still refused even with
`--trust-host-key`.

## The transfer only uses one stream

Expected if the volume contains hardlinks — the plan says so in its `reason`.
`rsync -H` only detects hardlinks within a single invocation, so splitting would
explode them into duplicate files. A backup volume using hardlinked snapshots can
go from 50 GB to several hundred. Correctness beats parallelism.
