<!-- AUTO-GENERATED FILE. DO NOT EDIT. Edit docs/README.template.MD instead. -->
<!-- Generated on 2026-07-16 21:06:46 UTC -->

# BAUER GROUP - Coolify Migration Toolkit

[![PyPI](https://img.shields.io/pypi/v/bg-coolify-migrate.svg)](https://pypi.org/project/bg-coolify-migrate/)
[![Python](https://img.shields.io/pypi/pyversions/bg-coolify-migrate.svg)](https://pypi.org/project/bg-coolify-migrate/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://github.com/bauer-group/IP-CoolifyMigration/actions/workflows/tests.yml/badge.svg)](https://github.com/bauer-group/IP-CoolifyMigration/actions/workflows/tests.yml)

> **Today, Tomorrow, Together** | Building Better Software Together

Moves a Coolify **project — with its data —** between servers, and relocates a
whole Coolify instance to a new host. Failsafe, resumable, and rollback-capable.

Coolify can clone a resource to another server but deliberately will not move the
data. `VolumeCloneJob` and `CloneMe`'s `cloneVolumeData` flag exist upstream, but
PR #4777 shipped them **disabled**. The maintainer's stated blockers — permission
damage, job-queue spam at 50+ resources, no progress tracking, large-volume
failures — are all consequences of running inside Coolify's Laravel queue. An
external orchestrator has none of those constraints. That is what this is.

## Repository Information

- **Version:** v2.5.2
- **Repository:** bauer-group/IP-CoolifyMigration
- **Branch:** main
- **Architecture:** Pure-logic cores with thin IO shells; saga engine with a
  crash-safe journal

## Features

- **Application-unaware mirroring** — a cleanly stopped stack makes a volume just
  bytes. No per-engine logic, no supported-database allowlist. Postgres, MySQL,
  MariaDB, MongoDB, Redis, KeyDB, Dragonfly, ClickHouse and anything else you run
  are all handled the same way, because none of them are special once stopped.
- **Byte-exact** — `rsync -aHAXS --numeric-ids`, parallelised, with SHA-256 and
  metadata verification on both sides. Never `chown`.
- **Failsafe** — every step has a compensating action, journalled to disk. The
  source is never destroyed until you explicitly say so, so rollback is always
  available. `resume` reconciles against reality, not against the journal.
- **DNS gate** — extracts every Traefik/Caddy hostname and refuses to start the
  target while DNS still resolves to the old server, with an actionable cutover
  checklist.
- **Honest about drift** — the target is built exactly as the source is
  configured, but a tag is a pointer and a branch moves. We detect a floating
  `latest` that could cross a database major, or a HEAD that has moved, and put
  the concrete question to you rather than deciding for you.
- **Server migration** — relocate the whole Coolify instance, with the `APP_KEY`
  treated as a first-class, asserted artifact rather than a lucky side effect.
- **Proven, not assumed** — an integration rig of two real sshd containers
  asserts that uid 999, hardlinks, xattrs, sparse files and symlinks survive an
  actual rsync, and that a wrong `chown` is caught by verification.

## Architecture

| Layer | Role |
| ----- | ---- |
| `domain/` | Pure logic: classification, compose analysis, volume pairing, drift, state machine |
| `api/` | Coolify REST client with per-endpoint request whitelists |
| `discovery/` | Docker + API reconciliation; the label-based quiesce gate |
| `transfer/` | asyncssh, rsync planning, checksum verification |
| `journal/` | Append-only crash-safe state |
| `dns/` | FQDN extraction, authoritative resolution, cutover gate |
| `ui/` | Rich dashboard, wizard, and a plain non-TTY fallback |

> **Requires the instance API switched on (Settings > API — off by default) and a
> token with `root` or `read:sensitive`.** Without the switch every call is
> `403 "API is disabled."`, which looks like a token problem but is not. Without
> the scope, Coolify's `ApiSensitiveData` middleware silently omits `value`,
> `real_value` and `docker_compose_raw` from responses — no error, no redaction
> marker. The tool checks both at startup and fails closed.

## Quick start

```bash
pip install bg-coolify-migrate

export COOLIFY_URL="https://coolify.example.com"
export COOLIFY_TOKEN="..."          # root or read:sensitive

coolify-migrate doctor                 # verify token scope + server reachability
coolify-migrate list                   # everything: server -> project -> env -> resource
coolify-migrate list my-project        # limited to one project

# Scope with a selector: project / project/environment / project/environment/resource
coolify-migrate plan my-project --to target-server              # whole project (dry run)
coolify-migrate run  my-project/production --to target-server   # one environment
coolify-migrate run  my-project/production/api --to target-server  # one resource
```

Omit the selector in a terminal and `plan`/`run` open an interactive picker.

Interrupted? `coolify-migrate resume <id>`. Regret it? `coolify-migrate rollback <id>`.

## Server migration

```bash
coolify-migrate server plan --to new-host.example.com
coolify-migrate server run  --to new-host.example.com
```

## Documentation

See [`docs/`](docs/): installation, configuration, cli, architecture, safety,
server-migration, troubleshooting.

## License

MIT © BAUER GROUP
