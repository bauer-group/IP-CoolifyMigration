"""Console output.

Rich for humans, plain lines for pipes and CI. The distinction is not cosmetic:
a Rich table piped into a log file is unreadable, and progress bars in CI produce
megabytes of escape codes.

We detect non-TTY and honour ``NO_COLOR`` (the de-facto standard), so the same
command works in a terminal, in a pipe, and in a GitHub Actions log without a
flag.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache

from rich.console import Console
from rich.theme import Theme

THEME = Theme(
    {
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "gate": "bold magenta",
        "muted": "dim",
        "path": "cyan",
        "host": "bold cyan",
        "count": "bold",
    }
)


@lru_cache(maxsize=2)
def get_console(*, stderr: bool = False) -> Console:
    """The shared console.

    Cached so Rich's Live display and ordinary prints share one lock; two
    Consoles writing to the same stream interleave and corrupt the output.
    """
    return Console(
        theme=THEME,
        stderr=stderr,
        no_color=bool(os.environ.get("NO_COLOR")),
        # force_terminal=None lets Rich decide; it disables styling when piped.
        highlight=False,
        soft_wrap=False,
    )


def is_interactive() -> bool:
    """Whether we can draw a live dashboard and ask questions.

    False in CI, in a pipe, and when NO_COLOR is set. Callers fall back to plain
    line-oriented output rather than refusing to run.
    """
    if os.environ.get("CI"):
        return False
    return sys.stdout.isatty() and sys.stderr.isatty()


def human_bytes(value: int | None) -> str:
    """Format bytes for an operator, not for a machine."""
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"  # pragma: no cover - unreachable given the loop


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def reset_console_cache() -> None:
    """For tests."""
    get_console.cache_clear()
