"""Tests for server-bound wildcard domain classification and rewriting."""

from __future__ import annotations

import pytest

from bg_coolify_migrate.dns.wildcard import remap_host, under_wildcard, wildcard_base

SOURCE = "app.0046-20.cloud.bauer-group.com"
TARGET = "app.0047-20.cloud.bauer-group.com"


class TestWildcardBase:
    @pytest.mark.parametrize(
        "stored",
        [
            "app.0046-20.cloud.bauer-group.com",
            "https://app.0046-20.cloud.bauer-group.com",
            "*.app.0046-20.cloud.bauer-group.com",
            "https://*.app.0046-20.cloud.bauer-group.com",
            "  app.0046-20.cloud.bauer-group.com  ",
        ],
    )
    def test_reduces_every_stored_form_to_the_bare_base(self, stored: str) -> None:
        # Coolify stores wildcard_domain with a scheme and/or a leading "*.";
        # all of them must reduce to the one comparable suffix.
        assert wildcard_base(stored) == SOURCE

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_missing_is_none(self, empty: str | None) -> None:
        assert wildcard_base(empty) is None


class TestUnderWildcard:
    def test_subdomain_is_under(self) -> None:
        assert under_wildcard("pdf-tool.app.0046-20.cloud.bauer-group.com", SOURCE)

    def test_base_itself_is_under(self) -> None:
        assert under_wildcard(SOURCE, SOURCE)

    def test_custom_domain_is_not_under(self) -> None:
        assert not under_wildcard("shop.example.com", SOURCE)

    def test_dot_boundary_is_anchored(self) -> None:
        # "notapp.0046-20…" must NOT be treated as under "app.0046-20…": the
        # match is anchored on the leading dot, not a bare suffix.
        assert not under_wildcard("xxapp.0046-20.cloud.bauer-group.com", SOURCE)

    def test_no_wildcard_configured_is_never_under(self) -> None:
        assert not under_wildcard("anything.example.com", None)


class TestRemapHost:
    def test_rewrites_subdomain_onto_target_wildcard(self) -> None:
        assert (
            remap_host("pdf-tool.app.0046-20.cloud.bauer-group.com", SOURCE, TARGET)
            == "pdf-tool.app.0047-20.cloud.bauer-group.com"
        )

    def test_preserves_multi_label_subdomain(self) -> None:
        assert (
            remap_host("a.b.app.0046-20.cloud.bauer-group.com", SOURCE, TARGET)
            == "a.b.app.0047-20.cloud.bauer-group.com"
        )

    def test_bare_base_maps_to_bare_target(self) -> None:
        assert remap_host(SOURCE, SOURCE, TARGET) == TARGET

    def test_custom_domain_is_not_rewritten(self) -> None:
        assert remap_host("shop.example.com", SOURCE, TARGET) is None

    def test_missing_either_wildcard_is_a_noop(self) -> None:
        host = "pdf-tool.app.0046-20.cloud.bauer-group.com"
        assert remap_host(host, None, TARGET) is None
        assert remap_host(host, SOURCE, None) is None

    def test_works_when_wildcards_are_stored_as_urls(self) -> None:
        # The rewrite must survive scheme/star noise on either side.
        assert (
            remap_host(
                "pdf-tool.app.0046-20.cloud.bauer-group.com",
                "https://*.app.0046-20.cloud.bauer-group.com",
                "https://*.app.0047-20.cloud.bauer-group.com",
            )
            == "pdf-tool.app.0047-20.cloud.bauer-group.com"
        )
