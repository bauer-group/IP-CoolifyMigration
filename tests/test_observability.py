"""Tests for logging redaction and settings.

Redaction is load-bearing: this tool handles a Coolify root token, SSH private
keys, database passwords and APP_KEY. A stray debug line would put every secret
of every project into a terminal scrollback.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.observability.logging_setup import (
    _redact,
    reset_logging,
    setup_logging,
)
from bg_coolify_migrate.settings.base import Settings, get_settings, reset_settings_cache


@pytest.fixture(autouse=True)
def _clean() -> None:
    reset_logging()
    reset_settings_cache()


def redact(event: dict) -> dict:
    return _redact(None, "info", event)


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "postgres_password",
            "secret",
            "token",
            "coolify_token",
            "api_key",
            "private_key",
            "app_key",
            "authorization",
            "real_value",
            "docker_compose_raw",
            "internal_db_url",
            "external_db_url",
        ],
    )
    def test_sensitive_keys_are_redacted(self, key: str) -> None:
        assert redact({key: "hunter2"})[key] == "«redacted»"

    def test_case_insensitive(self) -> None:
        assert redact({"PASSWORD": "x"})["PASSWORD"] == "«redacted»"

    def test_substring_match(self) -> None:
        # Broad on purpose: a false positive costs a redacted debug line, a false
        # negative costs a leaked credential.
        assert redact({"db_password_hash": "x"})["db_password_hash"] == "«redacted»"

    def test_nested_dict_redacted(self) -> None:
        out = redact({"env": {"key": "A", "value": "safe", "password": "leak"}})
        assert out["env"]["password"] == "«redacted»"
        assert out["env"]["key"] == "A"

    def test_list_of_dicts_redacted(self) -> None:
        out = redact({"envs": [{"key": "A", "token": "leak"}]})
        assert out["envs"][0]["token"] == "«redacted»"

    def test_non_sensitive_survives(self) -> None:
        out = redact({"event": "api.call", "path": "/servers", "count": 3})
        assert out == {"event": "api.call", "path": "/servers", "count": 3}

    def test_deep_nesting_does_not_recurse_forever(self) -> None:
        deep: dict = {"a": {}}
        node = deep["a"]
        for _ in range(20):
            node["a"] = {}
            node = node["a"]
        redact(deep)  # must not raise


class TestSetupLogging:
    def test_is_idempotent(self) -> None:
        setup_logging(log_level="INFO")
        setup_logging(log_level="DEBUG")  # must not raise or reconfigure

    def test_force_reconfigures(self) -> None:
        setup_logging(log_level="INFO")
        setup_logging(log_level="DEBUG", force=True)

    def test_json_format(self) -> None:
        setup_logging(log_format="json", force=True)

    def test_extra_fragments_are_additive(self) -> None:
        setup_logging(extra_sensitive_fragments=("customfield",), force=True)
        out = redact({"customfield": "x", "password": "y"})
        assert out["customfield"] == "«redacted»"
        # The built-in list must still apply — extras never replace it.
        assert out["password"] == "«redacted»"

    def test_survives_stderr_being_reassigned_after_configure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: logging must not freeze the stream it was configured with.

        `PrintLoggerFactory(file=sys.stderr)` captures whatever sys.stderr is at
        configure time. Anything that later swaps the stream — a CLI test runner's
        capture buffer, an embedding host, daemonizing — then leaves EVERY
        subsequent log call in ANY module writing to a stale, usually closed,
        handle. It surfaces as `ValueError: I/O operation on closed file` from
        code that has nothing to do with logging.
        """
        import io
        import sys

        import structlog

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured)
        setup_logging(log_level="INFO", force=True)

        # Swap the stream out and close the one that was live at configure time.
        captured.close()
        replacement = io.StringIO()
        monkeypatch.setattr(sys, "stderr", replacement)

        structlog.get_logger("test").warning("still.works", detail="x")
        assert "still.works" in replacement.getvalue()


class TestNoLocalsInCrashReports:
    """A traceback must never become a dump of every variable in scope.

    The redaction processor sees the event dict; it never sees the locals Rich
    formats from the frames afterwards. structlog's default console formatter
    shows them, and the executor logs unexpected errors with ``log.exception`` —
    so a crash inside copy_envs printed a resource's env values.
    """

    SENTINEL = "s3ntinel-env-value-7f3a"

    def test_console_exception_does_not_render_locals(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import structlog

        setup_logging(log_format="console", force=True)
        log = structlog.get_logger("test")

        def copy_envs_like() -> None:
            env_value = self.SENTINEL  # noqa: F841 - the local under test
            raise RuntimeError("boom")

        try:
            copy_envs_like()
        except RuntimeError:
            log.exception("engine.unexpected")

        err = capsys.readouterr().err
        assert "engine.unexpected" in err
        assert "RuntimeError" in err, "the traceback itself must still be rendered"
        assert self.SENTINEL not in err

    def test_typer_apps_never_show_locals(self) -> None:
        from bg_coolify_migrate.cli import app, server_app

        assert app.pretty_exceptions_show_locals is False
        assert server_app.pretty_exceptions_show_locals is False

    def test_command_output_stays_out_of_repr(self) -> None:
        from bg_coolify_migrate.transfer.ssh import CommandResult

        result = CommandResult(
            command="cat -- /data/coolify/services/x/.env",
            exit_status=0,
            stdout=f"API_SECRET={self.SENTINEL}",
            stderr=self.SENTINEL,
        )
        assert self.SENTINEL not in repr(result)
        assert "cat -- /data/coolify/services/x/.env" in repr(result)


class TestSettings:
    def test_defaults(self) -> None:
        s = Settings(_env_file=None)
        assert s.transfer_parallel == 4
        assert s.disk_headroom_factor > 1.0
        assert s.trust_host_key is False

    def test_require_coolify_fails_closed(self) -> None:
        from bg_coolify_migrate.errors import PreflightError

        s = Settings(_env_file=None, coolify_url="", coolify_token="")
        with pytest.raises(PreflightError, match="COOLIFY_URL"):
            s.require_coolify()

    def test_require_coolify_returns_credentials(self) -> None:
        s = Settings(_env_file=None, coolify_url="https://c.example.com", coolify_token="tok")
        assert s.require_coolify() == ("https://c.example.com", "tok")

    def test_token_is_a_secret(self) -> None:
        # repr() must not leak it into a traceback or a log line.
        s = Settings(_env_file=None, coolify_token="hunter2")
        assert "hunter2" not in repr(s)
        assert "hunter2" not in str(s.coolify_token)

    def test_invalid_log_format_rejected(self) -> None:
        with pytest.raises(ValueError, match="log_format"):
            Settings(_env_file=None, log_format="xml")

    def test_invalid_log_level_rejected(self) -> None:
        with pytest.raises(ValueError, match="log_level"):
            Settings(_env_file=None, log_level="LOUD")

    def test_parallel_bounds_enforced(self) -> None:
        with pytest.raises(ValueError):
            Settings(_env_file=None, transfer_parallel=0)
        with pytest.raises(ValueError):
            Settings(_env_file=None, transfer_parallel=99)

    def test_headroom_cannot_be_below_one(self) -> None:
        # Requiring less free space than the payload is never correct.
        with pytest.raises(ValueError):
            Settings(_env_file=None, disk_headroom_factor=0.5)

    def test_get_settings_is_cached(self) -> None:
        # A migration reads settings from many places; re-parsing the environment
        # mid-run could produce an inconsistent view.
        assert get_settings() is get_settings()
