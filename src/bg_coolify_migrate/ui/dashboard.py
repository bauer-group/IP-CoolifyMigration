"""The live run dashboard.

Rich `Live` over the step table. Deliberately modest: the useful information
during a migration is *which step, how long, what's left* — not a light show.

Two hard parts, both handled here rather than hoped away:

* **structlog and Live share stdout.** Two writers to one terminal interleave and
  corrupt each other. We route logs through the same Console, which serialises
  them behind Rich's lock, and let Live redirect stdout/stderr.
* **Non-TTY.** In a pipe or CI a Live display emits megabytes of escape codes.
  :func:`build` returns a plain progress reporter instead, which is a first-class
  format rather than a degraded one.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Protocol, Self

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from bg_coolify_migrate.domain.statemachine import ORDER, State
from bg_coolify_migrate.ui.console import get_console, human_duration, is_interactive

_STEP_LABEL = {
    State.INIT: "Initialise",
    State.PREFLIGHT: "Preflight checks",
    State.PLAN: "Plan",
    State.CREATE_TARGET: "Create target (stopped)",
    State.QUIESCE: "Stop source and verify",
    State.DISCOVER: "Discover volumes",
    State.COPY: "Copy data",
    State.VERIFY: "Verify checksums",
    State.DNS_GATE: "DNS gate",
    State.START_TARGET: "Start target",
    State.HEALTHCHECK: "Health check",
    State.FINALIZE: "Finalise source",
}


class Reporter(Protocol):
    """What the runner needs; both implementations satisfy it."""

    async def on_state(self, state: State) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class PlainReporter:
    """Line-oriented progress. For pipes, CI, and NO_COLOR.

    Not a fallback so much as the correct rendering for a non-terminal: a
    migration in a CI log must be greppable and must not contain a single escape
    code.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or get_console()
        self._started: dict[State, float] = {}
        self._current: State | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._finish_current()

    def _finish_current(self) -> None:
        if self._current is None:
            return
        elapsed = time.monotonic() - self._started[self._current]
        self.console.print(
            f"step: {self._current.value} done in {human_duration(elapsed)}", highlight=False
        )
        self._current = None

    async def on_state(self, state: State) -> None:
        self._finish_current()
        self._current = state
        self._started[state] = time.monotonic()
        self.console.print(f"step: {state.value} started", highlight=False)


class LiveDashboard:
    """A Rich Live step table."""

    def __init__(self, console: Console | None = None, *, title: str = "") -> None:
        self.console = console or get_console()
        self.title = title
        self._done: dict[State, float] = {}
        self._started: dict[State, float] = {}
        self._current: State | None = None
        self._failed: State | None = None
        self._live: Live | None = None
        self._spinner = Spinner("dots")

    def __enter__(self) -> Self:
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=8,
            # Without this, structlog's writes and Live's repaints interleave and
            # shred each other.
            redirect_stdout=True,
            redirect_stderr=True,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None and self._current is not None:
            self._failed = self._current
        self._close_current()
        if self._live is not None:
            self._live.update(self._render())
            self._live.__exit__(exc_type, exc, tb)
            self._live = None

    def _close_current(self) -> None:
        if self._current is not None and self._current not in self._done:
            self._done[self._current] = time.monotonic() - self._started[self._current]

    async def on_state(self, state: State) -> None:
        self._close_current()
        self._current = state
        self._started[state] = time.monotonic()
        if self._live is not None:
            self._live.update(self._render())

    def _render(self) -> Group:
        table = Table.grid(padding=(0, 2))
        table.add_column(width=3)
        table.add_column(style="bold", min_width=26)
        table.add_column(justify="right", style="muted")

        for state in ORDER:
            if state is State.DONE:
                continue
            label = _STEP_LABEL.get(state, state.value)

            if state is self._failed:
                table.add_row(Text("x", style="err"), Text(label, style="err"), "failed")
            elif state in self._done:
                table.add_row(
                    Text("+", style="ok"), label, human_duration(self._done[state])
                )
            elif state is self._current:
                elapsed = time.monotonic() - self._started[state]
                table.add_row(self._spinner, Text(label, style="bold"), human_duration(elapsed))
            else:
                table.add_row(Text("-", style="muted"), Text(label, style="muted"), "")

        header = Text(self.title, style="bold") if self.title else Text("")
        return Group(header, Text(""), table)


def build(*, title: str = "", force_plain: bool = False) -> Reporter:
    """Pick the right reporter for this terminal.

    Callers do not choose; the environment does. A `--plain` flag would just be
    another thing to forget in CI.
    """
    if force_plain or not is_interactive():
        return PlainReporter()
    return LiveDashboard(title=title)
