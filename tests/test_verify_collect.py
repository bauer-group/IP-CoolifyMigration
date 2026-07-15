"""Tests for manifest collection over SSH.

The GNU-vs-busybox probe matters more than it looks: `find -printf` does not
exist on busybox, and getting it wrong would not raise — it would silently
produce an EMPTY manifest, i.e. a verification that passes because it compared
nothing against nothing.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.errors import VerificationError
from bg_coolify_migrate.transfer.verify import (
    build_manifest,
    collect_content,
    collect_metadata,
    verify_volume,
)
from tests.conftest import FakeHost

GNU_FIND = "find (GNU findutils) 4.9.0"
BUSYBOX_FIND = "BusyBox v1.36.1 (2023-01-01) multi-call binary."

SHA_OUTPUT = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  ./base/1\n"
    "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03  ./base/2\n"
)
META_OUTPUT = "./base|d|755|999|999|\n./base/1|f|644|999|999|\n./link|l|777|999|999|/base/1\n"


class TestCollectContent:
    async def test_parses_sha256sum_output(self, fake_host: FakeHost) -> None:
        fake_host.on(r"sha256sum", stdout=SHA_OUTPUT)
        content = await collect_content(fake_host, "/vol")  # type: ignore[arg-type]
        assert content["./base/1"].startswith("e3b0c442")
        assert len(content) == 2

    async def test_hashing_is_parallelised(self, fake_host: FakeHost) -> None:
        # The user asked for multithreading; hashing is the genuinely CPU-bound part.
        fake_host.on(r"sha256sum", stdout="")
        await collect_content(fake_host, "/vol", parallel=8)  # type: ignore[arg-type]
        assert "-P 8" in fake_host.commands[0]

    async def test_only_regular_files_are_hashed(self, fake_host: FakeHost) -> None:
        # sha256sum cannot read sockets/FIFOs/devices; a Postgres data dir has one.
        fake_host.on(r"sha256sum", stdout="")
        await collect_content(fake_host, "/vol")  # type: ignore[arg-type]
        assert "-type f" in fake_host.commands[0]

    async def test_failure_raises_rather_than_reporting_an_empty_manifest(
        self, fake_host: FakeHost
    ) -> None:
        # An empty manifest would mean "verified" — the worst possible lie.
        fake_host.on(r"sha256sum", exit_status=1, stderr="permission denied")
        with pytest.raises(VerificationError, match="could not hash"):
            await collect_content(fake_host, "/vol")  # type: ignore[arg-type]

    async def test_empty_tree_yields_empty_content(self, fake_host: FakeHost) -> None:
        fake_host.on(r"sha256sum", stdout="")
        assert await collect_content(fake_host, "/vol") == {}  # type: ignore[arg-type]


class TestCollectMetadata:
    async def test_uses_gnu_printf_when_available(self, fake_host: FakeHost) -> None:
        fake_host.on(r"find --version", stdout=GNU_FIND)
        fake_host.on(r"find \. -printf", stdout=META_OUTPUT)
        metadata = await collect_metadata(fake_host, "/vol")  # type: ignore[arg-type]
        assert metadata["./base/1"] == "f|644|999|999|"
        assert "-printf" in fake_host.commands[1]

    async def test_falls_back_to_posix_loop_on_busybox(self, fake_host: FakeHost) -> None:
        # `find -printf` does NOT exist on busybox. Assuming it would produce an
        # empty manifest, i.e. a verification that checked nothing.
        fake_host.on(r"find --version", stdout=BUSYBOX_FIND)
        fake_host.on(r"while IFS= read", stdout=META_OUTPUT)
        metadata = await collect_metadata(fake_host, "/vol")  # type: ignore[arg-type]
        assert metadata["./base/1"] == "f|644|999|999|"
        assert "-printf" not in fake_host.commands[1]

    async def test_probe_failure_falls_back_safely(self, fake_host: FakeHost) -> None:
        fake_host.on(r"find --version", exit_status=127)
        fake_host.on(r"while IFS= read", stdout=META_OUTPUT)
        await collect_metadata(fake_host, "/vol")  # type: ignore[arg-type]
        assert "while IFS= read" in fake_host.commands[1]

    async def test_captures_symlink_targets(self, fake_host: FakeHost) -> None:
        fake_host.on(r"find --version", stdout=GNU_FIND)
        fake_host.on(r"find \. -printf", stdout=META_OUTPUT)
        metadata = await collect_metadata(fake_host, "/vol")  # type: ignore[arg-type]
        assert metadata["./link"].endswith("/base/1")

    async def test_captures_ownership(self, fake_host: FakeHost) -> None:
        # The half a content hash cannot see — and the half that decides whether
        # Postgres starts.
        fake_host.on(r"find --version", stdout=GNU_FIND)
        fake_host.on(r"find \. -printf", stdout=META_OUTPUT)
        metadata = await collect_metadata(fake_host, "/vol")  # type: ignore[arg-type]
        assert "999|999" in metadata["./base/1"]

    async def test_failure_raises(self, fake_host: FakeHost) -> None:
        fake_host.on(r"find --version", stdout=GNU_FIND)
        fake_host.on(r"find \. -printf", exit_status=1, stderr="boom")
        with pytest.raises(VerificationError, match="could not stat"):
            await collect_metadata(fake_host, "/vol")  # type: ignore[arg-type]


class TestBuildManifest:
    async def test_collects_both_halves(self, fake_host: FakeHost) -> None:
        fake_host.on(r"sha256sum", stdout=SHA_OUTPUT)
        fake_host.on(r"find --version", stdout=GNU_FIND)
        fake_host.on(r"find \. -printf", stdout=META_OUTPUT)
        manifest = await build_manifest(fake_host, "/vol")  # type: ignore[arg-type]
        assert manifest.file_count == 2
        assert manifest.entry_count == 3


class TestVerifyVolume:
    def _host(self, *, uid: str = "999") -> FakeHost:
        host = FakeHost()
        host.on(r"sha256sum", stdout=SHA_OUTPUT)
        host.on(r"find --version", stdout=GNU_FIND)
        host.on(
            r"find \. -printf",
            stdout=f"./base|d|755|{uid}|{uid}|\n./base/1|f|644|{uid}|{uid}|\n",
        )
        return host

    async def test_identical_volumes_verify(self) -> None:
        report = await verify_volume(
            self._host(),  # type: ignore[arg-type]
            self._host(),  # type: ignore[arg-type]
            source_path="/a",
            target_path="/b",
        )
        assert report.ok
        assert "identical" in report.summary()

    async def test_wrong_ownership_fails_verification(self) -> None:
        # Exactly what Coolify's own `chown -R 1000:1000` produces: identical
        # bytes, wrong uid, database will not start. A content-only check passes.
        report = await verify_volume(
            self._host(uid="999"),  # type: ignore[arg-type]
            self._host(uid="1000"),  # type: ignore[arg-type]
            source_path="/a",
            target_path="/b",
        )
        assert not report.ok
        assert report.differences
        assert "difference" in report.summary()

    async def test_report_carries_both_manifests_for_audit(self) -> None:
        report = await verify_volume(
            self._host(),  # type: ignore[arg-type]
            self._host(),  # type: ignore[arg-type]
            source_path="/a",
            target_path="/b",
        )
        assert report.source.file_count == 2
        assert report.target.file_count == 2
        assert report.source_path == "/a"
