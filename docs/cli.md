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
| `5` | Quiesce failed — the stack would not stop cleanly |
| `6` | Transfer failed — rolled back |
| `7` | Verification failed — rolled back, target **not** started |
| `8` | Rollback itself failed — human attention required |
| `9` | Coolify API error |
| `10` | Token lacks `root` / `read:sensitive` |
| `14` | Journal error |

Codes 3 and 4 are **not failures**. Code 3 is a deliberate, resumable stop: flip
DNS, then `coolify-migrate resume <id>`. Code 4 only appears unattended — it means
we had a question and no way to ask it.

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
| `keep` | Leave the source stopped and untouched. Safest. |
| `rename` | Rename to `{name}-old-{stamp}`, leave stopped. **Default.** |
| `delete` | Delete the source and its volumes. **Irreversible** — requires typed confirmation. |

`rename` also releases the FQDN on the source. Without that, the old host's
Traefik still claims the hostname and keeps trying to renew its certificate — so
"keep the old one around just in case" quietly costs you ACME rate limit budget.

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

## Non-TTY output

Piped or in CI, output switches to line-oriented text automatically — a plan in a
CI log must be greppable, and a Rich table in a log file is not. `NO_COLOR` is
honoured.
