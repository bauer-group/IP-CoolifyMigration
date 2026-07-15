#!/usr/bin/env python3
"""prepare.py — Lay down what a real Coolify install would, before booting it.

Coolify's installer creates /data/coolify/source/.env on the host and only then
starts the containers. We do the same, because the ordering is exactly the
invariant F2 depends on and we should not test against a rig that cheats.

On Docker Desktop /data/coolify resolves inside the Linux VM, which is the same
place it resolves to on a real server. A privileged helper container writes it,
because we cannot touch the VM's filesystem from Windows directly.

Usage
-----
    python tests/e2e/prepare.py
    docker compose -f tests/e2e/docker-compose.yml up -d --wait
    python tests/e2e/bootstrap.py
    pytest -m e2e

Exit codes
----------
    0  ready
    1  docker unavailable, or the write failed
"""

from __future__ import annotations

import base64
import contextlib
import os
import secrets
import subprocess
import sys
from pathlib import Path

KEY_DIR = Path(__file__).parent / "keys"
PRIVATE = KEY_DIR / "id_ed25519"
PUBLIC = KEY_DIR / "id_ed25519.pub"
ENV_OUT = Path(__file__).parent / "coolify.env"

ROOT_EMAIL = "e2e@bauer-group.test"
ROOT_PASSWORD = "e2e-password-1234"

#: Git Bash rewrites /data into C:\...\data. Every docker call here needs this.
_ENV = {**os.environ, "MSYS_NO_PATHCONV": "1"}


def run(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, env=_ENV, **kw)  # type: ignore[call-overload,no-any-return]


def docker_ok() -> bool:
    return run(["docker", "version", "--format", "{{.Server.Version}}"]).returncode == 0


def make_keys() -> None:
    if PRIVATE.exists() and PUBLIC.exists():
        print(f"keys present in {KEY_DIR}")
        return
    import asyncssh

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="bgcm-e2e-rig")
    PRIVATE.write_bytes(key.export_private_key())
    PUBLIC.write_bytes(key.export_public_key())
    # Best-effort: Windows has no POSIX mode bits, and asyncssh does not care.
    with contextlib.suppress(OSError):
        PRIVATE.chmod(0o600)
    print(f"minted {PRIVATE.name}")


def app_key() -> str:
    """Laravel's APP_KEY format: base64: plus 32 random bytes."""
    return "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()


def build_env() -> str:
    return "\n".join(
        [
            "APP_ID=e2e-instance",
            "APP_NAME=Coolify-E2E",
            f"APP_KEY={app_key()}",
            "APP_ENV=production",
            "APP_DEBUG=false",
            "APP_URL=http://localhost:8000",
            "",
            "DB_CONNECTION=pgsql",
            "DB_HOST=postgres",
            "DB_PORT=5432",
            "DB_DATABASE=coolify",
            "DB_USERNAME=coolify",
            "DB_PASSWORD=e2e-db-password",
            "",
            "REDIS_HOST=redis",
            "REDIS_PASSWORD=e2e-redis-password",
            "",
            "PUSHER_APP_ID=e2e-pusher-id",
            "PUSHER_APP_KEY=e2e-pusher-key",
            "PUSHER_APP_SECRET=e2e-pusher-secret",
            "PUSHER_HOST=localhost",
            "PUSHER_PORT=6001",
            "",
            # Coolify creates the root user from these on first boot, which is
            # what makes the whole rig scriptable — no UI onboarding.
            "ROOT_USERNAME=e2e",
            f"ROOT_USER_EMAIL={ROOT_EMAIL}",
            f"ROOT_USER_PASSWORD={ROOT_PASSWORD}",
            "",
            "SELF_HOSTED=true",
            "AUTOUPDATE=false",
            "",
        ]
    )


def write_env_into_vm(text: str) -> bool:
    """Write /data/coolify/source/.env inside the Docker VM.

    Piped through a helper container's stdin rather than a bind mount, so the
    file lands with VM-native ownership rather than whatever Windows would
    project onto it.
    """
    script = (
        "mkdir -p /host/data/coolify/source /host/data/coolify/ssh/keys "
        "/host/data/coolify/applications /host/data/coolify/databases "
        "/host/data/coolify/services /host/data/coolify/backups && "
        "cat > /host/data/coolify/source/.env && "
        "chmod 600 /host/data/coolify/source/.env && "
        # Coolify runs as uid 9999 and writes its SSH keys here on demand.
        # Its own installer does this chown; without it every server
        # validation fails with "SSH keys storage directory is not writable"
        # and it looks like a network problem.
        "chown -R 9999:9999 /host/data/coolify/ssh && "
        "chmod -R 700 /host/data/coolify/ssh && "
        "echo written"
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "--privileged", "-v", "/:/host", "alpine", "sh", "-c", script],
        input=text,
        capture_output=True,
        text=True,
        env=_ENV,
    )
    if result.returncode != 0:
        print(f"error: {result.stderr.strip()[:400]}", file=sys.stderr)
        return False
    return "written" in result.stdout


def main() -> int:
    if not docker_ok():
        print("error: docker is not available; start Docker Desktop", file=sys.stderr)
        return 1

    make_keys()

    env_text = build_env()
    ENV_OUT.write_text(env_text, encoding="utf-8", newline="\n")
    print(f"wrote {ENV_OUT.name} (local copy, for reference)")

    if not write_env_into_vm(env_text):
        print("error: could not write /data/coolify/source/.env into the VM", file=sys.stderr)
        return 1
    print("wrote /data/coolify/source/.env inside the Docker VM")

    print(
        "\nnext:\n"
        "  docker compose -f tests/e2e/docker-compose.yml up -d --wait\n"
        "  python tests/e2e/bootstrap.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
