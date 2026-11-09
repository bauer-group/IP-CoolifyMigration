"""Environment variables on Coolify 4.7 — the pure parts.

Each rule is pinned from both sides of the release it starts in, and for an
unknown version, which keeps the behaviour of the release before (a field or a
value form is sent only where it is known to arrive).

Upstream evidence at the release tags (v4.7.0 = v4.7.1 for every file read here):
app/Http/Middleware/TrimStrings.php v4.7.0:18-20, app/Models/EnvironmentVariable.php
v4.7.0:880-908.
"""

from __future__ import annotations

from typing import Any

import pytest

from bg_coolify_migrate.api.resources import build_env_entries
from bg_coolify_migrate.domain import fidelity

V47 = (4, 7, 0)
V471 = (4, 7, 1)
V46 = (4, 6, 0)

_ABSENT = object()


def _env(key: str, value: object = "v", **kw: Any) -> dict[str, Any]:
    env: dict[str, Any] = {"key": key, "is_literal": False, "is_multiline": False, **kw}
    if value is not _ABSENT:
        env["value"] = value
    return env


def _value_of(entries: list[dict[str, Any]], key: str) -> object:
    return next(e.get("value", _ABSENT) for e in entries if e["key"] == key)


# ── empty values (env_value_untrimmed) ───────────────────────────────────────

_PASSTHROUGH = "services:\n  a:\n    environment:\n      - OPT\n"


class TestEmptyValuesCarried:
    """ConvertEmptyStringsToNull nulls '' on every release; from 4.7.0 ' ' gets
    through TrimStrings and the model trims it to ''."""

    @pytest.mark.parametrize("version", [V47, V471])
    @pytest.mark.parametrize(
        "flags", [{}, {"is_literal": True}, {"is_multiline": True}, {"is_preview": True}]
    )
    def test_an_empty_value_is_sent_as_one_space(
        self, version: tuple[int, int, int], flags: dict[str, bool]
    ) -> None:
        entries = build_env_entries([_env("OPT", "", **flags)], version=version)
        assert _value_of(entries, "OPT") == " "

    @pytest.mark.parametrize("version", [V46, (4, 5, 0), None])
    def test_older_and_unknown_releases_keep_the_empty_string(
        self, version: tuple[int, int, int] | None
    ) -> None:
        """There ' ' is trimmed back to '' and nulled: no gain, and an unknown
        release is not assumed to be new."""
        entries = build_env_entries([_env("OPT", "")], version=version)
        assert _value_of(entries, "OPT") == ""

    def test_only_an_exactly_empty_value(self) -> None:
        entries = build_env_entries(
            [_env("A", "x"), _env("B", " "), _env("C", "a b")], version=V47
        )
        assert [_value_of(entries, k) for k in "ABC"] == ["x", " ", "a b"]

    def test_never_a_value_less_row(self) -> None:
        """A null row has nothing to carry and a locked row's value is unknown:
        neither may grow a value on the way."""
        entries = build_env_entries(
            [_env("NULL", None), _env("LOCKED", _ABSENT, is_shown_once=True)], version=V47
        )
        assert [e["key"] for e in entries] == ["NULL"]
        assert "value" not in entries[0]

    def test_services_too(self) -> None:
        entries = build_env_entries([_env("OPT", "")], collection="services", version=V47)
        assert _value_of(entries, "OPT") == " "


class TestEmptyValueWarning:
    def test_silent_where_the_empty_value_is_carried(self) -> None:
        envs = [_env("OPT", "", version="4.5.0")]
        assert (
            fidelity.env_warnings(
                envs, collection="services", engine=None, compose=_PASSTHROUGH, version=V47
            )
            == []
        )

    @pytest.mark.parametrize("version", [V46, None])
    def test_still_warns_where_it_is_not(self, version: tuple[int, int, int] | None) -> None:
        envs = [_env("OPT", "", version="4.5.0")]
        out = fidelity.env_warnings(
            envs, collection="services", engine=None, compose=_PASSTHROUGH, version=version
        )
        assert len(out) == 1 and "['OPT']" in out[0]

    def test_a_preserved_null_still_warns(self) -> None:
        """Nothing to send for it: the target's row stays null and is left out."""
        envs = [_env("OPT", None, preserve_compose_null=True, version="4.4.0")]
        out = fidelity.env_warnings(
            envs, collection="services", engine=None, compose=_PASSTHROUGH, version=V47
        )
        assert any("['OPT']" in w for w in out)
