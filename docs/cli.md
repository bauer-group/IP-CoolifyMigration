# CLI reference

```bash
coolify-migrate --help
```

## Commands

| Command | What it does |
| --- | --- |
| `doctor` | Check token scope, API reachability, server inventory. **Run this first.** |
| `plan <project>` | Produce a migration plan. Reads only. |
| `run <project> --to <server>` | Execute a migration. |
| `resume <id>` | Continue a blocked or interrupted migration. |
| `rollback <id>` | Undo a migration. |
| `status [id]` | List migrations, or show one in detail. |
| `server plan` / `server run` | Migrate the Coolify instance itself. |

## Exit codes

These are a stable contract; script against them.

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `2` | Preflight failed — **nothing was changed** |
| `3` | DNS gate blocked — resumable |
| `4` | Rebuild drift blocked — resumable with `--accept-rebuild-drift` |
| `5` | Quiesce failed — the stack would not stop cleanly |
| `6` | Transfer failed — rolled back |
| `7` | Verification failed — rolled back, target **not** started |
| `8` | Rollback itself failed — human attention required |
| `9` | Coolify API error |
| `10` | Token lacks `root` / `read:sensitive` |
| `14` | Journal error |

Codes 3 and 4 are **not failures**. They are deliberate, resumable stops: fix the
world, then run `coolify-migrate resume <id>`.

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
