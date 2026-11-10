# CLI reference

```bash
coolify-migrate --help
```

## Commands

| Command | What it does |
| --- | --- |
| `doctor` | Check token scope, API reachability, and each server (rsync + docker; `--install` adds rsync). **Run this first.** |
| `list [project]` | Recursively list every resource: server → project → environment → resource, with uuids. Reads only. |
| `plan <selector>` | Produce a migration plan for a scope. Reads only. |
| `run <selector> --to <server>` | Execute a migration for a scope. |
| `resume <id>` | Continue a blocked or interrupted migration. |
| `rollback <id>` | Undo a migration. |
| `unpin <id>` | Remove the volume pins a finished migration had to keep, once running containers keep their volumes mounted. Restarts nothing; exits `1` while a pin still has to stay. |
| `status [id]` | List migrations, or show one in detail. |
| `server plan` / `server run` | Migrate the Coolify instance itself. |

## Migration scope

Coolify nests **project → environment → resource** (a resource is one
app/service/database — possibly a whole compose stack). `plan` and `run` migrate
at any of those three levels; the migration always runs *on the resource*, and the
higher levels just decide which resources are in scope. Pick a level with the
**selector**, a path whose depth is the scope:

| Selector | Scope |
| --- | --- |
| `bauer-group` | The **whole project** — every environment, every resource. |
| `bauer-group/production` | One **environment** and all its resources (stops together). |
| `bauer-group/production/whistleblower-app` | One **resource**; its siblings keep running. |
| `<resource-uuid>` | The **resource** with that uuid, wherever it lives — paste it straight from `list`. |

Each segment is matched by **name or uuid**. A **bare token** is resolved the way
you'd expect after copying it from `list`: if it names a project it migrates the
whole project; otherwise it's looked up as a resource anywhere and migrates just
that one. `--environment` overrides the environment for a bare-project selector
(`plan bauer-group --environment staging`).

Migrating one resource stops only that resource on the source; the rest of the
environment stays up. A whole-project run migrates each environment in turn and
stops at the first failure, leaving later environments untouched.

The target must be **another server**. A plan whose target is the source's own
server is refused: on one server every custom domain resolves to both sides, so
the DNS gate cannot tell a cutover from none, labels kept verbatim would declare
the source's routers twice on one proxy, and bind mounts would be copied onto
themselves. When you pick an environment or a resource interactively, the target
menu leaves the source's server out. Coolify can also know **one machine as two
servers** (localhost and its public IP, or two SSH ports): `run` compares the
Docker engine ID of both at preflight and refuses when they are the same daemon.

An environment is migrated **from one server**: the server of its first
resource, where the run discovers, stops and copies every resource. A plan
refuses each resource that runs on another server (the target included), so an
environment spread over several servers is migrated one resource at a time, each
from its own server: `coolify-migrate run <project>/<environment>/<resource>`.

### Host keys

The first time a command reaches a server, its SSH host key is unknown. In a
terminal you get the OpenSSH prompt — the fingerprint and a y/N — and an accepted
key is recorded, so you are asked only once (trust on first use). Unattended (a
pipe or CI), pass **`--trust-host-key`** after verifying the fingerprint out of
band. Either way host-key checking is never disabled — an unknown key that you
don't accept stops the run.

### Interactive picker

Omit the selector (and/or `--to`) in a terminal and `plan`/`run` walk you through
it: project → environment → resource (or "all") → target server. In a pipe or CI
the selector is required instead — a prompt there is a hang.

## Finding a project or resource

`list` shows **everything** in one recursive pass — server → project → environment
→ resource — so there is nothing to piece together:

```bash
coolify-migrate list                 # the whole inventory
coolify-migrate list bauer-group     # limited to one project
coolify-migrate list --server 0047-20  # limited to one host
```

```text
0047-20  (5.6.7.8)
  bauer-group  [prj-9f2a]
    production
      whistleblower-app  application  [rsc-1a]
      redis              database     [rsc-2b]
    staging
      whistleblower-app  application  [rsc-3c]

hel-01   (1.2.3.4)
  shop  [prj-1c7b]
    production
      web  application  [rsc-4d]
```

Every level carries its **uuid**, the unambiguous handle when a name has spaces or
slashes — pass them straight to `plan`/`run`, e.g.
`plan prj-9f2a/production/rsc-1a`. `--json` emits one fully-qualified record per
resource (server, project, environment, name, all uuids) for scripting.

## Exit codes

These are a stable contract; script against them.

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `2` | Preflight failed — **nothing was changed** |
| `3` | DNS gate blocked — resumable |
| `4` | Drift needs your decision — re-run interactively, or `--accept-drift` |
| `5` | Quiesce failed — the stack would not stop cleanly, or did not stay stopped (see below) |
| `6` | Transfer failed — rolled back |
| `7` | Verification failed — rolled back, target **not** started |
| `8` | Rollback itself failed — human attention required |
| `9` | Coolify API error |
| `10` | Token lacks `root` / `read:sensitive` |
| `14` | Journal error |

Codes 3 and 4 are **not failures**. Code 3 is a deliberate, resumable stop: flip
DNS, then `coolify-migrate resume <id>`. Code 4 only appears unattended — it means
we had a question and no way to ask it.

Code 5 covers everything that says the stack is not held still:

- the stop could not be made safely or did not end cleanly (the source's volumes
  could not be pinned first, a SIGKILLed data holder, the stop gate timed out);
- a source deployment still queued or running when the stop was due — refused
  before anything is stopped (`SourceDeploymentPending`);
- a **containment breach** before START: something started the stopped source
  (`SourceRevived`) or a target before START asked for it
  (`TargetStartedEarly`). The run rolls back;
- the same found **once START has asked Coolify to start a target**: a revived
  source (`SourceRevivedAfterStart`), or a source whose deployment history
  Coolify did not give (`SourceUncheckedAfterStart`). This is a **block**, like
  code 3: resumable, nothing rolled back, because the target may already hold
  users' writes. Stop the source (or wait until Coolify answers) and `resume`, or
  `rollback` by hand.

See [troubleshooting](troubleshooting.md) for each message.

## Why `plan` is worth running

`coolify-migrate plan` exercises preflight, discovery, volume pairing, the drift
gate and the DNS gate — everything except mutation. If `plan` is clean, `run` has
already had its risky decisions made.

This is deliberately unlike `coolify-mover --dry-run`, which short-circuits
*before* all the SQL and rsync code, and therefore validates none of the parts
that actually break.

## Finalize policies

`run --finalize` decides what happens to the source once the target is verified
healthy:

| Policy | Effect |
| --- | --- |
| `keep` | Leave the source stopped, with auto-deploy off. Only its custom domains are released (the target serves them), so a restart cannot claim them twice. Safest. |
| `rename` | Rename to `{name}-old-{stamp}`, leave stopped with auto-deploy off. **Default.** |
| `delete` | Delete the source and its volumes. **Irreversible** — requires typed confirmation. |

`keep` and `rename` release the source's **custom** domains, as finalize's last
step. Without that, a restarted old stack's Traefik would claim the hostname
next to the target and keep trying to renew its certificate — quietly costing
ACME rate-limit budget. Its server-bound and generated URLs stay: they collide
with nothing on the target.

The release stops the routing only where Coolify manages an application's
labels. With **readonly labels off** (label management "manually"), Coolify
keeps `custom_labels` exactly as stored when the domains change, and every
deploy of the application writes them into its container — so a kept source
would route the moved hosts again on its next restart, redeploy or git push.
The plan therefore **refuses** `keep` and `rename` for an application (any
build pack but dockercompose) whose readonly labels are off and whose labels
route one of the custom domains finalize moves. Switch its label management
back to Coolify (it then regenerates the labels from its domains, dropping
hand-made ones) and plan again, or use `--finalize delete`. On Coolify 4.1,
whose API does not show the setting, the plan warns instead.

A dockercompose application or a service routes by the labels Coolify generates
from its domains — and by any router **written into its compose**, which Coolify
adds to every deploy whatever the domains say. Under `keep` and `rename` the plan
**refuses** a source whose compose routes a custom domain finalize moves where
the proxy still serves it: a Caddy site label, or a Traefik router on a compose
service that sets `traefik.enable=true` or keeps a domain after finalize. Remove
those routers — from the compose file in git for a compose application (then
reload the compose in Coolify), from the service's compose in Coolify for a
service — and plan again, or use `--finalize delete`. What the plan cannot tell
is a **warning** instead: a Traefik router on a service Coolify's proxy does not
serve (unless it was edited to), a rule that builds its host from a variable, and
the routes of minio, logto, garage or silo, which Coolify takes from env rows.
Finalize checks the labels again and does not list a source that still routes a
moved host as released.

`plan` takes the same `--finalize` (default `rename`), so it judges the policy
`run` will use; preflight judges the plan once more under the run's policy.

Under `keep` and `rename` the stopped source also keeps its **auto-deploy and
preview deployments off** after the run: it shares its repository and webhook
with the target, so a push would deploy the stale copy beside it. A
`rollback` switches them back on, once. See
[deploys during a run](#deploys-during-a-run).

## Deploys during a run

Coolify asks no deploy trigger whether a migration is running. So from QUIESCE
the source applications' auto-deploy (and their preview deployments) are
switched off, and every target is created with auto-deploy off and gets its
tags only at finalize, where it is given its source's auto-deploy setting. A
deployment of the source already queued or building is waited for before the
stop (up to `DEPLOY_TIMEOUT`, never cancelled); one that does not end refuses
the run while the source still serves.

What no setting stops — a CI call to `POST /deploy` by uuid or tag, a deploy
from the dashboard, the API or MCP — **pause yourself from QUIESCE until
finalize**. The run checks that nothing started either side at COPY, VERIFY, the
DNS gate, START and FINALIZE, and exits `5` when something did (see
[exit codes](#exit-codes)). After finalize nothing checks the kept source any
more. Details: [safety](safety.md#nothing-deploys-a-stack-the-run-holds-still).

## Domains & DNS

A resource answers on two kinds of hostname, and they migrate in opposite ways.

**Server-bound URLs** — the default URL Coolify generates under a server's
wildcard, e.g. `pdf-tool.app.0046-20.cloud.bauer-group.com` when the source
server's wildcard is `app.0046-20.cloud.bauer-group.com`. The wildcard's DNS
record binds it to that one server, so it *cannot* move. `run` **rewrites** it
onto the target server's wildcard automatically —
`pdf-tool.app.0047-20.cloud.bauer-group.com` — keeping the same subdomain. No
DNS change is needed and the gate never stops for these; they show as
`server_bound` in the DNS table.

The wildcard is read from each server's Coolify settings. If the **target** has
none configured, the URL can't be rewritten onto it and Coolify falls back to a
`sslip.io` name (a loud warning is logged). Supply it explicitly to keep the
proper URL — `--target-wildcard app.0047-20.cloud.bauer-group.com` (and
`--source-wildcard` to override a missing/wrong source wildcard). Both accept the
bare host or the `https://…` form.

Docker-compose applications keep a domain **per service** (Coolify's
`docker_compose_domains`, not `domains`); each service's URL is rewritten the
same way, so a compose stack keeps every service's address on the new server.

Services keep a domain per **sub-application** (each container of the stack),
and each is carried the same way: server-bound URLs rewritten, custom domains
carried verbatim with their port, Coolify-generated `sslip.io` names left for the
target to generate its own.

Custom domains change hands **make-before-break**, for applications and services
alike. The source is never written to before finalize: the target is created
without the source's custom domains and then claims them, overriding Coolify's
domain-conflict check only over this migration's own resources. Under `keep` as
well as `rename`, finalize then clears the custom domains from the stopped
source as its last step, so restarting it by hand cannot put a second router on
a hostname the target now serves — as long as Coolify generates its routers (see
[Finalize policies](#finalize-policies): a source with readonly labels off, or
whose compose routes a moved host itself, is refused for `keep` and `rename`, and
a source either applies to at finalize is not reported as released). That order
also keeps a DNS record Coolify (4.4+) manages at Cloudflare alive: Coolify
deletes one only when its hostname loses its last holder.

**Custom domains** — `shop.example.com` and the like. These are
server-independent: they move *with* the app, by repointing their DNS record at
the target. Because the target's proxy requests an ACME certificate the moment it
starts, and that challenge is routed by DNS to whichever server the record still
names, starting the target while the record points at the source burns the
Let's Encrypt rate limit. So by default the **DNS gate blocks** (exit `3`,
resumable) until the record points at the target:

```bash
coolify-migrate run pdf-tool --to 0047-20
# -> exit 3 with a cutover checklist, IF a custom domain still points at 0046-20
# repoint DNS, then:
coolify-migrate resume <migration-id>
```

If you would rather finalize now and cut the record over in parallel —
propagation can lag by the record's TTL — confirm interactively, or pass
**`--accept-dns`** unattended. The gate then only warns; the target may serve a
temporary certificate error until DNS catches up. `--accept-dns` affects custom
domains only; server-bound URLs are always rewritten regardless.

Two answers DNS can give are never covered by `--accept-dns`, because they say
something is wrong rather than late: a host that resolves to **neither** server
(a CDN, proxy or load balancer whose origin DNS cannot show — it may still be
the source), and a host that resolved when the migration was planned and
resolves to **nothing** now (a deleted record) — or whose lookup fails now (a
timeout; `resume` asks again). Each blocks until you fix it or accept that one
host, by name, on `run` or `resume`:

```bash
coolify-migrate resume <migration-id> --accept-dns-host shop.example.com
```

The plan records what every custom host resolved to (its *DNS baseline*). CREATE
checks it again while the source still serves — a custom host that stopped
resolving to the source refuses the run before anything is stopped — and a
rollback checks it at its end and lists the records to point back.

## Non-TTY output

Piped or in CI, output switches to line-oriented text automatically — a plan in a
CI log must be greppable, and a Rich table in a log file is not. `NO_COLOR` is
honoured.
