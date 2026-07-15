#!/usr/bin/env python3
"""prepare.py — Mint the throwaway keypair the integration rig uses.

The rig's two containers share one key so the source can reach the target, which
is what the real transfer path needs.

This key is generated on demand and gitignored. It authorises nothing but two
ephemeral containers on your own machine.

Usage
-----
    python tests/integration/prepare.py
    docker compose -f tests/integration/docker-compose.yml up -d --wait
    pytest -m integration
    docker compose -f tests/integration/docker-compose.yml down -v

Exit codes
----------
    0  keys ready
    1  could not write them
"""

from __future__ import annotations

import sys
from pathlib import Path

KEY_DIR = Path(__file__).parent / "keys"
PRIVATE = KEY_DIR / "id_ed25519"
PUBLIC = KEY_DIR / "id_ed25519.pub"


def main() -> int:
    if PRIVATE.exists() and PUBLIC.exists():
        print(f"keys already present in {KEY_DIR}")
        return 0

    try:
        import asyncssh
    except ImportError:
        print("error: asyncssh is not installed; run `pip install -e .[dev]`", file=sys.stderr)
        return 1

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="bgcm-integration-rig")

    PRIVATE.write_bytes(key.export_private_key())
    PUBLIC.write_bytes(key.export_public_key())

    # 0600 or sshd refuses the key. No-op on Windows, where the rig runs the key
    # inside the containers anyway.
    try:
        PRIVATE.chmod(0o600)
    except OSError:
        pass

    print(f"wrote {PRIVATE}")
    print(f"wrote {PUBLIC}")
    print("\nnext: docker compose -f tests/integration/docker-compose.yml up -d --wait")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
