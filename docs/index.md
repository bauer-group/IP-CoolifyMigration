# Coolify Migration Toolkit

Moves a Coolify **project — with its data —** between servers, and relocates a
whole Coolify instance to a new host.

Coolify can clone a resource to another server but deliberately will not move the
data. [Why this exists](why.md) explains what upstream disabled and why, and what
the two community tools get wrong.

## The short version

```bash
pip install bg-coolify-migrate

export COOLIFY_URL="https://coolify.example.com"
export COOLIFY_TOKEN="..."        # MUST have root or read:sensitive

coolify-migrate doctor            # verify token scope + reachability
coolify-migrate plan my-project   # dry run: manifest, drift, DNS gate
coolify-migrate run my-project --to target-server
```

!!! danger "The token needs `root` or `read:sensitive`"
    Without that scope Coolify silently omits environment variable values,
    compose files and database passwords from its API responses — HTTP 200, no
    error, the keys are simply absent. A migration would recreate every resource
    with empty secrets. `doctor` probes for this and refuses to continue.

## What makes it safe

- **Application-unaware.** A cleanly stopped stack makes a volume just bytes.
  Postgres, ClickHouse and the service nobody has heard of are all handled the
  same way — see [Safety model](safety.md).
- **Byte-exact.** `rsync -aHAXS --numeric-ids`. Never `chown`.
- **Verified.** SHA-256 *and* metadata manifests on both sides.
- **Reversible.** The source is never destroyed until you say so.
- **Gated.** It refuses to start a target whose DNS still points at the old
  server, and refuses to migrate an app that would rebuild different code.

## Where to go next

- [Installation](installation.md)
- [Configuration](configuration.md)
- [CLI reference](cli.md) — including the exit-code contract
- [Safety model](safety.md) — what is guaranteed, and what is not
- [Server migration](server-migration.md) — moving Coolify itself
