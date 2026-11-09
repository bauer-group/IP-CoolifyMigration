"""Ephemeral SSH keys for the source->target transfer.

The source must authenticate to the target to push data. Coolify's own keys are
per-server (they let *Coolify* reach a server), not source-to-target, so one has
to be provisioned.

We mint a keypair per migration, inject the public half into the target's
``authorized_keys``, use it, and revoke it in a **guaranteed compensation**. The
fingerprint — never the key — is journalled, so a crashed run still revokes on
the next invocation rather than leaving a credential behind forever.

Note the tunnel solves *reachability*, not *authentication*: the source still has
to prove who it is, whether it dials the target directly or through a forwarded
port. So the key is needed in both modes.

**Both edits of authorized_keys take Coolify's own lock** (:func:`_locked`). From
4.7.0 Coolify edits the same file — the tool connects as the server's user, and
Coolify edits that user's ``$HOME`` (``no_sudo``) — when an operator rotates a
server's key and, an hour or more later, when it removes the old one
(AuthorizedKeys v4.7.0:27-39 appends; :46-67 and :127-148 rewrite the file with
``mv``; RemoveRotatedAuthorizedKeyJob :26,44-71). Unlocked, a collision of
milliseconds loses a line: ours (COPY then fails after the stop), Coolify's new
key (its rotation fails safe), or Coolify's REMOVAL of the old key, which our
``sed -i`` rename would bring back while Coolify reports it removed. The lock
does no harm where nobody else takes it, so it is not gated on a version. Its
path is an upstream implementation detail, not a contract: if it moves, this
falls back to the old millisecond exposure, never to anything worse.
"""

from __future__ import annotations

import shlex

import asyncssh
import structlog

from bg_coolify_migrate.engine.context import EphemeralKey
from bg_coolify_migrate.errors import TransferError
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: Marks our lines in authorized_keys so revocation is exact rather than
#: heuristic. Never remove a line we did not add.
KEY_COMMENT_PREFIX = "bg-coolify-migrate"

#: `restrict` disables port forwarding, agent forwarding, X11 and pty allocation
#: while still permitting the command execution rsync needs. A forced command is
#: not usable here: rsync's server-side argv varies per invocation.
KEY_OPTIONS = "restrict"

#: Coolify's lock on the SSH user's authorized_keys: a directory, because mkdir is
#: atomic and needs no flock (AuthorizedKeys::runLocked v4.7.0:160-182). Unquoted
#: ``$HOME`` on purpose: the remote shell expands it, as it does for Coolify.
AUTHORIZED_KEYS_LOCK = "$HOME/.ssh/.coolify-authorized-keys.lock"

#: One-second attempts to take it — Coolify's own bound (:174), so neither side
#: waits longer for the other than it would for itself.
LOCK_ATTEMPTS = 30

#: The exit status of an edit that never got the lock (EX_TEMPFAIL), so the error
#: can say "busy" instead of "failed". Coolify exits 1 there; nothing else in our
#: two commands can exit 75.
LOCK_BUSY_EXIT = 75


def _locked(command: str) -> str:
    """``command`` run under Coolify's authorized_keys lock, released however it exits.

    A transcription of AuthorizedKeys::runLocked (v4.7.0:164-182), kept POSIX so
    it runs in whatever shell the login gives us: create ``~/.ssh`` if it is
    missing; remove a lock older than 10 minutes, which an edit that crashed left
    behind (edits take seconds); take the lock with ``mkdir`` in up to 30
    one-second attempts; release it with an EXIT trap, which a HUP/INT/TERM reaches
    through ``exit 1``. The trap does not change the edit's own exit status.
    """
    return (
        'if [ ! -d "$HOME/.ssh" ]; then mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"; fi; '
        f'lock="{AUTHORIZED_KEYS_LOCK}"; attempt=0; '
        'find "$lock" -maxdepth 0 -type d -mmin +10 -exec rmdir {} \\; 2>/dev/null || true; '
        'until mkdir "$lock" 2>/dev/null; do '
        "attempt=$((attempt + 1)); "
        f'if [ "$attempt" -ge {LOCK_ATTEMPTS} ]; then '
        'echo "$lock is held by another edit" >&2; '
        f"exit {LOCK_BUSY_EXIT}; fi; "
        "sleep 1; done; "
        "trap 'rmdir \"$lock\"' EXIT; trap 'exit 1' HUP INT TERM; "
        f"{command}"
    )


def _lock_busy_hint(host: str) -> str:
    return (
        f"Another edit held ~/.ssh/.coolify-authorized-keys.lock on {host} for "
        f"{LOCK_ATTEMPTS}s. Coolify (4.7+) takes it to rotate a server key, which "
        "takes seconds; try again shortly. A lock older than 10 minutes is removed "
        "as stale by Coolify and by this tool, so if nothing is editing the file, "
        "removing that directory by hand is safe."
    )


def generate(migration_id: str) -> tuple[str, str, str, str]:
    """Mint a keypair. Returns ``(private_pem, public_line, fingerprint, comment)``.

    Ed25519: small, fast, and no parameter choices to get wrong.
    """
    key = asyncssh.generate_private_key("ssh-ed25519")
    private_pem = key.export_private_key().decode()
    comment = f"{KEY_COMMENT_PREFIX}-{migration_id}"
    public_line = key.export_public_key().decode().strip()
    # export_public_key gives "ssh-ed25519 AAAA..."; append our comment so the
    # line is identifiable in authorized_keys.
    public_line = f"{public_line.split(' ', 2)[0]} {public_line.split(' ', 2)[1]} {comment}"
    fingerprint = key.get_fingerprint()
    return private_pem, public_line, fingerprint, comment


async def install(
    *,
    source: RemoteHost,
    target: RemoteHost,
    migration_id: str,
) -> EphemeralKey:
    """Mint, inject into the target, and drop onto the source.

    Raises:
        TransferError: If either half fails. We install the public key on the
            target FIRST: if writing the private key to the source then fails, we
            have an unusable-but-revocable credential rather than a private key
            lying around with nothing to authorise it.
    """
    private_pem, public_line, fingerprint, comment = generate(migration_id)

    # 1. Authorise on the target, under Coolify's lock (module docstring).
    await target.run_checked("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
    result = await target.run(
        _locked(
            f"printf '%s\\n' {shlex.quote(f'{KEY_OPTIONS} {public_line}')} "
            ">> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
        )
    )
    if result.exit_status == LOCK_BUSY_EXIT:
        raise TransferError(
            f"could not authorise the transfer key on the target ({target.target.host}): "
            "its authorized_keys stayed locked",
            hint=_lock_busy_hint(target.target.host),
        )
    if not result.ok:
        raise TransferError(
            f"could not authorise the transfer key on the target ({target.target.host})",
            hint=(result.stderr or "").strip()[:300] or None,
        )

    # 2. Drop the private half on the source, in a mode-700 dir under /root.
    #    NOT /tmp: it is world-traversable and often tmpfs.
    remote_dir = f"/root/.coolify-migrate/{migration_id}"
    remote_path = f"{remote_dir}/id_ed25519"
    install_cmd = (
        f"mkdir -p {shlex.quote(remote_dir)} && chmod 700 {shlex.quote(remote_dir)} && "
        f"umask 077 && cat > {shlex.quote(remote_path)} && chmod 600 {shlex.quote(remote_path)}"
    )
    result = await source.run(install_cmd, input_text=private_pem)
    if not result.ok:
        # Best-effort revoke so we do not leave an authorised key behind.
        await _revoke_on_target(target, comment)
        raise TransferError(
            f"could not place the transfer key on the source ({source.target.host})",
            hint=(result.stderr or "").strip()[:300] or None,
        )

    log.info(
        "keys.installed",
        fingerprint=fingerprint,
        source=source.target.host,
        target=target.target.host,
    )
    return EphemeralKey(
        private_key=private_pem,
        public_key=public_line,
        fingerprint=fingerprint,
        remote_path=remote_path,
    )


async def _revoke_on_target(target: RemoteHost, comment: str) -> bool:
    """Remove exactly our line from authorized_keys.

    Matched by our comment marker so we can never delete someone else's key.
    ``sed -i`` renames a rewritten copy over the file, which is safe only under
    Coolify's lock: outside it, the rename can undo an edit Coolify made between
    sed's read and the rename (module docstring).
    """
    escaped = comment.replace("/", r"\/")
    result = await target.run(
        _locked(
            "test -f ~/.ssh/authorized_keys && "
            f"sed -i.bak '/{escaped}/d' ~/.ssh/authorized_keys && rm -f ~/.ssh/authorized_keys.bak"
        )
    )
    if result.exit_status == LOCK_BUSY_EXIT:
        log.warning(
            "keys.revoke_lock_busy",
            target=target.target.host,
            hint=_lock_busy_hint(target.target.host),
        )
    return result.ok


async def revoke(
    *,
    source: RemoteHost | None,
    target: RemoteHost | None,
    migration_id: str,
) -> None:
    """Revoke and shred. Best-effort but loud.

    Both halves are attempted independently: failing to delete the private key
    must not prevent revoking the authorisation, which is the half that actually
    grants access.
    """
    comment = f"{KEY_COMMENT_PREFIX}-{migration_id}"

    if target is not None:
        if await _revoke_on_target(target, comment):
            log.info("keys.revoked", target=target.target.host, migration=migration_id)
        else:
            log.error(
                "keys.revoke_failed",
                target=target.target.host,
                migration=migration_id,
                hint=f"remove the line containing {comment!r} from ~/.ssh/authorized_keys",
            )

    if source is not None:
        remote_dir = f"/root/.coolify-migrate/{migration_id}"
        result = await source.run(f"rm -rf {shlex.quote(remote_dir)}")
        if not result.ok:
            log.error("keys.cleanup_failed", source=source.target.host, path=remote_dir)
