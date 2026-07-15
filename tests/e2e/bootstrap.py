#!/usr/bin/env python3
"""bootstrap.py — Turn a booted rig into a Coolify that manages two servers.

Does what a human would do in the UI, through the API and Artisan:

1. create the root user (the installer's seeder does this on a real box)
2. mint a `root` API token
3. switch the API on (off by default instance-wide)
4. register the rig's SSH key
5. register server-a and server-b and validate them

Writes tests/e2e/rig.json for the e2e tests to read.

Usage
-----
    python tests/e2e/prepare.py
    docker compose -f tests/e2e/docker-compose.yml up -d --wait
    python tests/e2e/bootstrap.py
    pytest -m e2e

Exit codes
----------
    0  rig ready
    1  something upstream refused; the message says what
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
KEY_DIR = HERE / "keys"
PRIVATE = KEY_DIR / "id_ed25519"
RIG_FILE = HERE / "rig.json"

COOLIFY = "bgcm_e2e_coolify"
URL = "http://localhost:8000"
ROOT_EMAIL = "e2e@bauer-group.test"
ROOT_PASSWORD = "e2e-password-1234"

#: Git Bash rewrites /data into a Windows path. Every docker call needs this.
_ENV = {**os.environ, "MSYS_NO_PATHCONV": "1"}


def tinker(code: str) -> str:
    """Run PHP inside the Coolify container. Returns stdout."""
    result = subprocess.run(
        ["docker", "exec", COOLIFY, "php", "artisan", "tinker", "--execute", code],
        capture_output=True,
        text=True,
        env=_ENV,
    )
    if result.returncode != 0:
        raise RuntimeError(f"artisan failed: {result.stderr.strip()[:400]}")
    return result.stdout


def api(path: str, token: str, *, method: str = "GET", body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{URL}/api/v1{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()[:300]}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()


def create_root_user() -> None:
    tinker(
        f'$u = App\\Models\\User::firstOrCreate(["email" => "{ROOT_EMAIL}"], '
        f'["name" => "e2e", "password" => Illuminate\\Support\\Facades\\Hash::make("{ROOT_PASSWORD}")]);'
        '$t = App\\Models\\Team::find(0) ?? App\\Models\\Team::first();'
        'if (!$u->teams->contains($t->id)) { $u->teams()->attach($t->id, ["role" => "owner"]); }'
        'echo "user=".$u->id;'
    )
    print("root user ready")


def mint_token() -> str:
    # Coolify's createToken reads session("currentTeam")->id, and tinker has no
    # HTTP context — so we put the team in the session ourselves.
    out = tinker(
        f'$u = App\\Models\\User::where("email","{ROOT_EMAIL}")->first();'
        '$team = App\\Models\\Team::find(0) ?? App\\Models\\Team::first();'
        'session(["currentTeam" => $team]);'
        '$u->tokens()->delete();'
        'echo PHP_EOL."TOKEN=".$u->createToken("e2e-rig", ["root"])->plainTextToken.PHP_EOL;'
    )
    match = re.search(r"TOKEN=(\d+\|[A-Za-z0-9]+)", out)
    if not match:
        raise RuntimeError(f"could not mint a token; artisan said:\n{out[-500:]}")
    print("root token minted")
    return match.group(1)


def enable_api() -> None:
    # Off by default instance-wide. A 403 "API is disabled." looks exactly like a
    # token problem unless you read the body.
    tinker('App\\Models\\InstanceSettings::get()->update(["is_api_enabled" => true]); echo "on";')
    print("instance API enabled")


def register_key(token: str) -> str:
    """Register the rig's SSH key with Coolify. Returns its uuid.

    Also what makes the sensitive-probe determinate: with no keys at all there is
    nothing to probe against, and the tool (correctly) refuses to guess.
    """
    existing = api("/security/keys", token)
    if isinstance(existing, list):
        for key in existing:
            if isinstance(key, dict) and key.get("name") == "e2e-rig":
                return str(key["uuid"])

    result = api(
        "/security/keys",
        token,
        method="POST",
        body={
            "name": "e2e-rig",
            "description": "throwaway key for the e2e rig",
            "private_key": PRIVATE.read_text(encoding="utf-8"),
        },
    )
    uuid = result.get("uuid") if isinstance(result, dict) else None
    if not uuid:
        raise RuntimeError(f"registering the key returned no uuid: {result}")
    print("ssh key registered")
    return str(uuid)


def container_ip(name: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            name,
        ],
        capture_output=True,
        text=True,
        env=_ENV,
    )
    ip = result.stdout.strip()
    if not ip:
        raise RuntimeError(f"could not read an IP for {name}; is the rig up?")
    return ip


def register_server(token: str, *, name: str, ip: str, key_uuid: str) -> str:
    for server in api("/servers", token):  # type: ignore[union-attr]
        if isinstance(server, dict) and server.get("name") == name:
            return str(server["uuid"])

    result = api(
        "/servers",
        token,
        method="POST",
        body={
            "name": name,
            "description": f"e2e rig {name}",
            "ip": ip,
            "port": 22,
            "user": "root",
            "private_key_uuid": key_uuid,
            "is_build_server": False,
            "instant_validate": True,
        },
    )
    uuid = result.get("uuid") if isinstance(result, dict) else None
    if not uuid:
        raise RuntimeError(f"registering {name} returned no uuid: {result}")
    print(f"registered {name} at {ip}")
    return str(uuid)


def validate_server(token: str, uuid: str, name: str) -> bool:
    for attempt in range(1, 13):
        try:
            api(f"/servers/{uuid}/validate", token)
        except RuntimeError as exc:
            print(f"  {name}: validate attempt {attempt} -> {str(exc)[:120]}")
        server = api(f"/servers/{uuid}", token)
        # Under settings, not at the top level — the same shape our client got
        # wrong until this rig showed it.
        settings = server.get("settings") if isinstance(server, dict) else None
        if isinstance(settings, dict) and settings.get("is_reachable"):
            print(f"  {name}: reachable")
            return True
        time.sleep(5)
    print(f"  {name}: NOT reachable")
    return False


def main() -> int:
    if not PRIVATE.exists():
        print("error: run tests/e2e/prepare.py first", file=sys.stderr)
        return 1

    try:
        create_root_user()
        token = mint_token()
        enable_api()

        version = api("/version", token)
        print(f"coolify {version}")

        key_uuid = register_key(token)
        a_ip = container_ip("bgcm_e2e_server_a")
        b_ip = container_ip("bgcm_e2e_server_b")
        a_uuid = register_server(token, name="e2e-server-a", ip=a_ip, key_uuid=key_uuid)
        b_uuid = register_server(token, name="e2e-server-b", ip=b_ip, key_uuid=key_uuid)

        print("validating servers (Coolify SSHes to them):")
        a_ok = validate_server(token, a_uuid, "server-a")
        b_ok = validate_server(token, b_uuid, "server-b")
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    RIG_FILE.write_text(
        json.dumps(
            {
                "url": URL,
                "token": token,
                "key_uuid": key_uuid,
                "server_a": {"uuid": a_uuid, "ip": a_ip, "reachable": a_ok},
                "server_b": {"uuid": b_uuid, "ip": b_ip, "reachable": b_ok},
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"\nwrote {RIG_FILE.name}")

    if not (a_ok and b_ok):
        print("error: Coolify cannot reach both servers; e2e tests would be meaningless", file=sys.stderr)
        return 1

    print("\nrig ready:  pytest -m e2e")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
