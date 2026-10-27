"""Reading the config file Coolify wrote for a resource, on the source server.

IO shell; what the text means is decided in ``domain/locked_envs.py``.

The file holds SECRETS — that is why it is read at all — so this module is
stricter than every other reader in the tool:

* ``host.run``, never ``read_file`` / ``run_checked``: ``CommandResult.check()``
  puts stdout into the error hint, and stdout here is the secret.
* No exception escapes. An SSH failure, a decode error, anything — it becomes
  an "unavailable" reason built from the exception's TYPE and the exit code,
  never from stdout or stderr. The caller then degrades exactly as for a
  missing file: a refusal or a warning, by the normal classes.
* The CommandResult is dropped here; only the text travels on, inside a
  SourceConfig whose ``repr`` leaves it out.
* No sudo. A non-root SSH user may not be able to read ``/data/coolify`` (Coolify
  itself notes this, v4.5.0 Service.php:1644); that is "unreadable", not a
  reason to escalate.
"""

from __future__ import annotations

import shlex

import structlog

from bg_coolify_migrate.domain.locked_envs import FileKind, SourceConfig, config_path, single_copy
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)

#: A config file is a few KB; a read that takes longer is not going to succeed.
_READ_TIMEOUT = 30.0


async def read_source_config(host: RemoteHost, collection: str, uuid: str) -> SourceConfig:
    """The file Coolify last wrote this resource's env values into. Never raises.

    Read twice in ONE command, separated by a NUL, and kept only if both reads
    agree (``locked_envs.single_copy``): the application .env and a database's
    compose are rewritten with ``tee``, and a read overlapping that sees a
    truncated last record.
    """
    try:
        path, kind = config_path(collection, uuid)
    except ValueError:
        return SourceConfig(
            path=f"the {collection} config file",
            kind=FileKind.DOTENV,
            unavailable="its uuid is not a plain Coolify uuid, so no config path is built from it",
        )

    quoted = shlex.quote(path)
    try:
        result = await host.run(
            f"cat -- {quoted} && printf '\\000' && cat -- {quoted}", timeout=_READ_TIMEOUT
        )
        exit_status, stdout = result.exit_status, result.stdout
        del result
    except Exception as exc:  # never fatal, never echoed: see the module docstring
        log.warning("envfiles.read_failed", path=path, error=type(exc).__name__)
        return SourceConfig(
            path=path,
            kind=kind,
            unavailable=f"{path} could not be read from the source server ({type(exc).__name__})",
        )

    if exit_status != 0:
        log.info("envfiles.unavailable", path=path, exit_status=exit_status)
        return SourceConfig(
            path=path,
            kind=kind,
            unavailable=f"{path} is missing or unreadable on the source server (exit {exit_status})",
        )

    text = single_copy(stdout)
    if text is None:
        log.info("envfiles.changed_while_read", path=path)
        return SourceConfig(
            path=path,
            kind=kind,
            unavailable=f"{path} changed while it was read (Coolify was rewriting it); plan again",
        )
    return SourceConfig(path=path, kind=kind, text=text)
