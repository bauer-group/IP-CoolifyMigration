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

    # 1. Authorise on the target.
    await target.run_checked("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
    result = await target.run(
        f"printf '%s\\n' {shlex.quote(f'{KEY_OPTIONS} {public_line}')} >> ~/.ssh/authorized_keys "
        "&& chmod 600 ~/.ssh/authorized_keys"
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
    """
    escaped = comment.replace("/", r"\/")
    result = await target.run(
        "test -f ~/.ssh/authorized_keys && "
        f"sed -i.bak '/{escaped}/d' ~/.ssh/authorized_keys && rm -f ~/.ssh/authorized_keys.bak"
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
