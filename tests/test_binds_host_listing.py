"""Who mounts what: a container gone mid-listing mounts nothing, a silent daemon refuses.

``host_binds`` listed with ``docker ps -aq | xargs -r docker inspect``. Two
answers were wrong:

* A container removed between the two (a ``coolify-helper`` deploy container,
  a ``--rm`` job) made ``docker inspect`` exit 1, so a healthy busy server
  refused PREFLIGHT, or the end of CREATE after all of its work.
* A daemon that did not answer made ``docker ps`` fail with nothing on stdout;
  ``xargs -r`` then ran nothing and the PIPELINE exited 0 — "nobody else mounts
  it" read out of a silence (seen with a real Docker 29 CLI).

The script runs here in a real ``sh`` against a fake ``docker`` on PATH, which
prints what Docker 29 prints.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from bg_coolify_migrate.domain.binds import HostBind
from bg_coolify_migrate.errors import TransferError
from bg_coolify_migrate.transfer import binds as transfer_binds
from bg_coolify_migrate.transfer.ssh import CommandResult, SshTarget
from tests.conftest import FakeHost
from tests.test_binds_transfer import _posix

GONE = "b" * 64


@pytest.mark.parametrize(
    "stderr",
    [
        f"error: no such object: {GONE}",  # Docker 29
        f"Error: No such object: {GONE}",  # older clients
        f"Error response from daemon: No such container: {GONE}",
        f"error: no such object: {GONE}\nerror: no such object: {'c' * 64}\n",
    ],
)
def test_only_vanished_containers(stderr: str) -> None:
    assert transfer_binds.vanished_only(stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        f"error: no such object: {GONE}\npermission denied while trying to connect",
        "error: no such object: ../../etc",
        "xargs: docker: exited with status 255; aborting",
    ],
)
def test_anything_else_is_an_error(stderr: str) -> None:
    assert not transfer_binds.vanished_only(stderr)


async def test_the_others_are_still_listed() -> None:
    live = '{"name":"/web","labels":{},"mounts":[{"Type":"bind","Source":"/srv/x","RW":true}]}'
    host = FakeHost()
    host.on(r"^sh -c ", exit_status=1, stdout=live, stderr=f"error: no such object: {GONE}")
    found = await transfer_binds.host_binds(host)  # type: ignore[arg-type]
    assert found == [HostBind("web", "/srv/x", rw=True, labels={})]


async def test_a_mixed_failure_still_refuses() -> None:
    host = FakeHost()
    host.on(
        r"^sh -c ", exit_status=1, stderr=f"error: no such object: {GONE}\nerror during connect: EOF"
    )
    with pytest.raises(TransferError, match="could not list"):
        await transfer_binds.host_binds(host)  # type: ignore[arg-type]


async def test_a_silent_failure_still_refuses() -> None:
    host = FakeHost()
    host.on(r"^sh -c ", exit_status=1)
    with pytest.raises(TransferError, match="could not list"):
        await transfer_binds.host_binds(host)  # type: ignore[arg-type]


#: A ``docker`` CLI: ``ps`` lists $FAKE_IDS (or fails with $FAKE_PS_ERR);
#: ``inspect`` prints one line per id and Docker 29's error for each id in
#: $FAKE_GONE, then exits 1 as the real one does.
FAKE_DOCKER = r"""#!/bin/sh
case "$1" in
ps)
  if [ -n "$FAKE_PS_ERR" ]; then echo "$FAKE_PS_ERR" >&2; exit 1; fi
  for id in $FAKE_IDS; do printf '%s\n' "$id"; done
  exit 0 ;;
inspect)
  echo inspect >> "$FAKE_LOG"
  shift 3
  rc=0
  for id in "$@"; do
    case " $FAKE_GONE " in
      *" $id "*) echo "error: no such object: $id" >&2; rc=1 ;;
      *) printf '{"name":"/c-%.4s","labels":{},"mounts":[{"Type":"bind","Source":"/srv/%.4s","RW":true}]}\n' "$id" "$id" ;;
    esac
  done
  exit $rc ;;
esac
exit 2
"""


class _DockerSh:
    """Runs commands in a real local ``sh`` with :data:`FAKE_DOCKER` first on PATH."""

    target = SshTarget(host="local")

    def __init__(self, tmp_path: Path, **env: str) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER, newline="\n")
        docker.chmod(0o755)
        self.log = tmp_path / "inspect.log"
        self._path = _posix(bin_dir)
        self._env = {
            **os.environ,
            "FAKE_IDS": "",
            "FAKE_GONE": "",
            "FAKE_PS_ERR": "",
            "FAKE_LOG": _posix(tmp_path) + "/inspect.log",
            **env,
        }

    async def run(
        self, command: str, *, timeout: float | None = None, input_text: str | None = None
    ) -> CommandResult:
        full = f"PATH={shlex.quote(self._path)}:$PATH; export PATH; {command}"
        done = subprocess.run(
            ["sh", "-c", full], capture_output=True, text=True, check=False, env=self._env
        )
        return CommandResult(command, done.returncode, done.stdout, done.stderr)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
class TestTheScript:
    """HOST_BINDS_SCRIPT in a real ``sh``: the script's own behaviour, not a model."""

    LIVE = "a" * 64

    async def test_a_container_gone_before_its_inspect_is_skipped(self, tmp_path: Path) -> None:
        host = _DockerSh(tmp_path, FAKE_IDS=f"{self.LIVE} {GONE}", FAKE_GONE=GONE)
        found = await transfer_binds.host_binds(host)  # type: ignore[arg-type]
        assert found == [HostBind("c-aaaa", "/srv/aaaa", rw=True, labels={})]

    async def test_a_daemon_that_does_not_answer_refuses(self, tmp_path: Path) -> None:
        host = _DockerSh(tmp_path, FAKE_PS_ERR="Cannot connect to the Docker daemon")
        with pytest.raises(TransferError, match="could not list") as exc:
            await transfer_binds.host_binds(host)  # type: ignore[arg-type]
        assert "Cannot connect" in (exc.value.hint or "")
        assert not host.log.exists()  # nothing was inspected

    async def test_no_containers_inspects_nothing(self, tmp_path: Path) -> None:
        host = _DockerSh(tmp_path)
        assert await transfer_binds.host_binds(host) == []  # type: ignore[arg-type]
        assert not host.log.exists()
