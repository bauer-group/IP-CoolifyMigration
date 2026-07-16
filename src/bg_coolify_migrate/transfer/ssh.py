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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
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


#: Interactive host-key decision: given the target and its fingerprint, return
#: whether to trust it. Supplied by the CLI on a TTY; None means non-interactive.
HostKeyPrompt = Callable[[SshTarget, str], Awaitable[bool]]


def _initial_known_hosts(known_hosts: Path | None, trust_new_host_key: bool) -> str | None | tuple[()]:
    """The asyncssh ``known_hosts`` value for the first connect attempt.

    * a managed file that exists -> validate against it;
    * no managed file, but trusting -> ``None`` (accept any): there is nowhere to
      record, so this is the tests / system-default escape hatch, matching the old
      behaviour. With a managed file we NEVER pass None — an unknown key raises and
      is then scanned, recorded and re-validated;
    * otherwise -> ``()`` (empty): an unknown key is unverifiable and we say so.
    """
    if trust_new_host_key and known_hosts is None:
        return None
    return str(known_hosts) if known_hosts and known_hosts.exists() else ()


def _append_known_host(known_hosts: Path, target: SshTarget, key: Any) -> None:
    """Append a host key to our managed known_hosts (OpenSSH format, deduped)."""
    entry = f"[{target.host}]:{target.port} {key.export_public_key().decode().strip()}"
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    existing = known_hosts.read_text(encoding="utf-8") if known_hosts.exists() else ""
    if entry.strip() in existing:
        return
    with known_hosts.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(entry.rstrip("\n") + "\n")


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
        host_key_prompt: HostKeyPrompt | None = None,
        connect_timeout: float = 15.0,
    ) -> AsyncIterator[RemoteHost]:
        """Open a connection, always verifying the host key.

        On an unknown key we never connect blindly. In order of preference:

        * ``trust_new_host_key`` (the ``--trust-host-key`` flag / env, for
          unattended use) — record it and proceed.
        * ``host_key_prompt`` (interactive) — show the fingerprint and ask; on
          yes, record it and proceed.
        * otherwise — refuse with :class:`HostKeyUnknown`, carrying the fingerprint.

        The key is scanned out of band and WRITTEN to ``known_hosts`` before we
        authenticate, so the next run recognises it without asking again — real
        trust-on-first-use, not ``known_hosts=None`` (which accepts silently and
        records nothing).

        Args:
            target: Where to connect.
            known_hosts: Our managed known_hosts file. ``None`` means the system
                default, and disables recording — only appropriate for tests.
            trust_new_host_key: Accept and record an unseen key without asking.
            host_key_prompt: Interactive decision for an unseen key (TTY only).
            connect_timeout: Seconds.

        Raises:
            HostKeyUnknown: The key is unknown and was neither trusted nor accepted.
            SshError: Anything else.
        """
        options: dict[str, Any] = {
            "username": target.user,
            "port": target.port,
            "connect_timeout": connect_timeout,
            "known_hosts": _initial_known_hosts(known_hosts, trust_new_host_key),
        }
        if target.private_key:
            options["client_keys"] = [
                asyncssh.import_private_key(target.private_key, passphrase=target.passphrase)
            ]
        if target.proxy_command:
            options["proxy_command"] = target.proxy_command

        try:
            conn = await asyncssh.connect(target.host, **options)
        except asyncssh.HostKeyNotVerifiable as exc:
            conn = await cls._accept_or_refuse(
                target, options, known_hosts, trust_new_host_key, host_key_prompt, exc
            )
        except (TimeoutError, OSError, asyncssh.Error) as exc:
            raise SshError(
                f"cannot connect to {target}: {exc}",
                hint="Check the address, port, and that the key is authorised for this user.",
            ) from exc

        host = cls(conn, target)
        try:
            yield host
        finally:
            conn.close()
            with_suppressed = asyncio.shield(conn.wait_closed())
            try:
                await with_suppressed
            except Exception:
                log.debug("ssh.close.failed", target=str(target))

    @classmethod
    async def _accept_or_refuse(
        cls,
        target: SshTarget,
        options: dict[str, Any],
        known_hosts: Path | None,
        trust_new_host_key: bool,
        host_key_prompt: HostKeyPrompt | None,
        original: BaseException,
    ) -> Any:
        """Handle an unknown host key: scan it, decide, record + retry, or refuse."""
        if known_hosts is None:
            # No managed file to record into (tests / system default).
            raise HostKeyUnknown(
                f"host key for {target} is not known and was not accepted"
            ) from original

        key = await cls._scan_host_key(target)
        fingerprint = key.get_fingerprint()

        accept = trust_new_host_key
        if not accept and host_key_prompt is not None:
            accept = await host_key_prompt(target, fingerprint)

        if not accept:
            raise HostKeyUnknown(
                f"host key for {target} is not known and was not accepted",
                hint=(
                    f"Fingerprint: {fingerprint}. Run in a terminal to accept it interactively, "
                    "or pass --trust-host-key once you have verified it out of band. Host key "
                    "checking is never disabled."
                ),
            ) from original

        _append_known_host(known_hosts, target, key)
        log.info("ssh.hostkey.recorded", target=str(target), fingerprint=fingerprint)
        try:
            return await asyncssh.connect(
                target.host, **{**options, "known_hosts": str(known_hosts)}
            )
        except (TimeoutError, OSError, asyncssh.Error) as exc:
            raise SshError(
                f"cannot connect to {target} after recording its host key: {exc}"
            ) from exc

    @classmethod
    async def _scan_host_key(cls, target: SshTarget) -> Any:
        """Fetch the server's host key without authenticating, to show and record."""
        kwargs: dict[str, Any] = {}
        if target.proxy_command:
            kwargs["proxy_command"] = target.proxy_command
        try:
            key = await asyncssh.get_server_host_key(target.host, target.port, **kwargs)
        except (TimeoutError, OSError, asyncssh.Error) as exc:
            raise SshError(f"cannot read the host key for {target}: {exc}") from exc
        if key is None:
            raise SshError(f"{target} presented no host key to record")
        return key

    @classmethod
    async def ensure_host_key(
        cls,
        target: SshTarget,
        *,
        known_hosts: Path | None,
        trust_new_host_key: bool = False,
        host_key_prompt: HostKeyPrompt | None = None,
    ) -> None:
        """Record ``target``'s host key if unseen, prompting or refusing as needed.

        A preflight run BEFORE the real connection, so a connection made under a
        live display (where a prompt cannot be shown) never has to ask. No-op when
        the key is already trusted or ``known_hosts`` is None.
        """
        if known_hosts is None:
            return
        key = await cls._scan_host_key(target)
        entry = f"[{target.host}]:{target.port} {key.export_public_key().decode().strip()}"
        existing = known_hosts.read_text(encoding="utf-8") if known_hosts.exists() else ""
        if entry.strip() in existing:
            return  # already trusted

        fingerprint = key.get_fingerprint()
        accept = trust_new_host_key
        if not accept and host_key_prompt is not None:
            accept = await host_key_prompt(target, fingerprint)
        if not accept:
            raise HostKeyUnknown(
                f"host key for {target} is not known and was not accepted",
                hint=(
                    f"Fingerprint: {fingerprint}. Run in a terminal to accept it interactively, "
                    "or pass --trust-host-key once you have verified it out of band."
                ),
            )
        _append_known_host(known_hosts, target, key)
        log.info("ssh.hostkey.recorded", target=str(target), fingerprint=fingerprint)

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
