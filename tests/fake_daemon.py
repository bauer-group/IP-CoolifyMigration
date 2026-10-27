"""A FakeHost whose docker answers come from STATE, for the volume-pin lifecycle.

The regex-routed FakeHost answers each command the same way every time, which
cannot express "the pin created in QUIESCE is listed in FINALIZE" or "the
restarted source now mounts the volume". This one keeps a tiny model of a
daemon — volumes and containers with labels and mounts — and answers exactly
the commands ``discovery/pins.py`` and ``discovery/docker.py`` send. Explicit
``.on()`` stubs still win, so a test can script anything else alongside.

It is deliberately strict where Docker is: ``docker volume rm`` refuses a
volume any container references, and ``docker rm`` of a missing container
fails. Those two refusals are what the pins exist to exploit and to respect.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from bg_coolify_migrate.transfer.ssh import CommandResult, SshTarget
from tests.conftest import FakeHost


@dataclass
class FakeContainer:
    name: str
    state: str = "running"
    labels: dict[str, str] = field(default_factory=dict)
    volumes: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"id-{self.name}"


class FakeDaemon(FakeHost):
    def __init__(self, *, volumes: tuple[str, ...] = (), target: SshTarget | None = None) -> None:
        super().__init__(target)
        self.volumes: set[str] = set(volumes)
        self.containers: dict[str, FakeContainer] = {}
        #: Popped per `docker create`: (exit_status, stderr). Empty = succeed.
        self.create_failures: list[tuple[int, str]] = []
        #: Popped per `docker rm`: (exit_status, stderr). Empty = Docker's own answer.
        self.rm_failures: list[tuple[int, str]] = []
        #: Called after every docker command we emulate, with the command.
        self.on_command: Callable[[str], None] | None = None

    def add(
        self,
        name: str,
        *,
        state: str = "running",
        labels: dict[str, str] | None = None,
        volumes: tuple[str, ...] = (),
    ) -> FakeContainer:
        container = FakeContainer(name, state, dict(labels or {}), list(volumes))
        self.containers[name] = container
        self.volumes.update(volumes)
        return container

    @property
    def created(self) -> list[str]:
        return [c for c in self.commands if "docker create" in c]

    async def run(
        self, command: str, *, timeout: float | None = None, input_text: str | None = None
    ) -> CommandResult:
        self.commands.append(command)
        for pattern, responder in self._routes:
            if pattern.search(command):
                return responder(command)
        result = self._docker(command)
        if self.on_command is not None:
            self.on_command(command)
        return result

    # ── emulation ────────────────────────────────────────────────────────────

    def _ok(self, command: str, stdout: str = "") -> CommandResult:
        return CommandResult(command=command, exit_status=0, stdout=stdout, stderr="")

    def _err(self, command: str, stderr: str, code: int = 1) -> CommandResult:
        return CommandResult(command=command, exit_status=code, stdout="", stderr=stderr)

    def _find(self, handle: str) -> FakeContainer | None:
        return next((c for c in self.containers.values() if handle in (c.name, c.id)), None)

    def _ps_line(self, c: FakeContainer) -> str:
        return json.dumps(
            {
                "ID": c.id,
                "Names": c.name,
                "State": c.state,
                "Labels": ",".join(f"{k}={v}" for k, v in c.labels.items()),
                "Image": "x",
            }
        )

    def _docker(self, command: str) -> CommandResult:
        if " && docker create " in command:
            return self._create(command, command.split(" && ", 1)[1])
        argv = shlex.split(command)
        if argv[:3] == ["docker", "ps", "-a"]:
            filters = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter"]
            found = list(self.containers.values())
            for flt in filters:
                kind, _, value = flt.partition("=")
                if kind == "label":
                    key, _, want = value.partition("=")
                    found = [c for c in found if c.labels.get(key) == want]
                elif kind == "volume":
                    found = [c for c in found if value in c.volumes]
            return self._ok(command, "\n".join(self._ps_line(c) for c in found))
        if argv[:3] == ["docker", "volume", "ls"]:
            return self._ok(
                command,
                "\n".join(json.dumps({"Name": v, "Driver": "local"}) for v in sorted(self.volumes)),
            )
        if argv[:3] == ["docker", "volume", "create"]:
            self.volumes.add(argv[3])
            return self._ok(command, argv[3])
        if argv[:3] == ["docker", "volume", "rm"]:
            name = argv[-1]
            if any(name in c.volumes for c in self.containers.values()):
                return self._err(
                    command, f"Error response from daemon: remove {name}: volume is in use"
                )
            self.volumes.discard(name)
            return self._ok(command, name)
        if argv[:2] == ["docker", "inspect"] and "{{json .Mounts}}" in argv:
            container = self._find(argv[-1])
            if container is None:
                return self._err(command, f"Error: No such object: {argv[-1]}")
            mounts = [
                {
                    "Type": "volume",
                    "Name": v,
                    "Source": f"/var/lib/docker/volumes/{v}/_data",
                    "Destination": f"/pin/{v}",
                    "RW": False,
                }
                for v in container.volumes
            ]
            return self._ok(command, json.dumps(mounts))
        if argv[:2] == ["docker", "rm"]:
            if self.rm_failures:
                code, stderr = self.rm_failures.pop(0)
                return self._err(command, stderr, code)
            container = self._find(argv[-1])
            if container is None:
                return self._err(
                    command, f"Error response from daemon: No such container: {argv[-1]}"
                )
            del self.containers[container.name]
            return self._ok(command, container.name)
        raise AssertionError(f"FakeDaemon cannot answer: {command!r}")

    def _create(self, command: str, create: str) -> CommandResult:
        if self.create_failures:
            code, stderr = self.create_failures.pop(0)
            return self._err(command, stderr, code)
        argv = shlex.split(create)
        name = argv[argv.index("--name") + 1]
        labels = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--label")
        volumes: list[str] = []
        for i, a in enumerate(argv):
            if a != "--mount":
                continue
            spec = dict(
                part.split("=", 1) if "=" in part else (part, "") for part in argv[i + 1].split(",")
            )
            if spec["source"] not in self.volumes:
                # What Docker would do, and what a pin must never cause.
                self.volumes.add(spec["source"])
            volumes.append(spec["source"])
        if name in self.containers:
            return self._err(command, f"Conflict. The container name {name!r} is already in use")
        self.containers[name] = FakeContainer(name, "created", labels, volumes)
        return self._ok(command, f"id-{name}")
