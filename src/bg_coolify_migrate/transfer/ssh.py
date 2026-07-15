"""SSH transport.

IO shell. Built on asyncssh rather than shelling out to the ``ssh`` binary for
three concrete reasons:

1. **The tunnel fallback needs a reverse port-forward.** When the source cannot
   reach the target directly we call :meth:`RemoteHost.forward_to`, and the
   source then runs ``rsync -e "ssh -p <lport>" root@localhost:...``. The bytes
   flow source -> a socket on the workstation -> target, and **never touch the
   Windows filesystem**. Managing that with a child ``ssh -R`` process means
   guessing at its lifecycle.
2. **No local binaries.** rsync only ever runs *on* the Linux servers, so a
   Windows operator needs neither ssh nor rsync installed.
3. **Host keys are handled explicitly.** ``StrictHostKeyChecking=no`` — which
   both predecessor tools use — is a non-negotiable no here.

Host key policy
---------------
We keep our own ``known_hosts`` under the state dir. An unknown host is a hard
error carrying the fingerprint, and the operator must accept it once
(``--trust-host-key``). That is trust-on-first-use with a human in the loop,
rather than trust-anything-forever.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import asyncssh
import structlog

from bg_coolify_migrate.errors import MigrationError, PreflightError

log = structlog.get_logger(__name__)


class SshError(MigrationError):
    """An SSH operation failed."""

    exit_code = 12


class HostKeyUnknown(PreflightError):
    """The host key is not in our known_hosts and has not been accepted."""

    exit_code = 13


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of one remote command."""

    command: str
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    def check(self) -> Self:
        """Raise unless the command succeeded.

        Never optional: a swallowed non-zero exit is exactly how coolify-mover
        ends up hot-copying a live Postgres data directory after a failed stop.
        """
        if not self.ok:
            raise SshError(
                f"remote command failed (exit {self.exit_status}): {self.command}",
                hint=(self.stderr or self.stdout or "").strip()[:500] or None,
            )
        return self


@dataclass(frozen=True, slots=True)
class SshTarget:
    """Where and how to connect."""

    host: str
    user: str = "root"
    port: int = 22
    private_key: str | None = None
    """PEM text. Coolify's own keys are readable via GET /v1/security/keys with a
    root token, which lets us reuse them instead of provisioning new ones."""
    passphrase: str | None = None
    proxy_command: str | None = None
    """For Coolify servers with `settings.is_cloudflare_tunnel`, which need
    `cloudflared access ssh --hostname %h`."""

    def __str__(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


class RemoteHost:
    """One SSH connection to a Linux server.

    Usage::

        async with RemoteHost.connect(target, known_hosts=path) as host:
            result = (await host.run("docker ps -a")).check()
    """

    def __init__(self, conn: asyncssh.SSHClientConnection, target: SshTarget) -> None:
        self._conn = conn
        self.target = target

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        target: SshTarget,
        *,
        known_hosts: Path | None = None,
        trust_new_host_key: bool = False,
        connect_timeout: float = 15.0,
    ) -> AsyncIterator[RemoteHost]:
        """Open a connection, verifying the host key.

        Args:
            target: Where to connect.
            known_hosts: Our managed known_hosts file. ``None`` means the
                system default, which is only appropriate for tests.
            trust_new_host_key: Record an unseen key instead of refusing. This is
                the ``--trust-host-key`` escape hatch; it is never the default.
            connect_timeout: Seconds.

        Raises:
            HostKeyUnknown: The key is unknown and ``trust_new_host_key`` is off.
            SshError: Anything else.
        """
        options: dict[str, Any] = {
            "username": target.user,
            "port": target.port,
            "connect_timeout": connect_timeout,
            # Never disable host key checking. If a key is unknown we say so.
            "known_hosts": str(known_hosts) if known_hosts and known_hosts.exists() else (),
        }
        if trust_new_host_key:
            options["known_hosts"] = None  # asyncssh: accept any (TOFU, opted in)
        if target.private_key:
            options["client_keys"] = [
                asyncssh.import_private_key(target.private_key, passphrase=target.passphrase)
            ]
        if target.proxy_command:
            options["proxy_command"] = target.proxy_command

        try:
            conn = await asyncssh.connect(target.host, **options)
        except asyncssh.HostKeyNotVerifiable as exc:
            raise HostKeyUnknown(
                f"host key for {target} is not known and was not accepted",
                hint=(
                    "Re-run with --trust-host-key to record it after verifying the fingerprint "
                    "out of band. We never disable host key checking: both tools this one "
                    "replaces use StrictHostKeyChecking=no, which makes every transfer "
                    "MITM-able."
                ),
            ) from exc
        except (TimeoutError, OSError, asyncssh.Error) as exc:
            raise SshError(
                f"cannot connect to {target}: {exc}",
                hint="Check the address, port, and that the key is authorised for this user.",
            ) from exc

        host = cls(conn, target)
        try:
            if trust_new_host_key and known_hosts is not None:
                await host._record_host_key(known_hosts)
            yield host
        finally:
            conn.close()
            with_suppressed = asyncio.shield(conn.wait_closed())
            try:
                await with_suppressed
            except Exception:
                log.debug("ssh.close.failed", target=str(target))

    async def _record_host_key(self, known_hosts: Path) -> None:
        key = self._conn.get_server_host_key()
        if key is None:  # pragma: no cover - only with an unusual transport
            return
        entry = f"[{self.target.host}]:{self.target.port} {key.export_public_key().decode()}"
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        existing = known_hosts.read_text(encoding="utf-8") if known_hosts.exists() else ""
        if entry.strip() in existing:
            return
        with known_hosts.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(entry.rstrip("\n") + "\n")
        log.info("ssh.hostkey.recorded", target=str(self.target), fingerprint=key.get_fingerprint())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._conn.close()

    # ── command execution ────────────────────────────────────────────────────

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        """Run a command and capture its output.

        Does NOT raise on a non-zero exit — call :meth:`CommandResult.check`.
        The split is deliberate: some callers legitimately expect failure (a
        probe), and making the check explicit means it can never be forgotten
        silently.
        """
        try:
            result = await self._conn.run(command, timeout=timeout, input=input_text, check=False)
        except asyncssh.Error as exc:
            raise SshError(f"failed to run on {self.target}: {command[:120]}: {exc}") from exc
        except TimeoutError as exc:
            raise SshError(
                f"timed out after {timeout}s on {self.target}: {command[:120]}"
            ) from exc

        return CommandResult(
            command=command,
            exit_status=result.exit_status if result.exit_status is not None else -1,
            stdout=_text(result.stdout),
            stderr=_text(result.stderr),
        )

    async def run_checked(self, command: str, *, timeout: float | None = None) -> CommandResult:
        return (await self.run(command, timeout=timeout)).check()

    async def which(self, binary: str) -> bool:
        """Whether a binary exists on PATH."""
        result = await self.run(f"command -v {shlex.quote(binary)} >/dev/null 2>&1")
        return result.ok

    async def read_file(self, path: str) -> str:
        return (await self.run_checked(f"cat {shlex.quote(path)}")).stdout

    async def path_exists(self, path: str) -> bool:
        return (await self.run(f"test -e {shlex.quote(path)}")).ok

    async def free_bytes(self, path: str) -> int:
        """Available bytes on the filesystem holding ``path``.

        Uses POSIX ``df -Pk`` and multiplies, rather than ``df -B1``, because the
        latter is a GNU extension and this must work on Alpine hosts too.
        """
        result = await self.run_checked(
            f"df -Pk {shlex.quote(path)} | awk 'NR==2 {{print $4}}'"
        )
        return int(result.stdout.strip()) * 1024

    # ── tunnelling ───────────────────────────────────────────────────────────

    @asynccontextmanager
    async def forward_to(self, remote_host: str, remote_port: int) -> AsyncIterator[int]:
        """Open a reverse forward so THIS host can reach ``remote_host``.

        Returns the port on *this* host's loopback that now tunnels to
        ``remote_host:remote_port`` via our workstation. The source can then run
        ``rsync -e 'ssh -p <port>' root@localhost:...`` and reach a target it has
        no route to.

        The workstation only relays TCP; it never stores a byte, so ownership,
        symlinks and xattrs are untouched — unlike coolify-mover's three-hop
        rsync through the operator's ``/tmp``.
        """
        listener = await self._conn.forward_remote_port("127.0.0.1", 0, remote_host, remote_port)
        port = listener.get_port()
        log.info(
            "ssh.tunnel.open",
            via=str(self.target),
            local_port=port,
            to=f"{remote_host}:{remote_port}",
        )
        try:
            yield port
        finally:
            listener.close()
            log.debug("ssh.tunnel.closed", via=str(self.target), local_port=port)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def join(parts: Sequence[str]) -> str:
    """Quote and join a command safely.

    Every remote command in this codebase goes through ``shlex.quote``.
    coolify-mover interpolates resource names — which are user-controlled from
    the Coolify dashboard — into a double-quoted shell string where ``$`` and
    backticks stay live, giving remote code execution as root.
    """
    return " ".join(shlex.quote(p) for p in parts)
