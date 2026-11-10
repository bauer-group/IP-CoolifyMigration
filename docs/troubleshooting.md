# Troubleshooting

## `doctor` says the token cannot read sensitive data (exit 10)

Create a new token in Coolify under **Keys & Tokens > API tokens** with `root`, or
`read` + `read:sensitive` + `write` + `deploy` (plus `delete` from Coolify 4.6) —
as an admin or owner of the team. Since
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
service's or a dockercompose application's generated magic variables
(`SERVICE_PASSWORD_*`, `SERVICE_USER_*`, `SERVICE_BASE64_*`, `SERVICE_HEX_*`,
`SERVICE_LOWERCASEUSER_*`, `SERVICE_SUPABASEANON_*` and the rest of Coolify's
list), a Redis `REDIS_PASSWORD` — because the copied data was initialised with
the source's. A **warning** otherwise: the target is created
without the key; set it by hand. The reason says what to do:

| Reason | What to do |
| --- | --- |
| `… is missing or unreadable on the source server (exit N)` | The resource was never deployed/started since the variable was locked, or the SSH user is not root (there is no sudo support). Deploy or restart the source once, or connect as root. |
| `the source's last deployment was pull request #N` | A preview deploy overwrote the application's `.env`. Redeploy production once, then plan again. |
| `… does not match the resource's current configuration` | A variable changed (API or UI) without a redeploy/restart, so the file is stale. Redeploy or restart the source once, then plan again. |
| `… changed while it was read` | Coolify was rewriting the file. Plan again. |
| `build-time only: Coolify never writes it to the server` | Build-only variables live only in the throwaway build container. Delete the variable and re-add it unlocked with the same value (a lock cannot be lifted: from Coolify 4.7 not even through the API), or set it on the target. |
| `a {{...}} shared-variable reference` | The file holds the resolved value, not the reference. Delete it on the source and add the same reference again unlocked, or set it on the target. |
| `… cannot be delimited exactly` / `… is ambiguous` / `… is not in the encoding Coolify uses …` | A value saved before Coolify 4.5 whose end the file cannot prove (a multi-line one, or one followed by a line Coolify writes only sometimes, such as `PORT=` or `COOLIFY_*=`), or a secret-manager value. Delete and re-add the variable unlocked (or save it again on 4.5 and redeploy), then migrate. |
| `the source database's API connection URL and … disagree` | The Redis password in the database record and the running compose differ. Restart the source database once. |
| `Coolify >= 4.5.1 withholds the connection URL of a locked credential, so … is the only source, and …` | From 4.5.1 the API no longer shows a locked Redis password inside the connection URL, so the database's compose file on the source server is the only place to read it. Fix what the rest of the reason names (usually: restart the source database once so Coolify rewrites the file). Do not delete and re-add a locked `REDIS_PASSWORD`: without the variable Redis falls back to the database's own password field, so a restart in between changes the server password. |

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
(it claimed the source's custom domains at create) and compares them against the
servers' **resolved** addresses — each server's `ip` (often a hostname) and its
wildcard base, which is the public address custom records usually point at. It
fires, per host, for a **custom domain** (`shop.example.com`) that still points
at the source, that resolves to neither server, or that resolved when the
migration was planned and resolves to nothing now. Server-bound URLs under a server's wildcard
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
`--accept-dns` covers that plain cutover only; the cases below are accepted one
host at a time.

### "the target would serve hostname(s) bound to the SOURCE server's wildcard"

A host the target is configured to route still sits under the **source**
server's wildcard: it was not rewritten onto the target's, usually because the
target has no wildcard configured. The wildcard's DNS record binds it to the
source for good, so no DNS change moves it and the target could never obtain a
certificate for it. This is not a cutover, so `--accept-dns` does not apply.
Change the target's domain in Coolify to a host under the target's wildcard (or
a custom domain), then `coolify-migrate resume <id>`. For a service, the gate
reads each sub-application's domain, the routing variables of a minio, logto,
garage or silo image (`MINIO_SERVER_URL` and friends — only when the stack runs
that image; for any other image they are ordinary variables) and Host() labels
written into a custom compose — not the derived `SERVICE_URL_*` variables.

### "… (elsewhere)": resolves to neither source nor target

A CDN, reverse proxy or load balancer is in front (Cloudflare's orange cloud, or
any other). DNS then tells us nothing about where the origin points — and it may
well still be the source, which is exactly the ACME failure the gate exists to
prevent. So it blocks, for every provider. The same goes for a host that resolves
only to an address **both** servers answer as — a wildcard base they share, or
one CDN in front of both wildcards: DNS cannot tell them apart. Make sure the
origin points at the target, then accept that one host:

```bash
coolify-migrate resume <id> --accept-dns-host shop.example.com
```

### "… (vanished)": resolved at plan time, resolves to nothing now

The host's record was deleted (or its zone broke) since the plan. From Coolify
4.4 a record Coolify manages at Cloudflare is deleted when its hostname loses
its last holder — this tool hands every hostname over so that never happens, but
a person or another tool can. Recreate the record pointing at the target (the
checklist names the old value), or accept the host with `--accept-dns-host` if
you know why it is gone.

### "… (unanswered)": resolved at plan time, DNS could not be asked now

The lookup timed out or the zone's nameservers failed (SERVFAIL, none
reachable). That is no answer at all: the record may be fine. But the source is
already stopped and nothing shows where the host points, so the gate does not
guess. Run `coolify-migrate resume <id>` to ask again; if DNS stays unreachable
and you have checked that the record points at the target, accept the host with
`--accept-dns-host`.

### "DNS no longer sends these custom hosts to the source, which still serves them"

CREATE refused, before anything was stopped: a custom host that resolved to the
source when the migration was planned now resolves elsewhere or to nothing. Its
record changed under the migration. The targets created by this run are deleted
again. Put the record back, then plan and migrate again. (A lookup that merely
timed out never triggers this.)

### Plan warning: "the zone is on Cloudflare, and Coolify >= 4.4 may manage these records"

Not a problem by itself. From 4.4 Coolify can manage DNS records at Cloudflare,
and it deletes one it manages when the hostname loses its last holder. The
migration hands every hostname over make-before-break, so that never happens.
Coolify does not re-point a record on its own, so the cutover is yours: after the
DNS gate, change the record at Cloudflare to the target's address. For a Cloudflare
record Coolify does *not* manage, the target's
**Domains > Configure DNS > Replace record** does it from Coolify's UI.

### Plan warning: "automatic DNS is on for the zone's integration token"

From 4.6, Coolify creates a DNS record when an API save adds a hostname to a
resource, and the target claims its hostnames while the source still serves. A host
with its own record at Cloudflare is safe: Coolify reports a conflict and writes
nothing. A host that resolves only through a wildcard record is not: it gets a new
record pointing at the target before the target runs, and a rollback does not
remove it. Give such a host its own record first, or turn automatic DNS off on that
integration token for the migration and back on afterwards. Coolify's API cannot
tell the two cases apart, so the plan warns for both.

### Plan warning: "… resolve(s) to neither server today"

The host already resolves to something other than both servers — a CDN, proxy
or load balancer. The DNS gate will stop before the target starts until you
accept that host by name, and by then the source is stopped. Check where its
origin will point, then pass `--accept-dns-host HOST` on the run itself.

### Plan warning: "custom hostname(s) also served outside this plan"

Another application or service of the team holds the same hostname (on another
path or port, or simply not selected for this run). DNS moves per hostname, so
the cutover would send its traffic to the target too, where it does not run.
Migrate it in the same run, or give it its own hostname before the cutover.

### After a rollback: "point DNS for … back to …" / "recreate the DNS record of …"

The rollback re-resolves every custom host at its end. A host that now resolves
to the target — through the gate, an accepted cutover, or a change you made by
hand — must be pointed back at the source, and one whose record vanished since
the plan must be recreated. The run report lists both.

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

## "could not switch off the auto-deploy of … before the stop" (exit 5)

QUIESCE's first write to the source failed: switching off its auto-deploy (and
preview deployments) so that a push cannot deploy the stopped source, or its
target before the DNS gate. **Nothing was stopped**: what was already switched
off is switched back on and the created target is deleted. The message carries
Coolify's answer; usually the token cannot write applications. Fix that, then
run again.

## "a source deployment has not ended, so the stop is refused" (exit 5)

`SourceDeploymentPending`, raised **before anything is stopped**. Coolify's stop
cancels no deployment, and its queue starts what is waiting, so the source would
come back up after the stop, beside a copy that never sees its writes. The lines
under it say which:

- **"…: deployment … is still in_progress after 900s"** — QUIESCE waited
  `DEPLOY_TIMEOUT` for it. Let it finish (or raise `DEPLOY_TIMEOUT` for long
  builds), or cancel it in the application's Deployments in Coolify;
- **"…: deployment … is queued"**, with a hint that says it was queued after the
  wait — something deployed the source in the seconds before the stop (auto-deploy
  is off by then: a CI call, the dashboard);
- **"Coolify's deployment queue could not be read for 900s"** — the API did not
  answer; check it.

Then run the migration again. This tool never cancels a source deployment. The
source keeps serving, its auto-deploy is switched back on and the created target
is deleted.

## "the source was started again after QUIESCE stopped it" (exit 5)

A containment check (the message says where: `found at the end of COPY`, `the
DNS gate`, `START`, `FINALIZE` …) found the stopped source running again. Each
line names the resource and the evidence:

- `… is running` — a container runs now;
- `… was started at …, after the stop, and has stopped again` — its `StartedAt`;
- `… (the daemon's event log; the container is gone)` — it came and went;
- `deployment … came after the stop, or had not ended by it` / `deployment … is
  queued (Coolify's deployment history)` — an application's deployment the stop
  did not see ended.

The usual causes: a CI job calling Coolify's `/deploy` with the source's uuid or
one of its tags, a deploy or restart from the dashboard, the API or MCP — none of
which auto-deploy being off stops — or a deployment queued just before the stop.

### Before START: rolled back

The run rolls back by itself: the target is stopped and deleted with its copied
volumes, and the source restarted. Nothing deletes the source before FINALIZE, so
what it wrote survives. Hold pushes and CI deploys of it, then migrate again.

### After START: "Stopped - the source was started again, and needs your decision"

Found by a resumed START or by FINALIZE, once START had asked Coolify to start a
target. A **block**, not a failure, and **nothing has been undone**: the DNS
gate let the target start, it may have served users since, and a rollback would
delete what they wrote. FINALIZE has not touched the source. Two ways on:

- **finish the migration**: stop the source in Coolify, cancel any of its
  deployments still queued or in progress, hold pushes and CI deploys of it,
  then `coolify-migrate resume <id>`. FINALIZE then applies its policy — under
  `delete` that deletes whatever the revived source wrote. The resume blocks
  again while a source container runs or a source deployment has not ended, and
  once more for a deployment that was still queued or running at the block,
  after it ends: it may have started the source after that check;
- **give up the target**: `coolify-migrate rollback <id>` deletes the target and
  everything written to it since START, and restarts the source.

### `containment.event_log_gap` in the run log

A warning, not a refusal. The Docker daemon keeps its last 256 events, and none
across a restart; on a busy server that window often does not reach back to the
stop. A source container that started and was removed again before it cannot be
ruled out — one that still exists is found, and an application's Coolify
deployment history still counts. A service or a database has no such history:
if one may have been deployed in that time, check it before you resume.

## "a target was started before START asked for it" (exit 5)

`TargetStartedEarly`. A target ran before the DNS gate let it: it served custom
hosts that still resolve to the source, asked ACME for their certificates, and may
have written to volumes being copied. START says the same in its own words:

- **"the target of … was started before START asked for it: Coolify refused
  START's request, reading it as already running"** — a service or database
  answered "already running", which a target this run never started cannot be;
- **"Coolify skipped START's deployment of the target of …: a deployment of the
  same commit is already queued or running for it"** — someone else deployed it.
  With "- or this run's own request from before an interruption" the run was
  killed right after START asked, and the skip may be its own.

The usual causes: a CI job calling `/deploy` with its uuid (or a tag someone gave
it — the run attaches tags only at FINALIZE), a deploy from the dashboard, the
API or MCP, or a git push after someone switched the target's auto-deploy on: it
is created off, and shares its source's repository, so a GitHub App or a manual
webhook delivers the same push to both. The run rolls back: the target is
stopped and deleted with its volumes, the source restarted. Hold pushes and CI
deploys of the stack, then migrate again.

## "could not read the deployment history of … to check that nothing deployed the stopped source" (exit 5)

A source application's deployment history is what shows a deploy whose container
came and went, so a check that cannot read it never passes:

- **at QUIESCE**, before the stop: the stop is refused and the run rolls back
  with the source still serving. A source Coolify itself answers 404 for (it was
  deleted in Coolify) refuses here too;
- **at a later check before START**: the run rolls back;
- **once START has asked for a target**: "Stopped - the source could not be
  checked, and nothing was undone" — a block, for the reason a revival blocks.
  Once Coolify answers again, `coolify-migrate resume <id>` checks again; or
  `coolify-migrate rollback <id>` gives up the target.

The hint quotes Coolify's answer for each application. The usual cause is a
Coolify that is restarting or updating, or a proxy in front of it answering
for it.

## "Left in place: ... still pins ..."

A run or a rollback ended with a pin it deliberately did not remove:

- **after a rollback**, the restarted source did not keep its volumes mounted
  — no running container mounted them within `DEPLOY_TIMEOUT`, or one did and
  was gone again within the settle window (Coolify removes a restarted
  container that fails its health check) — or its restart failed, so a stop
  the run had asked for could still land. Removing the pin then would let
  Coolify's next cleanup delete the source's data on a server with *Delete
  unused volumes* on. Get the source running, then
  `coolify-migrate unpin <id>`: it removes the pin once running containers keep
  the volumes mounted for the settle window (`STOP_TIMEOUT`, at least two
  minutes), and touches nothing else. Do **not** run `rollback <id>` again for
  this — it replays every compensation, and its restart redeploys a source that
  is serving by then.
- **after a finalize**, a copied target volume is mounted by no running target
  container. Check the resource's storages in Coolify; once something mounts the
  volume, `coolify-migrate unpin <id>` (or `docker rm <pin>` on the target)
  removes the pin.

`unpin` only works on a migration that has finished or rolled back; while one is
blocked or interrupted its pins belong to the run — resume it or roll it back.
It exits `1` while a pin still has to stay, naming the volume nothing running
mounts.

A pin is a stopped container that never ran; it holds no process and writes
nothing. List a migration's pins with
`docker ps -a --filter label=bg-coolify-migrate.migration=<id>`. A PREFLIGHT
warning `preflight.foreign_pin` means another migration's pin mounts this
stack's volume: harmless for a keep or rename finalize. With `--finalize delete`
it is a refusal instead, and so is a FINALIZE that finds one: Coolify's delete
cannot remove a pinned volume and does not say so, so the source would be
reported deleted while its data stays. `coolify-migrate unpin <that id>` once
its source runs, or `docker rm` the pin, then re-run (or resume).

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

Coolify >= 4.3.15 moves the `:port` of a compose domain into a hidden column that
no API read returns, and routes the domain there. The target would route it to
the service's first exposed port instead, and releasing the source's domains at
finalize would erase the port on the source as well — a rollback after finalize
could not bring it back.

Make the port the service's **first** `expose:` entry in the compose (in git),
redeploy the source so Coolify regenerates its labels, then migrate. Otherwise
migrate the application by hand.

If the plan instead **warns** that the parsed compose could not be read, the
check could not run: confirm the compose domains' ports in Coolify before
migrating. From Coolify 4.5.1 that also happens when a locked variable's value
(4+ characters, e.g. a locked `PORT=3000`) occurs in a domain's labels or
ports: Coolify shows `<locked>` there instead, and the port cannot be read.

## "anonymous volume: its id cannot be reproduced"

A running container mounts a volume with no name: a compose mount like
`- /data` with no source, or — more often — a `VOLUME` the image declares and
the compose does not mount (SonarQube's `/opt/sonarqube/extensions` and
`/opt/sonarqube/temp`). Docker names it with a random 64-hex id that cannot be
recreated on the target, and there is no stable key to pair it by.

Coolify removes every container on stop and before every deploy (`docker rm
-f`, then `compose up`), so such a volume already comes back **empty** after
each restart: its contents are nothing Coolify keeps. Stopped, the stack has no
container mounting it, and the plan clears. So an interactive `run` asks:

```text
Stop <name> now and migrate offline?
```

Yes stops the stack through Coolify, waits until its containers are gone, plans
again and continues; the downtime starts then instead of at QUIESCE. If the run
ends before it would have stopped the source itself (an abort, a failure in
CREATE), the source is started again. Unattended (`--yes`) runs do not stop
anything: stop the stack in Coolify, then run again.

Not offered for databases — there an anonymous volume can be the data itself
(an image whose data path moved, like Postgres 18's `/var/lib/postgresql`).

If the contents matter, give the volume a name in the compose before migrating:

```yaml
volumes:
  - app-data:/data     # instead of: - /data
```

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

## Bind mounts refused at the end of CREATE (exit 6)

CREATE reads back where the target mounts each bind and checks the place on the
target server **while the source still serves**: the source was never stopped,
and the created target is deleted.

- **"bind mount(s) have no free place on the target"** — the target path already
  holds data ("may be another resource's"), is a symbolic link or a special file,
  has a parent that is a symbolic link or a file, or is mounted by a container on
  the target server. Free the path there, or bind a different one on the source.
  A target whose `/data` or `/data/coolify` is a symlink (Coolify's data moved to
  another disk) refuses every relative bind (`./data`): a known limit that fails
  closed — migrate those resources by hand, or to another target;
- **"bind mount(s) the target would place on a host path that is not its own"** —
  the target's storage row names a system path, Coolify's own state or another
  resource's directory, usually because the target was created from a different
  compose (the branch HEAD moved);
- **"file mount(s) the target would start without"** — Coolify writes such a file
  from the content kept in its storage row, and the target's row has none: the
  template or the repository's compose no longer declares it with its content.
  Keep the file in a directory the compose mounts instead (it is then copied as
  data), or move the resource by hand;
- **"bind … has no counterpart on the target"** / **"… matches several target
  storages"** — no single storage row of the target at that mount path, so the
  target would not mount the copy; **"… would both be copied to …"** / **"… lies
  inside …: one copy would delete the other"** — two binds collide on the target.
  Give each resource its own host path.

## Bind mounts refused at DISCOVER or COPY (exit 6)

After the stop; the run rolls back (the source is restarted, the target deleted).

- **"bind mount(s) cannot be copied"** — the source path is gone, a symbolic
  link, or not a regular file or directory now;
- **"bind target … already holds data on the target server"** (or another state
  that changed since the end of CREATE) — something appeared there that is not
  this migration's, and it is not overwritten. One case is known and harmless: a
  COPY resumed between two nested binds (`./data` and `./data/uploads`) refuses
  the inner one, which the outer copy filled. A new run copies both;
- **"bind mount(s) this version never copies"** — the run began under an earlier
  version, whose journal paired a system path. Nothing was written there. If the
  rollback does not finish, run `coolify-migrate rollback <id>`; then plan again
  with this version.

## "the targets' containers are up, but the deployment START asked for did not finish within …s" (exit 6)

HEALTHCHECK counts a target's containers only once the deployment START asked
for has finished: a container that is up before then may be another
deployment's — a push or a CI deploy of the target. Each line says what Coolify
last read for that deployment, or that it could not be read. Check the deployment
in Coolify. The run rolls back; the source is still intact.

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

Check the target in Coolify: it holds the data. The hint starts with what
failed. If FINALIZE stopped part-way, some source resources may still exist:
`coolify-migrate resume <id>` finishes the step — each source deletion is
journalled the moment Coolify accepts it, so a resume skips what is already
gone, and a source Coolify answers 404 for counts as deleted. Or delete them by
hand once the target is verified; if the hint names a pin that holds the
remaining sources' volumes, `docker rm` it first, or Coolify's delete keeps
them. The journal named in the hint shows what FINALIZE recorded.

### "rollback refused: the source no longer holds its data"

The migration finished (or got into FINALIZE) with `keep` or `rename`, and the
source has since lost what a rollback would restart it on. The message lists
what is missing:

- **"… (no longer in Coolify)"** — a source resource was deleted;
- **"volume …"** — a copied volume is gone from the source server, usually
  Coolify's Docker cleanup with *Delete unused volumes* on, which the plan warned
  about (FINALIZE released the source's pins);
- **"volume … (created …, after the copy read the one created …: recreated
  empty)"** — the volume was pruned and a redeploy created a new, empty one under
  the same name;
- **"bind … (gone)"**, **"(now a …)"**, **"(empty now, where it held data when it
  was copied)"** — a copied bind mount's host path was removed, replaced, or
  emptied (a start recreates a missing one as an empty directory).

A rollback would stop the target, drop its volumes and bind copies, delete it
and restart the source on **empty** volumes and directories, so nothing was
undone: the target holds the only copy, including everything written since the
cutover. Keep it. Run the rollback again only once the source's data is back
(from a backup); otherwise the target is the migrated stack, and the source is
yours to clean up. `could not be read` / `could not be checked` means the look
itself failed (API or SSH): fix that and run the rollback again.

### `bind target(s) KEPT on …: … - they may hold the only copy of the data`

DROP_TARGET_VOLUMES removes on the target server only the bind paths COPY
created, and kept these. The reason is in brackets: the source's bind is gone
(after a `keep` or `rename` finalize the copy may be the only one), the path is
not one a rollback may delete, or it resolves elsewhere now (a symbolic link
appeared). The rest of the rollback still ran. Delete them by hand once you are
sure the data exists elsewhere; a second `rollback` keeps them while the reason
holds.

### `… was empty before this migration and now holds a copy of the source's bind mount`

Not an error: a rollback notice. The target path existed, empty, before COPY
filled it, so it is not the rollback's to delete — inside the target's own
directory Coolify's delete of the target takes it; this one is outside. Empty it
by hand if the copy is not wanted.

### `auto-deploy NOT switched off for: …` / `tags NOT removed from: …`

The rollback's first step after a finalize, DISARM_TARGETS, could not take back
what finalize gave a target: its auto-deploy, or the tags it attached. Until the
target is deleted, a push (auto-deploy) or a CI deploy by tag can redeploy it —
on empty volumes once the rollback has dropped them. The rest of the rollback
still ran. Switch "Auto Deploy" off on each named target's Advanced page in
Coolify, and remove the named tags from it; a target that is gone already needs
nothing. A second `rollback <id>` retries both and sends nothing where they are
off already.

### `auto-deploy NOT switched back on for: …`

RESTORE_SOURCE_DEPLOY_TRIGGERS could not give a source back the auto-deploy (and
preview deployments) QUIESCE switched off. The source was still restarted. Switch
"Auto Deploy" — and "Preview Deployments", if it had them — back on on each
source's Advanced page in Coolify. A second `rollback <id>` retries only the
sources not yet restored: one it already gave its switches back is left as you
set it since.

### `still queued or running after …s - …`

STOP_TARGET cancels every queued or running deployment of a target application
before it stops it, because a stop holds no deployment back. These did not end
within `DEPLOY_TIMEOUT`, or could not be read: one may still bring the target up,
serving the claimed hosts beside the restarted source. Cancel it in the target's
Deployments in Coolify, then stop the target there.

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

### `... still holds its custom domains: its target ... no longer holds ...`

FINALIZE re-reads each target before the source lets go of its custom domains,
and this target no longer holds the ones named. The usual cause is a
dockercompose app: every deploy reloads its compose from the branch, and a
service renamed or removed there since the target was created loses its domain.
Releasing the domain from the source too would leave it held by nothing — and
from Coolify 4.4 that deletes a DNS record Coolify manages at Cloudflare. So the
stopped source keeps it, and the run still succeeds. Give the target the domain
in Coolify (on the service that serves it now), then remove it from the source
before anyone restarts the source. The same notice with `could not be read`
means the target's or source's GET failed; check both by hand.

### `... let go of its custom domains, but its labels still route ...`

The source's readonly labels were switched off after the plan was made (the plan
refuses `keep` and `rename` for such a source). FINALIZE removed the custom
domains from it all the same, so Coolify's conflict check and managed DNS no
longer count it as a holder, but Coolify kept its labels exactly as they were,
and its next deploy — a restart, a redeploy, a git push — routes the named hosts
again next to the target and requests their certificates. The run still
succeeds; the source is not listed as released. Switch the source's label
management back to Coolify (Readonly labels on, or "Managed by Coolify" under
Labels on newer releases) — it then regenerates the labels from its domains —
or delete the source, before anyone redeploys it.

### `... let go of its custom domains, but routers written into its compose still route ...`

The source's compose declares a router of its own (a `traefik...rule=Host(...)`
or `caddy` label) for a host finalize moved, where the proxy still serves it. The
plan refuses `keep` and `rename` for such a source, so the router was added after
the plan, or names a host the plan could not read (a variable, a route env row;
it warned about those). Coolify adds a compose's own labels to every deploy,
whatever the domains say, so the source's next deploy routes the named hosts
again next to the target and requests their certificates. The run still
succeeds; the source is not listed as released. Before anyone redeploys the
source, remove those routers — from the compose file in git for a dockercompose
application (its next deploy reloads the compose from there; the target routes
these hosts from its domains), from the source's compose in Coolify for a
service (the target runs its own copy) — or delete the source.

### `… (the kept source) keep(s) auto-deploy and preview deployments OFF`

Not an error: a `keep` or `rename` finalize leaves the source's push-driven
deploys switched off, because it shares its repository and webhook with the
target and a push would deploy the stale source beside it. What still reaches it
— a CI call to `POST /deploy` by uuid or tag (it keeps its tags), a manual deploy,
the dashboard — simply starts the copy from before the cutover beside the
target: nothing checks the source after finalize. `coolify-migrate rollback <id>`
switches them back on, once.

### `the target of … was created with auto-deploy off, and switching it … as its source had it failed`

A finalize notice; the run succeeded. Every target is created with auto-deploy
off and given its source's setting at finalize, and that request failed (the
reason is in brackets). Switch "Auto Deploy" on its Advanced page in Coolify to
what the notice says; until then pushes do not deploy it (or, for a source that
had it off, may).

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
the retry saved it a second time; or a task CREATE made was not listed back.
CREATE compares the target's tasks with the ones it created by uuid (a name
can change on the way, because the API trims spaces the UI keeps), refuses a
doubled cron, deletes the target and leaves the source running. Re-run the
migration.

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
  and in every backup storage they were uploaded to (S3, or from Coolify 4.7
  also SFTP, FTPS or WebDAV). Nothing deletes them, and no retention prunes
  them once the source is stopped. Remove them by hand when you no longer need
  them.
- **"keep no local dumps on the source (disable_local_backup)"**: below Coolify
  4.5.1 no API can set that, so the target keeps local dumps. From 4.5.1 it is
  carried — unless the copy is created local-only (its S3 storage cannot be
  named), which must keep its dumps. Switch it off on the target if disk matters.
- **"service database(s) … can have backup schedules"**: no Coolify API exposes a
  service database's schedules. Check each one's Backups tab on the source and
  recreate them on the target by hand.
- **"… not carried: Coolify has no backup for redis"** (also KeyDB, Dragonfly):
  those schedules never produced a backup on the source either.

**Volume backups** (Coolify 4.3+, a volume's **Backups** setting) are not carried.
The API can create them but cannot read them, so the plan cannot see them; it
names every resource with a volume instead ("… can have their own backup
schedules"). If you use them, set them up on the target again.

With `--finalize delete` it is worse than "not carried": deleting the source
makes Coolify delete every archive its volume backups made, on the server and in
every backup storage they were uploaded to (S3, or from Coolify 4.7 also SFTP,
FTPS or WebDAV). The plan and the typed confirmation both say so. Download any
archive you still need before the run, or finalize with `rename`. Database dumps
are not affected; a delete leaves them where they are.

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

## Settings the target gets differently, and what Coolify 4.5.1 carries

The plan lists every setting the target will not share. From Coolify 4.5.1 more
of them are carried, so these lines disappear from a plan made against 4.5.1:

- the default `COOLIFY_*` variables turned off (applications, services, databases);
- an application's rolling-update drain time and Dockerfile build target;
- a database's log drain — only if the target server has a log drain on;
  otherwise the plan says so;
- a service sub-application's www/non-www redirect — once the target holds both
  domains;
- readonly labels turned off on an application. Only when its labels route
  nothing but its own custom domains: labels kept verbatim that name a host bound
  to the source server (or one the DNS gate never checks) would make the target
  answer for it. Then the plan says **"readonly labels ON instead"**: the target
  regenerates its labels from its own domains, and hand-made routers,
  middlewares or headers have to be re-applied by hand. What readonly labels
  off means for the SOURCE under `keep` or `rename` is a refusal, below.

On every release the plan also warns when an application's Dockerfile has its own
`HEALTHCHECK` while Coolify's health check is enabled: the target runs Coolify's
configured check instead, and from 4.5.1 so does the source's next deploy and a
rollback's restart. Disable Coolify's health check on the source, or redeploy the
source once to prove the configured check passes, before migrating.

## "readonly labels are off on this application and its labels route …"

A refusal under `--finalize keep` and `rename` only. Finalize releases the
source's custom domains, but with readonly labels off Coolify keeps the
application's labels exactly as stored when its domains change, and every deploy
writes them into its container. The kept source's next restart, redeploy or git
push would route the moved hosts again next to the target, and request their
certificates — invisibly, since Coolify's domain-conflict check and managed DNS
read the domains, never the labels. A dockercompose application has no
`custom_labels` in its deploy: Coolify generates its routers from its domains at
every deploy, next to whatever its compose declares (below).

Two ways out:

- switch the source's label management back to Coolify (Readonly labels on, or
  "Managed by Coolify" under Labels on newer releases). Coolify regenerates the
  labels from its domains — hand-made routers, middlewares or headers are lost,
  on the target too — then plan again;
- or keep the hand-made labels and migrate with `--finalize delete`: from
  Coolify 4.5.1 the target carries them verbatim (when they route nothing but
  its own custom domains, above), and the source is gone.

`plan --finalize delete` shows the plan under that policy. On Coolify 4.1, whose
API does not show the setting, the plan says **"does not show whether its
readonly labels are on"** instead: check the setting on the source by hand.

## "routers written into its compose route …, which finalize moves to the target"

A refusal under `--finalize keep` and `rename` only, for a dockercompose
application or a service. Its compose declares a router of its own for a custom
domain finalize moves, and the proxy still serves it after finalize: a Caddy site
label, or a Traefik router on a compose service that sets `traefik.enable=true`
or keeps a domain once finalize released the custom ones (a generated or
server-bound host, or one Coolify regenerates). Finalize releases the domains,
but Coolify adds a compose's own labels to every deploy whatever the domains say,
so the kept source's next deploy would route the host again next to the target
and request its certificate.

Remove those routers and plan again — Coolify generates the routers for these
hosts from the domains, on the target as well:

- a dockercompose application: from the compose file in git, then reload the
  compose in Coolify ("Reload compose" on the source, or a redeploy) so the plan
  sees it. The target deploys the same file, so hand-made middlewares or path
  rules in those routers are gone on the target too;
- a service: from the source service's compose in Coolify (the target runs a copy
  of it).

Or finalize with `--finalize delete`. A HostRegexp rule counts as routing every
moved host: it cannot be enumerated.

The plan **warns** instead where it cannot tell (under `keep` and `rename`
only):

- **"… on compose service …, which keeps no domain after finalize and sets no
  traefik.enable"** — Coolify's Traefik serves only containers labelled
  `traefik.enable=true`, so finalize stops this router, unless the server's
  proxy was edited to expose every container;
- **"routers written into its compose build their hosts from variables"** — a
  `` Host(`${DOMAIN}`) `` or Caddy site filled in by docker compose at every
  deploy, while the service's label escaping is off (or the API does not show
  it); with escaping on, the rule routes nothing;
- **"… and Coolify routes its parts from env rows (…) the plan does not read"** —
  minio, logto, garage and silo are routed from env rows that finalize blanks
  when they hold a custom host.

If one of them does route a moved host, remove it the same way, or finalize with
`delete`.

## "the target server '…' is the source server"

Source and target must be two servers. On one server every custom domain
resolves to both sides, so the DNS gate cannot tell a cutover from none (each
host would need its own `--accept-dns-host`), labels kept verbatim would declare
the source's routers twice on the same proxy, and bind mounts would be copied
onto themselves. Pick another target server with `--to`.

## "it runs on … not on the source server '…' this plan migrates from"

The environment spans servers. A plan migrates from one server, the server of
its first resource: it discovers the containers and volumes there, stops the
stack there and copies from there. A resource on another server would be looked
for in the wrong place — nothing found, so nothing copied, and a delete finalize
would remove it with its data. Migrate such a resource on its own, which plans
it from its own server:

```bash
coolify-migrate run shop/production/database --to <server>
```

**"the API did not say which server this resource runs on"** is the same
refusal for a resource whose GET names no server at all. Check the resource's
server (its destination) in Coolify, then plan again.

## "the source server '…' and the target server '…' are one Docker daemon" (exit 2)

A PREFLIGHT refusal; nothing has been changed. Both servers' daemons report the
same engine ID, so Coolify knows one machine twice — under localhost and its
public IP, or under two SSH ports. A migration between them never leaves it: the
checks that prove the source stays stopped would see the target's containers as
the source started again, every custom domain resolves to both sides, and bind
mounts would be copied onto themselves. Pick a target server on another machine.

If they really are two machines, one was cloned from the other together with
Docker's data root and kept its engine ID. Compare
`docker info --format '{{.ID}}'` on both; on one of them remove the `engine-id`
file in the data root (`/var/lib/docker` by default) and restart Docker, which
writes a new ID. A daemon that does not answer refuses nothing; the log then
says `preflight.daemon_id_unread`.

## Bind mounts the plan refuses (exit 2)

A bind mount is an arbitrary host path, so the plan copies only what is the
resource's own and refuses the rest, naming the path:

- **"writable bind of the host's own …"** — a system path, runtime state,
  Docker's or Coolify's own data, or another resource's directory (`/etc`,
  `/var/lib/docker`, `/root`, and `/`, `/data`, `/srv`, `/home` … themselves).
  Copying it would overwrite the target server's. Make it read-only if the
  container only reads it; the target server then supplies its own;
- **"writable bind of the resource's whole Coolify directory …"** — it mixes the
  generated compose and `.env` with data. Mount a subdirectory for the data;
- **"bind mount of … at … is declared in no Coolify storage"** — custom docker
  run options, or a compose bind with `bind.create_host_path: false`: where the
  target mounts it cannot be read back. Declare it as a storage (a plain compose
  bind does that) or move the data into a volume;
- **"bind mount of …: a database's storages are not carried to the target"** and
  **"… is a file mount whose content Coolify keeps in the database's storage
  row"** — CREATE carries no storage of a database. Remove the mount on the
  source and add it to the target after the migration, or move the setting into
  the database's own configuration;
- **"… is a file mount whose content the API cannot carry (binary or over 5
  MiB)"** — a plain application's: CREATE cannot recreate it. Turn it into a
  directory mount, or recreate it on the target by hand;
- **"… is a file Coolify writes from its storage row's content, but the target
  service is created from the source's compose …"** — a raw-compose service's
  file at a system path; mount it from the service's directory (`./` in the
  compose);
- **"bind source … is not an absolute path"**.

## "bind mount(s) cannot be copied safely" (exit 2)

A PREFLIGHT refusal; nothing has been changed and the source is untouched. Each
line names a path:

- **"… is also mounted by … , which this migration does not stop"** — a container
  outside the plan shares the path (the same path, one inside it, or a writable
  parent). It would keep writing during the copy, and after the cutover the two
  would hold diverging copies. Move the shared data into a volume of one
  resource, or migrate the resources that share it together;
- **"… does not exist on the source server"**, **"… is a symbolic link on the
  source server; bind its target instead"**, **"… is not a regular file or
  directory"** — nothing a copy can take.

### Plan warning: "… mounts the whole host writable (/), so it can write the bind mount(s) …"

Not a refusal. A container binds `/` writable — Portainer's agent (`-v /:/host`),
a host backup — so it can reach every bind path while the migration copies and
verifies it. It looks like a host agent rather than a resource sharing that data,
so the run goes on, but what it writes there meanwhile can fail VERIFY or go
missing from the copy. Make that mount read-only, or stop the container, before
the migration starts and until it has finished. The run repeats the warning at
PREFLIGHT for the source and at the end of CREATE for the target server, which
the plan cannot see.

### Plan notes about deploys

- **"deploys during the run: QUIESCE switches each source application's
  auto-deploy off …"** — on every plan that moves an application; it says what
  the run switches off and what it cannot (a CI `POST /deploy`, manual, API and
  MCP deploys). Pause those yourself from QUIESCE until FINALIZE. See
  [safety](safety.md#nothing-deploys-a-stack-the-run-holds-still).
- **"preview deployments are on: …"** — QUIESCE switches them off with
  auto-deploy, and the target is created without them; switch them on for the
  target afterwards if you want them.
- **"Coolify before 4.2 does not return this application's settings …"** — the
  run switches the source's auto-deploy off all the same, and gives the target
  (and, on rollback, the source) Coolify's default, on. If the source had it off,
  switch it off again there afterwards; switch its preview deployments off by
  hand before the run.

## Coolify 4.6: image names and HTTPS git credentials

**"docker_registry_image_name=… is not carried".** From 4.6 Coolify refuses a second
application of a team that builds the same image, and the source still holds the
name. The target comes up without it, builds its image locally and pushes nothing to
the registry. If something pulls that image, set a name on the target by hand; the
source's name is free once the source no longer has it.

**"this application builds the image …, and so does …".** Coolify 4.6 fails every
deploy of such an application, so a rollback could not bring the source back. Give
each application its own image name, then plan again.

**"target server … needs an image name there".** A deployments-only server pulls what
a build server pushed, and the target cannot take the source's name. Pick a target
that can build.

**"the source clones its repository with an HTTPS username and password or token".**
No API returns the password, so the target could not clone. Move the source to a
deploy key or a Git source, then plan again.

**"this application clones over HTTP(S) from a custom Git host".** The same credential
can exist without a username, and then nothing shows it. A public repository is
fine. If it is private, the target's deploy fails after the source is stopped and the
run rolls back, so move it to a deploy key first.

**A rollback or `DELETE` fails with "Missing required permissions: delete".** From 4.6
DELETE has its own ability. Tokens that had `write` got it during the upgrade; a token
created later has to select it.

## "the plan was made against Coolify X, but the instance now runs Y"

A PREFLIGHT refusal; nothing has been changed. The plan records the Coolify
release it was made against, and CREATE builds every request for that release:
what Coolify accepts, refuses or hides differs between releases. The instance
was upgraded (or downgraded) since the plan. Plan again, then run — `resume` of
an interrupted run that has passed PREFLIGHT keeps the plan's release.

## `422 This field is not allowed`

Our request whitelist has drifted from upstream Coolify. Coolify names the
offending field in the response. The tool sends only the fields the plan's
Coolify release accepts, so first check that the instance has not been upgraded
past the release range `coolify-migrate doctor` reports as validated.

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
