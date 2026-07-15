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
  4  drift needs your decision - resumable, or pass --accept-drift
  5  quiesce failed (the stack would not stop cleanly)
  6  transfer failed (rolled back)
  7  verification failed (rolled back; the target was NOT started)
  8  rollback itself failed - human attention required
  9  Coolify API error
 10  the API token lacks root/read:sensitive
 14  journal error
===  ==========================================================================
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

import typer

from bg_coolify_migrate import __version__
from bg_coolify_migrate.domain.plan import TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.errors import MigrationError
from bg_coolify_migrate.observability.logging_setup import setup_logging
from bg_coolify_migrate.settings.base import Settings
from bg_coolify_migrate.ui.console import get_console, is_interactive

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
    get_console(stderr=True).print(f"[err]error:[/err] {exc}")
    raise typer.Exit(exc.exit_code)


def _settings(log_level: str, log_format: str) -> Settings:
    settings = Settings()
    setup_logging(
        log_level=log_level or settings.log_level,
        log_format=log_format or settings.log_format,
    )
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
        console.print(f"[ok]OK[/ok] Coolify {version} reachable at [path]{url}[/path]")

        # The check that matters. Without this scope every env var comes back
        # with no value at all - HTTP 200, no error, keys simply absent.
        if await api.can_read_sensitive():
            console.print("[ok]OK[/ok] token can read sensitive data (root / read:sensitive)")
        else:
            console.print("[err]FAIL[/err] token CANNOT read sensitive data")
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
            # is_reachable lives under settings, not at the top level. Reading it
            # from the top silently rendered "unknown" for every server, always.
            reachable = CoolifyClient.server_is_reachable(server)
            status = (
                "[ok]yes[/ok]"
                if reachable
                else ("[err]no[/err]" if reachable is False else "[warn]unknown[/warn]")
            )
            table.add_row(
                str(server.get("name", "?")),
                str(server.get("uuid", "?")),
                str(server.get("ip", "?")),
                status,
            )
        console.print(table)

        projects = await api.list_projects()
        console.print(f"[ok]OK[/ok] {len(projects)} project(s) visible")

    console.print(f"[ok]OK[/ok] state dir: [path]{settings.resolved_state_dir()}[/path]")


# ── plan ─────────────────────────────────────────────────────────────────────


@app.command()
def plan(
    project: Annotated[str, typer.Argument(help="Project name or uuid.")],
    to: Annotated[str, typer.Option(help="Target server name or uuid.")],
    environment: Annotated[str, typer.Option(help="Environment name.")] = "production",
    output: Annotated[Path | None, typer.Option(help="Write the plan JSON here.")] = None,
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """Produce a migration plan. Reads only - nothing is changed.

    Exercises preflight, discovery, volume pairing, the drift gate and the DNS
    gate. If `plan` is clean, `run` has already had its risky decisions made.
    """
    settings = _settings(log_level, log_format)
    try:
        asyncio.run(_plan(settings, project, environment, to, output))
    except MigrationError as exc:
        _fail(exc)


async def _plan(
    settings: Settings,
    project: str,
    environment: str,
    target: str,
    output: Path | None,
) -> None:
    from rich.console import Group, RenderableType

    from bg_coolify_migrate.api.client import CoolifyClient
    from bg_coolify_migrate.ui import report as report_mod

    console = get_console()
    url, token = settings.require_coolify()

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        await api.assert_can_read_sensitive()
        migration_plan = await _build(api, settings, project, environment, target)

    if output:
        output.write_text(migration_plan.model_dump_json(indent=2), encoding="utf-8", newline="\n")
        console.print(f"[ok]OK[/ok] plan written to [path]{output}[/path]")

    if not is_interactive():
        console.print(report_mod.plain_plan(migration_plan))
    else:
        renderables: list[RenderableType] = [
            report_mod.plan_summary(migration_plan),
            report_mod.resources_table(migration_plan),
        ]
        for resource in migration_plan.resources:
            if resource.manifest.items:
                renderables.append(
                    report_mod.manifest_table(
                        resource.manifest, title=f"Volumes - {resource.snapshot.name}"
                    )
                )
            panel = report_mod.drift_panel(resource.drift) if resource.drift else None
            if panel is not None:
                renderables.append(panel)
        warnings = report_mod.warnings_panel(migration_plan)
        if warnings is not None:
            renderables.append(warnings)
        blocking = report_mod.blocking_panel(migration_plan)
        if blocking is not None:
            renderables.append(blocking)
        console.print(Group(*renderables))

    if migration_plan.is_blocked:
        raise typer.Exit(2)


async def _build(api: object, settings: Settings, project: str, environment: str, target: str):  # type: ignore[no-untyped-def]
    """Open SSH to the source and build the plan.

    The source server is only known after we resolve the project's resources, so
    we resolve it first with a throwaway lookup, then connect.
    """
    from bg_coolify_migrate.engine.planner import (
        build_plan,
        environment_resources,
        find_project,
        resolve_server,
        server_ref,
    )
    from bg_coolify_migrate.engine.runner import ssh_target_for
    from bg_coolify_migrate.errors import PreflightError
    from bg_coolify_migrate.transfer.ssh import RemoteHost

    project_data = await find_project(api, project)  # type: ignore[arg-type]
    resources = await environment_resources(api, str(project_data["uuid"]), environment)  # type: ignore[arg-type]
    if not resources:
        raise PreflightError(
            f"no resources in {project}/{environment}",
            hint="Check the environment name (default: production).",
        )

    collection, first = resources[0]
    full = await api.get_resource(collection, str(first["uuid"]))  # type: ignore[attr-defined]
    source_server = await resolve_server(api, full)  # type: ignore[arg-type]
    if source_server is None:
        raise PreflightError("the API did not report a server for these resources")

    source = server_ref(source_server)
    ssh = await ssh_target_for(api, source)  # type: ignore[arg-type]

    async with RemoteHost.connect(
        ssh,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=settings.trust_host_key,
        connect_timeout=settings.ssh_timeout,
    ) as source_host:
        return await build_plan(
            api,  # type: ignore[arg-type]
            source_host,
            project=project,
            environment=environment,
            target_server=target,
            transfer_mode=TransferMode(settings.transfer_mode),
        )


# ── run ──────────────────────────────────────────────────────────────────────


@app.command()
def run(
    project: Annotated[str, typer.Argument(help="Project name or uuid.")],
    to: Annotated[str, typer.Option(help="Target server name or uuid.")],
    environment: Annotated[str, typer.Option()] = "production",
    finalize: Annotated[
        str, typer.Option(help="keep|rename|delete - what happens to the source.")
    ] = "rename",
    accept_drift: Annotated[
        bool,
        typer.Option(
            help=(
                "Answer the compatibility question in advance: proceed even though the "
                "target may pull a newer image or build a newer commit. Needed only when "
                "unattended - interactively we show the detail and ask."
            )
        ),
    ] = False,
    delete_previews: Annotated[
        bool, typer.Option(help="Delete preview deployments first (they block the copy).")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """Execute a migration."""
    settings = _settings(log_level, log_format)
    try:
        policy = FinalizePolicy(finalize)
    except ValueError:
        get_console(stderr=True).print(
            f"[err]error:[/err] --finalize must be keep|rename|delete, got {finalize!r}"
        )
        raise typer.Exit(2) from None

    try:
        code = asyncio.run(
            _run(
                settings,
                project=project,
                environment=environment,
                target=to,
                policy=policy,
                accept_drift=accept_drift,
                delete_previews=delete_previews,
                assume_yes=yes,
            )
        )
    except MigrationError as exc:
        _fail(exc)
        return
    raise typer.Exit(code)


async def _run(
    settings: Settings,
    *,
    project: str,
    environment: str,
    target: str,
    policy: FinalizePolicy,
    accept_drift: bool,
    delete_previews: bool,
    assume_yes: bool,
) -> int:
    from bg_coolify_migrate.api.client import CoolifyClient
    from bg_coolify_migrate.engine.runner import make_migration_id, run_migration
    from bg_coolify_migrate.ui import dashboard, run_report, wizard

    console = get_console()
    url, token = settings.require_coolify()

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        await api.assert_can_read_sensitive()
        migration_plan = await _build(api, settings, project, environment, target)
        migration_plan = migration_plan.model_copy(update={"finalize_policy": policy})

        if not assume_yes:
            if not is_interactive():
                console.print(
                    "[err]error:[/err] refusing to run unattended without --yes",
                    style="err",
                )
                return 2
            # confirm_plan asks about drift too, so a yes here IS the answer to
            # the compatibility question. Without carrying it through, preflight
            # would ask again and abort — the operator would have answered into
            # the void.
            if not wizard.confirm_plan(migration_plan):
                return 0
            if not wizard.confirm_destructive(migration_plan):
                return 0
            accept_drift = True
        elif migration_plan.is_blocked:
            from bg_coolify_migrate.ui import report as report_mod

            console.print(report_mod.plain_plan(migration_plan))
            return 2

        migration_id = make_migration_id(migration_plan.project, migration_plan.environment)
        started = time.monotonic()

        reporter = dashboard.build(title=f"{migration_plan.project}/{migration_plan.environment}")
        with reporter:
            result = await run_migration(
                api,
                settings,
                migration_plan,
                migration_id=migration_id,
                accept_drift=accept_drift,
                delete_previews=delete_previews,
                on_state=reporter.on_state,
            )

        elapsed = time.monotonic() - started
        if is_interactive():
            console.print(
                run_report.outcome_panel(result, migration_id=migration_id, elapsed=elapsed)
            )
        else:
            console.print(run_report.plain_result(result, migration_id=migration_id))
        return result.exit_code


# ── resume / rollback ────────────────────────────────────────────────────────


@app.command()
def resume(
    migration_id: Annotated[str, typer.Argument(help="From `coolify-migrate status`.")],
    accept_drift: Annotated[bool, typer.Option()] = False,
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """Continue a blocked or interrupted migration.

    Skips only what the journal recorded as completed, and every step re-checks
    the world it depends on - the journal is a hypothesis, not a fact.
    """
    settings = _settings(log_level, log_format)
    try:
        code = asyncio.run(_resume(settings, migration_id, accept_drift))
    except MigrationError as exc:
        _fail(exc)
        return
    raise typer.Exit(code)


async def _resume(settings: Settings, migration_id: str, accept_drift: bool) -> int:
    from bg_coolify_migrate.api.client import CoolifyClient
    from bg_coolify_migrate.engine.runner import load_plan, resume_migration
    from bg_coolify_migrate.ui import dashboard, run_report

    console = get_console()
    url, token = settings.require_coolify()
    migration_plan = load_plan(settings.resolved_state_dir(), migration_id)

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        await api.assert_can_read_sensitive()
        started = time.monotonic()
        reporter = dashboard.build(title=f"resume {migration_id}")
        with reporter:
            result = await resume_migration(
                api,
                settings,
                migration_plan,
                migration_id,
                accept_drift=accept_drift,
                on_state=reporter.on_state,
            )
        elapsed = time.monotonic() - started
        if is_interactive():
            console.print(
                run_report.outcome_panel(result, migration_id=migration_id, elapsed=elapsed)
            )
        else:
            console.print(run_report.plain_result(result, migration_id=migration_id))
        return result.exit_code


@app.command()
def rollback(
    migration_id: Annotated[str, typer.Argument(help="From `coolify-migrate status`.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    log_level: Annotated[str, typer.Option()] = "INFO",
    log_format: Annotated[str, typer.Option()] = "console",
) -> None:
    """Undo a migration.

    The source is never destroyed before an explicit finalize, so a rollback is
    always available up to that point.
    """
    settings = _settings(log_level, log_format)
    try:
        code = asyncio.run(_rollback(settings, migration_id, yes))
    except MigrationError as exc:
        _fail(exc)
        return
    raise typer.Exit(code)


async def _rollback(settings: Settings, migration_id: str, assume_yes: bool) -> int:
    import questionary

    from bg_coolify_migrate.api.client import CoolifyClient
    from bg_coolify_migrate.engine.runner import load_plan, rollback_migration
    from bg_coolify_migrate.ui import run_report

    console = get_console()
    url, token = settings.require_coolify()
    migration_plan = load_plan(settings.resolved_state_dir(), migration_id)

    if not assume_yes:
        if not is_interactive():
            console.print("[err]error:[/err] refusing to roll back unattended without --yes")
            return 2
        console.print(
            f"Rolling back [bold]{migration_id}[/bold] will delete the target resources "
            f"created on [host]{migration_plan.target_server.name}[/host] and restart "
            f"[host]{migration_plan.source_server.name}[/host]."
        )
        if not questionary.confirm("Proceed?", default=False).ask():
            return 0

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        result = await rollback_migration(api, settings, migration_plan, migration_id)
        if is_interactive():
            console.print(run_report.outcome_panel(result, migration_id=migration_id))
        else:
            console.print(run_report.plain_result(result, migration_id=migration_id))
        return result.exit_code


# ── status ───────────────────────────────────────────────────────────────────


@app.command()
def status(
    migration_id: Annotated[str | None, typer.Argument(help="Show one migration.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
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
        records = list(journal.read())
        if as_json:
            console.print_json(json.dumps([r.model_dump() for r in records]))
            return
        table = Table(title=f"Migration {migration_id}", title_justify="left")
        table.add_column("#", justify="right", style="muted")
        table.add_column("When", style="muted")
        table.add_column("Event", style="bold")
        table.add_column("State")
        for record in records:
            table.add_row(str(record.seq), record.ts, record.event, record.state or "-")
        console.print(table)
        return

    ids = list_migrations(state_dir)
    if as_json:
        console.print_json(json.dumps(ids))
        return
    if not ids:
        console.print(f"no migrations recorded in [path]{state_dir}[/path]", style="muted")
        return

    table = Table(title="Migrations", title_justify="left")
    table.add_column("ID", style="bold")
    table.add_column("Last event")
    table.add_column("State")
    for mid in ids:
        last = Journal.open(state_dir, mid).last_event()
        table.add_row(mid, last.event if last else "-", (last.state or "-") if last else "-")
    console.print(table)


# ── server (F2) ──────────────────────────────────────────────────────────────


@server_app.command("plan")
def server_plan(
    to: Annotated[str, typer.Option(help="Target host (ip or dns name).")],
    log_level: Annotated[str, typer.Option()] = "INFO",
) -> None:
    """Inventory a whole-instance migration. Reads only."""
    settings = _settings(log_level, "console")
    try:
        asyncio.run(_server_plan(settings, to))
    except MigrationError as exc:
        _fail(exc)


async def _server_plan(settings: Settings, target: str) -> None:
    from bg_coolify_migrate.server.runner import plan_server_migration
    from bg_coolify_migrate.ui import server_report

    console = get_console()
    inventory = await plan_server_migration(settings, target)
    console.print(server_report.inventory_panel(inventory))
    console.print(server_report.inventory_table(inventory))
    if inventory.blocking_reasons:
        console.print(server_report.blocking_panel(inventory))
        raise typer.Exit(2)


@server_app.command("run")
def server_run(
    to: Annotated[str, typer.Option(help="Target host (ip or dns name).")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    force_overwrite: Annotated[
        bool, typer.Option(help="Proceed even if the destination is not empty. Dangerous.")
    ] = False,
    log_level: Annotated[str, typer.Option()] = "INFO",
) -> None:
    """Migrate the Coolify instance to a new host.

    Asserts APP_KEY survives - it decrypts every credential in Coolify's
    database, and it survives only if the archive is extracted BEFORE install.sh
    runs. Geczy's script gets that ordering right by luck and never mentions it.
    """
    settings = _settings(log_level, "console")
    try:
        code = asyncio.run(_server_run(settings, to, yes, force_overwrite))
    except MigrationError as exc:
        _fail(exc)
        return
    raise typer.Exit(code)


async def _server_run(
    settings: Settings, target: str, assume_yes: bool, force_overwrite: bool
) -> int:
    import questionary

    from bg_coolify_migrate.server.runner import plan_server_migration, run_server_migration
    from bg_coolify_migrate.ui import run_report, server_report

    console = get_console()
    inventory = await plan_server_migration(settings, target)
    console.print(server_report.inventory_panel(inventory))

    if inventory.blocking_reasons and not force_overwrite:
        console.print(server_report.blocking_panel(inventory))
        return 2

    if not assume_yes:
        if not is_interactive():
            console.print("[err]error:[/err] refusing to run unattended without --yes")
            return 2
        console.print(
            "\n[warn]This stops EVERYTHING on the source[/warn] - Coolify and every "
            "container it manages - for the duration of the transfer."
        )
        if not questionary.confirm("Proceed?", default=False).ask():
            return 0

    result, migration_id = await run_server_migration(
        settings, target, inventory, force_overwrite=force_overwrite
    )
    if is_interactive():
        console.print(run_report.outcome_panel(result, migration_id=migration_id))
    else:
        console.print(run_report.plain_result(result, migration_id=migration_id))
    return result.exit_code


def main() -> None:
    """Console-script entry point."""
    import sys

    try:
        app()
    except MigrationError as exc:  # pragma: no cover - defence in depth
        get_console(stderr=True).print(f"[err]error:[/err] {exc}")
        sys.exit(exc.exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
