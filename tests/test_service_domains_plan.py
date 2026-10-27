"""Service URLs the target cannot take, refused at plan time.

Only what WILL fail: one URL on two sub-applications makes Coolify reject the
target's whole domain update, and a URL with credentials in it must never reach
a request, a log line or the journal. Everything subtler is left to CREATE,
which runs before the stop.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.kinds import ResourceKind
from tests.test_engine_planner import _plan_one_app

V45 = (4, 5, 0)


def _apps(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"applications": [{"name": n, "fqdn": u} for n, u in pairs]}


class TestDuplicateUrls:
    def test_one_url_on_two_sub_applications_is_refused(self) -> None:
        source = _apps(("web", "https://shop.example.com"), ("api", "https://shop.example.com"))
        (line,) = fidelity.service_refusals(source, version=V45)
        assert "['api', 'web']" in line
        assert "'https://shop.example.com'" in line

    def test_scheme_and_host_case_are_normalised_from_4_2(self) -> None:
        source = _apps(("web", "HTTPS://Shop.example.com"), ("api", "https://shop.example.com"))
        assert fidelity.service_refusals(source, version=V45)
        assert fidelity.service_refusals(source, version=None), "unknown applies the rule"
        # 4.1.x compares the raw strings: these two pass its check.
        assert fidelity.service_refusals(source, version=(4, 1, 2)) == []

    @pytest.mark.parametrize(
        ("web", "api"),
        [
            # Different paths are different URLs.
            ("https://shop.example.com/a", "https://shop.example.com/b"),
            # These collide only in the per-item conflict check, which CREATE's
            # own-conflict retry resolves or refuses before the stop.
            ("https://shop.example.com/", "https://shop.example.com"),
            ("http://shop.example.com", "https://shop.example.com"),
            ("https://shop.example.com:8443", "https://shop.example.com"),
        ],
    )
    def test_anything_short_of_an_exact_duplicate_is_left_to_create(
        self, web: str, api: str
    ) -> None:
        assert fidelity.service_refusals(_apps(("web", web), ("api", api)), version=V45) == []

    def test_url_is_read_before_fqdn(self) -> None:
        source = {
            "applications": [
                {
                    "name": "web",
                    "fqdn": "https://shop.example.com",
                    "url": "https://shop.example.com:8443",
                },
                {"name": "api", "fqdn": "https://shop.example.com"},
            ]
        }
        assert fidelity.service_refusals(source, version=V45) == []

    def test_a_sub_application_listing_more_than_one_url(self) -> None:
        source = _apps(
            ("web", "https://a.example.com,https://shop.example.com"),
            ("api", "https://shop.example.com"),
        )
        assert len(fidelity.service_refusals(source, version=V45)) == 1

    @pytest.mark.parametrize(
        ("url", "same"),
        [
            ("https://Shop.example.com/x?A=1#F", "https://shop.example.com/x?A=1#F"),
            ("shop.example.com", "shop.example.com"),  # no scheme: compared as written
            ("http://[::1", "http://[::1"),  # unparseable: compared as written
        ],
    )
    def test_odd_shapes_are_compared_as_coolify_would(self, url: str, same: str) -> None:
        assert fidelity.service_refusals(_apps(("web", url), ("api", same)), version=V45)

    def test_no_sub_applications(self) -> None:
        assert fidelity.service_refusals({}, version=V45) == []
        assert fidelity.service_refusals({"applications": ["junk", {"fqdn": 3}]}, version=V45) == []


class TestCredentialsInUrls:
    def test_refused_naming_only_the_sub_application(self) -> None:
        source = _apps(
            ("web", "https://admin:hunter2@shop.example.com"), ("api", "https://a.example.com")
        )
        (line,) = fidelity.service_refusals(source, version=V45)
        assert "['web']" in line
        assert "hunter2" not in line and "admin:" not in line

    def test_never_echoed_by_the_duplicate_rule_either(self) -> None:
        url = "https://admin:hunter2@shop.example.com"
        lines = fidelity.service_refusals(_apps(("web", url), ("api", url)), version=V45)
        assert len(lines) == 1
        assert not any("hunter2" in line for line in lines)

    def test_an_at_sign_in_the_path_is_not_a_credential(self) -> None:
        source = _apps(("web", "https://shop.example.com/@team"), ("api", None))  # type: ignore[arg-type]
        assert fidelity.service_refusals(source, version=V45) == []

    def test_an_unparseable_url_with_credentials_is_still_refused(self) -> None:
        source = _apps(("web", "http://u:p@[::1"))
        assert "['web']" in fidelity.service_refusals(source, version=V45)[0]


class TestPlannerWiring:
    async def test_a_service_with_a_shared_url_blocks_the_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_one_app(
            monkeypatch,
            _apps(("web", "https://shop.example.com"), ("api", "https://shop.example.com")),
            snapshot={
                "collection": "services",
                "kind": ResourceKind.SERVICE_TEMPLATE,
                "service_type": "nginx",
            },
        )
        assert plan.is_blocked
        assert any("share the URL" in r for r in plan.resources[0].refusals)
