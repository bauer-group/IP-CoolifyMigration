"""The pure half of the domain hand-over: ordering, restore bodies and forcing.

The bug: a rollback after QUIESCE restarted the source BEFORE giving it its
domains back. A domain PATCH only writes the database, and the restart's deploy
bakes the stored labels into the container, so the source came back without its
custom hosts. These tests lock the decisions — including make-before-break (the
source keeps its hosts until FINALIZE, so a restore before it writes nothing);
the respx tests in test_rollback_domains_engine.py lock the API call order.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from bg_coolify_migrate.domain import domains
from bg_coolify_migrate.domain.kinds import GitAuth, ResourceKind
from bg_coolify_migrate.domain.plan import ResourceSnapshot
from bg_coolify_migrate.domain.statemachine import (
    ORDER,
    Compensation,
    State,
    rollback_plan,
    rollback_plan_for,
    states_through,
)

R = Compensation.RESTORE_SOURCE_FQDN
SRC = "app.0046-20.cloud.bauer-group.com"


def _comps(completed: list[State] | tuple[State, ...]) -> list[Compensation]:
    return [s.compensation for s in rollback_plan(list(completed))]


class TestRollbackOrder:
    def test_after_quiesce_the_domains_are_back_before_the_restart(self) -> None:
        # The regression lock at plan level. Exact, not an index comparison: on the
        # old map RESTORE was missing here entirely.
        assert _comps(states_through(State.QUIESCE)) == [
            R,
            Compensation.RESTART_SOURCE,
            Compensation.DELETE_TARGET_RESOURCE,
            Compensation.RELEASE_SOURCE_PINS,
        ]

    def test_full_run_order(self) -> None:
        # Pins last: released only after the source restart AND the target delete.
        assert _comps(states_through(State.FINALIZE)) == [
            Compensation.RESTORE_SOURCE_NAME,
            Compensation.STOP_TARGET,
            Compensation.DROP_TARGET_VOLUMES,
            Compensation.REVOKE_EPHEMERAL_KEY,
            R,
            Compensation.RESTART_SOURCE,
            Compensation.DELETE_TARGET_RESOURCE,
            Compensation.RELEASE_SOURCE_PINS,
        ]

    def test_finalize_no_longer_owes_the_domain_restore(self) -> None:
        plan = rollback_plan(list(states_through(State.FINALIZE)))
        owed = [s for s in plan if s.compensation is R]
        assert [s.because_of for s in owed] == [State.CREATE_TARGET.value]

    @pytest.mark.parametrize("state", ORDER[: ORDER.index(State.DONE)])
    def test_each_compensation_runs_once(self, state: State) -> None:
        comps = _comps(states_through(state))
        assert len(comps) == len(set(comps))

    @pytest.mark.parametrize("state", ORDER[ORDER.index(State.QUIESCE) : ORDER.index(State.DONE)])
    def test_restore_precedes_restart_for_every_prefix(self, state: State) -> None:
        comps = _comps(states_through(state))
        assert comps.index(R) < comps.index(Compensation.RESTART_SOURCE)

    @pytest.mark.parametrize(
        "state", ORDER[ORDER.index(State.START_TARGET) : ORDER.index(State.DONE)]
    )
    def test_stop_drop_delete_order_survives_precedence(self, state: State) -> None:
        comps = _comps(states_through(state))
        assert (
            comps.index(Compensation.STOP_TARGET)
            < comps.index(Compensation.DROP_TARGET_VOLUMES)
            < comps.index(Compensation.DELETE_TARGET_RESOURCE)
        )


class TestPrecedence:
    """``rollback_plan_for`` on a synthetic machine, so nothing F1-specific leaks in."""

    ORDER = ("a", "b", "c")
    MAP: ClassVar[dict[str, tuple[Compensation, ...]]] = {
        "a": (Compensation.DELETE_TARGET_RESOURCE, Compensation.RESTORE_SOURCE_FQDN),
        "b": (Compensation.RESTART_SOURCE,),
        "c": (Compensation.STOP_TARGET,),
    }

    def _plan(self, completed: list[str], precedence: dict[str, str] | None) -> list[str]:
        steps = rollback_plan_for(
            completed, order=self.ORDER, compensation_map=self.MAP, precedence=precedence
        )
        return [s.compensation.value for s in steps]

    def test_moves_first_immediately_before_then(self) -> None:
        out = self._plan(["a", "b", "c"], {R.value: Compensation.RESTART_SOURCE.value})
        assert out == ["stop_target", R.value, "restart_source", "delete_target_resource"]

    def test_noop_when_already_in_order(self) -> None:
        before = self._plan(["a", "b"], None)
        after = self._plan(["a", "b"], {"delete_target_resource": R.value})
        assert after == before == ["restart_source", "delete_target_resource", R.value]

    @pytest.mark.parametrize("completed", [["a"], ["b"], ["c"], ["b", "c"]])
    def test_noop_when_either_is_absent(self, completed: list[str]) -> None:
        precedence = {R.value: Compensation.RESTART_SOURCE.value}
        assert self._plan(completed, precedence) == self._plan(completed, None)

    def test_keeps_because_of(self) -> None:
        steps = rollback_plan_for(
            ["a", "b"],
            order=self.ORDER,
            compensation_map=self.MAP,
            precedence={R.value: Compensation.RESTART_SOURCE.value},
        )
        assert steps[0].compensation is R
        assert steps[0].because_of == "a"


class TestOnlyOwnHolders:
    """The ONE own-holder rule, shared by every claim (apps, services) and restore."""

    OWN = frozenset({"appT", "svcAppT"})

    @staticmethod
    def _body(*uuids: object) -> dict[str, object]:
        return {
            "message": "Domain conflicts detected. Use force_domain_override=true to proceed.",
            "conflicts": [
                {
                    "domain": "shop.example.com",
                    "resource_name": "shop",
                    "resource_uuid": u,
                    "resource_type": "application",
                }
                for u in uuids
            ],
            "warning": "Using the same domain for multiple resources can cause routing conflicts.",
        }

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (_body("appT"), True),
            (_body("appT", "svcAppT"), True),
            (_body("appT", "someone-else"), False),
            (_body(None), False),  # the instance's own fqdn
            (_body(), False),  # empty conflicts
            ({"message": "Domain conflicts detected."}, False),  # no conflicts at all
            ({"conflicts": "appT"}, False),
            ({"conflicts": ["appT"]}, False),
            ("not a dict", False),
            (None, False),
        ],
    )
    def test_table(self, body: object, expected: bool) -> None:
        assert domains.only_own_holders(body, self.OWN) is expected

    def test_holders_are_named_for_the_operator(self) -> None:
        body = self._body("appT", None)
        assert domains.conflict_holders(body) == ["shop/appT", "shop/None"]
        assert domains.conflict_holders("junk") == []
        assert domains.conflict_holders({"conflicts": None}) == []

    def test_foreign_holders_name_type_and_uuid_and_skip_our_own(self) -> None:
        body = self._body("appT", "orphan1", None)
        assert domains.foreign_holders(body, self.OWN) == [
            "shop.example.com: application 'shop' (uuid orphan1)",
            "shop.example.com: the Coolify instance itself (its own fqdn)",
        ]
        assert domains.foreign_holders({"conflicts": ["junk"]}, self.OWN) == []
        assert domains.foreign_holders(None, self.OWN) == []


def _app(kind: ResourceKind = ResourceKind.APP_DOCKERIMAGE) -> ResourceSnapshot:
    return ResourceSnapshot(
        uuid="app1",
        name="shop",
        collection="applications",
        kind=kind,
        git_auth=GitAuth.PUBLIC if kind is ResourceKind.APP_GIT_COMPOSE else GitAuth.NONE,
    )


class TestHostClassification:
    def test_custom_hosts_skip_server_bound_and_generated(self) -> None:
        value = f"https://shop.example.com,https://x.{SRC},https://a-1-2-3-4.sslip.io,,"
        assert domains.custom_hosts(value, source_wildcard=SRC) == ["shop.example.com"]

    def test_without_a_source_wildcard_every_real_host_is_custom(self) -> None:
        assert domains.custom_hosts(f"https://x.{SRC}", source_wildcard=None) == [f"x.{SRC}"]

    def test_without_custom_keeps_server_bound_and_generated_parts(self) -> None:
        value = f"https://shop.example.com:8443/p,https://x.{SRC},http://a.1.2.3.4.sslip.io"
        assert domains.without_custom(value, source_wildcard=SRC) == (
            f"https://x.{SRC},http://a.1.2.3.4.sslip.io"
        )

    def test_without_generated(self) -> None:
        assert domains.without_generated("https://a.sslip.io,https://b.example.com") == (
            "https://b.example.com"
        )
        assert domains.without_generated(None) == ""

    def test_resource_custom_hosts_reads_compose_per_service(self) -> None:
        raw = json.dumps(
            {"web": {"domain": "https://shop.example.com"}, "cfg": {"domain": f"https://c.{SRC}"}}
        )
        out = domains.resource_custom_hosts(
            _app(ResourceKind.APP_GIT_COMPOSE), {"docker_compose_domains": raw}, source_wildcard=SRC
        )
        assert out == ["shop.example.com"]

    def test_held_hosts_reads_every_column_coolify_reads(self) -> None:
        resource = {
            "fqdn": "https://a.example.com",
            "docker_compose_domains": json.dumps({"w": {"domain": "https://b.example.com:81"}}),
            "applications": [{"url": "https://c.example.com"}, {"fqdn": "https://d.example.com"}, 1],
        }
        assert domains.held_hosts(resource) == {
            "a.example.com",
            "b.example.com",
            "c.example.com",
            "d.example.com",
        }

    def test_missing_custom_hosts_ignore_server_bound_ones(self) -> None:
        body = {"domains": f"https://shop.example.com,https://x.{SRC}"}
        assert domains.missing_custom_hosts(body, {"fqdn": ""}, source_wildcard=SRC) == [
            "shop.example.com"
        ]
        held = {"fqdn": "https://SHOP.example.com/"}
        assert domains.missing_custom_hosts(body, held, source_wildcard=SRC) == []

    def test_body_hosts_reads_every_body_shape(self) -> None:
        assert domains.body_hosts({"urls": [{"name": "w", "url": "https://a.b.c"}, "x"]}) == [
            "a.b.c"
        ]
        assert domains.body_hosts(
            {"docker_compose_domains": [{"name": "w", "domain": "https://d.e.f,https://d.e.f"}]}
        ) == ["d.e.f"]


class TestRestoreBody:
    def _body(self, source: dict[str, object], **kw: object) -> dict[str, object] | None:
        kind = kw.pop("kind", ResourceKind.APP_DOCKERIMAGE)
        return domains.restore_body(_app(kind), source, **kw)  # type: ignore[arg-type]

    def test_no_domains_restores_nothing(self) -> None:
        assert self._body({}) is None

    def test_the_original_domains_whatever_they_hold(self) -> None:
        value = f"https://shop.example.com,https://x.{SRC}"
        assert self._body({"fqdn": value}) == {"domains": value}

    def test_compose_per_service(self) -> None:
        raw = json.dumps(
            {"web": {"domain": "https://shop.example.com"}, "cfg": {"domain": f"https://c.{SRC}"}}
        )
        assert self._body({"docker_compose_domains": raw}, kind=ResourceKind.APP_GIT_COMPOSE) == {
            "docker_compose_domains": [
                {"name": "web", "domain": "https://shop.example.com"},
                {"name": "cfg", "domain": f"https://c.{SRC}"},
            ]
        }

    def test_compose_without_domains_restores_nothing(self) -> None:
        assert self._body({"docker_compose_domains": "{}"}, kind=ResourceKind.APP_GIT_COMPOSE) is None

    def test_a_service_is_left_to_its_own_path(self) -> None:
        # GET /services has no top-level fqdn: its body is built from its
        # sub-applications (api/resources.service_restore_body).
        service = ResourceSnapshot(
            uuid="s1", name="wiki", collection="services", kind=ResourceKind.SERVICE_TEMPLATE
        )
        assert domains.restore_body(service, {"fqdn": "https://x.y.z"}) is None

    def test_noindex_flags_ride_along_from_4_3(self) -> None:
        source = {
            "fqdn": "https://shop.example.com",
            "noindex_domains": ["https://shop.example.com"],
        }
        assert self._body(source, version=(4, 3, 0)) == {
            "domains": "https://shop.example.com",
            "noindex_domains": ["https://shop.example.com"],
        }

    def test_noindex_flags_with_an_unknown_version_still_ride_along(self) -> None:
        source = {
            "fqdn": "https://shop.example.com",
            "noindex_domains": ["https://shop.example.com"],
        }
        assert "noindex_domains" in (self._body(source) or {})

    @pytest.mark.parametrize(
        ("noindex", "version"),
        [([], (4, 5, 0)), (None, (4, 5, 0)), (["", 3], (4, 5, 0)), (["https://a.b"], (4, 2, 9))],
    )
    def test_noindex_flags_are_left_out(self, noindex: object, version: tuple[int, ...]) -> None:
        source = {"fqdn": "https://shop.example.com", "noindex_domains": noindex}
        assert self._body(source, version=version) == {"domains": "https://shop.example.com"}

    def test_compose_junk_is_tolerated(self) -> None:
        assert domains.compose_domains("{not json") == {}
        assert domains.compose_domains("[1, 2]") == {}
        assert domains.compose_domains({"web": "https://a.example.com"}) == {
            "web": "https://a.example.com"
        }


class TestRestorePatch:
    """Before FINALIZE the source never lost a host: the restore must write nothing."""

    def test_same_domains_need_no_patch(self) -> None:
        body = {"domains": "https://Shop.example.com/,https://b.example.com"}
        assert domains.restore_patch(body, {"fqdn": "https://b.example.com,https://shop.example.com"}) is None

    def test_a_released_host_brings_the_whole_body_back(self) -> None:
        body = {"domains": "https://shop.example.com", "noindex_domains": ["https://shop.example.com"]}
        assert domains.restore_patch(body, {"fqdn": ""}) == body

    def test_compose_compares_per_service(self) -> None:
        body = {"docker_compose_domains": [{"name": "web", "domain": "https://a.b.c"}, "junk"]}
        same = {"docker_compose_domains": json.dumps({"web": {"domain": "https://a.b.c"}})}
        released = {"docker_compose_domains": json.dumps({"web": {"domain": ""}})}
        assert domains.restore_patch(body, same) is None
        assert domains.restore_patch(body, released) == body

    def test_an_unknown_body_needs_nothing(self) -> None:
        assert domains.restore_patch({"urls": []}, {}) is None


class TestRestoreSummary:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ({"domains": "https://shop.example.com"}, "https://shop.example.com"),
            ({"domains": ""}, ""),
            (
                {"docker_compose_domains": [{"name": "web", "domain": "https://a.b"}]},
                "web=https://a.b",
            ),
            ({"urls": [{"name": "app", "url": "https://s.b"}, "junk"]}, "app=https://s.b"),
            ({"other": 1}, "?"),
        ],
    )
    def test_names_what_to_set_back(self, body: dict[str, object], expected: str) -> None:
        assert domains.restore_summary(body) == expected
