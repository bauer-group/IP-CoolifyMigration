"""Command-line interface.

NOTE: this module deliberately does NOT use ``from __future__ import annotations``.
Typer introspects real annotation objects at decoration time, and a string
annotation makes it fail to build the parser. Every other module in this package
has the import; this one is the documented exception.

Exit codes are a contract (see docs/cli.md). They are stable and scriptable:

===  ==========================================================================
  0  success
  2  preflight failed (nothing was changed)
  3  DNS gate blocked - resumable
  4  rebuild drift blocked - resumable after --accept-rebuild-drift
  5  quiesce failed (the stack would not stop cleanly)
  6  transfer failed (rolled back)
  7  verification failed (rolled back; the target was NOT started)
  8  rollback itself failed - human attention required
  9  Coolify API error
 10  the API token lacks root/read:sensitive
===  ==========================================================================
"""

import asyncio
import sys
from typing import Annotated

import typer

from bg_coolify_migrate import __version__
from bg_coolify_migrate.errors import MigrationError
from bg_coolify_migrate.observability.logging_setup import setup_logging
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.ui.console import get_console

app = typer.Typer(
    name="coolify-migrate",
    help=(
        "Move a Coolify project - with its data - between servers, and relocate a whole "
        "Coolify instance.\n\n"
        "Requires a Coolify API token with `root` or `read:sensitive`: without it Coolify "
        "silently omits secret values from its responses."
    ),
    no_args_is_help=True,
    add_completion=False,
)

server_app = typer.Typer(
    help="Migrate the Coolify instance itself to a new host.",
    no_args_is_help=True,
)
app.add_typer(server_app, name="server")


def _fail(exc: MigrationError) -> None:
    """Render an error and exit with its documented code."""
    console = get_console(stderr=True)
    console.print(f"[err]error:[/err] {exc}")
    raise typer.Exit(exc.exit_code)


def _settings(log_level: str, log_format: str) -> Settings:
    settings = Settings()
    setup_logging(log_level=log_level or settings.log_level, log_format=log_format or settings.log_format)
    return settings


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"coolify-migrate {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Coolify migration toolkit."""


# ── doctor ───────────────────────────────────────────────────────────────────


@app.command()
def doctor(
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
    log_format: Annotated[str, typer.Option(help="console|json")] = "console",
) -> None:
    """Check the environment: token scope, API reachability, server inventory.

    Run this first. It proves the one thing that silently breaks everything else
    - whether the token can actually read secrets.
    """
    settings = _settings(log_level, log_format)
    try:
        asyncio.run(_doctor(settings))
    except MigrationError as exc:
        _fail(exc)


async def _doctor(settings: Settings) -> None:
    from rich.table import Table

    from bg_coolify_migrate.api.client import CoolifyClient

    console = get_console()
    url, token = settings.require_coolify()

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        version = await api.version()
        console.print(f"[ok]✓[/ok] Coolify {version} reachable at [path]{url}[/path]")

        # The check that matters. Without this scope every env var comes back
        # with no value at all - HTTP 200, no error, keys simply absent.
        if await api.can_read_sensitive():
            console.print("[ok]✓[/ok] token can read sensitive data (root / read:sensitive)")
        else:
            console.print("[err]✗[/err] token CANNOT read sensitive data")
            console.print(
                "\n  Coolify's ApiSensitiveData middleware omits `value`, `real_value` and\n"
                "  `docker_compose_raw` for tokens without root/read:sensitive - with no error\n"
                "  and no redaction marker. A migration would recreate every environment\n"
                "  variable empty and every service without its compose.\n\n"
                "  Create a token with root or read:sensitive in Coolify > Keys & Tokens.",
                style="muted",
            )
            raise typer.Exit(10)

        servers = await api.list_servers()
        table = Table(title="Servers", title_justify="left")
        table.add_column("Name", style="bold")
        table.add_column("UUID", style="muted")
        table.add_column("IP", style="host")
        table.add_column("Reachable")
        for server in servers:
            reachable = server.get("is_reachable")
            table.add_row(
                str(server.get("name", "?")),
                str(server.get("uuid", "?")),
                str(server.get("ip", "?")),
                "[ok]yes[/ok]" if reachable else "[warn]unknown[/warn]",
            )
        console.print(table)

        projects = await api.list_projects()
        console.print(f"[ok]✓[/ok] {len(projects)} project(s) visible")

    state_dir = settings.resolved_state_dir()
    console.print(f"[ok]✓[/ok] state dir: [path]{state_dir}[/path]")


# ── plan ─────────────────────────────────────────────────────────────────────


@app.command()
def plan(
    project: Annotated[str, typer.Argument(help="Project name or uuid.")],
    environment: Annotated[str, typer.Option(help="Environment name.")] = "production",
    to: Annotated[str | None, typer.Option(help="Target server name or uuid.")] = None,
    output: Annotated[str | None, typer.Option(help="Write the plan JSON here.")] = None,
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """Produce a migration plan. Reads only - nothing is changed.

    Unlike coolify-mover's --dry-run, which short-circuits before all the code
    that actually breaks, this exercises preflight, discovery, volume pairing,
    the drift gate and the DNS gate. If `plan` is clean, `run` has already had
    its risky decisions made.
    """
    _settings(log_level, log_format)
    console = get_console()
    console.print(
        f"[warn]not yet implemented:[/warn] planning {project}/{environment}"
        + (f" -> {to}" if to else "")
    )
    console.print(
        "The planning pipeline (discover -> pair -> drift -> dns) is built and unit-tested; "
        "wiring it to a live instance is the next milestone.",
        style="muted",
    )
    if output:
        console.print(f"would write: [path]{output}[/path]", style="muted")
    raise typer.Exit(1)


# ── run / resume / rollback ──────────────────────────────────────────────────


@app.command()
def run(
    project: Annotated[str, typer.Argument(help="Project name or uuid.")],
    to: Annotated[str, typer.Option(help="Target server name or uuid.")],
    environment: Annotated[str, typer.Option()] = "production",
    finalize: Annotated[
        str, typer.Option(help="keep|rename|delete - what happens to the source.")
    ] = "rename",
    accept_rebuild_drift: Annotated[
        bool,
        typer.Option(
            help=(
                "Proceed even though the target would rebuild different code than the "
                "source runs. Never implicit."
            )
        ),
    ] = False,
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """Execute a migration."""
    _settings(log_level, log_format)
    console = get_console()
    console.print(f"[warn]not yet implemented:[/warn] run {project} -> {to} ({finalize})")
    raise typer.Exit(1)


@app.command()
def resume(
    migration_id: Annotated[str, typer.Argument(help="From `coolify-migrate status`.")],
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """Continue a blocked or interrupted migration.

    Reconciles the journal against reality before trusting it - a stale journal
    is a hypothesis, not a fact.
    """
    _settings(log_level, log_format)
    console = get_console()
    console.print(f"[warn]not yet implemented:[/warn] resume {migration_id}")
    raise typer.Exit(1)


@app.command()
def rollback(
    migration_id: Annotated[str, typer.Argument(help="From `coolify-migrate status`.")],
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """Undo a migration.

    The source is never destroyed before an explicit finalize, so a rollback is
    always available up to that point.
    """
    _settings(log_level, log_format)
    console = get_console()
    console.print(f"[warn]not yet implemented:[/warn] rollback {migration_id}")
    raise typer.Exit(1)


# ── status ───────────────────────────────────────────────────────────────────


@app.command()
def status(
    migration_id: Annotated[str | None, typer.Argument(help="Show one migration.")] = None,
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """List migrations, or show one in detail."""
    from rich.table import Table

    from bg_coolify_migrate.journal.store import Journal, list_migrations

    settings = _settings(log_level, log_format)
    console = get_console()
    state_dir = settings.resolved_state_dir()

    if migration_id:
        try:
            journal = Journal.open(state_dir, migration_id)
        except MigrationError as exc:
            _fail(exc)
            return
        table = Table(title=f"Migration {migration_id}", title_justify="left")
        table.add_column("#", justify="right", style="muted")
        table.add_column("When", style="muted")
        table.add_column("Event", style="bold")
        table.add_column("State")
        for record in journal.read():
            table.add_row(str(record.seq), record.ts, record.event, record.state or "-")
        console.print(table)
        return

    ids = list_migrations(state_dir)
    if not ids:
        console.print(f"no migrations recorded in [path]{state_dir}[/path]", style="muted")
        return

    table = Table(title="Migrations", title_justify="left")
    table.add_column("ID", style="bold")
    table.add_column("Last event")
    table.add_column("State")
    for mid in ids:
        journal = Journal.open(state_dir, mid)
        last = journal.last_event()
        table.add_row(
            mid,
            last.event if last else "-",
            (last.state or "-") if last else "-",
        )
    console.print(table)


# ── server (F2) ──────────────────────────────────────────────────────────────


@server_app.command("plan")
def server_plan(
    to: Annotated[str, typer.Option(help="Target host (ip or dns name).")],
    log_level: Annotated[str, typer.Option()] = "INFO",
) -> None:
    """Inventory a whole-instance migration. Reads only."""
    _settings(log_level, "console")
    console = get_console()
    console.print(f"[warn]not yet implemented:[/warn] server plan -> {to}")
    raise typer.Exit(1)


@server_app.command("run")
def server_run(
    to: Annotated[str, typer.Option(help="Target host (ip or dns name).")],
    log_level: Annotated[str, typer.Option()] = "INFO",
) -> None:
    """Migrate the Coolify instance to a new host.

    Asserts APP_KEY survives - it is what decrypts every credential in Coolify's
    database, and it survives only if the archive is extracted BEFORE install.sh
    runs. Geczy's script gets that ordering right by luck and never mentions it.
    """
    _settings(log_level, "console")
    console = get_console()
    console.print(f"[warn]not yet implemented:[/warn] server run -> {to}")
    raise typer.Exit(1)


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except MigrationError as exc:  # pragma: no cover - defence in depth
        get_console(stderr=True).print(f"[err]error:[/err] {exc}")
        sys.exit(exc.exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
