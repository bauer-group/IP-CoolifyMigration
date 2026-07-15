# Installation

## Requirements

**On your workstation** (Windows, macOS or Linux):

- Python 3.12, 3.13 or 3.14 (3.14 recommended; 3.12 is the supported floor)
- Nothing else. You do **not** need `ssh` or `rsync` locally — rsync always runs
  *on* the Linux servers, and SSH is pure-Python (asyncssh). This is deliberate:
  routing volume data through a Windows filesystem would destroy UNIX ownership,
  symlinks and xattrs, which is one of coolify-mover's bugs.

**On the Coolify servers**:

- `rsync` and `docker` — checked during preflight, *before* anything is stopped,
  so a missing binary is an aborted plan rather than an outage.
- SSH access as a user that can drive Docker.

**In Coolify**:

- An API token with **`root`** or **`read:sensitive`**
  (Keys & Tokens > API tokens).

## Install

```bash
pip install bg-coolify-migrate
```

From source:

```bash
git clone https://github.com/bauer-group/IP-CoolifyMigration
cd IP-CoolifyMigration
uv venv --python 3.14
uv pip install -e ".[dev]"
```

## Verify

```bash
coolify-migrate doctor
```

`doctor` proves the one thing that silently breaks everything else — whether the
token can actually read secrets — plus API reachability and your server
inventory. Run it before anything else.

Expected output:

```text
✓ Coolify 4.0.0 reachable at https://coolify.example.com
✓ token can read sensitive data (root / read:sensitive)
  Servers
  ...
✓ 3 project(s) visible
✓ state dir: /home/you/.local/state/bg-coolify-migrate/migrations
```

If the token check fails, `doctor` exits `10` and tells you why. Do not proceed:
a migration with that token would recreate every environment variable empty.

## Development

```bash
make install-dev   # or: .\Make.cmd install-dev
make all-checks    # ruff + mypy strict + pytest (>=80% coverage)
```
