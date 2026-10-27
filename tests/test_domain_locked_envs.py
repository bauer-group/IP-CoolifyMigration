"""Reading locked variables back from the files Coolify writes — the pure part.

Every value here is either recovered EXACTLY or reported unrecoverable with a
reason. The one outcome that must never happen is a DIFFERENT value: the target
would then hold a credential the copied data was never initialised with, and
nothing downstream could tell. The property test at the bottom hunts for it.

Golden lines are hand-checked against upstream's encoders at the release tags
(v4.5.0 bootstrap/helpers/docker.php:1991-2037, HasSecretManager.php:174-232,
RemoteSecretValueFormatter.php:17-33; v4.1.2 docker.php:1267-1280).
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain import locked_envs as le
from bg_coolify_migrate.domain.kinds import DatabaseEngine
from bg_coolify_migrate.domain.locked_envs import FileKind, SourceConfig

V45 = (4, 5, 0)
SENTINEL = "s3ntinel-9QxZ"
_ABSENT = object()
APP_ENV = "/data/coolify/applications/app1/.env"
SVC_ENV = "/data/coolify/services/svc1/.env"
DB_COMPOSE = "/data/coolify/databases/db1/docker-compose.yml"


def row(
    key: str, value: object = _ABSENT, *, version: str | None = "4.5.0", **kw: Any
) -> dict[str, Any]:
    """An env row as the API returns it: locked rows have NO value key at all."""
    env: dict[str, Any] = {
        "key": key,
        "is_literal": False,
        "is_multiline": False,
        "is_preview": False,
        "is_runtime": True,
        "is_shared": False,
        "is_shown_once": value is _ABSENT,
        **kw,
    }
    if version is not None:
        env["version"] = version
    if value is not _ABSENT:
        env["value"] = value
        if isinstance(value, str):
            env["real_value"] = (
                le.legacy_format(env, value) if le.saved_before_exact_escaping(env) else value
            )
    return env


def dotenv(lines: list[tuple[str, str]]) -> str:
    return "\n".join(f"{k}={raw}" for k, raw in lines)


def compose_file(uuid: str, lines: list[tuple[str, str]]) -> str:
    return yaml.safe_dump({"services": {uuid: {"environment": [f"{k}={v}" for k, v in lines]}}})


def source(path: str, text: str, kind: FileKind = FileKind.DOTENV) -> SourceConfig:
    return SourceConfig(path=path, kind=kind, text=text)


def written(env: dict[str, Any], value: str, kind: FileKind = FileKind.DOTENV) -> str:
    """What Coolify writes after KEY= for this row — the module's own port."""
    return le.encode(env, value, kind)


def recover_service(envs: list[dict[str, Any]], text: str, **kw: Any) -> le.LockedRecovery:
    return le.recover(envs, source(SVC_ENV, text), collection="services", uuid="svc1", **kw)


# ── where Coolify writes ─────────────────────────────────────────────────────


class TestConfigPath:
    @pytest.mark.parametrize(
        ("collection", "path", "kind"),
        [
            ("applications", "/data/coolify/applications/abc123/.env", FileKind.DOTENV),
            ("services", "/data/coolify/services/abc123/.env", FileKind.DOTENV),
            ("databases", "/data/coolify/databases/abc123/docker-compose.yml", FileKind.COMPOSE),
        ],
    )
    def test_per_collection(self, collection: str, path: str, kind: FileKind) -> None:
        assert le.config_path(collection, "abc123") == (path, kind)

    @pytest.mark.parametrize("uuid", ["../etc", "a-b", "a b", "", "x/y", "a;rm"])
    def test_rejects_anything_but_a_plain_uuid(self, uuid: str) -> None:
        """The uuid goes into a path that is cat'ed as root: path traversal is out."""
        with pytest.raises(ValueError):
            le.config_path("services", uuid)

    def test_unknown_collection(self) -> None:
        with pytest.raises(ValueError):
            le.config_path("servers", "abc")


# ── the encoders, ported ─────────────────────────────────────────────────────


class TestEncoders:
    def test_escape_env_variables(self) -> None:
        assert (
            le.escape_env_variables("a\\b\"c'd\te\rf\0g\nh$") == "a\\\\b\\\"c\\'d\\te\\rf\\0g\nh$"
        )

    def test_escape_compose_env_file_value(self) -> None:
        assert le.escape_compose_env_file_value('p "q" $H \\x', True) == '"p \\"q\\" $H \\\\x"'
        assert le.escape_compose_env_file_value("a$b", False) == '"a\\$b"'

    def test_escape_dollar_sign(self) -> None:
        assert le.escape_dollar_sign("a$b$$") == "a$$b$$$$"

    def test_legacy_format_by_shape(self) -> None:
        assert le.legacy_format(row("K", "x"), '{"a": 1}') == '{"a": 1}'
        assert le.legacy_format(row("K", "x", is_literal=True), "v") == "'v'"
        assert le.legacy_format(row("K", "x", is_multiline=True), "a\nb") == "'a\nb'"
        assert le.legacy_format(row("K", "x"), "it's") == "it\\'s"

    @pytest.mark.parametrize(
        ("value", "flags", "allowed"),
        [
            ("plain $X", {}, True),
            ("plain", {"is_literal": True}, False),
            ("plain", {"is_multiline": True}, False),
            ('{"a": "$X"}', {}, False),
            (' {"a": 1}', {}, False),  # PHP ltrims before looking at the first char
            ("{not json", {}, True),
        ],
    )
    def test_allows_interpolation(self, value: str, flags: dict[str, bool], allowed: bool) -> None:
        assert le.allows_interpolation(row("K", "x", **flags), value) is allowed

    @pytest.mark.parametrize("value", ["NaN", "[Infinity]", '{"a": -Infinity}', ""])
    def test_is_json_is_php_json_validate(self, value: str) -> None:
        """Python's json takes NaN/Infinity; PHP's json_validate does not."""
        assert not le.is_json(value)

    def test_is_json_accepts_json(self) -> None:
        assert le.is_json('{"a": [1, 2.5, null, true]}')


# ── golden lines ─────────────────────────────────────────────────────────────


class TestGoldenLines:
    def test_exact_dotenv(self) -> None:
        assert le.decode('"p@ss \\"q\\" $HOME \\\\x"', row("K", "x"), FileKind.DOTENV) == (
            'p@ss "q" $HOME \\x'
        )

    def test_exact_literal_escapes_the_dollar(self) -> None:
        env = row("K", "x", is_literal=True)
        assert le.decode('"a\\$b"', env, FileKind.DOTENV) == "a$b"
        # An unescaped $ is not what Coolify writes for a literal: refused.
        assert le.decode('"a$b"', env, FileKind.DOTENV) is None

    def test_legacy_plain(self) -> None:
        assert le.decode("it\\'s", row("K", "x", version="4.4.7"), FileKind.DOTENV) == "it's"

    def test_legacy_literal_strips_one_pair_of_quotes(self) -> None:
        env = row("K", "x", version=None, is_literal=True)
        assert le.decode("'v'", env, FileKind.DOTENV) == "v"

    def test_database_legacy_literal_keeps_its_quotes_in_the_file(self) -> None:
        env = row("K", "x", version="4.3.10", is_literal=True)
        assert le.decode("'v'", env, FileKind.COMPOSE) == "v"

    def test_database_exact_non_interpolating(self) -> None:
        assert le.decode("a$$b", row("K", "x", is_literal=True), FileKind.COMPOSE) == "a$b"

    def test_database_exact_interpolating(self) -> None:
        assert le.decode("a$b", row("K", "x"), FileKind.COMPOSE) == "a$b"

    def test_dollar_references_are_not_expanded(self) -> None:
        """The file keeps ${SERVICE_FQDN_X}; the container would show the SOURCE host."""
        envs = [row("API_URL", is_shown_once=True)]
        text = dotenv([("API_URL", '"${SERVICE_FQDN_APP}/api"')])
        rec = recover_service(envs, text)
        assert rec.recovered.get("API_URL") == "${SERVICE_FQDN_APP}/api"

    @pytest.mark.parametrize("raw", ["plain", '"a\\xb"', '"a"b"', '"a\\"', '"'])
    def test_malformed_exact_dotenv_lines_are_refused(self, raw: str) -> None:
        assert le.decode(raw, row("K", "x"), FileKind.DOTENV) is None

    def test_a_dangling_legacy_backslash_is_refused(self) -> None:
        assert le.decode("abc\\", row("K", "x", version="4.4.7"), FileKind.DOTENV) is None
        assert le.decode("a\\qb", row("K", "x", version="4.4.7"), FileKind.DOTENV) is None


class TestSecretManagerLinesFailClosed:
    def test_single_quoted_dotenv_on_a_legacy_plain_row(self) -> None:
        envs = [row("TOKEN", version="4.4.7")]
        rec = recover_service(envs, dotenv([("TOKEN", f"'{SENTINEL}'")]))
        assert "TOKEN" not in rec.recovered
        assert "not in the encoding" in rec.unrecoverable["TOKEN"]

    def test_double_dollar_dotenv_on_an_exact_literal_row(self) -> None:
        envs = [row("TOKEN", is_literal=True)]
        rec = recover_service(envs, dotenv([("TOKEN", '"it\'s $$x"')]))
        assert "TOKEN" not in rec.recovered
        assert "TOKEN" in rec.unrecoverable


# ── per-row rules ────────────────────────────────────────────────────────────


class TestRules:
    def test_a_service_secret_is_recovered(self) -> None:
        envs = [row("SERVICE_PASSWORD_PG")]
        rec = recover_service(envs, dotenv([("SERVICE_PASSWORD_PG", f'"{SENTINEL}"')]))
        assert rec.recovered.get("SERVICE_PASSWORD_PG") == SENTINEL
        assert rec.unrecoverable == {}

    def test_shared_reference_is_not_recovered(self) -> None:
        envs = [row("DB_URL", is_shared=True)]
        rec = recover_service(envs, dotenv([("DB_URL", '"resolved"')]))
        assert rec.unrecoverable["DB_URL"] == le.SHARED_REFERENCE
        assert "unlock it on the source" in le.SHARED_REFERENCE

    def test_service_fqdn_and_url_are_left_to_the_target(self) -> None:
        envs = [row("SERVICE_FQDN_APP"), row("SERVICE_URL_APP")]
        rec = recover_service(envs, dotenv([("SERVICE_FQDN_APP", '"old.example.com"')]))
        assert rec.left_to_target == ("SERVICE_FQDN_APP", "SERVICE_URL_APP")
        assert len(rec.recovered) == 0 and rec.unrecoverable == {}

    def test_build_time_only_and_buildpack_control(self) -> None:
        envs = [row("NPM_TOKEN", is_runtime=False), row("NIXPACKS_NODE_VERSION"), row("RAILPACKX")]
        rec = le.recover(envs, source(APP_ENV, ""), collection="applications", uuid="app1")
        assert rec.unrecoverable["NPM_TOKEN"] == le.BUILD_TIME_ONLY
        assert rec.unrecoverable["NIXPACKS_NODE_VERSION"] == le.BUILD_TIME_ONLY
        # SQL LIKE 'RAILPACK_%': the underscore is a wildcard, so RAILPACKX matches.
        assert rec.unrecoverable["RAILPACKX"] == le.BUILD_TIME_ONLY

    def test_runtime_buildpack_control_keys_reach_the_file_from_4_5(self) -> None:
        envs = [row("NIXPACKS_PHP_ROOT_DIR")]
        text = dotenv([("NIXPACKS_PHP_ROOT_DIR", '"/app/public"'), ("HOST", "0.0.0.0")])
        rec = le.recover(
            envs, source(APP_ENV, text), collection="applications", uuid="app1", version=V45
        )
        assert rec.recovered.get("NIXPACKS_PHP_ROOT_DIR") == "/app/public"
        old = le.recover(
            envs, source(APP_ENV, text), collection="applications", uuid="app1", version=(4, 4, 7)
        )
        assert old.unrecoverable["NIXPACKS_PHP_ROOT_DIR"] == le.BUILD_TIME_ONLY

    def test_locked_preview_rows_are_listed_not_recovered(self) -> None:
        envs = [row("TOKEN"), row("TOKEN", is_preview=True)]
        rec = le.recover(
            envs,
            source(APP_ENV, dotenv([("TOKEN", '"prod"')])),
            collection="applications",
            uuid="app1",
        )
        assert rec.recovered.get("TOKEN") == "prod"
        assert rec.previews == ("TOKEN",)

    def test_an_unavailable_file_names_its_reason(self) -> None:
        cfg = SourceConfig(path=SVC_ENV, kind=FileKind.DOTENV, unavailable=f"{SVC_ENV} is missing")
        rec = le.recover([row("SERVICE_PASSWORD_PG")], cfg, collection="services", uuid="svc1")
        assert rec.unrecoverable["SERVICE_PASSWORD_PG"] == f"{SVC_ENV} is missing"

    def test_a_key_missing_from_the_file(self) -> None:
        rec = recover_service([row("TOKEN")], dotenv([("OTHER", '"x"')]) + "\n")
        assert rec.unrecoverable["TOKEN"] == f"not in {SVC_ENV}"

    def test_a_duplicate_key_is_ambiguous(self) -> None:
        rec = recover_service([row("TOKEN")], dotenv([("TOKEN", '"a"'), ("TOKEN", '"b"')]))
        assert "ambiguous" in rec.unrecoverable["TOKEN"]

    def test_an_exact_multiline_value_shields_a_key_shaped_line(self) -> None:
        """Inside its double quotes, 'OTHER=evil' is part of the value, not a record."""
        value = "line1\nOTHER=evil\n'x'"
        envs = [row("CERT", is_multiline=True), row("OTHER", "fine")]
        text = dotenv([("CERT", written(envs[0], value)), ("OTHER", '"fine"')])
        rec = recover_service(envs, text)
        assert rec.recovered.get("CERT") == value

    def test_a_spilled_legacy_line_makes_open_records_untrustworthy(self) -> None:
        """A legacy plain value with a raw newline truncates at the line end.

        The round trip alone would accept the truncation ("x" re-encodes to "x");
        the leftover line is what gives it away.
        """
        envs = [row("TOKEN", version="4.4.7"), row("SAFE")]
        text = dotenv([("TOKEN", "x\nnot a record"), ("SAFE", '"kept"')])
        rec = recover_service(envs, text)
        assert "cannot be delimited exactly" in rec.unrecoverable["TOKEN"]
        # A closed double-quoted record is delimited exactly regardless.
        assert rec.recovered.get("SAFE") == "kept"

    def test_a_record_under_a_key_coolify_never_writes_is_an_anomaly(self) -> None:
        envs = [row("TOKEN", version="4.4.7")]
        text = dotenv([("TOKEN", "x"), ("FOO", "bar")])
        rec = recover_service(envs, text)
        assert "cannot be delimited exactly" in rec.unrecoverable["TOKEN"]

    def test_generated_service_name_lines_are_expected(self) -> None:
        envs = [row("TOKEN", version="4.4.7")]
        text = dotenv([("SERVICE_NAME_DB", "db"), ("TOKEN", "x")])
        rec = recover_service(envs, text, compose="services:\n  db:\n    image: x\n")
        assert rec.recovered.get("TOKEN") == "x"
        # With the compose known, only ITS service names are expected.
        stray = recover_service(
            envs,
            dotenv([("SERVICE_NAME_OTHER", "o"), ("TOKEN", "x")]),
            compose="services:\n  db:\n    image: x\n",
        )
        assert "TOKEN" in stray.unrecoverable

    @pytest.mark.parametrize("compose", ["services: [", "x: 1\n", None])
    def test_an_unknown_compose_falls_back_to_the_prefix(self, compose: str | None) -> None:
        envs = [row("TOKEN", version="4.4.7")]
        text = dotenv([("SERVICE_NAME_ANY", "any"), ("TOKEN", "x")])
        assert recover_service(envs, text, compose=compose).recovered.get("TOKEN") == "x"

    def test_a_config_without_text_or_reason_was_not_read(self) -> None:
        cfg = SourceConfig(path=SVC_ENV, kind=FileKind.DOTENV)
        rec = le.recover([row("TOKEN")], cfg, collection="services", uuid="svc1")
        assert rec.unrecoverable["TOKEN"] == f"{SVC_ENV} was not read"

    def test_an_unterminated_double_quote_is_an_anomaly(self) -> None:
        envs = [row("TOKEN", version="4.4.7"), row("OPEN")]
        rec = recover_service(envs, dotenv([("TOKEN", "x"), ("OPEN", '"never closed')]))
        assert "TOKEN" in rec.unrecoverable and "OPEN" in rec.unrecoverable

    def test_junk_after_a_closing_quote_is_an_anomaly(self) -> None:
        envs = [row("TOKEN", version="4.4.7"), row("ODD")]
        rec = recover_service(envs, dotenv([("TOKEN", "x"), ("ODD", '"a"junk')]))
        assert "TOKEN" in rec.unrecoverable and "ODD" in rec.unrecoverable

    def test_an_unterminated_single_quote_is_an_anomaly(self) -> None:
        envs = [row("TOKEN", version="4.4.7"), row("LIT", version="4.4.7", is_literal=True)]
        rec = recover_service(envs, dotenv([("TOKEN", "x"), ("LIT", "'open")]))
        assert "TOKEN" in rec.unrecoverable


class TestPreviewMarker:
    def test_container_name_marks_a_preview_env(self) -> None:
        envs = [row("TOKEN")]
        text = dotenv([("COOLIFY_CONTAINER_NAME", '"abc-pr-3"'), ("TOKEN", '"preview-value"')])
        rec = le.recover(envs, source(APP_ENV, text), collection="applications", uuid="app1")
        assert "pull request #3" in rec.unrecoverable["TOKEN"]
        assert len(rec.recovered) == 0

    def test_service_name_marks_a_compose_preview(self) -> None:
        envs = [row("TOKEN")]
        text = dotenv([("SERVICE_NAME_WEB", "web-pr-12"), ("TOKEN", '"x"')])
        rec = le.recover(
            envs,
            source(APP_ENV, text),
            collection="applications",
            uuid="app1",
            compose_app=True,
            compose="services:\n  web:\n    image: x\n",
        )
        assert "pull request #12" in rec.unrecoverable["TOKEN"]

    def test_a_production_container_name_is_not_a_marker(self) -> None:
        envs = [row("TOKEN")]
        text = dotenv([("COOLIFY_CONTAINER_NAME", '"abc-20261027T101010"'), ("TOKEN", '"x"')])
        rec = le.recover(envs, source(APP_ENV, text), collection="applications", uuid="app1")
        assert rec.recovered.get("TOKEN") == "x"

    def test_services_ignore_the_marker(self) -> None:
        envs = [row("TOKEN")]
        rec = recover_service(envs, dotenv([("SERVICE_NAME_WEB", "web-pr-3"), ("TOKEN", '"x"')]))
        assert rec.recovered.get("TOKEN") == "x"


class TestFreshness:
    """The file must still describe the resource: its unlocked rows tell."""

    def test_an_unlocked_row_that_changed_since_the_last_write(self) -> None:
        envs = [row("TOKEN"), row("MODE", "new")]
        rec = recover_service(envs, dotenv([("TOKEN", '"x"'), ("MODE", '"old"')]))
        assert "does not match the resource's current configuration" in rec.unrecoverable["TOKEN"]

    def test_an_unlocked_row_added_since_the_last_write(self) -> None:
        envs = [row("TOKEN"), row("ADDED", "v")]
        rec = recover_service(envs, dotenv([("TOKEN", '"x"')]))
        assert "does not match" in rec.unrecoverable["TOKEN"]

    def test_the_legacy_expectation_is_the_apis_real_value(self) -> None:
        envs = [row("TOKEN", version="4.4.7"), row("MODE", "it's", version="4.4.7")]
        assert envs[1]["real_value"] == "it\\'s"
        rec = recover_service(envs, dotenv([("TOKEN", "x"), ("MODE", "it\\'s")]))
        assert rec.recovered.get("TOKEN") == "x"

    def test_the_exact_expectation_is_escaped_from_real_value(self) -> None:
        envs = [row("TOKEN"), row("PRICE", "a$b", is_literal=True)]
        rec = recover_service(envs, dotenv([("TOKEN", '"x"'), ("PRICE", '"a\\$b"')]))
        assert rec.recovered.get("TOKEN") == "x"

    def test_blank_rows_may_be_left_out_but_not_filled_in(self) -> None:
        envs = [row("TOKEN"), row("EMPTY", "")]
        ok = recover_service(envs, dotenv([("TOKEN", '"x"')]))
        assert ok.recovered.get("TOKEN") == "x"
        bad = recover_service(envs, dotenv([("TOKEN", '"x"'), ("EMPTY", '"surprise"')]))
        assert "does not match" in bad.unrecoverable["TOKEN"]

    def test_a_row_without_real_value_cannot_be_predicted_and_is_skipped(self) -> None:
        envs = [row("TOKEN"), {**row("MODE", "x"), "real_value": None}]
        rec = recover_service(envs, dotenv([("TOKEN", '"t"'), ("MODE", '"other"')]))
        assert rec.recovered.get("TOKEN") == "t"

    def test_secret_manager_rows_cannot_be_predicted_and_are_skipped(self) -> None:
        envs = [row("TOKEN"), row("VAULTED", "{{vault.X}}")]
        rec = recover_service(envs, dotenv([("TOKEN", '"x"'), ("VAULTED", "'resolved'")]))
        assert rec.recovered.get("TOKEN") == "x"

    def test_database_compose_is_checked_too(self) -> None:
        envs = [row("POSTGRES_PASSWORD"), row("POSTGRES_USER", "new")]
        text = compose_file("db1", [("POSTGRES_PASSWORD", "x"), ("POSTGRES_USER", "old")])
        rec = le.recover(
            envs, source(DB_COMPOSE, text, FileKind.COMPOSE), collection="databases", uuid="db1"
        )
        assert "does not match" in rec.unrecoverable["POSTGRES_PASSWORD"]


class TestComposeFiles:
    def test_list_form(self) -> None:
        envs = [row("POSTGRES_PASSWORD"), row("POSTGRES_USER", "shop")]
        text = compose_file(
            "db1", [("POSTGRES_USER", "shop"), ("POSTGRES_PASSWORD", f"p{SENTINEL}")]
        )
        rec = le.recover(
            envs, source(DB_COMPOSE, text, FileKind.COMPOSE), collection="databases", uuid="db1"
        )
        assert rec.recovered.get("POSTGRES_PASSWORD") == f"p{SENTINEL}"

    def test_mapping_form(self) -> None:
        text = yaml.safe_dump({"services": {"db1": {"environment": {"K": "v"}}}})
        rec = le.recover(
            [row("K")],
            source(DB_COMPOSE, text, FileKind.COMPOSE),
            collection="databases",
            uuid="db1",
        )
        assert rec.recovered.get("K") == "v"

    @pytest.mark.parametrize(
        "text",
        [
            "services: [",
            "services:\n  other:\n    image: x\n",
            yaml.safe_dump({"services": {"db1": {"environment": ["NOEQUALS"]}}}),
            yaml.safe_dump({"services": {"db1": {"environment": {"K": 5}}}}),
        ],
    )
    def test_a_shape_start_never_writes_is_unparseable(self, text: str) -> None:
        rec = le.recover(
            [row("K")],
            source(DB_COMPOSE, text, FileKind.COMPOSE),
            collection="databases",
            uuid="db1",
        )
        assert rec.unrecoverable["K"] == f"{DB_COMPOSE} could not be parsed"


class TestSingleCopy:
    def test_two_equal_reads(self) -> None:
        assert le.single_copy("A=1\n\0A=1\n") == "A=1\n"

    def test_a_nul_inside_the_file_is_fine(self) -> None:
        text = 'K="a\0b"'
        assert le.single_copy(f"{text}\0{text}") == text

    @pytest.mark.parametrize("stdout", ["A=1\0A=2", "A=1\0A=1x", "", "A=1A=1x"])
    def test_reads_that_differ_are_refused(self, stdout: str) -> None:
        assert le.single_copy(stdout) is None


# ── Redis: the one API path ──────────────────────────────────────────────────


class TestRedisPasswordFromUrl:
    @pytest.mark.parametrize(
        ("url", "password"),
        [
            ("redis://pw@abc:6379/0", "pw"),  # redis < 6: no username part
            ("redis://default:pw@abc:6379/0", "pw"),
            ("rediss://default:pw@abc:6380/0?cacert=/etc/ssl/certs/coolify-ca.crt", "pw"),
            ("redis://default:a%2Bb%40c%3Ad%25e%20f@abc:6379/0", "a+b@c:d%e f"),
            ("redis://us%3Aer:p%3Aw@abc:6379/0", "p:w"),
        ],
    )
    def test_reads_the_rawurlencoded_password(self, url: str, password: str) -> None:
        assert le.redis_password_from_url(url) == password

    @pytest.mark.parametrize(
        "url",
        [
            "redis://default:@abc:6379/0",
            "redis://default:{{vault.REDIS}}@abc:6379/0",
            None,
            5,
            "redis://abc:6379",
            "redis://default:pw@[::1:6379/0",  # urlsplit refuses it
        ],
    )
    def test_no_password_is_none(self, url: object) -> None:
        assert le.redis_password_from_url(url) is None


class TestRedisRecovery:
    URL = "redis://default:api%24pw@db1:6379/0"

    def _recover(self, text: str | None, *, url: str | None = URL) -> le.LockedRecovery:
        cfg = (
            SourceConfig(path=DB_COMPOSE, kind=FileKind.COMPOSE, text=text)
            if text is not None
            else SourceConfig(path=DB_COMPOSE, kind=FileKind.COMPOSE, unavailable="gone")
        )
        return le.recover(
            [row("REDIS_PASSWORD")],
            cfg,
            collection="databases",
            uuid="db1",
            engine=DatabaseEngine.REDIS,
            internal_db_url=url,
        )

    def test_the_api_url_is_primary_and_needs_no_file(self) -> None:
        rec = self._recover(None)
        assert rec.recovered.get("REDIS_PASSWORD") == "api$pw"
        assert rec.from_api == ("REDIS_PASSWORD",)

    def test_an_agreeing_file_is_fine(self) -> None:
        rec = self._recover(compose_file("db1", [("REDIS_PASSWORD", "api$pw")]))
        assert rec.recovered.get("REDIS_PASSWORD") == "api$pw"

    def test_a_disagreeing_file_refuses_to_pick(self) -> None:
        rec = self._recover(compose_file("db1", [("REDIS_PASSWORD", "other")]))
        assert "disagree" in rec.unrecoverable["REDIS_PASSWORD"]

    def test_without_the_url_the_file_answers(self) -> None:
        rec = self._recover(compose_file("db1", [("REDIS_PASSWORD", "file$pw")]), url=None)
        assert rec.recovered.get("REDIS_PASSWORD") == "file$pw"
        assert rec.from_api == ()

    def test_keydb_does_not_read_the_url(self) -> None:
        rec = le.recover(
            [row("REDIS_PASSWORD")],
            SourceConfig(path=DB_COMPOSE, kind=FileKind.COMPOSE, unavailable="gone"),
            collection="databases",
            uuid="db1",
            engine=DatabaseEngine.KEYDB,
            internal_db_url=self.URL,
        )
        assert "REDIS_PASSWORD" in rec.unrecoverable


# ── what the plan says ───────────────────────────────────────────────────────


class TestNamesSourceServer:
    WILDCARD = "https://*.app.0046-20.cloud.bauer-group.com"

    @pytest.mark.parametrize(
        "value",
        [
            "https://api.app.0046-20.cloud.bauer-group.com/hook",
            "postgres://u:p@10.0.0.1:5432/db",
            "http://shop.10-0-0-1.sslip.io",
        ],
    )
    def test_hits(self, value: str) -> None:
        assert le.names_source_server(value, wildcard=self.WILDCARD, ip="10.0.0.1")

    @pytest.mark.parametrize(
        "value",
        ["https://shop.example.com", "110.0.0.12", "notapp.0046-20.cloud.bauer-group.com.evil"],
    )
    def test_misses(self, value: str) -> None:
        assert not le.names_source_server(value, wildcard=self.WILDCARD, ip="10.0.0.1")

    def test_ipv6(self) -> None:
        assert le.names_source_server("http://[2001:db8::1]:80", wildcard=None, ip="2001:db8::1")

    def test_no_wildcard_and_no_ip_matches_nothing(self) -> None:
        assert not le.names_source_server("https://10.0.0.1/x", wildcard=None, ip=None)


class TestPlanLines:
    def _recovery(self) -> le.LockedRecovery:
        return le.LockedRecovery(
            recovered=le.Secrets(
                {
                    "API_SECRET": f"https://x.app.0046-20.cloud.bauer-group.com/{SENTINEL}",
                    "REDIS_PASSWORD": SENTINEL,
                }
            ),
            from_api=("REDIS_PASSWORD",),
            left_to_target=("SERVICE_FQDN_APP",),
        )

    def test_lines_name_keys_and_path_never_values_or_hosts(self) -> None:
        rec = self._recovery()
        hits = le.source_server_keys(
            rec, wildcard="*.app.0046-20.cloud.bauer-group.com", ip="10.0.0.1"
        )
        assert hits == ("API_SECRET",)
        lines = le.recovered_lines(rec, path=SVC_ENV, source_server_hits=hits)
        text = "\n".join(lines)
        assert "['API_SECRET'] will be read from /data/coolify/services/svc1/.env" in text
        assert "the value Coolify last wrote on the source server" in text
        assert "connection URL" in text and "['REDIS_PASSWORD']" in text
        assert (
            "the recovered value of API_SECRET names the source server's wildcard domain or IP"
            in text
        )
        assert "['SERVICE_FQDN_APP'] are generated by the target" in text
        assert SENTINEL not in text
        assert "x.app.0046-20" not in text, "a host inside a locked value is a fragment of it"


class TestMergedView:
    def test_fills_recovered_production_rows_only(self) -> None:
        envs = [row("A"), row("A", is_preview=True), row("B"), row("C", "plain")]
        rec = le.LockedRecovery(recovered=le.Secrets({"A": "va"}), unrecoverable={"B": "why"})
        view = le.merged_view(envs, rec)
        assert view[0]["value"] == "va"
        assert "value" not in view[1] and "value" not in view[2]
        assert view[3]["value"] == "plain"
        assert "value" not in envs[0], "the input is not mutated"


# ── nothing ever leaks ───────────────────────────────────────────────────────


class TestNoValuesInText:
    def test_secrets_repr_and_str(self) -> None:
        secrets = le.Secrets({"TOKEN": SENTINEL})
        assert SENTINEL not in repr(secrets) and SENTINEL not in str(secrets)
        assert repr(secrets) == "Secrets(keys=['TOKEN'])"
        assert secrets.get("TOKEN") == SENTINEL and "TOKEN" in secrets and len(secrets) == 1

    def test_source_config_repr_hides_the_text(self) -> None:
        cfg = SourceConfig(path=SVC_ENV, kind=FileKind.DOTENV, text=f"TOKEN={SENTINEL}")
        assert SENTINEL not in repr(cfg)

    def test_recovery_and_reasons_never_carry_a_value(self) -> None:
        envs = [
            row("GOOD"),
            row("SHARED", is_shared=True),
            row("BAD", is_literal=True),
            row("STALE_SIBLING", f"new{SENTINEL}"),
        ]
        text = dotenv(
            [
                ("GOOD", f'"{SENTINEL}"'),
                ("SHARED", f'"{SENTINEL}"'),
                ("BAD", f'"{SENTINEL}$"'),
                ("STALE_SIBLING", f'"old{SENTINEL}"'),
            ]
        )
        for rec in (recover_service(envs, text), recover_service(envs[:3], text)):
            blob = (
                repr(rec)
                + str(rec)
                + " ".join(rec.unrecoverable.values())
                + " ".join(rec.unrecoverable)
            )
            assert SENTINEL not in blob


# ── fidelity, fed the recovery ───────────────────────────────────────────────


class TestFidelityWithRecovery:
    def test_a_recovered_regenerated_secret_no_longer_refuses(self) -> None:
        envs = [row("SERVICE_PASSWORD_PG")]
        rec = le.LockedRecovery(recovered=le.Secrets({"SERVICE_PASSWORD_PG": "pw"}))
        view = le.merged_view(envs, rec)
        assert (
            fidelity.env_refusals(
                view, collection="services", engine=None, version=V45, unrecovered={}
            )
            == []
        )

    def test_an_unrecovered_one_refuses_with_its_reason(self) -> None:
        envs = [row("SERVICE_PASSWORD_PG")]
        out = fidelity.env_refusals(
            envs,
            collection="services",
            engine=None,
            version=V45,
            unrecovered={"SERVICE_PASSWORD_PG": f"not in {SVC_ENV}"},
        )
        assert len(out) == 1
        assert f"['SERVICE_PASSWORD_PG'] (SERVICE_PASSWORD_PG: not in {SVC_ENV})" in out[0]
        assert "could not be read back from the source server" in out[0]
        assert "Fix the cause above, or delete and re-add them unlocked" in out[0]

    def test_a_compose_application_regenerates_its_service_secrets(self) -> None:
        """applicationParser firstOrCreates SERVICE_PASSWORD_* like a service does."""
        envs = [row("SERVICE_PASSWORD_DB")]
        refused = fidelity.env_refusals(
            envs, collection="applications", engine=None, version=V45, compose_app=True
        )
        assert len(refused) == 1
        plain = fidelity.env_refusals(envs, collection="applications", engine=None, version=V45)
        assert plain == []
        assert fidelity.target_regenerates(
            "SERVICE_PASSWORD_DB", collection="applications", engine=None, compose_app=True
        )

    def test_recovered_values_feed_the_legacy_and_redis_warnings(self) -> None:
        envs = [row("REDIS_PASSWORD", version="4.4.7")]
        rec = le.LockedRecovery(recovered=le.Secrets({"REDIS_PASSWORD": f"pa$s;{SENTINEL}"}))
        out = fidelity.env_warnings(
            le.merged_view(envs, rec),
            collection="databases",
            engine=DatabaseEngine.REDIS,
            compose=None,
            version=V45,
            unrecovered={},
        )
        joined = "\n".join(out)
        assert "['REDIS_PASSWORD'] were saved before Coolify 4.5" in joined
        assert "--requirepass unquoted" in joined
        assert SENTINEL not in joined

    def test_other_unrecovered_keys_warn_with_their_reason(self) -> None:
        envs = [row("STRIPE_SECRET")]
        out = fidelity.env_warnings(
            envs,
            collection="applications",
            engine=None,
            compose=None,
            version=V45,
            unrecovered={"STRIPE_SECRET": "not in x"},
        )
        assert "['STRIPE_SECRET'] (STRIPE_SECRET: not in x) could not be read back" in out[0]
        assert "WITHOUT them" in out[0]

    def test_build_time_only_on_a_building_app_says_the_build_will_fail(self) -> None:
        envs = [row("NPM_TOKEN", is_runtime=False)]
        unrecovered = {"NPM_TOKEN": le.BUILD_TIME_ONLY}
        building = fidelity.env_warnings(
            envs,
            collection="applications",
            engine=None,
            compose=None,
            version=V45,
            unrecovered=unrecovered,
            builds=True,
        )
        assert any("AFTER the source is stopped" in w and "rolls back" in w for w in building)
        image = fidelity.env_warnings(
            envs,
            collection="applications",
            engine=None,
            compose=None,
            version=V45,
            unrecovered=unrecovered,
            builds=False,
        )
        assert not any("AFTER the source is stopped" in w for w in image)

    def test_locked_previews_get_their_own_warning(self) -> None:
        envs = [row("TOKEN", is_preview=True)]
        out = fidelity.env_warnings(
            envs, collection="applications", engine=None, compose=None, version=V45
        )
        assert out == [
            f"locked preview variable(s) ['TOKEN'] are not carried ({le.PREVIEW_ONLY}): the "
            "target's pull-request deployments run without them until you set them by hand."
        ]
        assert (
            fidelity.env_refusals(envs, collection="applications", engine=None, version=V45) == []
        )


# ── the property: exact, or nothing ──────────────────────────────────────────

_FRAGMENTS = [
    "a",
    "Z",
    "é",
    "€",
    "\\",
    '"',
    "'",
    "$",
    "${X}",
    "$HOME",
    " ",
    " #",
    "#",
    "=",
    "{",
    "}",
    "[",
    "]",
    ":",
    ",",
    "\n",
    "\r",
    "\t",
    "\0",
    "\nFOO=bar",
    "'\nA=b",
    "\nA=x",
    "\nK=dup",
    '\nB="q"',
    '{\n  "u": "a=b"\n}',
    '{"a": "$b"}',
    "[1, 2]",
    "it's",
    "\\'",
    '\\"',
    "$$",
    "'",
    "\\0",
    "\\n",
]


@st.composite
def _cases(draw: st.DrawFn) -> tuple[str, dict[str, Any], str, FileKind, bool]:
    value = "".join(draw(st.lists(st.sampled_from(_FRAGMENTS), min_size=1, max_size=8)))
    value = value.strip(" \t\n\r\0\x0b")
    flags = {
        "is_literal": draw(st.booleans()),
        "is_multiline": draw(st.booleans()),
    }
    exact = draw(st.booleans())
    collection = draw(st.sampled_from(["applications", "services", "databases"]))
    kind = FileKind.COMPOSE if collection == "databases" else FileKind.DOTENV
    return value, flags, collection, kind, exact


@settings(max_examples=400, deadline=None)
@given(_cases())
def test_round_trip_property_exact_or_nothing(
    case: tuple[str, dict[str, Any], str, FileKind, bool],
) -> None:
    value, flags, collection, kind, exact = case
    assume(value)
    version = "4.5.0" if exact else "4.4.7"
    target = row("K", version=version, **flags)
    a = row("A", "plain-a")
    b = row("B", "b$1 'x'", version="4.3.10")
    lines = [
        ("A", written(a, "plain-a", kind)),
        ("K", written(target, value, kind)),
        ("B", written(b, "b$1 'x'", kind)),
    ]
    if collection == "applications":
        lines = [
            ("COOLIFY_CONTAINER_NAME", '"app1-20261027T101010"'),
            *lines,
            ("PORT", "80"),
            ("HOST", "0.0.0.0"),
        ]
    if kind is FileKind.COMPOSE:
        cfg = source(DB_COMPOSE, compose_file("db1", lines), kind)
        uuid = "db1"
    else:
        cfg = source(APP_ENV if collection == "applications" else SVC_ENV, dotenv(lines))
        uuid = "app1" if collection == "applications" else "svc1"

    rec = le.recover([a, target, b], cfg, collection=collection, uuid=uuid, version=V45)

    if "K" in rec.recovered:
        assert rec.recovered.get("K") == value, "recovered a DIFFERENT value"
    else:
        assert "K" in rec.unrecoverable

    raw = written(target, value, kind)
    delimited = (
        kind is FileKind.COMPOSE
        or raw[0] == '"'  # exact: every inner quote is escaped
        or "\n" not in raw
        or (raw[0] == "'" and "'\n" not in raw[1:])
    )
    if delimited:
        assert rec.recovered.get("K") == value, (
            f"failed to recover a cleanly written value: {rec.unrecoverable}"
        )
