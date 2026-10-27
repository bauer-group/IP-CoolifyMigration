"""Our slugify must agree with Laravel's Str::slug, byte for byte.

This is load-bearing, not cosmetic. Container discovery filters on
`coolify.projectName` / `coolify.environmentName` / `coolify.resourceName`, and
Coolify writes all three through `Str::slug`. If our slug differs by one
character the filter matches nothing — and `docker ps` returns an empty list, not
an error. The migration then reports a stack with no volumes and moves nothing,
successfully.

A unit test cannot settle this: it would compare our slugify against our own idea
of Laravel's, which is how the eszett bug got in (NFKD drops `ß` entirely, so
`Grüße` became `grue`). So we ask the real Laravel.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bg_coolify_migrate.domain.naming import slugify

pytestmark = pytest.mark.e2e

#: Names a German estate actually produces, plus the shapes that break slugs.
CASES = [
    "Grüße GmbH",
    "Ötztal AG",
    "Straße & Co",
    "BAUER GROUP",
    "my-project",
    "My  Project",
    "Über_Cool 2024",
    "Käse-Fabrik Süd",
    "a.b.c",
    "api.example.com",
    "me@example.com",
    "shop@2024",
    "50% Rabatt!",
    "c++ builder",
    "foo (bar) [baz]",
    "Ärger mit Öl und Ünsinn",
    "production",
    "MiXeD CaSe",
    "trailing---dashes---",
    "Weiße Röslein",
]


def _laravel_slugs(names: list[str]) -> list[str]:
    """Ask the running Coolify what Str::slug makes of each name."""
    payload = json.dumps(names)
    code = (
        f"$names = json_decode('{payload}', true);"
        "echo PHP_EOL.'SLUGS='.json_encode(array_map("
        "fn($n) => Illuminate\\Support\\Str::slug($n), $names)).PHP_EOL;"
    )
    result = subprocess.run(
        ["docker", "exec", "bgcm_e2e_coolify", "php", "artisan", "tinker", "--execute", code],
        capture_output=True,
        text=True,
        # Git Bash rewrites bare paths in argv; harmless here but consistent
        # with the rest of the rig.
        env={"MSYS_NO_PATHCONV": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    if result.returncode != 0:
        pytest.skip(f"cannot reach the rig's Coolify container: {result.stderr[:200]}")
    for line in result.stdout.splitlines():
        if line.startswith("SLUGS="):
            parsed: list[str] = json.loads(line[len("SLUGS=") :])
            return parsed
    pytest.skip(f"tinker returned nothing usable: {result.stdout[-200:]}")


def test_slug_matches_laravel() -> None:
    """Every name we might see must slug identically on both sides."""
    theirs = _laravel_slugs(CASES)
    ours = [slugify(name) for name in CASES]

    mismatches = [
        f"  {name!r}: laravel={t!r} ours={o!r}"
        for name, t, o in zip(CASES, theirs, ours, strict=True)
        if t != o
    ]
    assert not mismatches, (
        "slugify disagrees with Laravel's Str::slug; container discovery would "
        "silently match nothing for these:\n" + "\n".join(mismatches)
    )


def test_health_check_defaults_match_schema() -> None:
    """Our idea of Coolify's health-check defaults must be Coolify's.

    These drive a warning, and a wrong default warns on databases that are in
    fact stock — noise that teaches operators to skip warnings, which is worse
    than no warning at all. The first draft guessed four of five wrong.
    """
    from bg_coolify_migrate.api.fields import DATABASE_HEALTH_CHECK_DEFAULTS

    result = subprocess.run(
        [
            "docker", "exec", "bgcm_e2e_coolify_db",
            "psql", "-U", "coolify", "-d", "coolify", "-tAc",
            "SELECT column_name||'='||COALESCE(column_default,'<none>') "
            "FROM information_schema.columns WHERE table_name='standalone_postgresqls' "
            # The backslash escapes the underscore for SQL's LIKE, not for Python.
            r"AND column_name LIKE 'health\_check%' ORDER BY column_name",
        ],
        capture_output=True,
        text=True,
        env={"MSYS_NO_PATHCONV": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    if result.returncode != 0:
        pytest.skip(f"cannot reach the rig's Coolify database: {result.stderr[:200]}")

    # Postgres reports defaults as SQL literals: `true`, `15`, `'x'::text`.
    schema: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            column, _, default = line.strip().partition("=")
            schema[column] = default

    mismatches = []
    for field, ours in DATABASE_HEALTH_CHECK_DEFAULTS.items():
        theirs = schema.get(field)
        if theirs is None:
            mismatches.append(f"  {field}: absent from the schema entirely")
            continue
        expected = "true" if ours is True else ("false" if ours is False else str(ours))
        if theirs != expected:
            mismatches.append(f"  {field}: schema={theirs!r} ours={ours!r}")

    assert not mismatches, "health-check defaults drifted from Coolify:\n" + "\n".join(mismatches)


#: Every engine with TLS, and its table (migration 2025_01_27_102616). ClickHouse
#: has no TLS at all, so no column.
_TLS_TABLES = frozenset(
    {
        "standalone_postgresqls",
        "standalone_mysqls",
        "standalone_mariadbs",
        "standalone_redis",
        "standalone_keydbs",
        "standalone_dragonflies",
        "standalone_mongodbs",
    }
)


def test_tls_defaults_match_schema() -> None:
    """Every TLS-capable engine's enable_ssl defaults to false.

    The premise of fidelity.database_tls_lost_warning: no create path sets the
    column, so a target always comes up with its default — plaintext. Mongo's was
    TRUE until beta.400 (migration 2025_03_26_104103). If upstream flips a default
    again, the one-way warning must become a source/target comparison.
    """
    result = subprocess.run(
        [
            "docker", "exec", "bgcm_e2e_coolify_db",
            "psql", "-U", "coolify", "-d", "coolify", "-tAc",
            "SELECT table_name||'='||COALESCE(column_default,'<none>') "
            "FROM information_schema.columns WHERE column_name='enable_ssl' "
            r"AND table_name LIKE 'standalone\_%' ORDER BY table_name",
        ],
        capture_output=True,
        text=True,
        env={"MSYS_NO_PATHCONV": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    if result.returncode != 0:
        pytest.skip(f"cannot reach the rig's Coolify database: {result.stderr[:200]}")

    schema = dict(
        line.strip().partition("=")[::2]
        for line in result.stdout.strip().splitlines()
        if "=" in line
    )
    assert set(schema) == _TLS_TABLES, f"tables with enable_ssl changed: {sorted(schema)}"
    flipped = {table: default for table, default in schema.items() if default != "false"}
    assert not flipped, f"enable_ssl no longer defaults to false: {flipped}"
