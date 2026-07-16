"""APP_KEY: the invariant the whole of F2 depends on.

``/data/coolify/source/.env`` holds ``APP_KEY``, and Laravel uses it to encrypt
Coolify's entire credential store — every environment variable value, every
database password, every SSH private key, every log-drain key.

It survives a migration because of an ordering nobody documents::

    # install.sh merges the .env, EXISTING VALUES FIRST:
    awk -F '=' '!seen[$1]++' "$ENV_FILE" "/data/coolify/source/.env.production"

    # ...and only fills EMPTY or MISSING vars:
    update_env_var "APP_KEY" "base64:$(openssl rand -base64 32)"

A migrated ``.env`` already has ``APP_KEY=base64:...`` populated, so neither
branch fires. Reverse the order — install first, extract second — and it still
appears to work, until extraction fails and you have a fresh key against a
restored database. Every secret is then permanently undecryptable.

Geczy's script gets the ordering right and never mentions APP_KEY at all. It
works by luck. This module makes it an assertion:

1. Read it BEFORE anything moves.
2. Assert it is byte-identical AFTER install.sh ran.
3. Probe that decryption actually works, rather than assuming.

The key is never journalled, never logged, and never returned in an error
message — only its fingerprint.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from enum import StrEnum

import structlog

from bg_coolify_migrate.errors import MigrationError
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

COOLIFY_ENV_PATH = "/data/coolify/source/.env"

_APP_KEY_RE = re.compile(r"^APP_KEY=(.+)$", re.MULTILINE)
_DB_PASSWORD_RE = re.compile(r"^DB_PASSWORD=(.+)$", re.MULTILINE)


class AppKeyError(MigrationError):
    """APP_KEY is missing, changed, or cannot decrypt.

    Always fatal. A Coolify whose APP_KEY does not match its database is not a
    degraded Coolify — it is one where every secret is unreadable, and no amount
    of retrying fixes it.
    """

    exit_code = 15


def fingerprint(key: str) -> str:
    """A stable, non-reversible identifier for an APP_KEY.

    We compare and journal THIS, never the key. A fingerprint in a log is
    useless to an attacker and sufficient for us.
    """
    return "sha256:" + hashlib.sha256(key.strip().encode()).hexdigest()[:16]


def extract_app_key(env_text: str) -> str | None:
    """Pull APP_KEY out of a .env. PURE."""
    match = _APP_KEY_RE.search(env_text)
    if not match:
        return None
    value = match.group(1).strip().strip("\"'")
    return value or None


def extract_db_password(env_text: str) -> str | None:
    """Pull DB_PASSWORD out of a .env. PURE.

    Matters as much as APP_KEY: the copied ``coolify-db`` volume still holds the
    OLD password hash, so a regenerated DB_PASSWORD locks Coolify out of its own
    database.
    """
    match = _DB_PASSWORD_RE.search(env_text)
    if not match:
        return None
    value = match.group(1).strip().strip("\"'")
    return value or None


async def read(host: RemoteHost) -> tuple[str, str | None]:
    """Read ``(app_key, db_password)`` from a host's Coolify .env.

    Raises:
        AppKeyError: If the file or the key is absent. Refusing here — before
            anything is stopped — is the whole point.
    """
    if not await host.path_exists(COOLIFY_ENV_PATH):
        raise AppKeyError(
            f"{COOLIFY_ENV_PATH} does not exist on {host.target.host}",
            hint=(
                "That path holds APP_KEY, which decrypts every credential Coolify stores. "
                "Is this actually a Coolify host?"
            ),
        )

    text = await host.read_file(COOLIFY_ENV_PATH)
    app_key = extract_app_key(text)
    if not app_key:
        raise AppKeyError(
            f"APP_KEY not found in {COOLIFY_ENV_PATH} on {host.target.host}",
            hint="Without it, the migrated database is a locked vault with no key.",
        )

    log.info("appkey.read", host=host.target.host, fingerprint=fingerprint(app_key))
    return app_key, extract_db_password(text)


async def assert_survived(
    host: RemoteHost, *, expected: str, expected_db_password: str | None = None
) -> None:
    """Assert the target's APP_KEY is byte-identical to the source's.

    Run AFTER install.sh. If this fails, install.sh regenerated the key — which
    means the archive was not in place when it ran, and the ordering invariant
    was violated.

    Raises:
        AppKeyError: On any mismatch.
    """
    text = await host.read_file(COOLIFY_ENV_PATH)
    actual = extract_app_key(text)

    if not actual:
        raise AppKeyError(
            f"APP_KEY vanished from {COOLIFY_ENV_PATH} on the target",
            hint="install.sh should have preserved it. The archive may not have extracted.",
        )

    if actual.strip() != expected.strip():
        raise AppKeyError(
            "the target's APP_KEY does not match the source's",
            hint=(
                f"expected {fingerprint(expected)}, got {fingerprint(actual)}.\n"
                "install.sh regenerated it, which means the archive was NOT in place when "
                "it ran. Every secret in the migrated database is now undecryptable with "
                "this key.\n\n"
                "Recovery: restore the original APP_KEY into "
                f"{COOLIFY_ENV_PATH}, or add the old key to APP_PREVIOUS_KEYS."
            ),
        )

    if expected_db_password:
        actual_db = extract_db_password(text)
        if actual_db and actual_db.strip() != expected_db_password.strip():
            raise AppKeyError(
                "the target's DB_PASSWORD does not match the source's",
                hint=(
                    "The copied coolify-db volume still holds the OLD password hash, so "
                    "Coolify cannot log in to its own database. install.sh only fills "
                    "EMPTY vars, so this means the .env was not in place when it ran."
                ),
            )

    log.info("appkey.survived", host=host.target.host, fingerprint=fingerprint(actual))


async def add_previous_key(host: RemoteHost, previous: str) -> None:
    """Add an old key to ``APP_PREVIOUS_KEYS``.

    Laravel's escape hatch: it tries the current key, then each previous one. Use
    when the key genuinely rotated and the data was encrypted with the old one.
    """
    line = f"APP_PREVIOUS_KEYS={previous}"
    await host.run_checked(
        f"grep -q '^APP_PREVIOUS_KEYS=' {shlex.quote(COOLIFY_ENV_PATH)} || "
        f"printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(COOLIFY_ENV_PATH)}"
    )
    log.info("appkey.previous_added", fingerprint=fingerprint(previous))


class ProbeResult(StrEnum):
    """Three outcomes, and the distinction is load-bearing.

    NOT_READY and DECRYPT_FAILED both mean "no decrypted value came back", but one
    is transient and one is terminal. Conflating them is what made F2 abort a
    successful migration: it probed one second after the container reported
    ``running`` — long before Laravel had finished booting — so ``artisan
    tinker`` could not run at all, and that was read as "the data is corrupt".
    """

    OK = "ok"
    """A stored value decrypted, or there was nothing to decrypt (empty instance)."""
    NOT_READY = "not_ready"
    """Coolify's app is not up enough to answer yet. Retry — do not conclude."""
    DECRYPT_FAILED = "decrypt_failed"
    """Artisan RAN and could not read a value. Terminal: the key/data disagree."""


async def decrypt_probe(host: RemoteHost) -> ProbeResult:
    """Prove decryption works, rather than assuming it from a matching key.

    Asks Coolify's own Artisan to decrypt a stored value. A matching APP_KEY that
    still cannot decrypt means something else is wrong (a truncated volume, a
    mismatched cipher), and finding that out now beats finding it out from a user.

    Distinguishes "the app cannot answer yet" from "the app answered and the data
    is unreadable" — see ProbeResult. The caller polls; only DECRYPT_FAILED is a
    reason to abort.
    """
    result = await host.run(
        "docker exec coolify php artisan tinker --execute="
        + shlex.quote(
            "echo App\\\\Models\\\\EnvironmentVariable::query()->whereNotNull('value')"
            "->first()?->value ? 'DECRYPT_OK' : 'NO_DATA';"
        ),
        timeout=60,
    )
    if not result.ok:
        # tinker itself could not run — app still booting, DB not connected yet.
        # Transient, not evidence about the data.
        log.debug("appkey.probe.not_ready", stderr=result.stderr[:200])
        return ProbeResult.NOT_READY

    if "DECRYPT_OK" in result.stdout or "NO_DATA" in result.stdout:
        log.info("appkey.probe.ok", empty="NO_DATA" in result.stdout)
        return ProbeResult.OK

    # Artisan ran and produced neither marker: a decrypt exception. Terminal.
    log.error("appkey.probe.decrypt_failed", output=result.stdout[:200])
    return ProbeResult.DECRYPT_FAILED
