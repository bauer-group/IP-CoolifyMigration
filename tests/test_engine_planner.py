"""The pure decisions in the planner, each one a thing a real Coolify taught us.

Both functions here were wrong in ways that reported no error and moved no data,
which is why they are worth table-driven tests rather than a passing glance.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.engine.planner import resource_labels, server_uuid_of

# ── server_uuid_of ───────────────────────────────────────────────────────────
# The three kinds hang their server off different relations. The original read
# `server_uuid` only, which exists on services and on nothing else, so the two
# kinds that matter most died at "could not determine the source server".

SERVER_SHAPES: list[tuple[str, dict[str, Any], str | None]] = [
    (
        "service: a real server() belongsTo relation",
        {"server": {"uuid": "srv-1", "name": "prod"}},
        "srv-1",
    ),
    (
        "some endpoints flatten it to server_uuid",
        {"server_uuid": "srv-2"},
        "srv-2",
    ),
    (
        "application: only destination(), a morphTo",
        {"destination": {"server_id": 1, "server": {"uuid": "srv-3"}}},
        "srv-3",
    ),
    (
        "database: same as application — this is the common case",
        {
            "uuid": "db-1",
            "destination_type": "App\\Models\\StandaloneDocker",
            "destination": {"id": 1, "server_id": 1, "server": {"uuid": "srv-4", "ip": "10.0.0.4"}},
        },
        "srv-4",
    ),
    (
        "server_uuid wins when both are present",
        {"server_uuid": "srv-5", "destination": {"server": {"uuid": "other"}}},
        "srv-5",
    ),
    (
        "destination present but its server relation was not loaded",
        {"destination": {"server_id": 7}},
        None,
    ),
    ("no server information at all", {"uuid": "x"}, None),
    ("destination is null, as it is on an undeployed resource", {"destination": None}, None),
]


@pytest.mark.parametrize(
    ("shape", "expected"),
    [(shape, expected) for _, shape, expected in SERVER_SHAPES],
    ids=[name for name, _, _ in SERVER_SHAPES],
)
def test_reads_the_server_from_whichever_relation_carries_it(
    shape: dict[str, Any], expected: str | None
) -> None:
    assert server_uuid_of(shape) == expected


def test_returns_none_rather_than_guessing_from_server_id() -> None:
    """None means "ask /servers", not "there is no server".

    Resolving a numeric id needs an API round trip, so this stays pure and the
    caller does it. Inventing a uuid from the id would be a fabrication.
    """
    assert server_uuid_of({"destination": {"server_id": 3}}) is None


# ── resource_labels ──────────────────────────────────────────────────────────
# Coolify's own filter is `--filter label=coolify.{kind}Id={id}`, and copying it
# from outside is impossible: every controller calls makeHidden(['id']). These
# are the labels that are actually visible, and they go through Str::slug.


def test_labels_a_resource_by_what_is_visible() -> None:
    assert resource_labels(project="shop", environment="production", name="api") == {
        "coolify.projectName": "shop",
        "coolify.environmentName": "production",
        "coolify.resourceName": "api",
    }


def test_slugifies_every_part() -> None:
    """Coolify slugs all three when it writes them, so we must when we read.

    Not cosmetic: an unslugged filter matches no containers, and `docker ps`
    answers an empty list rather than an error — the stack then looks like it has
    no volumes and the migration moves nothing, successfully.
    """
    labels = resource_labels(project="Grüße GmbH", environment="Pre Prod", name="api.example.com")
    assert labels == {
        "coolify.projectName": "grusse-gmbh",
        "coolify.environmentName": "pre-prod",
        # Dots are stripped, not turned into separators — see Str::slug.
        "coolify.resourceName": "apiexamplecom",
    }


# ── server_ref ───────────────────────────────────────────────────────────────
# Coolify's localhost self-record carries user='' and port can be blank. Both
# must fall back, or F2 (and F1) SSH with an empty user / port 0.


class TestServerRef:
    def test_empty_user_falls_back_to_root(self) -> None:
        """The localhost self-record has user='' — get-default does not catch it.

        `get("user", "root")` returns "" because the key is present-but-empty, so
        F2 SSHed as `@host` and got Permission denied. Coolify's own DB column
        defaults to 'root'; the record just blanks it. Found by the F2 e2e run.
        """
        from bg_coolify_migrate.engine.planner import server_ref

        ref = server_ref({"uuid": "u", "name": "localhost", "ip": "127.0.0.1", "user": ""})
        assert ref.user == "root"

    def test_missing_user_falls_back_to_root(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        assert server_ref({"uuid": "u", "ip": "10.0.0.1"}).user == "root"

    def test_explicit_user_is_kept(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        assert server_ref({"uuid": "u", "ip": "10.0.0.1", "user": "deploy"}).user == "deploy"

    def test_blank_port_falls_back_to_22(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        assert server_ref({"uuid": "u", "ip": "10.0.0.1", "port": 0}).port == 22

    def test_reads_the_wildcard_from_the_settings_relation(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        ref = server_ref(
            {
                "uuid": "u",
                "ip": "10.0.0.1",
                "settings": {"wildcard_domain": "app.0046-20.cloud.bauer-group.com"},
            }
        )
        assert ref.wildcard_domain == "app.0046-20.cloud.bauer-group.com"

    def test_missing_settings_yields_an_empty_wildcard(self) -> None:
        from bg_coolify_migrate.engine.planner import server_ref

        # The LIST endpoint does not eager-load settings; a missing relation must
        # not crash, it just means "no wildcard known here".
        assert server_ref({"uuid": "u", "ip": "10.0.0.1"}).wildcard_domain == ""
