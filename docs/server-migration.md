# Server migration (F2)

Relocates a whole Coolify **instance** to a new host. This is a different problem
from moving a project between servers, and it replaces
[`Geczy/coolify-migration`](https://github.com/Geczy/coolify-migration).

```bash
coolify-migrate server plan --to new-host.example.com   # reads only
coolify-migrate server run  --to new-host.example.com
```

!!! danger "This stops everything"
    F2 stops Docker on the source — Coolify **and** every container it manages —
    for the duration of the transfer. Unlike F1, where the outage is one project,
    here it is the whole box. The compensation that ends it is
    `START_SOURCE_DOCKER`, and it runs on any failure.

## What Geczy gets right (and we kept)

Its architecture is correct, and we did not improve on it:

1. **Copy `/data/coolify` wholesale**, so `source/.env` — containing `APP_KEY` —
   rides along.
2. **Extract the archive BEFORE running `install.sh`.** The ordering is
   load-bearing (see below).
3. **Never mutate the source.** That gives an implicit rollback: restart Docker
   on the old box and you are back.

## APP_KEY: the invariant everything depends on

`APP_KEY` lives in `/data/coolify/source/.env` and decrypts Coolify's entire
credential store — every environment variable value, every database password,
every SSH private key, every log-drain key.

It survives a migration because of an ordering nobody documents:

```bash
# install.sh merges the .env, EXISTING VALUES FIRST:
awk -F '=' '!seen[$1]++' "$ENV_FILE" "/data/coolify/source/.env.production"

# ...and only fills EMPTY or MISSING vars:
update_env_var "APP_KEY" "base64:$(openssl rand -base64 32)"
```

Since the migrated `.env` already has `APP_KEY=base64:...` populated, neither
branch fires and the key is preserved. `DB_PASSWORD` likewise — which matters,
because the copied `coolify-db` volume still holds the old password hash.

**Reverse the two steps — install first, extract second — and it still "works",
until extraction fails and you have a fresh APP_KEY against a restored database.
Then every secret is permanently undecryptable.**

Geczy's script never mentions APP_KEY. It works by luck. We make it an asserted
invariant:

1. Extract `APP_KEY` from the source `.env` **before** transfer.
2. Assert it is byte-identical in the target `.env` **after** `install.sh` ran.
3. Run a **decrypt probe** — read an environment variable back through the API
   and confirm it decrypts — rather than assuming.
4. On drift, fall back to `APP_PREVIOUS_KEYS`.

## What we fix

Each is a designed mechanism rather than a lucky side effect:

| Geczy's behaviour | Consequence | Ours |
| --- | --- | --- |
| Volumes from `docker ps` | Stopped containers' volumes **silently skipped** | `docker volume ls` + `docker ps -a`, reconciled |
| Bind mounts dropped (`.Name` empty) | Silent data loss | Classified and mirrored |
| Stopping Docker is a *prompt*; `tar --warning=no-file-changed` | One keystroke from a torn Postgres | Clean stop **mandatory and verified** |
| Fixed 1 GB disk check | 100 GB migration dies mid-transfer | Proportional, both ends |
| Destination assumed empty; `tar -Pxf -C /` **merges** | Two Postgres data dirs merged | Refuse a non-empty destination |
| `root@` hardcoded | Non-root unsupported | Configurable user |
| Stale `coolify_backup.tar.gz` reused, skipping discovery | Sends yesterday's data | Journal reconciled against reality |
| One `tar \| ssh` stream, no resume | 100 GB drops at 95% → start over | Chunked, resumable rsync |
| **Zero verification** — success means `ssh` returned 0 | You find out later | Checksum + metadata manifests |

## Version pinning: the bug nobody mentions

Geczy pipes `cdn.coollabs.io/coolify/install.sh | bash`, which installs the
**latest** Coolify against a database copied from an **older** instance. First
boot then runs an unplanned schema migration.

We read the source version, install the same one, verify, and leave upgrading as
a separate deliberate step.

## Fencing: the biggest real-world hazard

After a successful migration **both instances are live**, with:

- the same FQDNs, racing for ACME renewals,
- the same Coolify SSH keys, so **both can drive the same managed fleet**,
- the same scheduler, both running backups and health checks.

Geczy is silent on this. Two Coolify brains managing one fleet is not a
theoretical problem. We stop the source and disable its scheduler explicitly, as
a named step.

## The sequence

```text
INIT
PREFLIGHT        rsync + systemctl on both ends; inventory not blocked
INVENTORY        volumes (docker volume ls + docker ps -a), bind mounts, sizes
READ_APP_KEY     captured BEFORE anything moves        <- you cannot assert what you never saw
STOP_SOURCE      docker down, VERIFIED                 undo: START_SOURCE_DOCKER
TRANSFER         rsync /data/coolify + volumes + binds undo: wipe target, revoke key
VERIFY           checksum + metadata, both ends
INSTALL_COOLIFY  pinned to the SOURCE's version        <- MUST be after TRANSFER
ASSERT_APP_KEY   byte-identical, or fatal
BOOT             wait for Coolify, then decrypt-probe
RECONCILE        compare volumes against the inventory
FENCE_SOURCE     stop the old brain                    undo: UNFENCE_SOURCE
```

`INSTALL_COOLIFY` has no compensation on purpose: an installed Coolify on a box
we were told was empty is inert, and uninstalling it would be a bigger
intervention than leaving it.

## Shares with F1

`transfer/`, `journal/`, `verify/`, `ssh/`, `engine/executor.py` (the saga is
generic over its state machine), `engine/keys.py`, `ui/`. F2-specific:
`server/statemachine.py`, `appkey.py`, `fencing.py`, `inventory.py`.

## Rollback

There is no `FINALIZE` and no delete policy: **F2 never destroys the source.** It
is left intact but fenced, so rollback always means "start it again". That is
Geczy's one genuinely good architectural decision, kept.

## Verified against a real instance

F2 was run end to end against a real Coolify installed on a host (systemd Ubuntu,
Docker, `install.sh`) and migrated to a second host. That run **proved**:

- the full saga executes in order (preflight → inventory → read_app_key →
  stop_source → transfer → verify → install_coolify → assert_app_key → boot);
- **APP_KEY survives byte-identical** — the whole feature — with the source and
  target fingerprints matching every time;
- the inventory finds and transfers both `/data/coolify` and the docker volumes
  (`coolify-db`, `coolify-redis` — Coolify's own database is a named volume, not
  under `/data/coolify`);
- on failure, the source is restarted and never fenced — rollback works.

It also found three real bugs, each invisible to a mock and fixed with a
regression test:

1. **The source's containers were never stopped.** `systemctl stop docker` stops
   the daemon, but `KillMode=process` leaves the containers running under
   containerd, so Postgres was still writing while its volume was copied — a torn
   `coolify-db`. Now the containers are `docker stop`ped first.
2. **The decrypt probe ran before Coolify was ready** and read "not booted yet"
   as "data is corrupt", rolling back a good migration. Now waits for health and
   polls a tri-state probe.
3. **An empty `user` on the localhost server record** was not defaulted to root,
   so F2 SSHed as `@host`.

### What the local rig cannot prove

The final step — the migrated Coolify booting and *serving* the copied database —
was not reproduced locally, and the reason is the test environment, not F2.
Coolify's `install.sh` installs Docker on a fresh target; a local nested-Docker
rig must instead pre-provision Docker with the `vfs` storage driver (the only one
that survives Coolify's opaque-whiteout images under nesting), and a
pre-installed daemon running during the volume copy does not hand the copied
named volumes to `install.sh`'s `compose up` the way a fresh host's newly
installed daemon does. That handoff is what a real target does natively. The
decrypt probe (finding 2, now fixed) is exactly the guard that would catch it if
it ever did go wrong in production.
