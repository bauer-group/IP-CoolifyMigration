# Quickstart: run in a venv (as root)

A short operational reference for running `bg-coolify-migrate` inside its own
Python virtual environment on a Linux server, as `root`. Covers the full
lifecycle: set up, use, update, remove.

The package on PyPI is **`bg-coolify-migrate`**; the command it installs is
**`coolify-migrate`**.

## 1. Prerequisites (once)

Debian/Ubuntu:

```bash
apt update
apt install -y python3 python3-venv python3-pip
```

RHEL/CentOS/Alma:

```bash
dnf install -y python3 python3-venv python3-pip
```

## 2. Create the venv (once)

```bash
python3 -m venv /opt/coolify-migrate-venv
```

This creates the environment under `/opt/coolify-migrate-venv`. A venv is just a
directory — which is what makes step 9 a single `rm -rf`.

## 3. Activate the venv

```bash
source /opt/coolify-migrate-venv/bin/activate
```

The shell prompt then shows `(coolify-migrate-venv)`. Every following `pip` and
Python command acts **only** inside the venv, never on the system Python.

## 4. Install the tool

```bash
pip install --upgrade pip
pip install bg-coolify-migrate
```

## 5. Use the tool

```bash
export COOLIFY_URL="https://coolify.example.com"
export COOLIFY_TOKEN="..."          # MUST have root or read:sensitive

coolify-migrate doctor                                # verify token + reachability
coolify-migrate plan my-project --to target-server    # dry run: no changes
coolify-migrate run  my-project --to target-server
```

Run `doctor` first — it proves the token can actually read secrets before
anything is touched. See [Installation](installation.md#verify) for the full
`doctor` contract and [CLI reference](cli.md) for every command and exit code.

!!! note "The `export` lines last only for the current shell session"
    For permanent operation, put the values in `/etc/environment`, a `.env` file
    you `source`, or a secrets manager — never hard-code them into a script.

## 6. Update the tool

The venv must be active (step 3), then:

```bash
pip install --upgrade bg-coolify-migrate
```

Check the currently installed version:

```bash
pip show bg-coolify-migrate
```

## 7. Deactivate the venv

```bash
deactivate
```

Returns the shell to the normal system Python.

## 8. Reactivate in a new session

After a login, reboot or new terminal window the venv is no longer active. Just
run:

```bash
source /opt/coolify-migrate-venv/bin/activate
```

!!! tip "Skip activation in cron jobs and scripts"
    Activation only prepends the venv's `bin/` to `PATH`, so calling the binary
    by its absolute path is equivalent and needs no active shell:

    ```bash
    /opt/coolify-migrate-venv/bin/coolify-migrate doctor
    ```

## 9. Remove the venv

Because a venv is only a directory, this is all it takes:

```bash
deactivate 2>/dev/null   # if currently active
rm -rf /opt/coolify-migrate-venv
```

That removes both the tool and all its Python packages completely. The system
Python is untouched.

## Alternative: pipx instead of a manual venv

For pure CLI use without activating or handling paths yourself:

```bash
apt install -y pipx
pipx install bg-coolify-migrate
pipx ensurepath
```

Update:

```bash
pipx upgrade bg-coolify-migrate
```

Remove:

```bash
pipx uninstall bg-coolify-migrate
```

pipx keeps each tool in its own isolated venv but exposes the `coolify-migrate`
command on your `PATH` — the isolation of a venv without the manual bookkeeping.

## Security note

A `COOLIFY_TOKEN` with `root` or `read:sensitive` scope is far-reaching: it can
read every secret Coolify holds. Scope the token as tightly as the migration
allows, and review each `coolify-migrate plan` carefully before the matching
`run`. See the [Safety model](safety.md) for what the tool guarantees — and what
it does not.
