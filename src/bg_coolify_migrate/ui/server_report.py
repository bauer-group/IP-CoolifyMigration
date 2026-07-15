"""Rendering an instance-migration inventory."""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bg_coolify_migrate.server.inventory import ServerInventory
from bg_coolify_migrate.ui.console import human_bytes


def inventory_panel(inventory: ServerInventory) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("From", inventory.source_host)
    grid.add_row("To", inventory.target_host)
    grid.add_row("Coolify", inventory.coolify_version)
    grid.add_row("Containers", f"{inventory.container_count} ({inventory.running_count} running)")
    grid.add_row("Volumes", str(len(inventory.volumes)))
    grid.add_row("/data/coolify", human_bytes(inventory.coolify_data_bytes))
    grid.add_row("Docker volumes", human_bytes(inventory.volumes_bytes))
    grid.add_row("Total", human_bytes(inventory.total_bytes))
    grid.add_row("Target free", human_bytes(inventory.target_free_bytes))
    # The fingerprint, never the key.
    grid.add_row("APP_KEY", inventory.app_key_fingerprint or "[err]not found[/err]")

    border = "err" if inventory.is_blocked else "ok"
    return Panel(grid, title="Instance migration", border_style=border, title_align="left")


def inventory_table(inventory: ServerInventory) -> Group:
    renderables: list[Table | Panel] = []

    if inventory.unattached_volumes:
        table = Table(
            title="Volumes with no container attached (migrated anyway)", title_justify="left"
        )
        table.add_column("Volume", style="path")
        for name in inventory.unattached_volumes:
            table.add_row(name)
        renderables.append(table)

    if inventory.bind_mounts:
        table = Table(title="Bind mounts (migrated)", title_justify="left")
        table.add_column("Host path", style="path")
        for path in inventory.bind_mounts:
            table.add_row(path)
        renderables.append(table)

    if inventory.warnings:
        renderables.append(
            Panel(
                Group(*[Text(f"- {w}") for w in inventory.warnings]),
                title="Warnings",
                border_style="warn",
                title_align="left",
            )
        )

    return Group(*renderables)


def blocking_panel(inventory: ServerInventory) -> Panel:
    return Panel(
        Group(*[Text(f"- {r}", style="err") for r in inventory.blocking_reasons]),
        title="Blocked - nothing has been changed",
        border_style="err",
        title_align="left",
    )


def plain_inventory(inventory: ServerInventory) -> str:
    lines = [
        f"source: {inventory.source_host}",
        f"target: {inventory.target_host}",
        f"coolify_version: {inventory.coolify_version}",
        f"containers: {inventory.container_count}",
        f"volumes: {len(inventory.volumes)}",
        f"unattached_volumes: {len(inventory.unattached_volumes)}",
        f"bind_mounts: {len(inventory.bind_mounts)}",
        f"total_bytes: {inventory.total_bytes}",
        f"target_free_bytes: {inventory.target_free_bytes}",
        f"app_key: {inventory.app_key_fingerprint}",
        f"blocked: {inventory.is_blocked}",
    ]
    lines.extend(f"blocking: {r}" for r in inventory.blocking_reasons)
    lines.extend(f"warning: {w}" for w in inventory.warnings)
    return "\n".join(lines)
