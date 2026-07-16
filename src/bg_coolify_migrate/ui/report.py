"""Rendering plans, manifests and gate reports.

The guiding rule: **every line must explain itself**. An operator reading a
blocked migration at 3am should not have to re-derive why. That is why every
manifest item carries a ``reason`` and every gate verdict carries a ``detail`` —
this module only surfaces them.
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bg_coolify_migrate.dns.gate import DnsGateReport, Verdict
from bg_coolify_migrate.domain.drift import RebuildDriftReport, Severity
from bg_coolify_migrate.domain.manifest import Decision, VolumeManifest
from bg_coolify_migrate.domain.plan import (
    MigrationPlan,
    ProjectListing,
    ProjectPlacement,
    ResourceRow,
)
from bg_coolify_migrate.ui.console import human_bytes

_DECISION_STYLE = {
    Decision.MIGRATE: "ok",
    Decision.SKIP: "muted",
    Decision.REFUSE: "err",
}

_VERDICT_STYLE = {
    Verdict.READY: "ok",
    Verdict.CUTOVER_NEEDED: "err",
    Verdict.ELSEWHERE: "warn",
    Verdict.UNRESOLVED: "warn",
    Verdict.GENERATED: "muted",
}


def manifest_table(manifest: VolumeManifest, *, title: str = "Volumes") -> Table:
    table = Table(title=title, show_lines=False, title_justify="left")
    table.add_column("Decision", style="bold", no_wrap=True)
    table.add_column("Mount path", style="path")
    table.add_column("Source", overflow="fold")
    table.add_column("Size", justify="right")
    table.add_column("Why", style="muted", overflow="fold")

    for item in manifest.items:
        table.add_row(
            Text(item.decision.value, style=_DECISION_STYLE[item.decision]),
            item.mount_path,
            item.source_name or item.source_path,
            human_bytes(item.bytes) if item.decision is Decision.MIGRATE else "—",
            item.reason,
        )
    return table


_SEVERITY_STYLE = {
    Severity.BLOCK: "err",
    Severity.WARN: "warn",
    Severity.NOTICE: "muted",
    Severity.OK: "ok",
}


def drift_panel(report: RebuildDriftReport | None) -> Panel | None:
    """Render what the target may run that the source does not.

    ``None`` when there is nothing worth saying. Never framed as a refusal: we
    build the target as configured and report what could still differ, because
    whether that is compatible is the operator's call.
    """
    if report is None or report.severity is Severity.OK:
        return None

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for finding in report.findings:
        table.add_row(
            Text(finding.axis.value, style=_SEVERITY_STYLE[finding.severity]), finding.summary
        )
        if finding.source_value and finding.target_value and finding.source_value != finding.target_value:
            table.add_row("", Text(f"  source runs: {finding.source_value}", style="muted"))
            table.add_row("", Text(f"  target gets: {finding.target_value}", style="muted"))
        if finding.detail:
            table.add_row("", Text(finding.detail, style="muted"))

    needs = report.requires_confirmation
    return Panel(
        table,
        title=f"{report.resource_name} — {'your decision' if needs else 'for information'}",
        border_style="warn" if needs else "muted",
        title_align="left",
    )


def dns_table(report: DnsGateReport) -> Table:
    table = Table(title="DNS", show_lines=False, title_justify="left")
    table.add_column("Verdict", no_wrap=True)
    table.add_column("Hostname", style="host")
    table.add_column("Resolves to")
    table.add_column("TTL", justify="right")
    table.add_column("Why", style="muted", overflow="fold")

    for verdict in report.verdicts:
        table.add_row(
            Text(verdict.verdict.value, style=_VERDICT_STYLE[verdict.verdict]),
            verdict.hostname.host,
            ", ".join(verdict.addresses) or "—",
            str(verdict.ttl) if verdict.ttl else "—",
            verdict.detail,
        )
    return table


def cutover_panel(report: DnsGateReport) -> Panel:
    """The actionable checklist shown when the gate blocks."""
    lines = [Text("Change these DNS records, then resume:", style="bold")]
    for entry in report.cutover_checklist():
        lines.append(Text(f"  {entry}"))
    if report.max_ttl:
        lines.append(Text(""))
        lines.append(
            Text(
                f"Allow up to {report.max_ttl}s for the old answer to expire from caches.",
                style="muted",
            )
        )
    return Panel(Group(*lines), title="Cutover checklist", border_style="gate", title_align="left")


def plan_summary(plan: MigrationPlan) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    # Text, not markup: names may carry '[' etc. and must not be parsed as tags.
    grid.add_row("Project", Text(f"{plan.project} / {plan.environment}"))
    grid.add_row("From", Text(f"{plan.source_server.name} ({plan.source_server.ip})"))
    grid.add_row("To", Text(f"{plan.target_server.name} ({plan.target_server.ip})"))
    grid.add_row("Resources", str(len(plan.resources)))
    grid.add_row("Data", human_bytes(plan.total_bytes))
    grid.add_row("On success", plan.finalize_policy.value)
    grid.add_row("Transfer", plan.transfer_mode.value)

    border = "err" if plan.is_blocked else "ok"
    return Panel(grid, title="Migration plan", border_style=border, title_align="left")


def resources_table(plan: MigrationPlan) -> Table:
    table = Table(title="Resources", show_lines=False, title_justify="left")
    table.add_column("Name", style="bold")
    table.add_column("Kind")
    table.add_column("Strategy")
    table.add_column("Volumes", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Status")

    for resource in plan.resources:
        status = (
            Text("BLOCKED", style="err") if resource.is_blocked else Text("ready", style="ok")
        )
        table.add_row(
            Text(resource.snapshot.name),
            resource.snapshot.kind.value,
            resource.strategy.value,
            str(len(resource.manifest.to_migrate)),
            human_bytes(resource.manifest.total_bytes),
            status,
        )
    return table


def blocking_panel(plan: MigrationPlan) -> Panel | None:
    """Why the migration will not proceed."""
    if not plan.is_blocked:
        return None
    lines: list[Text] = []
    for resource in plan.blocked_resources:
        lines.append(Text(resource.snapshot.name, style="bold err"))
        for reason in resource.blocking_reasons:
            lines.append(Text(f"  • {reason}"))
    return Panel(
        Group(*lines),
        title="Blocked — nothing has been changed",
        border_style="err",
        title_align="left",
    )


def warnings_panel(plan: MigrationPlan) -> Panel | None:
    if not plan.warnings:
        return None
    return Panel(
        Group(*[Text(f"• {w}") for w in plan.warnings]),
        title="Warnings",
        border_style="warn",
        title_align="left",
    )


def plain_plan(plan: MigrationPlan) -> str:
    """Line-oriented rendering for non-TTY output.

    Not a degraded fallback — a first-class format. A migration plan in a CI log
    must be greppable.
    """
    lines = [
        f"project: {plan.project}/{plan.environment}",
        f"from: {plan.source_server.name} ({plan.source_server.ip})",
        f"to: {plan.target_server.name} ({plan.target_server.ip})",
        f"resources: {len(plan.resources)}",
        f"bytes: {plan.total_bytes}",
        f"finalize: {plan.finalize_policy.value}",
        f"blocked: {plan.is_blocked}",
    ]
    for resource in plan.resources:
        lines.append(
            f"resource: name={resource.snapshot.name} kind={resource.snapshot.kind.value} "
            f"strategy={resource.strategy.value} volumes={len(resource.manifest.to_migrate)} "
            f"bytes={resource.manifest.total_bytes} blocked={resource.is_blocked}"
        )
        for reason in resource.blocking_reasons:
            lines.append(f"  blocking: {reason}")
    for warning in plan.warnings:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


# ── project listing (the `list` command) ─────────────────────────────────────


def _group_by_server(listing: ProjectListing) -> dict[str, list[ProjectPlacement]]:
    grouped: dict[str, list[ProjectPlacement]] = {}
    for placement in listing.placements:
        grouped.setdefault(placement.server_uuid, []).append(placement)
    for items in grouped.values():
        items.sort(key=lambda p: (p.project.lower(), p.environment))
    return grouped


def _resource_word(count: int) -> str:
    return "resource" if count == 1 else "resources"


def _placement_line(placement: ProjectPlacement) -> Text:
    # Text.append renders literally (no markup parsing), so a project or environment
    # named e.g. "api [v2]" is shown as-is instead of throwing a MarkupError.
    line = Text("  ")
    line.append(placement.project)
    line.append(" / ", style="muted")
    line.append(placement.environment)
    line.append(f"   {placement.resources} {_resource_word(placement.resources)}", style="muted")
    if placement.project_uuid:
        line.append(f"   [{placement.project_uuid}]", style="muted")
    return line


def listing_group(listing: ProjectListing) -> Group:
    """Projects grouped under the server they run on. Rich, for a TTY."""
    grouped = _group_by_server(listing)
    lines: list[Text] = []

    for server in sorted(listing.servers, key=lambda s: s.name.lower()):
        header = Text()
        header.append(server.name, style="host")
        if server.ip:
            header.append(f"  ({server.ip})", style="muted")
        lines.append(header)

        items = grouped.pop(server.uuid, [])
        if not items:
            lines.append(Text("  (no projects)", style="muted"))
        lines.extend(_placement_line(p) for p in items)
        lines.append(Text(""))

    # Placements whose server did not resolve to a known host are never dropped:
    # an unplaceable project is exactly what an operator needs to notice.
    orphans = sorted(
        (p for items in grouped.values() for p in items),
        key=lambda p: (p.project.lower(), p.environment),
    )
    if orphans:
        lines.append(Text("unknown server", style="warn"))
        lines.extend(_placement_line(p) for p in orphans)

    return Group(*lines)


def plain_listing(listing: ProjectListing) -> str:
    r"""Tab-separated ``server<TAB>project<TAB>project_uuid<TAB>environment<TAB>resources``.

    Greppable for CI, and the uuid column lets a script drive ``plan``/``run`` by
    uuid. An empty server is one row with ``-`` placeholders.
    """
    grouped = _group_by_server(listing)
    rows: list[str] = []
    for server in sorted(listing.servers, key=lambda s: s.name.lower()):
        items = grouped.pop(server.uuid, [])
        if not items:
            rows.append(f"{server.name}\t-\t-\t-\t0")
        rows.extend(
            f"{server.name}\t{p.project}\t{p.project_uuid}\t{p.environment}\t{p.resources}"
            for p in items
        )
    rows.extend(
        f"?\t{p.project}\t{p.project_uuid}\t{p.environment}\t{p.resources}"
        for p in (p for items in grouped.values() for p in items)
    )
    return "\n".join(rows)


def listing_dicts(listing: ProjectListing) -> list[dict[str, object]]:
    """Flat placement records for ``--json``. Empty servers are omitted."""
    name = {s.uuid: s.name for s in listing.servers}
    ip = {s.uuid: s.ip for s in listing.servers}
    return [
        {
            "server": name.get(p.server_uuid, ""),
            "server_uuid": p.server_uuid,
            "server_ip": ip.get(p.server_uuid, ""),
            "project": p.project,
            "project_uuid": p.project_uuid,
            "environment": p.environment,
            "resources": p.resources,
        }
        for p in listing.placements
    ]


# ── resource listing (`list <project>`) ──────────────────────────────────────


def resource_rows_table(project: str, rows: list[ResourceRow]) -> Table:
    """One row per resource, with the uuid to select it unambiguously.

    Cells are :class:`Text`, not markup: a resource literally named ``api [v2]``
    must render, not raise a ``MarkupError``.
    """
    table = Table(
        title=Text(f"Resources in {project}"), show_lines=False, title_justify="left"
    )
    table.add_column("Environment", style="muted")
    table.add_column("Resource", style="bold")
    table.add_column("Kind")
    table.add_column("UUID", style="muted")
    table.add_column("Server", style="host")
    for row in sorted(rows, key=lambda r: (r.environment, r.name.lower())):
        table.add_row(
            Text(row.environment),
            Text(row.name),
            Text(row.kind),
            Text(row.uuid),
            Text(row.server or "?"),
        )
    return table


def plain_resource_rows(rows: list[ResourceRow]) -> str:
    r"""Tab-separated ``environment<TAB>resource<TAB>kind<TAB>uuid<TAB>server``."""
    return "\n".join(
        f"{r.environment}\t{r.name}\t{r.kind}\t{r.uuid}\t{r.server or '?'}"
        for r in sorted(rows, key=lambda r: (r.environment, r.name.lower()))
    )


def resource_row_dicts(rows: list[ResourceRow]) -> list[dict[str, object]]:
    """Flat resource records for ``--json``."""
    return [
        {
            "environment": r.environment,
            "name": r.name,
            "uuid": r.uuid,
            "kind": r.kind,
            "server": r.server,
            "server_uuid": r.server_uuid,
        }
        for r in rows
    ]
