# Configuration

Environment-first, via env vars or a `.env` file. See
[`.env.example`](https://github.com/bauer-group/IP-CoolifyMigration/blob/main/.env.example)
for the fully commented reference.

## Required

| Variable | Notes |
| --- | --- |
| `COOLIFY_URL` | e.g. `https://coolify.example.com` (with or without `/api/v1`) |
| `COOLIFY_TOKEN` | **Must** carry `root` or `read:sensitive` |

!!! danger "Why the token scope is not optional"
    Coolify's `ApiSensitiveData` middleware computes
    `can_read_sensitive = token->can('root') || token->can('read:sensitive')`.
    Without it, controllers call `makeHidden(['value', 'real_value', ...])` and
    the keys **vanish** from the JSON — HTTP 200, no error, no redaction marker.
    A migration driven by a plain `read` token would happily recreate every
    environment variable with no value at all, and every service without its
    compose file.

    That token can read every secret of every project in the team. Keep it in a
    secret manager or a gitignored `.env` — never in a config file.

## Transfer

| Variable | Default | Notes |
| --- | --- | --- |
| `TRANSFER_MODE` | `auto` | `direct`, `tunnel` or `auto` |
| `TRANSFER_PARALLEL` | `4` | Concurrent rsync streams per volume |
| `TRANSFER_COMPRESS` | `false` | rsync `-z`; only worth it on a slow WAN |
| `TRANSFER_BANDWIDTH_KBPS` | unlimited | rsync `--bwlimit` |
| `VERIFY_PARALLEL` | `4` | Parallelism for SHA-256 manifests |
| `DISK_HEADROOM_FACTOR` | `1.2` | Free space required on the target, as a multiple of the real payload |

!!! note "Hardlinks override `TRANSFER_PARALLEL`"
    A volume containing hardlinks is always transferred as **one** stream.
    `rsync -H` only detects hardlinks within a single invocation, so splitting
    would explode them into duplicate files — a backup volume using hardlinked
    snapshots can go from 50 GB to several hundred. Correctness beats
    parallelism, and the plan tells you why it chose one stream.

!!! note "`DISK_HEADROOM_FACTOR` is proportional on purpose"
    Geczy's script checks against a fixed 1 GB floor — it computes the real
    total, prints it, then never compares against it. That is how a 100 GB
    migration sails through preflight and dies mid-transfer.

## Transfer modes

**`direct`** runs rsync on the source, pushing straight to the target. Fastest,
and your laptop can disconnect mid-transfer.

**`tunnel`** opens a reverse SSH port-forward through your workstation, so the
source runs `rsync -e 'ssh -p <port>' root@localhost:...` and reaches a target it
has no route to. The workstation relays TCP only — **no byte lands on its disk**,
so ownership, symlinks and xattrs are untouched.

**`auto`** probes for direct reachability and falls back to the tunnel.

## SSH

| Variable | Default | Notes |
| --- | --- | --- |
| `SSH_TIMEOUT` | `15.0` | Connect timeout |
| `SSH_KNOWN_HOSTS` | state dir | Our managed known_hosts |
| `TRUST_HOST_KEY` | `false` | Record an unseen key. A *changed* key is still refused. |
| `STOP_TIMEOUT` | `300.0` | How long to wait for a stack to stop |

Host key checking is **never** disabled. Both tools this replaces use
`StrictHostKeyChecking=no`, which accepts anything, forever, and is MITM-able.

`STOP_TIMEOUT` is generous on purpose: a large database can legitimately take
minutes to flush, and rushing it produces the SIGKILL that the quiesce gate
treats as fatal.

## Observability

| Variable | Default | Notes |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `console` | `console` (Rich) or `json` (one object per line) |
| `STATE_DIR` | platform state dir | Where journals live |
| `NO_COLOR` | `false` | Honoured; also auto-detected for pipes and CI |

Secrets are redacted from logs by a structlog processor extended *additively*,
and journals refuse to store credential values at all — they raise rather than
redact, so a caller trying to journal a secret is a bug you find immediately.
