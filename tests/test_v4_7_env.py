"""Environment variables on Coolify 4.7 — the pure parts.

Each rule is pinned from both sides of the release it starts in, and for an
unknown version, which keeps the behaviour of the release before (a field or a
value form is sent only where it is known to arrive).

Upstream evidence at the release tags (v4.7.0 = v4.7.1 for every file read here):
app/Http/Middleware/TrimStrings.php v4.7.0:18-20, app/Models/EnvironmentVariable.php
v4.7.0:880-908, app/Traits/HasSecretManager.php v4.6.0:153-255 and v4.7.0:153-258.
Locked values never appear in an assertion message: Secrets shows key names only.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from bg_coolify_migrate.api.resources import build_env_entries
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain import locked_envs as le
from bg_coolify_migrate.domain.locked_envs import FileKind, SourceConfig

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


# ── legacy rows Coolify writes exact (4.6.0, 4.7.0) ──────────────────────────
#
# HasSecretManager::useExactEscaping v4.6.0:153-171 (legacyEnvFileLineIsUnreadable
# :178-189: a literal/multiline value holding a quote) and v4.7.0:153-192 (:187-189:
# also a JSON object or array holding a newline, whatever its flags). Databases
# never (:159-161).

SVC_ENV = "/data/coolify/services/svc1/.env"
APP_ENV = "/data/coolify/applications/app1/.env"
SECRET = "s3ntinel-9QxZ"
JSON_ML = '{\n  "token": "' + SECRET + '",\n  "url": "https://x/$path"\n}'


def _locked(key: str, *, version: str = "4.4.7", **flags: Any) -> dict[str, Any]:
    """A locked row as the API returns it: no value key at all."""
    return {
        "key": key,
        "version": version,
        "is_shown_once": True,
        "is_preview": False,
        "is_runtime": True,
        "is_literal": False,
        "is_multiline": False,
        **flags,
    }


def _readable(key: str, value: str, *, version: str = "4.4.7", **flags: Any) -> dict[str, Any]:
    """An unlocked row whose real_value is the resolved value: a row with exact
    escaping, or a legacy one Coolify writes exact (EnvironmentVariable v4.7.0:377-381)."""
    env = {**_locked(key, version=version, **flags), "is_shown_once": False}
    env.update(value=value, real_value=value)
    return env


def _exact_line(env: dict[str, Any], value: str) -> str:
    """escapeComposeEnvFileValue (docker.php v4.7.0:2126-2135), as both writers use it."""
    return le.escape_compose_env_file_value(value, le.allows_interpolation(env, value))


def _recover(
    envs: list[dict[str, Any]], text: str, collection: str = "services"
) -> le.LockedRecovery:
    path, uuid = (SVC_ENV, "svc1") if collection == "services" else (APP_ENV, "app1")
    return le.recover(
        envs,
        SourceConfig(path=path, kind=FileKind.DOTENV, text=text),
        collection=collection,
        uuid=uuid,
        version=V47,
    )


class TestLockedLegacyRowWrittenExact:
    @pytest.mark.parametrize("collection", ["services", "applications"])
    @pytest.mark.parametrize("flags", [{}, {"is_multiline": True}, {"is_literal": True}])
    def test_a_multiline_json_value_is_read_back(
        self, collection: str, flags: dict[str, bool]
    ) -> None:
        """Up to 4.6 this row was unrecoverable (its raw JSON spilled over lines);
        4.7.0 writes it exact, and the exact form decodes to the value written."""
        env = _locked("CONFIG", **flags)
        rec = _recover([env], f"CONFIG={_exact_line(env, JSON_ML)}", collection)
        assert rec.recovered.get("CONFIG") == JSON_ML
        assert rec.unrecoverable == {}

    def test_a_literal_holding_a_quote_is_read_back(self) -> None:
        """The 4.6.0 rule: the old single quotes would end the value at its quote."""
        env = _locked("GREETING", is_literal=True)
        value = "it's $5"
        line = _exact_line(env, value)
        assert line == '"it\'s \\$5"'
        assert _recover([env], f"GREETING={line}").recovered.get("GREETING") == value

    def test_the_legacy_forms_still_read_back(self) -> None:
        rec = _recover([_locked("K")], "K=it\\'s")
        assert rec.recovered.get("K") == "it's"

    def test_a_secret_manager_line_under_json_stays_closed(self) -> None:
        """RemoteSecretValueFormatter::dotenv (v4.7.0:17-24) writes `$` as `$$`; a JSON
        row's exact form writes it as `\\$`, so the round trip refuses the line."""
        secret = '{\n  "a": "it\'s $x"\n}'
        line = '"' + secret.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'
        rec = _recover([_locked("CONFIG")], f"CONFIG={line}")
        assert "CONFIG" not in rec.recovered
        assert "CONFIG" in rec.unrecoverable

    def test_a_database_compose_keeps_only_the_legacy_form(self) -> None:
        env = _locked("K")
        assert le.decode(_exact_line(env, JSON_ML), env, FileKind.COMPOSE) is None
        assert le.decode(JSON_ML, env, FileKind.COMPOSE) == JSON_ML

    def test_an_unlocked_sibling_written_exact_is_current(self) -> None:
        """The staleness check expects the same pair of forms (written_forms)."""
        sibling = _readable("SETTINGS", JSON_ML)
        token = _locked("TOKEN")
        text = f"TOKEN={_exact_line(token, SECRET)}\nSETTINGS={_exact_line(sibling, JSON_ML)}"
        assert _recover([token, sibling], text).recovered.get("TOKEN") == SECRET

    @given(value=st.text(min_size=1, max_size=40), literal=st.booleans(), multiline=st.booleans())
    def test_no_legacy_form_starts_with_a_double_quote(
        self, value: str, literal: bool, multiline: bool
    ) -> None:
        """Why both forms may be accepted without a version: every exact dotenv form
        starts with `"`, no legacy form does, so no line is both."""
        env = _locked("K", is_literal=literal, is_multiline=multiline)
        legacy, exact = le.written_forms(env, value, FileKind.DOTENV)
        assert not legacy.startswith('"')
        assert exact.startswith('"')


# ── 4.7.0: multi-line values keep their edge whitespace ──────────────────────


class TestEdgeWhitespace:
    """withoutEdgeWhitespace (EnvironmentVariable v4.7.0:894-899): a key's trailing
    newline is kept on set, get and real_value (:907, :886, :833)."""

    KEY = "-----BEGIN KEY-----\nAAAA\n-----END KEY-----\n"

    def test_an_exact_line_ending_in_a_newline(self) -> None:
        env = _locked("SSH_KEY", version="4.7.0", is_multiline=True)
        line = _exact_line(env, self.KEY)
        assert line.endswith('\n"')  # the closing quote opens a line of its own
        text = f'SSH_KEY={line}\nOTHER="x"'
        rec = _recover([env, _readable("OTHER", "x", version="4.7.0")], text)
        assert rec.recovered.get("SSH_KEY") == self.KEY

    def test_an_unlocked_row_ending_in_a_newline_is_current(self) -> None:
        sibling = _readable("CERT", self.KEY, version="4.7.0", is_multiline=True)
        token = _locked("TOKEN", version="4.7.0")
        text = f'TOKEN="t"\nCERT={_exact_line(sibling, self.KEY)}'
        assert _recover([token, sibling], text).recovered.get("TOKEN") == "t"

    def test_a_legacy_multiline_line_ending_in_a_newline(self) -> None:
        env = _locked("SSH_KEY", is_multiline=True)
        assert _recover([env], f"SSH_KEY='{self.KEY}'").recovered.get("SSH_KEY") == self.KEY

    def test_a_database_compose_item(self) -> None:
        env = _locked("SSH_KEY", version="4.7.0", is_multiline=True)
        # Yaml::dump($compose, 10) (StartPostgresql v4.7.0:211): double-quoted, \n escapes.
        item = "SSH_KEY=" + self.KEY.replace("\n", "\\n")
        text = f'services:\n  db1:\n    environment:\n      - "{item}"\n'
        assert yaml.safe_load(text)["services"]["db1"]["environment"] == [f"SSH_KEY={self.KEY}"]
        rec = le.recover(
            [env],
            SourceConfig(
                path="/data/coolify/databases/db1/docker-compose.yml",
                kind=FileKind.COMPOSE,
                text=text,
            ),
            collection="databases",
            uuid="db1",
            version=V47,
        )
        assert rec.recovered.get("SSH_KEY") == self.KEY


# ── the plan's old-escaping warning ──────────────────────────────────────────

V452 = (4, 5, 2)
_MULTILINE_JSON = '{"a":\n"b"}'


def _legacy(key: str, value: str, **flags: Any) -> dict[str, Any]:
    return _env(key, value, version="4.4.7", **flags)


class TestLegacyEscapingFalsePositives:
    """legacyEscapingChangesValue returns false first whenever useExactEscaping holds
    (HasSecretManager v4.6.0:224-229, v4.7.0:227-231)."""

    @pytest.mark.parametrize(
        ("collection", "env", "since"),
        [
            ("services", _legacy("K", _MULTILINE_JSON), V47),
            ("applications", _legacy("K", '{"a":\n"$b"}'), V47),
            ("services", _legacy("K", '{"a":\n"it\'s"}', is_literal=True), V47),
            ("applications", _legacy("K", "it's", is_literal=True), V46),
            ("services", _legacy("K", "it's\nok", is_multiline=True), V46),
        ],
    )
    def test_silent_from_the_release_that_writes_it_exact(
        self, collection: str, env: dict[str, Any], since: tuple[int, int, int]
    ) -> None:
        before = V46 if since == V47 else V452
        for version, changes in ((before, True), (since, False), (V471, False), (None, True)):
            assert (
                fidelity.legacy_escaping_changes_value(
                    env, collection=collection, engine=None, version=version
                )
                is changes
            ), version

    @pytest.mark.parametrize(
        ("collection", "env"),
        [
            ("databases", _legacy("K", _MULTILINE_JSON)),  # never exact (:159-161)
            ("databases", _legacy("K", "it's", is_literal=True)),
            ("applications", _legacy("K", '{"a": "$b"}')),  # single-line JSON: no rule
            ("services", _legacy("K", 'pa"ss')),  # plain: no rule
        ],
    )
    def test_still_warns_where_nothing_is_written_exact(
        self, collection: str, env: dict[str, Any]
    ) -> None:
        assert fidelity.legacy_escaping_changes_value(
            env, collection=collection, engine=None, version=V47
        )

    def test_env_warnings_passes_the_version(self) -> None:
        envs = [_legacy("CONFIG", _MULTILINE_JSON), _legacy("DB_PASSWORD", 'x"y')]
        out = fidelity.env_warnings(
            envs, collection="services", engine=None, compose=None, version=V47
        )
        assert len(out) == 1
        assert "['DB_PASSWORD']" in out[0] and "CONFIG" not in out[0]
        older = fidelity.env_warnings(
            envs, collection="services", engine=None, compose=None, version=V46
        )
        assert "['CONFIG', 'DB_PASSWORD']" in older[0]
