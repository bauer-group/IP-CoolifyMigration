"""Shared fixtures.

The `FakeHost` is the important one. It lets the discovery, quiesce and verify
layers — which are otherwise pure SSH shells — be tested against scripted daemon
responses. Without it the quiesce gate, the single most safety-critical control
in the tool, would be untestable without a live server, which is precisely the
excuse both predecessor tools used for not testing theirs.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest

from bg_coolify_migrate.transfer.ssh import CommandResult, SshTarget


class FakeHost:
    """A RemoteHost stand-in that answers scripted commands.

    Matches commands by regex, in insertion order, so a test declares only what
    it cares about. An unmatched command is an ERROR rather than a silent empty
    result — a test that accidentally exercises an unstubbed code path should
    fail loudly rather than assert against nothing.
    """

    def __init__(self, target: SshTarget | None = None) -> None:
        self.target = target or SshTarget(host="10.0.0.1")
        self._routes: list[tuple[re.Pattern[str], Callable[[str], CommandResult]]] = []
        self.commands: list[str] = []
        self.forwards: list[tuple[str, int]] = []

    def on(
        self,
        pattern: str,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_status: int = 0,
    ) -> FakeHost:
        compiled = re.compile(pattern)

        def responder(command: str) -> CommandResult:
            return CommandResult(
                command=command, exit_status=exit_status, stdout=stdout, stderr=stderr
            )

        self._routes.append((compiled, responder))
        return self

    def on_sequence(self, pattern: str, results: list[dict[str, object]]) -> FakeHost:
        """Answer a repeated command differently on each call — the last repeats.

        For a command that is legitimately run more than once and expected to
        change between calls, like `docker ps -q` before and after a stop.
        """
        compiled = re.compile(pattern)
        queue = list(results)

        def responder(command: str) -> CommandResult:
            spec = queue.pop(0) if len(queue) > 1 else queue[0]
            return CommandResult(
                command=command,
                exit_status=int(spec.get("exit_status", 0)),  # type: ignore[arg-type]
                stdout=str(spec.get("stdout", "")),
                stderr=str(spec.get("stderr", "")),
            )

        self._routes.append((compiled, responder))
        return self

    async def run(
        self, command: str, *, timeout: float | None = None, input_text: str | None = None
    ) -> CommandResult:
        self.commands.append(command)
        for pattern, responder in self._routes:
            if pattern.search(command):
                return responder(command)
        raise AssertionError(
            f"FakeHost has no stub for command: {command!r}\n"
            f"Stubbed patterns: {[p.pattern for p, _ in self._routes]}"
        )

    async def run_checked(self, command: str, *, timeout: float | None = None) -> CommandResult:
        return (await self.run(command, timeout=timeout)).check()

    async def which(self, binary: str) -> bool:
        result = await self.run(f"command -v {binary}")
        return result.ok

    async def path_exists(self, path: str) -> bool:
        return (await self.run(f"test -e {path}")).ok

    async def read_file(self, path: str) -> str:
        return (await self.run_checked(f"cat {path}")).stdout

    async def free_bytes(self, path: str) -> int:
        result = await self.run_checked(f"df -Pk {path}")
        return int(result.stdout.strip()) * 1024

    @asynccontextmanager
    async def forward_to(self, remote_host: str, remote_port: int) -> AsyncIterator[int]:
        """Record that a reverse forward was opened, and hand back a fixed port.

        Exists so ``maybe_tunnel``'s decision is testable at all. Whether the
        tunnel is opened is what routes the migration's bytes, and it was decided
        by an untested branch until 2.6.2.
        """
        self.forwards.append((remote_host, remote_port))
        yield 44087


@pytest.fixture
def fake_host() -> FakeHost:
    return FakeHost()
