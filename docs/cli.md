# CLI reference

```bash
coolify-migrate --help
```

## Commands

| Command | What it does |
| --- | --- |
| `doctor` | Check token scope, API reachability, server inventory. **Run this first.** |
| `list [project]` | List every project and its server, or one project's resources. Reads only. |
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

Each segment is matched by **name or uuid**, so a resource whose name is ambiguous
can be named by uuid: `bauer-group/production/<uuid>`. `--environment` is an
override for a bare-project selector (`plan bauer-group --environment staging`).

Migrating one resource stops only that resource on the source; the rest of the
environment stays up. A whole-project run migrates each environment in turn and
stops at the first failure, leaving later environments untouched.

### Interactive picker

Omit the selector (and/or `--to`) in a terminal and `plan`/`run` walk you through
it: project → environment → resource (or "all") → target server. In a pipe or CI
the selector is required instead — a prompt there is a hang.

## Finding a project or resource

`plan`/`run` take a **project** name/uuid, not a `team/app` path. To see what you
can migrate and where it lives:

```bash
coolify-migrate list                 # projects, grouped by server
coolify-migrate list bauer-group     # that project's resources, with uuids
```

```text
0047-20  (5.6.7.8)
  whistleblower-app / production   3 resources   [prj-9f2a…]

hel-01   (1.2.3.4)
  shop / production                5 resources   [prj-1c7b…]
  shop / staging                   2 resources   [prj-1c7b…]
```

The bold server heads a group; the indented name is the project and the trailing
`[…]` is its **uuid**. `list <project>` drills in and prints each resource's
**name, uuid, kind, environment and server**. Every level is selectable by uuid,
which is the unambiguous handle when a name carries spaces or slashes — e.g.
`plan prj-1c7b/production/<resource-uuid>`. Narrow the overview to one host with
`--server 0047-20`, or get JSON (uuids included) with `--json`.

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

## Non-TTY output

Piped or in CI, output switches to line-oriented text automatically — a plan in a
CI log must be greppable, and a Rich table in a log file is not. `NO_COLOR` is
honoured.
