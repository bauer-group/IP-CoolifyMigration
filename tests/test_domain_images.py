"""Tests for image reference parsing and tag stability.

We build the target with the SAME image reference the source uses. A tag is a
pointer though, so "the same reference" can still mean "a different image" — and
for a database crossing a major version, the byte-exactly copied data directory
becomes unreadable. These tests encode which references carry that risk.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.domain.images import (
    TagStability,
    classify_tag,
    mount_path_is_guessable,
    parse,
    risk_note,
    same_image,
)


class TestParse:
    def test_bare_name(self) -> None:
        ref = parse("postgres")
        assert ref.name == "postgres"
        assert ref.tag is None
        assert ref.effective_tag == "latest"
        assert ref.registry is None

    def test_name_and_tag(self) -> None:
        ref = parse("postgres:16")
        assert ref.name == "postgres"
        assert ref.tag == "16"

    def test_org_and_name(self) -> None:
        # `minio/minio` is an org, not a registry: no dot, no port.
        ref = parse("minio/minio:latest")
        assert ref.registry is None
        assert ref.name == "minio/minio"
        assert ref.tag == "latest"

    def test_registry_with_dot(self) -> None:
        ref = parse("ghcr.io/bauer-group/app:v1")
        assert ref.registry == "ghcr.io"
        assert ref.name == "bauer-group/app"
        assert ref.tag == "v1"

    def test_registry_with_port_is_not_a_tag(self) -> None:
        # The hard case: both a registry port and a tag are a colon. Docker's
        # rule — a colon before the last slash is a port — is what we use.
        ref = parse("registry.local:5000/app:v1")
        assert ref.registry == "registry.local:5000"
        assert ref.name == "app"
        assert ref.tag == "v1"

    def test_registry_with_port_and_no_tag(self) -> None:
        ref = parse("registry.local:5000/app")
        assert ref.registry == "registry.local:5000"
        assert ref.name == "app"
        assert ref.tag is None

    def test_localhost_is_a_registry(self) -> None:
        ref = parse("localhost/app:v1")
        assert ref.registry == "localhost"

    def test_digest(self) -> None:
        ref = parse("postgres@sha256:abc123")
        assert ref.name == "postgres"
        assert ref.digest == "sha256:abc123"

    def test_tag_and_digest(self) -> None:
        ref = parse("postgres:16@sha256:abc123")
        assert ref.tag == "16"
        assert ref.digest == "sha256:abc123"

    def test_raw_is_preserved(self) -> None:
        assert parse("  postgres:16  ").raw == "postgres:16"


class TestClassifyTag:
    def test_digest_is_pinned(self) -> None:
        assert classify_tag("16", has_digest=True) is TagStability.PINNED

    @pytest.mark.parametrize("tag", [None, "latest", "edge", "main", "master", "stable", "nightly"])
    def test_moving_tags(self, tag: str | None) -> None:
        # These can cross a MAJOR version, which for a database means the copied
        # data directory may be unreadable.
        assert classify_tag(tag) is TagStability.MOVING

    def test_moving_is_case_insensitive(self) -> None:
        assert classify_tag("LATEST") is TagStability.MOVING

    @pytest.mark.parametrize("tag", ["16", "8", "v3"])
    def test_bare_major_floats_on_minors(self, tag: str) -> None:
        assert classify_tag(tag) is TagStability.MINOR_FLOATING

    @pytest.mark.parametrize("tag", ["16.4", "v1.2", "16.4-alpine"])
    def test_major_minor_floats_on_patches(self, tag: str) -> None:
        assert classify_tag(tag) is TagStability.PATCH_FLOATING

    @pytest.mark.parametrize("tag", ["16.4.1", "1.2.3-alpine", "v2.0.0"])
    def test_full_versions_are_exact(self, tag: str) -> None:
        assert classify_tag(tag) is TagStability.EXACT

    def test_named_tags_are_treated_as_patch_floating(self) -> None:
        # Nothing enforces immutability on `16-alpine`; worth mentioning, not
        # worth alarming about.
        assert classify_tag("16-alpine") is TagStability.PATCH_FLOATING


class TestIsFloating:
    @pytest.mark.parametrize("image", ["postgres", "postgres:latest", "postgres:16", "postgres:16.4"])
    def test_floating_references(self, image: str) -> None:
        assert parse(image).is_floating is True

    @pytest.mark.parametrize("image", ["postgres@sha256:abc", "postgres:16.4.1"])
    def test_stable_references(self, image: str) -> None:
        assert parse(image).is_floating is False


class TestRiskNote:
    def test_pinned_has_nothing_to_say(self) -> None:
        assert risk_note(parse("postgres@sha256:abc")) is None

    def test_exact_has_nothing_to_say(self) -> None:
        assert risk_note(parse("postgres:16.4.1")) is None

    def test_moving_database_warns_about_the_data_directory(self) -> None:
        # THE case worth stopping for. Not "you might get a newer version" but
        # "the engine may refuse to start on your data".
        note = risk_note(parse("postgres:latest"), is_database=True)
        assert note is not None
        assert "MAJOR" in note
        assert "refuse to start" in note

    def test_moving_non_database_is_plainer(self) -> None:
        note = risk_note(parse("nginx:latest"), is_database=False)
        assert note is not None
        assert "refuse to start" not in note

    def test_minor_floating_says_normally_compatible(self) -> None:
        # postgres:16 -> 16.4 is a non-event. Say so rather than alarm.
        note = risk_note(parse("postgres:16"), is_database=True)
        assert note is not None
        assert "Normally compatible" in note

    def test_note_names_the_actual_reference(self) -> None:
        note = risk_note(parse("ghcr.io/org/app:latest"))
        assert note is not None
        assert "ghcr.io/org/app:latest" in note


class TestMountPathIsGuessable:
    @pytest.mark.parametrize("image", ["postgres:16", "postgres:18", "postgres:pg17"])
    def test_versioned_tags_are_readable(self, image: str) -> None:
        assert mount_path_is_guessable(image) is True

    @pytest.mark.parametrize("image", ["postgres", "postgres:latest", "postgres:stable"])
    def test_unversioned_tags_defeat_coolifys_regex(self, image: str) -> None:
        # Coolify picks the volume mount path by regexing the tag for a number,
        # silently taking the pre-18 path when it finds none — wrong if the image
        # is actually 18+.
        assert mount_path_is_guessable(image) is False


class TestSameImage:
    def test_identical(self) -> None:
        assert same_image("postgres:16", "postgres:16") is True

    def test_implicit_latest_matches_explicit(self) -> None:
        assert same_image("postgres", "postgres:latest") is True

    def test_different_tags(self) -> None:
        assert same_image("postgres:16", "postgres:17") is False

    def test_different_registries(self) -> None:
        assert same_image("ghcr.io/org/app:v1", "docker.io/org/app:v1") is False

    def test_none_is_never_equal(self) -> None:
        assert same_image(None, "postgres:16") is False
        assert same_image("postgres:16", None) is False
