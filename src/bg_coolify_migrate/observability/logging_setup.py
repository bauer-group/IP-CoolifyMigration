"""Logging.

structlog is the single source of truth. Two modes: ``console`` (Rich-rendered,
for humans) and ``json`` (one object per line, for CI and log shipping).

The redaction processor is the load-bearing part. This tool handles a Coolify
root token, SSH private keys, database passwords and APP_KEY; a stray
``log.debug("api.response", body=...)`` would put every secret of every project
into a terminal scrollback. Redaction is applied **additively** — callers may
extend the fragment list but never replace it.

``setup_logging`` is idempotent via a module-level latch, because the CLI, the
wizard and the engine may all reasonably try to configure logging and the last
one must not win.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

#: Substrings that mark a key as sensitive. Deliberately broad: a false positive
#: costs a redacted debug line, a false negative costs a leaked credential.
_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "privatekey",
    "app_key",
    "appkey",
    "authorization",
    "auth",
    "credential",
    "real_value",
    "docker_compose_raw",
)

_REDACTED = "«redacted»"

_initialized = False
_extra_fragments: tuple[str, ...] = ()


class _LazyStderr:
    """Resolves ``sys.stderr`` at write time, not at configure time.

    structlog's ``PrintLoggerFactory(file=sys.stderr)`` captures whatever
    ``sys.stderr`` happens to be when logging is configured. Any later
    reassignment — a test runner's capture buffer, an embedding host, a
    daemonize — leaves every subsequent log call writing to a stale (often
    closed) handle, raising ``ValueError: I/O operation on closed file`` from
    unrelated code paths.

    Indirecting through this proxy costs one attribute lookup per line and makes
    the logger correct under stream reassignment.
    """

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(f in lowered for f in (*_SENSITIVE_KEY_FRAGMENTS, *_extra_fragments))


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive keys anywhere in the event, including nested."""

    def _walk(value: Any, depth: int = 0) -> Any:
        if depth > 6:  # pragma: no cover - pathological nesting
            return value
        if isinstance(value, dict):
            return {
                k: (_REDACTED if _is_sensitive(str(k)) else _walk(v, depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_walk(v, depth + 1) for v in value]
        return value

    return {
        k: (_REDACTED if _is_sensitive(k) else _walk(v))
        for k, v in event_dict.items()
    }


def setup_logging(
    *,
    log_level: str = "INFO",
    log_format: str = "console",
    extra_sensitive_fragments: tuple[str, ...] = (),
    force: bool = False,
) -> None:
    """Configure structlog and route stdlib logging through it.

    Idempotent: repeated calls are no-ops unless ``force`` is set. The CLI, the
    wizard and the engine may all try to configure logging; the last must not
    silently win.

    Args:
        log_level: DEBUG | INFO | WARNING | ERROR | CRITICAL.
        log_format: ``console`` (Rich) or ``json``.
        extra_sensitive_fragments: Additional key fragments to redact. ADDITIVE —
            the built-in list is never replaced.
        force: Reconfigure even if already initialised.
    """
    global _initialized, _extra_fragments
    if _initialized and not force:
        return

    _extra_fragments = tuple(extra_sensitive_fragments)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
    ]

    if log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
        shared.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        # NOT `file=sys.stderr`: that would freeze the current stream. See _LazyStderr.
        logger_factory=structlog.PrintLoggerFactory(file=_LazyStderr()),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (httpx, asyncssh) through the same pipeline so their
    # output matches ours instead of interleaving two formats.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, log_level.upper(), logging.INFO),
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncssh").setLevel(logging.WARNING)

    _initialized = True


def reset_logging() -> None:
    """For tests."""
    global _initialized, _extra_fragments
    _initialized = False
    _extra_fragments = ()
    structlog.reset_defaults()


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
