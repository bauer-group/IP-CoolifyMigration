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

## Locked (`is_shown_once`) variables

Coolify returns a locked variable's value through **no** API read, not even to a
root token. The tool reads it back instead from the file Coolify itself wrote on
the source server — `/data/coolify/applications/{uuid}/.env`,
`/data/coolify/services/{uuid}/.env`, or the `environment:` list of
`/data/coolify/databases/{uuid}/docker-compose.yml` — and sets it on the target,
**still locked**. A Redis `REDIS_PASSWORD` comes from the database's connection
URL in the API, so it needs no file at all.

The plan says what it will carry:

> locked (is_shown_once) variable(s) ['API_SECRET'] will be read from
> /data/coolify/services/abc/.env on the source server and set on the target,
> still locked. They carry the value Coolify last wrote on the source server …

*The value Coolify last wrote* is exact: the tool decodes the line, re-encodes
it the way Coolify does and accepts it only if the line comes back byte for byte.
It never guesses. Two consequences worth knowing:

* A `{{project.X}}`-style reference **inside** a value arrives resolved.
* A secret-manager value (`{{vault.X}}`) is written to the file resolved. Where
  its line is indistinguishable from a plain value's, the resolved secret is
  carried — Coolify's secret-manager link is visible through no API.

### "… could not be read back from the source server (reason)"

A **refusal** when the target would generate its own value for the key — a
service's or a dockercompose application's `SERVICE_PASSWORD_*` /
`SERVICE_USER_*`, a Redis `REDIS_PASSWORD` — because the copied data was
initialised with the source's. A **warning** otherwise: the target is created
without the key; set it by hand. The reason says what to do:

| Reason | What to do |
| --- | --- |
| `… is missing or unreadable on the source server (exit N)` | The resource was never deployed/started since the variable was locked, or the SSH user is not root (there is no sudo support). Deploy or restart the source once, or connect as root. |
| `the source's last deployment was pull request #N` | A preview deploy overwrote the application's `.env`. Redeploy production once, then plan again. |
| `… does not match the resource's current configuration` | A variable changed (API or UI) without a redeploy/restart, so the file is stale. Redeploy or restart the source once, then plan again. |
| `… changed while it was read` | Coolify was rewriting the file. Plan again. |
| `build-time only: Coolify never writes it to the server` | Build-only variables live only in the throwaway build container. Unlock the variable, or set it on the target. |
| `a {{...}} shared-variable reference` | The file holds the resolved value, not the reference. Unlock it on the source, or set it on the target. |
| `… cannot be delimited exactly` / `… is ambiguous` / `… is not in the encoding Coolify uses …` | A multi-line value saved before Coolify 4.5, or a secret-manager value. Delete and re-add the variable unlocked (or save it again on 4.5 and redeploy), then migrate. |
| `the source database's API connection URL and … disagree` | The Redis password in the database record and the running compose differ. Restart the source database once. |

A locked **build-time** variable of an application the target rebuilds gets an
extra warning: the target's build runs after the source is stopped, without the
variable, so a build that needs it fails and the run rolls back.

### "the recovered value of KEY names the source server's wildcard domain or IP"

The value is carried unchanged. If it should point at the target (a callback
URL, a database host), update it on the target after the cutover.

### "can no longer be read back from the source server" at CREATE_TARGET (exit 2)

CREATE reads every locked value again before it writes anything. A key the plan
promised to carry — or a credential the target would regenerate — that cannot be
read any more refuses the run. **Nothing has been changed**: fix the reason it
names (usually a redeploy of the source since the plan), then run again.

## The DNS gate blocked me (exit 3)

Working as designed. It is a resumable stop, not a failure — the target is created
and its data is verified. The gate checks the domains the **target** will serve
(it claimed the source's custom domains at create) and compares them against the servers' **resolved** addresses (Coolify's `ip` is often
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

The run rolls back on its own, and that includes restarting the source: the stop
had already been requested, and Coolify's stop removes the containers, so the
source is down although QUIESCE never completed. The same holds when the stop
gate times out.

A kill of a container that mounts none of the migrated volumes — an app server,
a worker, a sidecar — is logged as `quiesce.killed_without_data` and does not
stop the run. Nothing it held is being copied. Fix its shutdown path anyway.

## Quiesce failed: could not pin the source's volumes (exit 5)

Before it stops anything, the migration creates a never-started container that
keeps Coolify's Docker cleanup off the source's volumes (see
[safety](safety.md#coolifys-docker-cleanup-cannot-prune-the-data-in-flight)).
The `docker create` on the source failed, so the run refused to stop the source
unprotected. **Nothing was stopped**; the created target is deleted.

The hint carries Docker's own error. The pin needs no registry: it imports an
empty image with `head -c 10240 /dev/zero | docker import -`, so check that the
daemon answers and has disk space, then re-run.

## "Left in place: ... still pins ..."

A run or a rollback ended with a pin it deliberately did not remove:

- **after a rollback**, the restarted source did not mount its volumes within
  `DEPLOY_TIMEOUT` — or its restart failed. Removing the pin then would let
  Coolify's next cleanup delete the
  source's data on a server with *Delete unused volumes* on. Get the source
  running, then `coolify-migrate rollback <id>` again: it removes the pin once
  the volumes are mounted.
- **after a finalize**, a copied target volume is mounted by no target
  container. Check the resource's storages in Coolify; once something mounts the
  volume, `docker rm <pin>` on the target removes the pin.

A pin is a stopped container that never ran; it holds no process and writes
nothing. List a migration's pins with
`docker ps -a --filter label=bg-coolify-migrate.migration=<id>`. A PREFLIGHT
warning `preflight.foreign_pin` means another migration's pin mounts this
stack's volume: harmless, and that migration's finalize or rollback removes it.

## Plan warning: "server 'X' has 'Delete unused volumes' on"

Not a blocker. That server's Docker cleanup (Coolify > Server > Docker
Cleanup) runs `docker volume prune -af`, which deletes every volume no
container references, and a stopped resource has no containers. The migration
pins what it stops and fills, so the move itself is safe.

The second line is the one to act on: after a KEEP or RENAME finalize the
source stays stopped with no containers, so the next cleanup on that server
deletes its volumes. If you want the old source as a fallback copy, turn
*Delete unused volumes* off on the source server before you finalize, or back
the data up. The tool never changes the setting for you.

## Quiesce failed: preview deployments present

Coolify's stop endpoint does **not** stop preview containers
(`StopApplication` filters `pullRequestId=0`), so they would keep writing while
volumes are mirrored.

Delete them first — they are rebuilt from the PR, so nothing of value is lost:

```bash
coolify-migrate run ... --delete-previews
# or manually: DELETE /v1/applications/{uuid}/previews/{pr_id}
```

## "still answers on old-xxxxxx.…, parked by an earlier migration"

(Versions up to 2.9 parked a source's custom domains at CREATE; current versions
never touch the source before FINALIZE.)

An earlier migration of this project freed the source's custom domain for its
target by renaming it to `old-<tag>.<host>` in Coolify — and its rollback could
not give it back. The tag is the same for every run of this project and
environment, so a new run would read the parked host as the real one, carry it
to the target and lose the real domain from every record.

Set the domain back to the host the message names (without the `old-…` prefix)
on the source in Coolify, redeploy the source, then plan again.

## "compose service domains route to port(s) their domain does not name"

Coolify >= 4.4 moves the `:port` of a compose domain into a hidden column that
no API read returns, and routes the domain there. The target would route it to
the service's first exposed port instead, and releasing the source's domains at
finalize would erase the port on the source as well — a rollback after finalize
could not bring it back.

Make the port the service's **first** `expose:` entry in the compose (in git),
redeploy the source so Coolify regenerates its labels, then migrate. Otherwise
migrate the application by hand.

If the plan instead **warns** that the parsed compose could not be read, the
check could not run: confirm the compose domains' ports in Coolify before
migrating.

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
created; whatever the run created before it is deleted again.

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

## "DISCOVER paired volumes for X, but this run holds no pairs" (exit 6)

COPY or VERIFY found DISCOVER's volume pairs in the journal but not in the
running process. Proceeding would have copied or verified nothing and still
reported success, so the run stops and rolls back: the source is restarted and
the target deleted.

A resume rebuilds the pairs from the journal, so this should not happen. If it
does, please report it with the journal (`coolify-migrate status <id>`), then
start a fresh run.

## Rollback failed (exit 8)

A compensating action could not run, so the system is in a state only you can
adjudicate. The error names the journal path.

**Your source has not been deleted** — that only ever happens at an explicit,
confirmed finalize. Inspect with:

```bash
coolify-migrate status <id>
```

### "rollback refused: FINALIZE ran with policy 'delete'"

The one exit 8 where the source **is** gone: the migration got as far as deleting
it, with the confirmation `delete` requires. From there the **target is the only
copy** of the data, and undoing the run would mean stopping the target,
dropping its volumes and deleting it — so nothing was undone, on purpose.

Check the target in Coolify: it holds the data. If FINALIZE stopped part-way,
some source resources may still exist; delete them by hand once the target is
verified. The journal named in the hint shows what FINALIZE recorded.

### "point DNS for … back to …"

Not an error — a step a rollback cannot take for you. The DNS gate had already
found these custom domains pointing at the target (or you accepted the cutover
with `--accept-dns`). The source has its domains back and runs again, but DNS
still sends their traffic to the target this rollback removed. Point the records
back at the source; until then those domains are dead, and the source's proxy
cannot obtain a certificate for them.

### "creating the target failed, and undoing it did not complete"

CREATE failed before anything was stopped, then could not fully undo itself —
a target is left on the new server (kept while it holds custom domains its
source does not, or because its delete failed).
The source never stopped and is still serving. Fix what the message names (often
the same outage that failed the create), then let the tool finish the undo:

```bash
coolify-migrate rollback <id>
```

`resume` refuses this journal on purpose; `rollback` retries exactly the
unfinished undo, and running it again is safe — a target already gone counts as
deleted.

### `domains NOT restored: shop -> https://shop.example.com (...)`

The rollback could not give the source its own domains back — they were
released at FINALIZE. Everything else still ran — a stopped source was
restarted — so it is up, but without its custom hosts.

Set exactly the listed domains back on the source in Coolify, then **Restart**
it: Coolify writes a domain change to its database only and bakes the proxy
labels into the container at deploy time, so the restart is what makes the
domain live again.

`held by <name>/<uuid>, which this migration did not create - not forcing` means
another resource claims the same domain. We override a conflict only with this
migration's own sources and targets; anything else is yours to decide.

### `target(s) KEPT, holding custom domains their source does not hold`

A rollback deletes a target that holds custom domains only once its source holds
them again: deleting the last holder makes Coolify (4.4+) delete a DNS record it
manages at Cloudflare, for good. The target is stopped and harmless. Give the
source its domains back (the message above it names them), then run
`coolify-migrate rollback <id>` again — it deletes the target now.

### `the target cannot claim its domain(s), held elsewhere: ...`

CREATE refused before anything was stopped: another resource of the team — or
the Coolify instance's own URL — holds a custom domain of the source. The message
names each holder by type and uuid (an orphaned target of an earlier run carries
the source's name, so the uuid is what tells them apart). Free the domain there,
then migrate again.

### `the target does not hold [...] after claiming its domains`

Coolify accepted the domain update but did not keep it: the target server's
proxy is off (Coolify ignores domains then), or a compose service of that name
is missing from the target's compose. The source was never stopped; the target
is deleted. Fix the target server or the compose, then migrate again.

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

## The plan refuses a service's URLs

- **"sub-applications [...] share the URL ..."** — two containers of the stack
  are set to the same URL. Coolify rejects the target's whole domain update when
  that happens (from 4.2 comparing with scheme and host lowercased), so none of
  the service's domains could be carried. Give each sub-application its own URL
  on the source, then plan again.
- **"sub-application(s) [...] carry a URL with credentials in it"** — a domain
  written as `https://user:password@host`. It would be sent to Coolify and
  repeated in its error messages, our logs and the journal, so the tool refuses
  before reading it any further. Remove the credentials from the URL on the
  source; Coolify's basic auth is configured elsewhere.

## CREATE refused a service's domains

Both refusals come before the source is stopped: the created target is deleted
and the source keeps serving.

- **"the target has no sub-application(s) [...] to carry the source's domains
  to"** — a service is created from this instance's *current* template catalog,
  and the template now names those containers differently, so their domains
  would be served by nothing. Migrate the service by hand, or re-create the
  source from the current template first.
- **"the target cannot claim its domain(s), held elsewhere: ..."** — another
  resource of the team, or the Coolify instance itself, holds a hostname the
  service serves. The tool forces a domain away only from this migration's own
  sources and targets, never from anything else. Free it on the resource the
  message names (by type and uuid), then run again.

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
