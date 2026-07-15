"""Tests for the planner, runner and key lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.engine import keys
from bg_coolify_migrate.engine.planner import _engine_of, decode_compose, server_ref, stack_labels
from bg_coolify_migrate.engine.runner import load_plan, make_migration_id, plan_path, save_plan
from bg_coolify_migrate.errors import MigrationError
from tests.conftest import FakeHost

COMPOSE = "services:\n  web:\n    image: nginx\n"


class TestDecodeCompose:
    def test_plain_yaml_passes_through(self) -> None:
        assert decode_compose(COMPOSE) == COMPOSE

    def test_base64_is_decoded(self) -> None:
        import base64

        encoded = base64.b64encode(COMPOSE.encode()).decode()
        assert decode_compose(encoded) == COMPOSE

    def test_version_prefixed_yaml_is_not_mistaken_for_base64(self) -> None:
        raw = "version: '3'\nservices:\n  a:\n    image: x\n"
        assert decode_compose(raw) == raw

    def test_comment_prefixed_yaml(self) -> None:
        raw = "# my stack\nservices:\n  a:\n    image: x\n"
        assert decode_compose(raw) == raw

    def test_garbage_is_returned_unchanged_rather_than_swallowed(self) -> None:
        # A compose we fail to decode is a compose whose volumes we cannot
        # enumerate. Returning it lets the parser raise a useful error.
        assert decode_compose("!!!not base64 or yaml!!!") == "!!!not base64 or yaml!!!"

    def test_none_and_empty(self) -> None:
        assert decode_compose(None) is None
        assert decode_compose("") is None


class TestServerRef:
    def test_maps_the_api_shape(self) -> None:
        ref = server_ref({"uuid": "s1", "name": "prod", "ip": "10.0.0.1", "user": "deploy", "port": 2222})
        assert ref.uuid == "s1"
        assert ref.user == "deploy"
        assert ref.port == 2222

    def test_defaults(self) -> None:
        ref = server_ref({"uuid": "s1", "ip": "10.0.0.1"})
        assert ref.user == "root"
        assert ref.port == 22

    def test_null_port_falls_back(self) -> None:
        assert server_ref({"uuid": "s1", "ip": "1.1.1.1", "port": None}).port == 22


class TestEngineOf:
    @pytest.mark.parametrize(
        ("type_value", "expected"),
        [
            ("standalone-postgresql", DatabaseEngine.POSTGRESQL),
            ("postgresql", DatabaseEngine.POSTGRESQL),
            ("standalone-mysql", DatabaseEngine.MYSQL),
            ("standalone-clickhouse", DatabaseEngine.CLICKHOUSE),
        ],
    )
    def test_from_type(self, type_value: str, expected: DatabaseEngine) -> None:
        assert _engine_of("databases", {"type": type_value}) is expected

    def test_falls_back_to_the_image(self) -> None:
        assert _engine_of("databases", {"image": "postgres:16"}) is DatabaseEngine.POSTGRESQL

    def test_non_database_is_none(self) -> None:
        assert _engine_of("applications", {"type": "postgresql"}) is None

    def test_unknown_is_none(self) -> None:
        assert _engine_of("databases", {"type": "cockroach"}) is None


class TestStackLabels:
    def test_slugifies_both(self) -> None:
        # Coolify slugifies when it writes the labels, so we must to read them.
        plan = MigrationPlan(
            project="My Shop",
            environment="Production Env",
            source_server=ServerRef(uuid="s1", name="a", ip="1.1.1.1"),
            target_server=ServerRef(uuid="s2", name="b", ip="2.2.2.2"),
        )
        assert stack_labels(plan) == {
            "coolify.projectName": "my-shop",
            "coolify.environmentName": "production-env",
        }


class TestMigrationId:
    def test_is_stable_for_a_fixed_time(self) -> None:
        when = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
        assert make_migration_id("shop", "production", when) == make_migration_id(
            "shop", "production", when
        )

    def test_differs_per_project(self) -> None:
        when = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
        assert make_migration_id("a", "production", when) != make_migration_id(
            "b", "production", when
        )

    def test_survives_awkward_names(self) -> None:
        # A hash, so a project called "a/b c" cannot produce an unusable filename.
        mid = make_migration_id("a/b c", "prod/env")
        assert "/" not in mid
        assert " " not in mid

    def test_contains_a_readable_timestamp(self) -> None:
        when = datetime(2026, 7, 15, 12, 30, 45, tzinfo=UTC)
        assert make_migration_id("shop", "production", when).startswith("20260715-123045")


class TestPlanPersistence:
    def _plan(self) -> MigrationPlan:
        return MigrationPlan(
            project="shop",
            environment="production",
            source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
            target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
            resources=(
                ResourcePlan(
                    snapshot=ResourceSnapshot(
                        uuid="db1",
                        name="postgres",
                        collection="databases",
                        kind=ResourceKind.DATABASE,
                        engine=DatabaseEngine.POSTGRESQL,
                    ),
                    strategy=Strategy.COPY_DATA,
                ),
            ),
        )

    def test_roundtrip(self, tmp_path: Path) -> None:
        save_plan(tmp_path, "mig-001", self._plan())
        loaded = load_plan(tmp_path, "mig-001")
        assert loaded.project == "shop"
        assert loaded.resources[0].snapshot.engine is DatabaseEngine.POSTGRESQL

    def test_missing_plan_refuses_rather_than_replanning(self, tmp_path: Path) -> None:
        # Re-planning after the fact could disagree with what was actually
        # created, and then the rollback deletes the wrong things.
        with pytest.raises(MigrationError, match="no saved plan"):
            load_plan(tmp_path, "nope")

    def test_path_is_next_to_the_journal(self, tmp_path: Path) -> None:
        assert plan_path(tmp_path, "m1").name == "m1.plan.json"

    def test_saved_plan_contains_no_secrets(self, tmp_path: Path) -> None:
        # A plan carries uuids, names, paths and sizes - never credentials.
        path = save_plan(tmp_path, "mig-001", self._plan())
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("password", "app_key", "private_key", "token"):
            assert forbidden not in text


class TestEphemeralKeys:
    def test_generate_produces_a_usable_pair(self) -> None:
        private, public, fingerprint, comment = keys.generate("mig-001")
        assert "PRIVATE KEY" in private
        assert public.startswith("ssh-ed25519 ")
        assert fingerprint.startswith("SHA256:")
        assert comment == "bg-coolify-migrate-mig-001"

    def test_comment_identifies_our_line(self) -> None:
        # Revocation matches on this, so we can never delete someone else's key.
        _, public, _, comment = keys.generate("mig-xyz")
        assert comment in public
        assert "mig-xyz" in comment

    def test_each_migration_gets_a_distinct_key(self) -> None:
        a = keys.generate("m1")
        b = keys.generate("m1")
        assert a[0] != b[0]

    async def test_install_authorises_on_the_target_first(self) -> None:
        # The public key goes on the target BEFORE the private key on the source:
        # a half-install then leaves an unusable-but-revocable credential, not a
        # private key with nothing to authorise it.
        source = FakeHost()
        target = FakeHost()
        target.on(r"mkdir -p ~/\.ssh", exit_status=0)
        target.on(r"authorized_keys", exit_status=0)
        source.on(r"mkdir -p /root/\.coolify-migrate", exit_status=0)

        key = await keys.install(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]
        assert "authorized_keys" in target.commands[1]
        assert key.remote_path.startswith("/root/.coolify-migrate/m1")

    async def test_private_key_never_lands_in_tmp(self) -> None:
        # /tmp is world-traversable and often tmpfs.
        source = FakeHost()
        target = FakeHost()
        target.on(r"mkdir -p ~/\.ssh", exit_status=0)
        target.on(r"authorized_keys", exit_status=0)
        source.on(r"mkdir -p", exit_status=0)

        key = await keys.install(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]
        assert not key.remote_path.startswith("/tmp")
        assert "umask 077" in source.commands[0]

    async def test_failed_source_install_revokes_the_target_key(self) -> None:
        source = FakeHost()
        target = FakeHost()
        target.on(r"mkdir -p ~/\.ssh", exit_status=0)
        target.on(r"authorized_keys.*chmod", exit_status=0)
        source.on(r"mkdir -p", exit_status=1, stderr="read-only filesystem")
        target.on(r"sed -i", exit_status=0)

        from bg_coolify_migrate.errors import TransferError

        with pytest.raises(TransferError, match="could not place the transfer key"):
            await keys.install(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]
        # We must not leave an authorised key behind.
        assert any("sed -i" in c for c in target.commands)

    async def test_revoke_matches_only_our_comment(self) -> None:
        target = FakeHost()
        target.on(r"sed -i", exit_status=0)
        source = FakeHost()
        source.on(r"rm -rf", exit_status=0)

        await keys.revoke(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]
        sed = next(c for c in target.commands if "sed -i" in c)
        assert "bg-coolify-migrate-m1" in sed

    async def test_revoke_tries_both_halves_independently(self) -> None:
        # Failing to delete the private key must not prevent revoking the
        # authorisation, which is the half that grants access.
        target = FakeHost()
        target.on(r"sed -i", exit_status=1)
        source = FakeHost()
        source.on(r"rm -rf", exit_status=0)

        await keys.revoke(source=source, target=target, migration_id="m1")  # type: ignore[arg-type]
        assert any("rm -rf" in c for c in source.commands)

    async def test_revoke_tolerates_a_missing_host(self) -> None:
        await keys.revoke(source=None, target=None, migration_id="m1")
