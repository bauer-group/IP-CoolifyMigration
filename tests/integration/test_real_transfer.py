"""End-to-end transfer tests against real sshd + real rsync.

These are the tests that actually prove the tool's central promise. The unit
tests verify the volume-pairing algebra and the rsync command construction; they
cannot verify that a file owned by uid 999, with an xattr, an ACL, a hardlink and
a sparse hole, arrives intact. Only real rsync over real ssh can.

Every case here corresponds to a flag coolify-mover omits:

  uid 999      -> --numeric-ids   (without it the DB will not start)
  hardlinks    -> -H              (without it: exploded into duplicates)
  xattrs       -> -X
  ACLs         -> -A
  sparse       -> -S
  symlinks     -> -a

Run:
    docker compose -f tests/integration/docker-compose.yml up -d --wait
    pytest -m integration
    docker compose -f tests/integration/docker-compose.yml down -v
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bg_coolify_migrate.transfer import rsync, verify
from bg_coolify_migrate.transfer.ssh import RemoteHost, SshTarget

pytestmark = pytest.mark.integration

KEY_DIR = Path(__file__).parent / "keys"
PRIVATE_KEY = KEY_DIR / "id_ed25519"

SOURCE_PORT = 2222
TARGET_PORT = 2223

#: In-container path both hosts mount a volume at.
DATA = "/data"


def _target(port: int) -> SshTarget:
    return SshTarget(
        host="127.0.0.1",
        user="root",
        port=port,
        private_key=PRIVATE_KEY.read_text(encoding="utf-8"),
    )


@pytest.fixture(scope="module", autouse=True)
def _require_rig() -> None:
    if not PRIVATE_KEY.exists():
        pytest.skip(
            "integration rig not prepared; run: python tests/integration/prepare.py "
            "&& docker compose -f tests/integration/docker-compose.yml up -d --wait"
        )


@pytest.fixture
async def source() -> AsyncIterator[RemoteHost]:
    async with RemoteHost.connect(_target(SOURCE_PORT), trust_new_host_key=True) as host:
        await host.run(f"rm -rf {DATA}/* {DATA}/.[!.]* 2>/dev/null")
        yield host


@pytest.fixture
async def target() -> AsyncIterator[RemoteHost]:
    async with RemoteHost.connect(_target(TARGET_PORT), trust_new_host_key=True) as host:
        await host.run(f"rm -rf {DATA}/* {DATA}/.[!.]* 2>/dev/null")
        yield host


async def _install_key(source: RemoteHost) -> str:
    """Put the shared key on the source so it can reach the target."""
    path = "/root/transfer_key"
    await source.run(
        f"umask 077 && cat > {path}", input_text=PRIVATE_KEY.read_text(encoding="utf-8")
    )
    await source.run_checked(f"chmod 600 {path}")
    return path


async def _copy(source: RemoteHost, identity: str, *, src: str = DATA, dst: str = DATA) -> None:
    spec = rsync.RsyncSpec(
        source_path=src,
        target_path=dst,
        # The rig exposes the target on the host's loopback; from inside the
        # source container that is the gateway.
        target_host="host.docker.internal",
        target_user="root",
        target_port=TARGET_PORT,
        identity_file=identity,
    )
    await rsync.run(source, spec)


class TestOwnershipPreservation:
    async def test_uid_999_survives(self, source: RemoteHost, target: RemoteHost) -> None:
        """THE test. Postgres/MySQL/Redis run as uid 999.

        Without --numeric-ids rsync maps ownership BY NAME through the remote
        passwd database. If 999 is some other user there — or absent — the files
        land wrong and the database will not start. coolify-mover omits the flag.
        """
        await source.run_checked(f"mkdir -p {DATA}/pgdata && echo data > {DATA}/pgdata/base")
        await source.run_checked(f"chown -R 999:999 {DATA}/pgdata")

        identity = await _install_key(source)
        await _copy(source, identity)

        result = await target.run_checked(f"stat -c '%u:%g' {DATA}/pgdata/base")
        assert result.stdout.strip() == "999:999"

    async def test_clickhouse_uid_101_survives(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        await source.run_checked(f"mkdir -p {DATA}/ch && echo x > {DATA}/ch/f")
        await source.run_checked(f"chown -R 101:101 {DATA}/ch")

        identity = await _install_key(source)
        await _copy(source, identity)

        result = await target.run_checked(f"stat -c '%u:%g' {DATA}/ch/f")
        assert result.stdout.strip() == "101:101"

    async def test_modes_survive(self, source: RemoteHost, target: RemoteHost) -> None:
        await source.run_checked(f"echo secret > {DATA}/key && chmod 600 {DATA}/key")
        identity = await _install_key(source)
        await _copy(source, identity)
        result = await target.run_checked(f"stat -c '%a' {DATA}/key")
        assert result.stdout.strip() == "600"


class TestHardlinks:
    async def test_hardlinks_stay_linked(self, source: RemoteHost, target: RemoteHost) -> None:
        """Without -H a hardlink pair becomes two full copies.

        For a backup volume with hardlinked snapshots that turns 50 GB into
        hundreds. coolify-mover uses `-avz` and omits -H.
        """
        await source.run_checked(f"echo content > {DATA}/original")
        await source.run_checked(f"ln {DATA}/original {DATA}/hardlink")

        identity = await _install_key(source)
        await _copy(source, identity)

        inode_a = (await target.run_checked(f"stat -c '%i' {DATA}/original")).stdout.strip()
        inode_b = (await target.run_checked(f"stat -c '%i' {DATA}/hardlink")).stdout.strip()
        assert inode_a == inode_b, "hardlink was exploded into a separate file"

        links = (await target.run_checked(f"stat -c '%h' {DATA}/original")).stdout.strip()
        assert links == "2"


class TestSymlinks:
    async def test_symlinks_are_copied_as_symlinks(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        await source.run_checked(f"echo real > {DATA}/real")
        await source.run_checked(f"ln -s real {DATA}/link")

        identity = await _install_key(source)
        await _copy(source, identity)

        result = await target.run_checked(f"readlink {DATA}/link")
        assert result.stdout.strip() == "real"

    async def test_dangling_symlinks_survive(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        # A symlink to a path that does not exist yet is legitimate; rsync must
        # not resolve or drop it.
        await source.run_checked(f"ln -s /nowhere {DATA}/dangling")
        identity = await _install_key(source)
        await _copy(source, identity)
        result = await target.run_checked(f"readlink {DATA}/dangling")
        assert result.stdout.strip() == "/nowhere"


class TestXattrs:
    async def test_xattrs_survive(self, source: RemoteHost, target: RemoteHost) -> None:
        await source.run_checked(f"echo x > {DATA}/f")
        set_result = await source.run(f"setfattr -n user.test -v hello {DATA}/f")
        if not set_result.ok:
            pytest.skip("xattrs not supported by this filesystem")

        identity = await _install_key(source)
        await _copy(source, identity)

        result = await target.run_checked(f"getfattr -n user.test --only-values {DATA}/f")
        assert "hello" in result.stdout


class TestSparseFiles:
    async def test_sparse_files_stay_sparse(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        # Without -S a sparse DB file inflates to its apparent size and can fill
        # the target's disk.
        await source.run_checked(f"truncate -s 64M {DATA}/sparse")

        identity = await _install_key(source)
        await _copy(source, identity)

        blocks = (await target.run_checked(f"stat -c '%b' {DATA}/sparse")).stdout.strip()
        apparent = (await target.run_checked(f"stat -c '%s' {DATA}/sparse")).stdout.strip()
        assert int(apparent) == 64 * 1024 * 1024
        # A non-sparse 64M file would be ~131072 512-byte blocks.
        assert int(blocks) < 1000, "sparse file was materialised"


class TestVerification:
    async def test_identical_trees_verify(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        await source.run_checked(f"mkdir -p {DATA}/d && echo a > {DATA}/d/1 && echo b > {DATA}/d/2")
        await source.run_checked(f"chown -R 999:999 {DATA}/d")

        identity = await _install_key(source)
        await _copy(source, identity)

        report = await verify.verify_volume(
            source, target, source_path=DATA, target_path=DATA
        )
        assert report.ok, [d.describe() for d in report.differences]

    async def test_a_wrong_chown_is_caught(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        """Exactly the corruption Coolify's own clone causes.

        VolumeCloneJob does `chown -R 1000:1000 /target` after copying. The bytes
        are identical, so a content-only checksum passes — and the database will
        not start. Only the metadata manifest catches it.
        """
        await source.run_checked(f"echo data > {DATA}/f && chown 999:999 {DATA}/f")
        identity = await _install_key(source)
        await _copy(source, identity)

        # Simulate the damage.
        await target.run_checked(f"chown -R 1000:1000 {DATA}")

        report = await verify.verify_volume(
            source, target, source_path=DATA, target_path=DATA
        )
        assert not report.ok
        assert any("metadata_differs" in d.kind.value for d in report.differences)

    async def test_content_difference_is_caught(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        await source.run_checked(f"echo original > {DATA}/f")
        identity = await _install_key(source)
        await _copy(source, identity)
        await target.run_checked(f"echo tampered > {DATA}/f")

        report = await verify.verify_volume(
            source, target, source_path=DATA, target_path=DATA
        )
        assert not report.ok
        assert any("content_differs" in d.kind.value for d in report.differences)

    async def test_missing_file_is_caught(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        await source.run_checked(f"echo a > {DATA}/keep && echo b > {DATA}/gone")
        identity = await _install_key(source)
        await _copy(source, identity)
        await target.run_checked(f"rm {DATA}/gone")

        report = await verify.verify_volume(
            source, target, source_path=DATA, target_path=DATA
        )
        assert not report.ok
        assert any("missing_on_target" in d.kind.value for d in report.differences)


class TestIdempotence:
    async def test_rerunning_converges(self, source: RemoteHost, target: RemoteHost) -> None:
        # --delete makes a retry idempotent. Without it a retry merges into
        # whatever a failed attempt left behind.
        await source.run_checked(f"echo a > {DATA}/f")
        identity = await _install_key(source)
        await _copy(source, identity)

        # Leftovers from a hypothetical failed attempt.
        await target.run_checked(f"echo junk > {DATA}/stale")
        await _copy(source, identity)

        assert not (await target.run(f"test -e {DATA}/stale")).ok
        report = await verify.verify_volume(
            source, target, source_path=DATA, target_path=DATA
        )
        assert report.ok


class TestChunkedTransfer:
    async def test_files_from_transfers_only_the_chunk(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        await source.run_checked(f"mkdir -p {DATA}/a {DATA}/b")
        await source.run_checked(f"echo 1 > {DATA}/a/f && echo 2 > {DATA}/b/f")

        identity = await _install_key(source)
        spec = rsync.RsyncSpec(
            source_path=DATA,
            target_path=DATA,
            target_host="host.docker.internal",
            target_port=TARGET_PORT,
            identity_file=identity,
            paths=("a",),
        )
        await rsync.run(source, spec)

        assert (await target.run(f"test -e {DATA}/a/f")).ok
        assert not (await target.run(f"test -e {DATA}/b/f")).ok


class TestVerifyIdentical:
    async def test_checksum_dry_run_is_silent_when_identical(
        self, source: RemoteHost, target: RemoteHost
    ) -> None:
        await source.run_checked(f"echo x > {DATA}/f")
        identity = await _install_key(source)
        spec = rsync.RsyncSpec(
            source_path=DATA,
            target_path=DATA,
            target_host="host.docker.internal",
            target_port=TARGET_PORT,
            identity_file=identity,
        )
        await rsync.run(source, spec)

        differences = await rsync.verify_identical(source, spec)
        assert differences == [], differences


class TestPathSize:
    async def test_reports_real_sizes(self, source: RemoteHost) -> None:
        from bg_coolify_migrate.discovery.docker import path_size

        await source.run_checked(f"mkdir -p {DATA}/d")
        await source.run_checked(f"dd if=/dev/zero of={DATA}/d/big bs=1024 count=100 2>/dev/null")

        size, count = await path_size(source, f"{DATA}/d")
        assert size >= 100 * 1024
        assert count == 1


class TestHostKeyPolicy:
    async def test_unknown_host_key_is_refused_by_default(self, tmp_path: Path) -> None:
        """We never disable host key checking.

        Both predecessor tools use StrictHostKeyChecking=no, which accepts
        anything forever and is MITM-able.
        """
        from bg_coolify_migrate.transfer.ssh import HostKeyUnknown

        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text("", encoding="utf-8")

        with pytest.raises(HostKeyUnknown):
            async with RemoteHost.connect(
                _target(SOURCE_PORT), known_hosts=known_hosts, trust_new_host_key=False
            ):
                pass

    async def test_trusting_records_the_key(self, tmp_path: Path) -> None:
        known_hosts = tmp_path / "known_hosts"
        async with RemoteHost.connect(
            _target(SOURCE_PORT), known_hosts=known_hosts, trust_new_host_key=True
        ):
            pass
        assert known_hosts.exists()
        assert "ssh-" in known_hosts.read_text(encoding="utf-8")
