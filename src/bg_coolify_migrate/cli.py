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
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.text import Text

from bg_coolify_migrate import __version__
from bg_coolify_migrate.domain.plan import MigrationPlan, TransferMode
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.errors import EmptyEnvironment, MigrationError, PreflightError
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
    """Render an error and exit with its documented code.

    The message is assembled as literal Text, not markup: an error that names a
    resource - ``no resource named 'api [v2]'`` - must not itself throw a
    MarkupError on the ``[v2]``.
    """
    get_console(stderr=True).print(Text.assemble(("error: ", "err"), str(exc)))
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


_TRUST_HOST_KEY_HELP = (
    "Record an unseen SSH host key instead of refusing (trust on first use). For "
    "unattended runs; interactively you are asked with the fingerprint instead."
)


# ── doctor ───────────────────────────────────────────────────────────────────


@app.command()
def doctor(
    check_servers: Annotated[
        bool,
        typer.Option(
            "--check-servers/--no-check-servers",
            help="SSH to each reachable server and check rsync + docker.",
        ),
    ] = True,
    install: Annotated[
        bool, typer.Option("--install", help="Install rsync on servers that are missing it.")
    ] = False,
    trust_host_key: Annotated[
        bool, typer.Option("--trust-host-key", help=_TRUST_HOST_KEY_HELP)
    ] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
    log_format: Annotated[str, typer.Option(help="console|json")] = "console",
) -> None:
    """Check the environment: token scope, API reachability, and each server.

    Run this first. It proves the one thing that silently breaks everything else -
    whether the token can read secrets - and, per reachable server, that rsync and
    docker are present (`--install` adds a missing rsync). Accepting host keys here
    means `plan`/`run` will not have to ask.
    """
    settings = _settings(log_level, log_format)
    try:
        asyncio.run(_doctor(settings, check_servers, install, trust_host_key))
    except MigrationError as exc:
        _fail(exc)


async def _doctor(
    settings: Settings, check_servers: bool, install: bool, trust_host_key: bool
) -> None:
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
        table.add_column("rsync")
        table.add_column("docker")
        for server in servers:
            # is_reachable lives under settings, not at the top level. Reading it
            # from the top silently rendered "unknown" for every server, always.
            reachable = CoolifyClient.server_is_reachable(server)
            reach = (
                Text("yes", style="ok")
                if reachable
                else (Text("no", style="err") if reachable is False else Text("?", style="warn"))
            )
            rsync_cell: Text = Text("-", style="muted")
            docker_cell: Text = Text("-", style="muted")
            if check_servers and reachable is not False:
                rsync_cell, docker_cell = await _check_server_deps(
                    api, settings, server, install, trust_host_key
                )
            table.add_row(
                Text(str(server.get("name", "?"))),
                Text(str(server.get("uuid", "?"))),
                Text(str(server.get("ip", "?"))),
                reach,
                rsync_cell,
                docker_cell,
            )
        console.print(table)

        projects = await api.list_projects()
        console.print(f"[ok]OK[/ok] {len(projects)} project(s) visible")

    console.print(f"[ok]OK[/ok] state dir: [path]{settings.resolved_state_dir()}[/path]")


async def _check_server_deps(
    api: object, settings: Settings, server: dict[str, Any], install: bool, trust_host_key: bool
) -> tuple[Text, Text]:
    """SSH to one server and report (rsync, docker) presence. Never raises."""
    from bg_coolify_migrate.engine.planner import server_ref
    from bg_coolify_migrate.engine.runner import ssh_target_for
    from bg_coolify_migrate.transfer import rsync
    from bg_coolify_migrate.transfer.ssh import RemoteHost

    ref = server_ref(server)
    try:
        await _accept_host_key(api, settings, ref, trust_host_key)
        ssh = await ssh_target_for(api, ref)  # type: ignore[arg-type]
        async with RemoteHost.connect(
            ssh,
            known_hosts=settings.resolved_known_hosts(),
            trust_new_host_key=trust_host_key or settings.trust_host_key,
            connect_timeout=settings.ssh_timeout,
        ) as host:
            has_rsync = await host.which("rsync")
            if not has_rsync and install:
                try:
                    await rsync.ensure_installed(host, label=str(server.get("name", "?")))
                    rsync_cell = Text("installed", style="ok")
                except MigrationError:
                    rsync_cell = Text("install failed", style="err")
            else:
                rsync_cell = Text("yes", style="ok") if has_rsync else Text("no", style="err")
            docker_cell = (
                Text("yes", style="ok") if await host.which("docker") else Text("no", style="err")
            )
            return rsync_cell, docker_cell
    except MigrationError as exc:
        note = str(exc).splitlines()[0][:24]
        return Text("?", style="warn"), Text(note, style="warn")


# ── list ─────────────────────────────────────────────────────────────────────


@app.command("list")
def list_projects(
    project: Annotated[
        str | None,
        typer.Argument(help="Limit the tree to one project (name or uuid)."),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", "-s", help="Only show resources on this server (name or uuid)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
    log_format: Annotated[str, typer.Option(help="console|json")] = "console",
) -> None:
    """List everything: server -> project -> environment -> resource, recursively.

    Discovery for `plan` and `run` in one pass - no drilling, nothing to piece
    together. Every level shows its uuid, so you can drive `plan`/`run` entirely by
    uuid (`plan <project-uuid>/<environment>/<resource-uuid>`). Narrow with a project
    argument or `--server`, or get JSON with `--json`. Reads only; needs no
    `read:sensitive` scope.
    """
    settings = _settings(log_level, log_format)
    try:
        asyncio.run(_list(settings, project, server, as_json))
    except MigrationError as exc:
        _fail(exc)


async def _list(
    settings: Settings, project: str | None, server_filter: str | None, as_json: bool
) -> None:
    from bg_coolify_migrate.api.client import CoolifyClient
    from bg_coolify_migrate.engine.planner import list_all_resources, list_project_resources
    from bg_coolify_migrate.ui import report as report_mod

    console = get_console()
    url, token = settings.require_coolify()
    scanning = Text(f"Scanning {project}..." if project else "Scanning everything...")

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        show_status = is_interactive() and not as_json
        with console.status(scanning) if show_status else contextlib.nullcontext():
            if project is not None:
                _name, rows, servers = await list_project_resources(api, project)
            else:
                rows, servers = await list_all_resources(api)

    if server_filter is not None:
        matches = [s for s in servers if server_filter in (s.name, s.uuid)]
        if not matches:
            raise PreflightError(
                f"no server named {server_filter!r}",
                hint="Run `coolify-migrate list` without --server to see every server.",
            )
        keep = {s.uuid for s in matches}
        rows = tuple(r for r in rows if r.server_uuid in keep)
        servers = tuple(matches)

    if as_json:
        console.print_json(json.dumps(report_mod.resource_row_dicts(rows)))
        return
    if not rows:
        where = f" in {project}" if project else ""
        console.print(Text(f"no resources visible{where}", style="muted"))
        return
    if is_interactive():
        console.print(report_mod.resource_tree(rows, servers))
    else:
        console.print(report_mod.plain_resource_tree(rows))


# ── selection: project[/environment[/resource]] ──────────────────────────────


@dataclass(frozen=True)
class Selection:
    """A parsed migration scope.

    The resource is the atom; environment and project are aggregations over it:

    * ``environment is None``          -> the whole project (every environment)
    * ``environment`` set, ``resource`` None -> the whole environment
    * both set                         -> one resource
    """

    project: str
    environment: str | None
    resource: str | None


def _parse_selector(selector: str, environment_override: str | None) -> Selection:
    """Parse ``project[/environment[/resource]]``. Each segment is a name or uuid."""
    parts = selector.split("/")
    if len(parts) > 3 or any(not part.strip() for part in parts):
        raise PreflightError(
            f"invalid selector {selector!r}",
            hint=(
                "Use project, project/environment, or project/environment/resource - "
                "e.g. bauer-group, bauer-group/production, or "
                "bauer-group/production/whistleblower-app."
            ),
        )
    project = parts[0].strip()
    environment = parts[1].strip() if len(parts) >= 2 else None
    resource = parts[2].strip() if len(parts) == 3 else None

    if environment_override is not None:
        if environment is not None and environment != environment_override:
            raise PreflightError(
                "the environment was given twice",
                hint="Put it in the selector path OR in --environment, not both.",
            )
        environment = environment_override
    return Selection(project=project, environment=environment, resource=resource)


async def _resolve_selection(
    api: object, selector: str | None, target: str | None, environment_override: str | None
) -> tuple[Selection, str]:
    """Return ``(Selection, target_server)``. Parses the selector, or picks on a TTY."""
    from bg_coolify_migrate.ui import wizard

    if selector is not None:
        selection = _parse_selector(selector, environment_override)
        if target is None:
            if not is_interactive():
                raise PreflightError(
                    "--to is required",
                    hint="Name the target server, or run in a terminal to pick one.",
                )
            servers = await api.list_servers()  # type: ignore[attr-defined]
            target = await _off_loop(wizard.choose_server, servers, message="Target server?")
        return selection, target

    if not is_interactive():
        raise PreflightError(
            "provide a selector: project, project/environment, or project/environment/resource",
            hint=(
                "Run `coolify-migrate list` for projects, or `list <project>` for its "
                "resources. In a terminal you can omit the selector to pick interactively."
            ),
        )
    return await _pick(api, target)


async def _off_loop(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking questionary prompt off the running event loop.

    questionary's ``.ask()`` calls ``asyncio.run()`` internally (via prompt_toolkit),
    which raises ``RuntimeError: asyncio.run() cannot be called from a running event
    loop`` when invoked inside our own ``asyncio.run(...)``. A worker thread has no
    running loop, so its ``asyncio.run`` works and prompt_toolkit skips the
    main-thread-only signal handlers. This is why every interactive prompt goes
    through here.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _host_key_prompt(target: object, fingerprint: str) -> bool:
    """OpenSSH-style: show the fingerprint and ask whether to trust an unseen host."""
    import questionary

    console = get_console()
    console.print(
        Text(
            f"\nThe authenticity of host '{target.host}' (port {target.port}) "  # type: ignore[attr-defined]
            "can't be established.",
            style="warn",
        )
    )
    console.print(Text(f"Key fingerprint is {fingerprint}"))
    prompt = questionary.confirm("Are you sure you want to continue connecting?", default=False)
    return bool(await _off_loop(prompt.ask))


def _host_key_decision() -> object:
    """The interactive host-key prompt on a TTY; None in a pipe/CI (use the flag)."""
    return _host_key_prompt if is_interactive() else None


async def _accept_host_key(
    api: object, settings: Settings, server: object, trust_host_key: bool
) -> None:
    """Preflight one server's SSH host key (prompt/record) before any live display."""
    from bg_coolify_migrate.engine.runner import ssh_target_for
    from bg_coolify_migrate.transfer.ssh import RemoteHost

    ssh = await ssh_target_for(api, server)  # type: ignore[arg-type]
    await RemoteHost.ensure_host_key(
        ssh,
        known_hosts=settings.resolved_known_hosts(),
        trust_new_host_key=trust_host_key or settings.trust_host_key,
        host_key_prompt=_host_key_decision(),  # type: ignore[arg-type]
    )


async def _pick(api: object, target: str | None) -> tuple[Selection, str]:
    """The interactive picker: project -> environment -> resource -> target server."""
    from bg_coolify_migrate.engine.planner import environment_resources
    from bg_coolify_migrate.ui import wizard

    projects = await api.list_projects()  # type: ignore[attr-defined]
    project_name = await _off_loop(wizard.choose_project, projects)
    project = next((p for p in projects if str(p.get("name")) == project_name), None)
    if project is None:  # pragma: no cover - defensive; the name came from this list
        raise PreflightError(f"project {project_name!r} is no longer visible")
    project_uuid = str(project["uuid"])

    detail = await api.get_project(project_uuid)  # type: ignore[attr-defined]
    environments = [
        str(env["name"])
        for env in detail.get("environments") or []
        if isinstance(env, dict) and env.get("name")
    ]
    environment = await _off_loop(wizard.choose_scope_environment, environments)

    resource: str | None = None
    if environment is not None:
        resources = await environment_resources(api, project_uuid, environment)  # type: ignore[arg-type]
        resource = await _off_loop(wizard.choose_scope_resource, resources)

    if target is None:
        servers = await api.list_servers()  # type: ignore[attr-defined]
        target = await _off_loop(wizard.choose_server, servers, message="Target server?")

    return Selection(project=project_name, environment=environment, resource=resource), target


async def _project_or_none(api: object, name_or_uuid: str) -> dict[str, Any] | None:
    """The project matching a name or uuid, or None. Does not raise."""
    for project in await api.list_projects():  # type: ignore[attr-defined]
        if name_or_uuid in (project.get("uuid"), project.get("name")):
            return project  # type: ignore[no-any-return]
    return None


async def _resolve_jobs(
    api: object, selection: Selection
) -> tuple[str, list[tuple[str, str, str | None]]]:
    """Expand a selection into ``(display_name, [(project, environment, resource), ...])``.

    A bare token is resolved the way an operator expects when they paste a uuid from
    ``list``: if it names a **project** it fans out to every environment; otherwise it
    is looked up as a **resource** anywhere and migrates just that one. An explicit
    ``project/environment[/resource]`` path is trusted as given.
    """
    from bg_coolify_migrate.engine.planner import list_all_resources, project_environments

    # Explicit path — the environment was named, so trust project/env[/resource].
    if selection.environment is not None:
        name, _envs = await project_environments(api, selection.project)  # type: ignore[arg-type]
        return name, [(selection.project, selection.environment, selection.resource)]

    # Single token: a project (whole-project), or a resource anywhere?
    project = await _project_or_none(api, selection.project)
    if project is not None:
        name = str(project.get("name", selection.project))
        _n, environments = await project_environments(api, selection.project)  # type: ignore[arg-type]
        return name, [(selection.project, environment, None) for environment in environments]

    rows, _servers = await list_all_resources(api)  # type: ignore[arg-type]
    matches = [row for row in rows if selection.project in (row.uuid, row.name)]
    if not matches:
        raise PreflightError(
            f"no project or resource matches {selection.project!r}",
            hint="Run `coolify-migrate list` to see projects and resources with their uuids.",
        )
    if len(matches) > 1:
        where = ", ".join(f"{m.project}/{m.environment}" for m in matches)
        raise PreflightError(
            f"{selection.project!r} matches {len(matches)} resources ({where})",
            hint="Name it by its uuid (unique), or as project/environment/<name>.",
        )
    row = matches[0]
    return row.project, [(row.project_uuid, row.environment, row.uuid)]


# ── plan ─────────────────────────────────────────────────────────────────────


_SELECTOR_HELP = (
    "project, project/environment, project/environment/resource, or a resource uuid - "
    "name or uuid. Omit in a terminal to pick interactively."
)


@app.command()
def plan(
    selector: Annotated[str | None, typer.Argument(help=_SELECTOR_HELP)] = None,
    to: Annotated[
        str | None,
        typer.Option(
            "--to", "--to-server", help="Target server (name or uuid). Omit in a terminal to pick."
        ),
    ] = None,
    environment: Annotated[
        str | None, typer.Option(help="Environment override when the selector names only a project.")
    ] = None,
    trust_host_key: Annotated[
        bool, typer.Option("--trust-host-key", help=_TRUST_HOST_KEY_HELP)
    ] = False,
    output: Annotated[
        Path | None, typer.Option(help="Write the plan JSON here (single-scope only).")
    ] = None,
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
    log_format: Annotated[str, typer.Option(help="console|json")] = "console",
) -> None:
    """Produce a migration plan. Reads only - nothing is changed.

    Scope it with the selector: a project (all environments), a project/environment,
    a project/environment/resource, or just a resource's uuid. Exercises preflight,
    discovery, volume pairing, the drift gate and the DNS gate. If `plan` is clean,
    `run` has already had its risky decisions made.
    """
    settings = _settings(log_level, log_format)
    try:
        asyncio.run(_plan(settings, selector, to, environment, output, trust_host_key))
    except MigrationError as exc:
        _fail(exc)


async def _plan(
    settings: Settings,
    selector: str | None,
    target: str | None,
    environment_override: str | None,
    output: Path | None,
    trust_host_key: bool = False,
) -> None:
    from bg_coolify_migrate.api.client import CoolifyClient
    from bg_coolify_migrate.ui import wizard

    console = get_console()
    url, token = settings.require_coolify()

    plans: list[MigrationPlan] = []
    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        await api.assert_can_read_sensitive()
        try:
            selection, resolved_target = await _resolve_selection(
                api, selector, target, environment_override
            )
        except wizard.Cancelled:
            console.print("aborted", style="muted")
            return
        project_name, jobs = await _resolve_jobs(api, selection)

        for job_project, job_environment, job_resource in jobs:
            try:
                plans.append(
                    await _build(
                        api, settings, job_project, job_environment, resolved_target,
                        job_resource, trust_host_key,
                    )
                )
            except EmptyEnvironment:
                # Only an empty environment is skippable, and only while scanning a
                # whole project. A single scope, or any real failure (host key, no
                # server), must surface - never be buried under "nothing to plan".
                if len(jobs) == 1:
                    raise
                console.print(
                    Text(f"skip {project_name}/{job_environment}: no resources", style="muted")
                )

    if not plans:
        raise PreflightError(
            f"nothing to plan for {project_name}",
            hint="No resources matched the selection.",
        )

    if output:
        if len(plans) == 1:
            output.write_text(plans[0].model_dump_json(indent=2), encoding="utf-8", newline="\n")
            console.print(f"[ok]OK[/ok] plan written to [path]{output}[/path]")
        else:
            console.print(
                "[warn]--output is ignored for a whole-project plan (multiple environments)."
                "[/warn]"
            )

    for migration_plan in plans:
        _render_plan(migration_plan)

    if any(migration_plan.is_blocked for migration_plan in plans):
        raise typer.Exit(2)


def _render_plan(migration_plan: MigrationPlan) -> None:
    from rich.console import Group, RenderableType

    from bg_coolify_migrate.ui import report as report_mod

    console = get_console()
    if not is_interactive():
        console.print(report_mod.plain_plan(migration_plan))
        return

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


async def _build(
    api: object,
    settings: Settings,
    project: str,
    environment: str,
    target: str,
    only_resource: str | None = None,
    trust_host_key: bool = False,
) -> MigrationPlan:
    """Open SSH to the source and build the plan for one environment.

    The source server is only known after we resolve the environment's resources,
    so we resolve it first with a throwaway lookup, then connect. ``only_resource``
    narrows the plan (and the source lookup) to a single resource.
    """
    from bg_coolify_migrate.engine.planner import (
        build_plan,
        environment_resources,
        find_project,
        resolve_server,
        server_ref,
    )
    from bg_coolify_migrate.engine.runner import ssh_target_for
    from bg_coolify_migrate.transfer.ssh import RemoteHost

    project_data = await find_project(api, project)  # type: ignore[arg-type]
    resources = await environment_resources(api, str(project_data["uuid"]), environment)  # type: ignore[arg-type]
    if not resources:
        raise EmptyEnvironment(f"no resources in {project}/{environment}")
    if only_resource is not None:
        resources = [
            (collection, resource)
            for collection, resource in resources
            if only_resource in (resource.get("name"), resource.get("uuid"))
        ]
        if not resources:
            raise PreflightError(
                f"no resource named {only_resource!r} in {project}/{environment}",
                hint=f"Run `coolify-migrate list {project}` to see its resources.",
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
        trust_new_host_key=trust_host_key or settings.trust_host_key,
        host_key_prompt=_host_key_decision(),  # type: ignore[arg-type]
        connect_timeout=settings.ssh_timeout,
    ) as source_host:
        return await build_plan(
            api,  # type: ignore[arg-type]
            source_host,
            project=project,
            environment=environment,
            target_server=target,
            only_resource=only_resource,
            transfer_mode=TransferMode(settings.transfer_mode),
        )


# ── run ──────────────────────────────────────────────────────────────────────


@app.command()
def run(
    selector: Annotated[str | None, typer.Argument(help=_SELECTOR_HELP)] = None,
    to: Annotated[
        str | None,
        typer.Option(
            "--to", "--to-server", help="Target server (name or uuid). Omit in a terminal to pick."
        ),
    ] = None,
    environment: Annotated[
        str | None, typer.Option(help="Environment override when the selector names only a project.")
    ] = None,
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
    trust_host_key: Annotated[
        bool, typer.Option("--trust-host-key", help=_TRUST_HOST_KEY_HELP)
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
    log_format: Annotated[str, typer.Option(help="console|json")] = "console",
) -> None:
    """Execute a migration.

    Scope it with the selector, exactly like `plan`: a resource (name or uuid), a
    whole environment, or a whole project (every environment, migrated in turn).
    """
    settings = _settings(log_level, log_format)
    try:
        policy = FinalizePolicy(finalize)
    except ValueError:
        get_console(stderr=True).print(
            Text(f"error: --finalize must be keep|rename|delete, got {finalize!r}", style="err")
        )
        raise typer.Exit(2) from None

    try:
        code = asyncio.run(
            _run(
                settings,
                selector=selector,
                target=to,
                environment_override=environment,
                policy=policy,
                accept_drift=accept_drift,
                delete_previews=delete_previews,
                trust_host_key=trust_host_key,
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
    selector: str | None,
    target: str | None,
    environment_override: str | None,
    policy: FinalizePolicy,
    accept_drift: bool,
    delete_previews: bool,
    trust_host_key: bool = False,
    assume_yes: bool,
) -> int:
    from bg_coolify_migrate.api.client import CoolifyClient
    from bg_coolify_migrate.engine.runner import make_migration_id, run_migration
    from bg_coolify_migrate.ui import dashboard, run_report, wizard
    from bg_coolify_migrate.ui import report as report_mod

    console = get_console()
    url, token = settings.require_coolify()

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        await api.assert_can_read_sensitive()
        try:
            selection, resolved_target = await _resolve_selection(
                api, selector, target, environment_override
            )
        except wizard.Cancelled:
            console.print("aborted", style="muted")
            return 0
        project_name, jobs = await _resolve_jobs(api, selection)

        plans: list[MigrationPlan] = []
        for job_project, job_environment, job_resource in jobs:
            try:
                plan = await _build(
                    api, settings, job_project, job_environment, resolved_target,
                    job_resource, trust_host_key,
                )
                plans.append(plan.model_copy(update={"finalize_policy": policy}))
            except EmptyEnvironment:
                # Only skip a genuinely empty environment, and only across a whole
                # project. Anything else (host key, no server) must surface.
                if len(jobs) == 1:
                    raise
                console.print(
                    Text(f"skip {project_name}/{job_environment}: no resources", style="muted")
                )

        if not plans:
            console.print(Text(f"error: nothing to migrate for {project_name}", style="err"))
            return 2

        if not assume_yes:
            if not is_interactive():
                console.print(
                    "[err]error:[/err] refusing to run unattended without --yes", style="err"
                )
                return 2
            # A yes here IS the answer to the drift/compatibility question, so we
            # carry accept_drift through; otherwise preflight would ask again into
            # the void.
            try:
                if not await _off_loop(_confirm_plans, plans):
                    return 0
            except wizard.Cancelled:
                console.print("aborted", style="muted")
                return 0
            accept_drift = True
        else:
            blocked = [migration_plan for migration_plan in plans if migration_plan.is_blocked]
            if blocked:
                for migration_plan in blocked:
                    console.print(report_mod.plain_plan(migration_plan))
                return 2

        # Accept the target host key HERE - before the live dashboard, where a
        # prompt cannot be shown. The source key was recorded while building the plan.
        await _accept_host_key(api, settings, plans[0].target_server, trust_host_key)

        worst = 0
        for migration_plan in plans:
            migration_id = make_migration_id(migration_plan.project, migration_plan.environment)
            started = time.monotonic()
            reporter = dashboard.build(
                title=f"{migration_plan.project}/{migration_plan.environment}"
            )
            with reporter:
                result = await run_migration(
                    api,
                    settings,
                    migration_plan,
                    migration_id=migration_id,
                    accept_drift=accept_drift,
                    delete_previews=delete_previews,
                    trust_host_key=trust_host_key,
                    host_key_prompt=None,  # keys pre-accepted; never prompt under Live
                    on_state=reporter.on_state,
                )
            elapsed = time.monotonic() - started
            if is_interactive():
                console.print(
                    run_report.outcome_panel(result, migration_id=migration_id, elapsed=elapsed)
                )
            else:
                console.print(run_report.plain_result(result, migration_id=migration_id))

            if result.exit_code != 0:
                worst = result.exit_code
                if len(plans) > 1:
                    console.print(
                        Text(
                            f"stopping: {migration_plan.environment} failed "
                            f"(exit {result.exit_code}); later environments were not started.",
                            style="warn",
                        )
                    )
                break
        return worst


def _confirm_plans(plans: list[MigrationPlan]) -> bool:
    """Confirm one or many plans. A single scope reuses the full plan wizard."""
    from bg_coolify_migrate.ui import report as report_mod
    from bg_coolify_migrate.ui import wizard

    if len(plans) == 1:
        if not wizard.confirm_plan(plans[0]):
            return False
        return wizard.confirm_destructive(plans[0])

    # Whole-project: show each environment's plan, then one confirmation.
    console = get_console()
    console.print(
        f"\n[bold]Whole-project migration:[/bold] {escape(plans[0].project)} - "
        f"{len(plans)} environment(s)"
    )
    for migration_plan in plans:
        console.print(report_mod.plan_summary(migration_plan))
        console.print(report_mod.resources_table(migration_plan))

    if any(migration_plan.is_blocked for migration_plan in plans):
        console.print(
            "\n[err]At least one environment is blocked.[/err] Nothing has been changed."
        )
        return False

    import questionary

    proceed = questionary.confirm(
        f"Migrate all {len(plans)} environment(s) of {plans[0].project}?", default=False
    ).ask()
    if not proceed:
        return False
    return all(wizard.confirm_destructive(migration_plan) for migration_plan in plans)


# ── resume / rollback ────────────────────────────────────────────────────────


@app.command(no_args_is_help=True)
def resume(
    migration_id: Annotated[str, typer.Argument(help="From `coolify-migrate status`.")],
    accept_drift: Annotated[
        bool, typer.Option(help="Proceed past the drift gate without asking (for unattended runs).")
    ] = False,
    trust_host_key: Annotated[
        bool, typer.Option("--trust-host-key", help=_TRUST_HOST_KEY_HELP)
    ] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
    log_format: Annotated[str, typer.Option(help="console|json")] = "console",
) -> None:
    """Continue a blocked or interrupted migration.

    Skips only what the journal recorded as completed, and every step re-checks
    the world it depends on - the journal is a hypothesis, not a fact.
    """
    settings = _settings(log_level, log_format)
    try:
        code = asyncio.run(_resume(settings, migration_id, accept_drift, trust_host_key))
    except MigrationError as exc:
        _fail(exc)
        return
    raise typer.Exit(code)


async def _resume(
    settings: Settings, migration_id: str, accept_drift: bool, trust_host_key: bool = False
) -> int:
    from bg_coolify_migrate.api.client import CoolifyClient
    from bg_coolify_migrate.engine.runner import load_plan, resume_migration
    from bg_coolify_migrate.ui import dashboard, run_report

    console = get_console()
    url, token = settings.require_coolify()
    migration_plan = load_plan(settings.resolved_state_dir(), migration_id)

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        await api.assert_can_read_sensitive()
        # Accept both host keys before the live dashboard - resume has no plan phase
        # to have recorded the source key.
        await _accept_host_key(api, settings, migration_plan.source_server, trust_host_key)
        await _accept_host_key(api, settings, migration_plan.target_server, trust_host_key)
        started = time.monotonic()
        reporter = dashboard.build(title=f"resume {migration_id}")
        with reporter:
            result = await resume_migration(
                api,
                settings,
                migration_plan,
                migration_id,
                accept_drift=accept_drift,
                trust_host_key=trust_host_key,
                host_key_prompt=None,
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


@app.command(no_args_is_help=True)
def rollback(
    migration_id: Annotated[str, typer.Argument(help="From `coolify-migrate status`.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    trust_host_key: Annotated[
        bool, typer.Option("--trust-host-key", help=_TRUST_HOST_KEY_HELP)
    ] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
    log_format: Annotated[str, typer.Option(help="console|json")] = "console",
) -> None:
    """Undo a migration.

    The source is never destroyed before an explicit finalize, so a rollback is
    always available up to that point.
    """
    settings = _settings(log_level, log_format)
    try:
        code = asyncio.run(_rollback(settings, migration_id, yes, trust_host_key))
    except MigrationError as exc:
        _fail(exc)
        return
    raise typer.Exit(code)


async def _rollback(
    settings: Settings, migration_id: str, assume_yes: bool, trust_host_key: bool = False
) -> int:
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
            f"Rolling back [bold]{escape(migration_id)}[/bold] will delete the target "
            f"resources created on [host]{escape(migration_plan.target_server.name)}[/host] "
            f"and restart [host]{escape(migration_plan.source_server.name)}[/host]."
        )
        if not await _off_loop(questionary.confirm("Proceed?", default=False).ask):
            return 0

    async with CoolifyClient(url, token, verify=settings.coolify_verify_tls) as api:
        # rollback has no live dashboard, so prompting during connect is fine.
        result = await rollback_migration(
            api,
            settings,
            migration_plan,
            migration_id,
            trust_host_key=trust_host_key,
            host_key_prompt=_host_key_decision(),  # type: ignore[arg-type]
        )
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
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
    log_format: Annotated[str, typer.Option(help="console|json")] = "console",
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


@server_app.command("plan", no_args_is_help=True)
def server_plan(
    to: Annotated[str, typer.Option(help="Target host (ip or dns name).")],
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
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


@server_app.command("run", no_args_is_help=True)
def server_run(
    to: Annotated[str, typer.Option(help="Target host (ip or dns name).")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    force_overwrite: Annotated[
        bool, typer.Option(help="Proceed even if the destination is not empty. Dangerous.")
    ] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG|INFO|WARNING|ERROR")] = "INFO",
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
        if not await _off_loop(questionary.confirm("Proceed?", default=False).ask):
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
        get_console(stderr=True).print(Text.assemble(("error: ", "err"), str(exc)))
        sys.exit(exc.exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
