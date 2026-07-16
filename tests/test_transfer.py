"""Tests for the pure parts of the transfer layer.

The rsync flag tests are executable documentation of why each flag is there —
every one of them is a bug in coolify-mover, which uses `-avz --progress`.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.transfer.partition import (
    Chunk,
    PathEntry,
    plan_transfer,
    suggest_parallelism,
    whole_tree,
)
from bg_coolify_migrate.transfer.rsync import (
    BASE_FLAGS,
    RsyncSpec,
    build_command,
    build_ssh_option,
    parse_progress,
)
from bg_coolify_migrate.transfer.verify import DiffKind, Manifest, compare


def _spec(**kw: object) -> RsyncSpec:
    base = {
        "source_path": "/var/lib/docker/volumes/old/_data",
        "target_path": "/var/lib/docker/volumes/new/_data",
        "target_host": "10.0.0.2",
    }
    return RsyncSpec(**{**base, **kw})  # type: ignore[arg-type]


class TestRsyncFlags:
    def test_numeric_ids_is_always_present(self) -> None:
        # THE critical flag. Docker volume files are owned by container UIDs
        # (postgres/mysql/redis=999, clickhouse=101). Without this, rsync maps
        # ownership BY NAME through the remote passwd db and the DB won't start.
        assert "--numeric-ids" in BASE_FLAGS
        assert "--numeric-ids" in build_command(_spec())

    def test_hardlinks_preserved(self) -> None:
        assert "-H" in BASE_FLAGS

    def test_acls_and_xattrs_preserved(self) -> None:
        assert "-A" in BASE_FLAGS
        assert "-X" in BASE_FLAGS

    def test_sparse_preserved(self) -> None:
        assert "-S" in BASE_FLAGS

    def test_delete_makes_reruns_idempotent(self) -> None:
        assert "--delete" in BASE_FLAGS

    def test_partial_enables_resume(self) -> None:
        assert "--partial" in BASE_FLAGS

    def test_no_chown_anywhere(self) -> None:
        # Coolify's own VolumeCloneJob does `chown -R 1000:1000 /target`, which
        # is exactly why its volume cloning got disabled. We must never.
        assert "chown" not in build_command(_spec())

    def test_compression_is_off_by_default(self) -> None:
        # Volume data is usually already compressed and server links are fast;
        # -z would burn CPU for nothing.
        assert "-z" not in build_command(_spec()).split()

    def test_compression_can_be_enabled_for_wan(self) -> None:
        assert " -z " in f" {build_command(_spec(compress=True))} "


class TestRsyncCommand:
    def test_source_gets_a_trailing_slash(self) -> None:
        # Load-bearing: `rsync /a /b` creates /b/a; `rsync /a/ /b` copies a's
        # CONTENTS into b. We mirror contents, never nest.
        cmd = build_command(_spec(source_path="/vol/_data"))
        assert "/vol/_data/" in cmd

    def test_target_is_user_at_host_colon_path(self) -> None:
        cmd = build_command(_spec(target_user="root", target_host="10.0.0.2"))
        assert "root@10.0.0.2:/var/lib/docker/volumes/new/_data/" in cmd

    def test_dry_run_flag(self) -> None:
        assert "--dry-run" in build_command(_spec(dry_run=True))

    def test_checksum_flag(self) -> None:
        assert "--checksum" in build_command(_spec(checksum=True))

    def test_progress_by_default(self) -> None:
        assert "--info=progress2" in build_command(_spec())

    def test_itemize_replaces_progress(self) -> None:
        cmd = build_command(_spec(itemize=True))
        assert "--itemize-changes" in cmd
        assert "--info=progress2" not in cmd

    def test_bandwidth_limit(self) -> None:
        assert "--bwlimit=1000" in build_command(_spec(bandwidth_limit_kbps=1000))

    def test_chunked_transfer_uses_files_from(self) -> None:
        cmd = build_command(_spec(paths=("base", "pg_wal")))
        assert "--files-from=-" in cmd
        assert "--relative" in cmd
        assert "base" in cmd and "pg_wal" in cmd

    def test_chunked_transfer_forces_recursion(self) -> None:
        """Regression: --files-from turns OFF the recursion that -a implies.

        Without an explicit -r, a directory named in the file list is transferred
        as a bare directory ENTRY: rsync exits 0, the tree looks right, and every
        file inside is missing. Found by the integration rig, invisible to any
        amount of command-string inspection.
        """
        cmd = build_command(_spec(paths=("base",)))
        assert " -r " in f" {cmd} " or cmd.endswith(" -r")

    def test_whole_tree_needs_no_explicit_recursion(self) -> None:
        # -a implies -r when --files-from is absent.
        assert "--files-from" not in build_command(_spec(paths=(".",)))

    def test_whole_tree_does_not_use_files_from(self) -> None:
        assert "--files-from" not in build_command(_spec(paths=(".",)))


class TestSshOption:
    def test_never_disables_host_key_checking(self) -> None:
        # Both predecessor tools use StrictHostKeyChecking=no, which accepts
        # anything forever and is MITM-able.
        opt = build_ssh_option(_spec())
        assert "StrictHostKeyChecking=no" not in opt

    def test_accept_new_without_known_hosts(self) -> None:
        # accept-new still refuses a CHANGED key, unlike =no.
        assert "StrictHostKeyChecking=accept-new" in build_ssh_option(_spec())

    def test_strict_with_known_hosts(self) -> None:
        opt = build_ssh_option(_spec(known_hosts_file="/tmp/kh"))
        assert "StrictHostKeyChecking=yes" in opt
        assert "UserKnownHostsFile=/tmp/kh" in opt

    def test_identity_file_implies_identities_only(self) -> None:
        opt = build_ssh_option(_spec(identity_file="/tmp/key"))
        assert "-i /tmp/key" in opt
        assert "IdentitiesOnly=yes" in opt

    def test_batch_mode_prevents_interactive_hangs(self) -> None:
        assert "BatchMode=yes" in build_ssh_option(_spec())

    def test_keepalive_for_long_transfers(self) -> None:
        opt = build_ssh_option(_spec())
        assert "ServerAliveInterval=30" in opt

    def test_custom_port(self) -> None:
        assert "-p 2222" in build_ssh_option(_spec(target_port=2222))


class TestParseProgress:
    def test_parses_a_progress2_line(self) -> None:
        line = "  1,234,567  45%   12.34MB/s    0:00:12 (xfr#12, to-chk=100/200)"
        p = parse_progress(line)
        assert p is not None
        assert p.bytes_done == 1234567
        assert p.percent == 45
        assert p.rate == "12.34MB/s"
        assert p.files_done == 12
        assert p.files_left == 100
        assert p.files_total == 200

    def test_parses_without_the_xfr_suffix(self) -> None:
        p = parse_progress("  1,000  10%   1.00MB/s    0:00:01")
        assert p is not None
        assert p.bytes_done == 1000
        assert p.files_done is None

    @pytest.mark.parametrize(
        "line", ["sending incremental file list", "", "total size is 123", "./"]
    )
    def test_ignores_non_progress_lines(self, line: str) -> None:
        assert parse_progress(line) is None


class TestPartition:
    def test_hardlinks_force_a_single_chunk(self) -> None:
        # -H only dedups WITHIN one rsync invocation. Splitting a hardlinked tree
        # explodes it into duplicate files: identity lost, size inflated.
        entries = [PathEntry(f"d{i}", 100) for i in range(10)]
        plan = plan_transfer(entries, max_parallel=4, has_hardlinks=True)
        assert plan.parallelism == 1
        assert plan.chunks[0].is_whole_tree
        assert "hardlink" in plan.reason

    def test_splits_across_bins(self) -> None:
        entries = [PathEntry(f"d{i}", 100) for i in range(8)]
        plan = plan_transfer(entries, max_parallel=4, has_hardlinks=False)
        assert plan.parallelism == 4
        packed = sorted(p for c in plan.chunks for p in c.paths)
        assert packed == sorted(e.relpath for e in entries)

    def test_chunks_are_disjoint_and_cover_everything(self) -> None:
        entries = [PathEntry(f"d{i}", i * 10) for i in range(1, 12)]
        plan = plan_transfer(entries, max_parallel=3, has_hardlinks=False)
        all_paths = [p for c in plan.chunks for p in c.paths]
        assert len(all_paths) == len(set(all_paths))
        assert set(all_paths) == {e.relpath for e in entries}

    def test_lpt_balances_by_size(self) -> None:
        # One huge entry plus small ones: the huge one gets its own bin.
        entries = [PathEntry("big", 1000), PathEntry("a", 10), PathEntry("b", 10)]
        plan = plan_transfer(entries, max_parallel=2, has_hardlinks=False)
        sizes = sorted(c.bytes for c in plan.chunks)
        assert sizes == [20, 1000]

    def test_single_entry_is_whole_tree(self) -> None:
        plan = plan_transfer([PathEntry("only", 100)], max_parallel=4)
        assert plan.parallelism == 1
        assert "single top-level entry" in plan.reason

    def test_empty_tree_is_whole_tree(self) -> None:
        plan = plan_transfer([], max_parallel=4)
        assert plan.chunks[0].is_whole_tree

    def test_parallelism_one_is_whole_tree(self) -> None:
        entries = [PathEntry(f"d{i}", 100) for i in range(5)]
        plan = plan_transfer(entries, max_parallel=1)
        assert plan.chunks[0].is_whole_tree
        assert "disabled" in plan.reason

    def test_never_more_chunks_than_entries(self) -> None:
        entries = [PathEntry("a", 1), PathEntry("b", 1)]
        plan = plan_transfer(entries, max_parallel=16)
        assert plan.parallelism <= 2

    def test_total_bytes_preserved(self) -> None:
        entries = [PathEntry(f"d{i}", 100) for i in range(8)]
        plan = plan_transfer(entries, max_parallel=4)
        assert plan.total_bytes == 800

    def test_whole_tree_helper(self) -> None:
        plan = whole_tree(500, "because")
        assert plan.chunks == (Chunk(paths=(".",), bytes=500),)
        assert plan.reason == "because"


class TestSuggestParallelism:
    def test_single_entry_is_serial(self) -> None:
        assert suggest_parallelism(entry_count=1, total_bytes=10**12) == 1

    def test_small_volume_is_serial(self) -> None:
        # Per-process overhead dominates below ~512 MB.
        assert suggest_parallelism(entry_count=20, total_bytes=1024) == 1

    def test_large_volume_parallelises(self) -> None:
        assert suggest_parallelism(entry_count=20, total_bytes=10 * 1024**3) > 1

    def test_bounded_by_entry_count(self) -> None:
        assert suggest_parallelism(entry_count=2, total_bytes=10 * 1024**3) <= 2

    def test_bounded_by_cap(self) -> None:
        assert suggest_parallelism(entry_count=100, total_bytes=10 * 1024**3, cap=2) <= 2


class TestVerifyCompare:
    def test_identical_manifests_have_no_differences(self) -> None:
        m = Manifest(content={"./a": "hash1"}, metadata={"./a": "f|644|999|999|"})
        assert compare(m, m) == ()

    def test_content_difference_detected(self) -> None:
        a = Manifest(content={"./a": "hash1"}, metadata={"./a": "f|644|999|999|"})
        b = Manifest(content={"./a": "hash2"}, metadata={"./a": "f|644|999|999|"})
        (diff,) = compare(a, b)
        assert diff.kind is DiffKind.CONTENT_DIFFERS
        assert diff.path == "./a"

    def test_wrong_ownership_detected_even_with_identical_content(self) -> None:
        # THE case a content-only check misses. Coolify's chown -R 1000:1000
        # leaves bytes identical and the database unable to start.
        a = Manifest(content={"./pg": "same"}, metadata={"./pg": "f|644|999|999|"})
        b = Manifest(content={"./pg": "same"}, metadata={"./pg": "f|644|1000|1000|"})
        (diff,) = compare(a, b)
        assert diff.kind is DiffKind.METADATA_DIFFERS
        assert "999" in str(diff.source_value)
        assert "1000" in str(diff.target_value)

    def test_mode_difference_detected(self) -> None:
        a = Manifest(metadata={"./k": "f|600|0|0|"})
        b = Manifest(metadata={"./k": "f|644|0|0|"})
        (diff,) = compare(a, b)
        assert diff.kind is DiffKind.METADATA_DIFFERS

    def test_missing_file_detected(self) -> None:
        a = Manifest(content={"./a": "h"}, metadata={"./a": "f|644|0|0|"})
        b = Manifest()
        (diff,) = compare(a, b)
        assert diff.kind is DiffKind.MISSING_ON_TARGET

    def test_extra_file_detected(self) -> None:
        a = Manifest()
        b = Manifest(content={"./x": "h"}, metadata={"./x": "f|644|0|0|"})
        (diff,) = compare(a, b)
        assert diff.kind is DiffKind.EXTRA_ON_TARGET

    def test_symlink_target_difference_detected(self) -> None:
        a = Manifest(metadata={"./link": "l|777|0|0|/real/target"})
        b = Manifest(metadata={"./link": "l|777|0|0|/other/target"})
        (diff,) = compare(a, b)
        assert diff.kind is DiffKind.METADATA_DIFFERS

    def test_socket_is_compared_by_metadata_only(self) -> None:
        # sha256sum cannot read a socket; the metadata pass still covers it.
        m = Manifest(content={}, metadata={"./.s.PGSQL.5432": "s|755|999|999|"})
        assert compare(m, m) == ()

    def test_all_differences_are_reported_not_just_the_first(self) -> None:
        a = Manifest(
            content={"./a": "h1", "./b": "h2"},
            metadata={"./a": "f|644|0|0|", "./b": "f|644|0|0|"},
        )
        b = Manifest(
            content={"./a": "X", "./b": "Y"},
            metadata={"./a": "f|600|0|0|", "./b": "f|644|1|1|"},
        )
        diffs = compare(a, b)
        assert len(diffs) == 4  # 2 metadata + 2 content

    def test_differences_describe_themselves(self) -> None:
        a = Manifest(content={"./a": "h1"}, metadata={"./a": "f|644|0|0|"})
        b = Manifest()
        (diff,) = compare(a, b)
        assert "missing on target" in diff.describe()


class TestHostKeyRecording:
    """Regression for the broken TOFU: a recorded host key must actually match a
    later connection, and unseen keys must be prompted/refused, not accepted blindly."""

    @staticmethod
    def _public_key():  # type: ignore[no-untyped-def]
        import asyncssh

        return asyncssh.generate_private_key("ssh-ed25519").convert_to_public()

    def test_initial_known_hosts_never_disables_verification(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from bg_coolify_migrate.transfer.ssh import _initial_known_hosts

        # Never None (which would disable host-key checking). No file -> empty, so an
        # unknown key is unverifiable and gets scanned+recorded rather than trusted.
        assert _initial_known_hosts(None) == ()
        assert _initial_known_hosts(tmp_path / "nope") == ()
        existing = tmp_path / "known_hosts"
        existing.write_text("x\n", encoding="utf-8")
        assert _initial_known_hosts(existing) == str(existing)

    async def test_trusting_without_a_known_hosts_path_is_rejected(self) -> None:
        # Accepting a key with nowhere to persist it would trust a new key on every
        # connect. Requiring a path is the fix flagged by the security review.
        from bg_coolify_migrate.transfer.ssh import RemoteHost, SshTarget

        with pytest.raises(ValueError, match="requires a known_hosts path"):
            async with RemoteHost.connect(SshTarget(host="h"), trust_new_host_key=True):
                pass

    def test_recorded_key_matches_the_way_a_connection_looks_it_up(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import asyncssh

        from bg_coolify_migrate.transfer.ssh import SshTarget, _append_known_host

        key = self._public_key()

        # Port 22: asyncssh's connection passes port=None (connection.py:
        # `port = self._port if self._port != DEFAULT_PORT else None`), so it looks
        # up the BARE hostname. A `[host]:22` entry would never be re-verified.
        kh22 = tmp_path / "kh22"
        _append_known_host(kh22, SshTarget(host="host.example.com", port=22), key)
        parsed22 = asyncssh.import_known_hosts(kh22.read_text(encoding="utf-8"))
        assert parsed22.match("host.example.com", "1.2.3.4", None)[0], (
            "a port-22 key must match a bare-hostname lookup, or TOFU never re-verifies"
        )

        # A non-default port: the [host]:port form, looked up with the port.
        kh = tmp_path / "kh2222"
        _append_known_host(kh, SshTarget(host="host.example.com", port=2222), key)
        parsed = asyncssh.import_known_hosts(kh.read_text(encoding="utf-8"))
        assert parsed.match("host.example.com", "1.2.3.4", 2222)[0]

    async def test_trust_flag_records_without_prompting(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from bg_coolify_migrate.transfer.ssh import RemoteHost, SshTarget

        key = self._public_key()

        async def _scan(target: object) -> object:
            return key

        monkeypatch.setattr(RemoteHost, "_scan_host_key", _scan)
        known_hosts = tmp_path / "known_hosts"
        await RemoteHost.ensure_host_key(
            SshTarget(host="h", port=22), known_hosts=known_hosts, trust_new_host_key=True
        )
        assert known_hosts.exists() and "h" in known_hosts.read_text(encoding="utf-8")

    async def test_prompt_yes_shows_fingerprint_and_records(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from bg_coolify_migrate.transfer.ssh import RemoteHost, SshTarget

        key = self._public_key()

        async def _scan(target: object) -> object:
            return key

        monkeypatch.setattr(RemoteHost, "_scan_host_key", _scan)
        seen: dict[str, object] = {}

        async def _prompt(target: object, fingerprint: str) -> bool:
            seen["fingerprint"] = fingerprint
            return True

        known_hosts = tmp_path / "known_hosts"
        await RemoteHost.ensure_host_key(
            SshTarget(host="h"), known_hosts=known_hosts, host_key_prompt=_prompt
        )
        assert seen["fingerprint"]
        assert known_hosts.exists()

    async def test_prompt_no_refuses_and_records_nothing(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from bg_coolify_migrate.transfer.ssh import HostKeyUnknown, RemoteHost, SshTarget

        key = self._public_key()

        async def _scan(target: object) -> object:
            return key

        monkeypatch.setattr(RemoteHost, "_scan_host_key", _scan)

        async def _prompt(target: object, fingerprint: str) -> bool:
            return False

        known_hosts = tmp_path / "known_hosts"
        with pytest.raises(HostKeyUnknown):
            await RemoteHost.ensure_host_key(
                SshTarget(host="h"), known_hosts=known_hosts, host_key_prompt=_prompt
            )
        assert not known_hosts.exists()

    async def test_already_known_key_never_prompts(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from bg_coolify_migrate.transfer.ssh import RemoteHost, SshTarget, _append_known_host

        key = self._public_key()
        target = SshTarget(host="h", port=22)
        known_hosts = tmp_path / "known_hosts"
        _append_known_host(known_hosts, target, key)

        async def _scan(t: object) -> object:
            return key

        monkeypatch.setattr(RemoteHost, "_scan_host_key", _scan)
        prompted = {"count": 0}

        async def _prompt(t: object, fingerprint: str) -> bool:
            prompted["count"] += 1
            return True

        await RemoteHost.ensure_host_key(target, known_hosts=known_hosts, host_key_prompt=_prompt)
        assert prompted["count"] == 0


class TestRsyncEnsureInstalled:
    """rsync auto-installs when missing, so an operator never has to prepare the
    servers by hand — and fails loudly only if no package manager can install it."""

    async def test_noop_when_already_present(self) -> None:
        from bg_coolify_migrate.transfer import rsync
        from tests.conftest import FakeHost

        host = FakeHost()
        host.on(r"command -v rsync", exit_status=0)
        await rsync.ensure_installed(host, label="source")  # no raise
        assert not any("install" in c for c in host.commands)

    async def test_installs_with_apt_when_missing(self) -> None:
        from bg_coolify_migrate.transfer import rsync
        from tests.conftest import FakeHost

        host = FakeHost()
        # Missing on the first check, present after the install.
        host.on_sequence(r"command -v rsync", [{"exit_status": 1}, {"exit_status": 0}])
        host.on(r"command -v apt-get", exit_status=0)
        host.on(r"apt-get.*install.*rsync", exit_status=0)
        await rsync.ensure_installed(host, label="target")
        assert any("apt-get" in c and "rsync" in c for c in host.commands)

    async def test_raises_when_no_package_manager(self) -> None:
        from bg_coolify_migrate.errors import TransferError
        from bg_coolify_migrate.transfer import rsync
        from tests.conftest import FakeHost

        host = FakeHost()
        host.on(r"command -v rsync", exit_status=1)  # always missing
        host.on(r"command -v \S+", exit_status=1)  # no package manager present
        with pytest.raises(TransferError, match="could not be installed"):
            await rsync.ensure_installed(host, label="source")
