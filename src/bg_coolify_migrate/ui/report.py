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
from bg_coolify_migrate.domain.plan import MigrationPlan
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


def drift_panel(report: RebuildDriftReport) -> Panel | None:
    """Render a rebuild-drift verdict, or ``None`` when there is nothing to say."""
    if not report.builds or report.severity is Severity.OK:
        return None

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for finding in report.findings:
        style = "err" if finding.severity is Severity.BLOCK else "warn"
        table.add_row(Text(finding.axis.value, style=style), finding.summary)
        if finding.source_value or finding.target_value:
            table.add_row("", Text(f"  running: {finding.source_value}", style="muted"))
            table.add_row("", Text(f"  would build: {finding.target_value}", style="muted"))
        if finding.detail:
            table.add_row("", Text(finding.detail, style="muted"))

    border = "err" if report.is_blocked else "warn"
    title = "Rebuild drift — BLOCKED" if report.is_blocked else "Rebuild drift — warning"
    return Panel(table, title=title, border_style=border, title_align="left")


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
    grid.add_row("Project", f"{plan.project} / {plan.environment}")
    grid.add_row("From", f"{plan.source_server.name} ({plan.source_server.ip})")
    grid.add_row("To", f"{plan.target_server.name} ({plan.target_server.ip})")
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
            resource.snapshot.name,
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
