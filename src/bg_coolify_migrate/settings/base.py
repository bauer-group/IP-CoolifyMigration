"""Configuration.

Follows the BAUER GROUP convention: pydantic-settings composed from
single-concern mixins, env-first, no ``env_prefix`` (env names map 1:1 to
snake_case fields), and a ``model_validator`` that runs non-negotiable invariants
before anything else.

Secrets are env-only and never inline. ``COOLIFY_TOKEN`` in particular is a
root-scoped credential that can read every secret of every project in the team —
it belongs in a secret manager or a gitignored ``.env``, never in a config file
that might be committed.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.errors import PreflightError


class CoolifySettingsMixin(BaseSettings):
    """Which Coolify instance to talk to."""

    coolify_url: str = Field(
        default="",
        description="Base URL of the Coolify instance, e.g. https://coolify.example.com",
    )
    coolify_token: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "API token. MUST carry `root` or `read:sensitive` — without it Coolify "
            "silently omits secret values from responses (HTTP 200, keys absent)."
        ),
    )
    coolify_verify_tls: bool = Field(
        default=True, description="Verify the instance's TLS certificate."
    )
    coolify_timeout: float = Field(default=30.0, description="Per-request timeout in seconds.")


class TransferSettingsMixin(BaseSettings):
    """How data moves."""

    transfer_mode: TransferMode = Field(
        default=TransferMode.AUTO,
        description="direct | tunnel | auto (probe for direct, fall back to tunnel).",
    )
    transfer_parallel: int = Field(
        default=4, ge=1, le=16, description="Max concurrent rsync streams per volume."
    )
    transfer_compress: bool = Field(
        default=False,
        description="Enable rsync -z. Off by default: volume data is usually already "
        "compressed and server links are usually fast.",
    )
    transfer_bandwidth_kbps: int | None = Field(
        default=None, description="rsync --bwlimit, in KB/s. None = unlimited."
    )
    verify_parallel: int = Field(
        default=4, ge=1, le=32, description="Parallelism for sha256 manifest generation."
    )
    disk_headroom_factor: float = Field(
        default=1.2,
        ge=1.0,
        description=(
            "Required free space on the target as a multiple of the actual payload. "
            "Geczy's script checks a fixed 1 GB floor and never compares it to the "
            "size it just computed, which is how a 100 GB migration dies mid-transfer."
        ),
    )


class SshSettingsMixin(BaseSettings):
    """How we reach the servers."""

    ssh_timeout: float = Field(default=15.0, description="Connect timeout in seconds.")
    ssh_known_hosts: Path | None = Field(
        default=None, description="Our managed known_hosts. Defaults to the state dir."
    )
    trust_host_key: bool = Field(
        default=False,
        description=(
            "Record an unseen host key instead of refusing. Never disables checking — "
            "a CHANGED key is still refused."
        ),
    )
    stop_timeout: float = Field(
        default=300.0,
        ge=10.0,
        description=(
            "How long to wait for a stack to stop. Generous on purpose: a large "
            "database can take minutes to flush, and a SIGKILL at the timeout means "
            "an unflushed, torn volume."
        ),
    )
    deploy_timeout: float = Field(
        default=900.0,
        ge=10.0,
        description=(
            "How long to wait for the target to come up after start. Much longer than "
            "stop_timeout because a git-built application clones and BUILDS on the "
            "target first, which can take many minutes."
        ),
    )


class ObservabilityMixin(BaseSettings):
    """Logging and output."""

    log_level: str = Field(default="INFO")
    log_format: str = Field(default="console", description="console | json")
    state_dir: Path | None = Field(
        default=None, description="Where journals live. Defaults to the platform state dir."
    )
    no_color: bool = Field(default=False)


class Settings(
    CoolifySettingsMixin,
    TransferSettingsMixin,
    SshSettingsMixin,
    ObservabilityMixin,
):
    """The composed settings object."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        # Non-negotiable invariants first; these cannot be relaxed by a caller.
        self._validate_core()
        return self

    def _validate_core(self) -> None:
        if self.log_format not in ("console", "json"):
            raise ValueError(f"log_format must be console|json, got {self.log_format!r}")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"invalid log_level: {self.log_level!r}")

    def require_coolify(self) -> tuple[str, str]:
        """Return ``(url, token)``, failing closed if either is missing.

        Deliberately not a validator: ``coolify-migrate --help`` and the docs
        commands must work without credentials. The check belongs at the point of
        use, not at import time.
        """
        token = self.coolify_token.get_secret_value()
        missing = [
            name
            for name, value in (("COOLIFY_URL", self.coolify_url), ("COOLIFY_TOKEN", token))
            if not value
        ]
        if missing:
            raise PreflightError(
                f"missing required configuration: {', '.join(missing)}",
                hint=(
                    "Set them in the environment or a .env file:\n"
                    '  COOLIFY_URL="https://coolify.example.com"\n'
                    '  COOLIFY_TOKEN="..."   # needs root or read:sensitive\n'
                    "See .env.example."
                ),
            )
        return self.coolify_url, token

    def resolved_state_dir(self) -> Path:
        from bg_coolify_migrate.journal.store import default_state_dir

        return self.state_dir or default_state_dir()

    def resolved_known_hosts(self) -> Path:
        return self.ssh_known_hosts or (self.resolved_state_dir().parent / "known_hosts")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings.

    Cached because a migration reads them from many places and re-parsing the
    environment mid-run could produce an inconsistent view.
    """
    return Settings()


def reset_settings_cache() -> None:
    """For tests."""
    get_settings.cache_clear()
