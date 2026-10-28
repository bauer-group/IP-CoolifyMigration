"""The release registry: one table of what differs between Coolify releases.

Pinned here: the gate arithmetic, the two OPPOSITE unknown-version policies
(rules apply, fields are sent only if every release takes them), that the field
views api/fields.py exports are exactly the registry's union, and that the plan
carries the one version CREATE builds its bodies for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_coolify_migrate.api import fields
from bg_coolify_migrate.domain import coolify_versions as cv
from bg_coolify_migrate.domain.coolify_versions import Field, Gate
from bg_coolify_migrate.domain.plan import MigrationPlan, ServerRef
from bg_coolify_migrate.engine.context import MigrationContext
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings


class TestGate:
    def test_since_is_inclusive_and_until_exclusive(self) -> None:
        gate = Gate(since=(4, 3, 0), until=(4, 3, 22))
        assert not gate.holds((4, 2, 9))
        assert gate.holds((4, 3, 0))
        assert gate.holds((4, 3, 21))
        assert not gate.holds((4, 3, 22))

    def test_compares_numerically(self) -> None:
        """4.3.10 is NEWER than 4.3.3 — a string compare says otherwise."""
        assert Gate(since=(4, 3, 3)).holds((4, 3, 10))

    def test_an_unknown_version_is_inside_a_bare_gate(self) -> None:
        assert Gate(since=(9, 9, 9)).holds(None)

    def test_throughout_the_validated_range(self) -> None:
        assert cv.ALWAYS.holds_throughout()
        assert not Gate(since=(4, 2, 0)).holds_throughout()
        assert not Gate(until=(4, 3, 22)).holds_throughout()
        assert Gate(since=cv.VALIDATED_MIN, until=(9,)).holds_throughout()


class TestSupports:
    def test_follows_the_gate_for_a_known_version(self) -> None:
        assert not cv.supports("managed_dns", (4, 3, 24))
        assert cv.supports("managed_dns", (4, 4, 0))

    def test_an_unknown_version_applies_the_rule(self) -> None:
        """Failing toward checking: "we could not tell" is not "it is fine"."""
        assert cv.supports("managed_dns", None)

    def test_a_caller_can_demand_a_known_version(self) -> None:
        assert not cv.supports("managed_dns", None, if_unknown=False)
        assert cv.supports("managed_dns", (4, 5, 0), if_unknown=False)

    def test_a_typo_is_an_error_not_every_release(self) -> None:
        with pytest.raises(KeyError):
            cv.supports("managed_dsn", (4, 5, 0))

    def test_every_feature_names_where_it_starts_or_ends(self) -> None:
        """A row without a bound gates nothing — it would only hide a decision."""
        for name, gate in cv.FEATURES.items():
            assert gate != cv.ALWAYS, name


class TestFieldsFor:
    def test_a_known_version_gets_what_it_accepts(self) -> None:
        assert "host_path" in cv.fields_for("storages", (4, 3, 21))
        assert "host_path" not in cv.fields_for("storages", (4, 3, 22))
        assert "is_host_file" not in cv.fields_for("storages", (4, 1, 2))
        assert "is_host_file" in cv.fields_for("storages", (4, 2, 0))

    def test_an_unknown_version_gets_only_what_every_release_takes(self) -> None:
        """A field that 422s takes the whole resource with it."""
        assert "host_path" not in cv.fields_for("storages", None)
        assert "name" in cv.fields_for("storages", None)

    def test_a_column_gated_field_is_sent_on_an_unknown_release(self) -> None:
        """Its key exists in a GET only where the create takes it."""
        field = Field("x", Gate(since=(4, 5, 0)), column_gated=True)
        assert field.sent_on(None)
        assert not field.sent_on((4, 4, 7))
        assert not Field("y", Gate(since=(4, 5, 0))).sent_on(None)

    def test_an_unknown_endpoint_raises(self) -> None:
        with pytest.raises(KeyError):
            cv.fields_for("databases/cockroachdb", (4, 5, 0))

    def test_every_route_and_engine_has_an_endpoint(self) -> None:
        for route in cv.APPLICATION_ROUTES:
            assert cv.known_fields(f"applications/{route}")
        for engine in cv.DATABASE_ENGINES:
            assert {"image", "server_uuid"} <= cv.known_fields(f"databases/{engine}")

    def test_carries(self) -> None:
        assert cv.carries("storages", "host_path", (4, 3, 0))
        assert not cv.carries("storages", "host_path", (4, 5, 0))

    def test_the_exported_whitelists_are_the_registry_union(self) -> None:
        assert cv.known_fields("storages") == fields.STORAGE_CREATE
        assert cv.known_fields("backups") == fields.BACKUP_CREATE
        assert cv.known_fields("applications/dockerimage") <= fields.APPLICATION_CREATE
        assert fields.database_allowed("redis") == cv.known_fields("databases/redis")
        assert fields.database_allowed("redis", (4, 5, 0)) == cv.fields_for(
            "databases/redis", (4, 5, 0)
        )

    def test_rows_are_unique_per_endpoint(self) -> None:
        for endpoint in (*cv.CREATE_FIELDS, *cv.UPDATE_FIELDS):
            names = [f.name for f in cv.endpoint_fields(endpoint)]
            assert len(names) == len(set(names)), endpoint


class TestFilterFor:
    def test_drops_what_the_release_refuses_and_none(self) -> None:
        body = fields.filter_for(
            {"type": "persistent", "mount_path": "/d", "host_path": "/srv", "name": None, "x": 1},
            "storages",
            (4, 5, 0),
        )
        assert body == {"type": "persistent", "mount_path": "/d"}


class TestValidatedRange:
    def test_the_bounds_are_ordered_releases(self) -> None:
        assert cv.VALIDATED_MIN < cv.VALIDATED_MAX
        assert cv.format_version(cv.VALIDATED_MIN) == "4.1.2"


def _plan(**kw: object) -> MigrationPlan:
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=ServerRef(uuid="s1", name="old", ip="10.0.0.1"),
        target_server=ServerRef(uuid="s2", name="new", ip="10.0.0.2"),
        **kw,  # type: ignore[arg-type]
    )


class TestThePlanCarriesTheVersion:
    def test_parsed_from_what_get_version_said(self) -> None:
        assert _plan(coolify_version="v4.5.0").instance_version == (4, 5, 0)
        assert _plan().instance_version is None
        assert _plan(coolify_version="nightly").instance_version is None

    def test_a_plan_saved_before_the_field_still_loads(self) -> None:
        saved = _plan().model_dump_json(exclude={"coolify_version"})
        assert MigrationPlan.model_validate_json(saved).coolify_version is None

    def _ctx(self, tmp_path: Path, plan: MigrationPlan) -> MigrationContext:
        return MigrationContext(
            api=None,  # type: ignore[arg-type]
            settings=Settings(_env_file=None, state_dir=tmp_path),
            plan=plan,
            journal=Journal.create(tmp_path, "m1"),
            migration_id="m1",
            source_host=None,  # type: ignore[arg-type]
            target_host=None,  # type: ignore[arg-type]
        )

    def test_the_context_reads_the_plan_first(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, _plan(coolify_version="4.5.0"))
        ctx.journal.append("step_completed", state="preflight", detail={"coolify_version": "4.4.0"})
        assert ctx.coolify_version == (4, 5, 0)

    def test_an_old_plan_falls_back_to_what_preflight_journaled(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, _plan())
        assert ctx.coolify_version is None
        ctx.journal.append("step_completed", state="preflight", detail={"coolify_version": "4.4.0"})
        assert ctx.coolify_version == (4, 4, 0)
