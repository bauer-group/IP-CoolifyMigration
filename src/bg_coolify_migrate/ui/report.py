"""Rendering plans, manifests and gate reports.

The guiding rule: **every line must explain itself**. An operator reading a
blocked migration at 3am should not have to re-derive why. That is why every
manifest item carries a ``reason`` and every gate verdict carries a ``detail`` —
this module only surfaces them.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bg_coolify_migrate.dns.gate import DnsGateReport, Verdict
from bg_coolify_migrate.domain.drift import RebuildDriftReport, Severity
from bg_coolify_migrate.domain.manifest import Decision, VolumeManifest
from bg_coolify_migrate.domain.plan import MigrationPlan, ResourceRow, ServerRef
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
    Verdict.SERVER_BOUND: "ok",
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


# ── resource listing (the `list` command) ────────────────────────────────────


def resource_tree(rows: Iterable[ResourceRow], servers: Iterable[ServerRef]) -> Group:
    """The whole inventory as a server -> project -> environment -> resource tree.

    One pass shows everything: no drilling, nothing to piece together. Text
    throughout (never markup), so a name containing '[' renders literally instead of
    throwing a MarkupError, and every level carries its uuid for uuid-based selection.
    """
    ip_by_uuid = {s.uuid: s.ip for s in servers}
    by_server: dict[tuple[str, str], list[ResourceRow]] = {}
    for row in rows:
        by_server.setdefault((row.server or "unknown server", row.server_uuid), []).append(row)

    lines: list[Text] = []
    for (server_name, server_uuid), server_rows in sorted(
        by_server.items(), key=lambda kv: kv[0][0].lower()
    ):
        header = Text()
        header.append(server_name, style="host")
        ip = ip_by_uuid.get(server_uuid, "")
        if ip:
            header.append(f"  ({ip})", style="muted")
        lines.append(header)

        by_project: dict[tuple[str, str], list[ResourceRow]] = {}
        for row in server_rows:
            by_project.setdefault((row.project, row.project_uuid), []).append(row)

        for (project_name, project_uuid), project_rows in sorted(
            by_project.items(), key=lambda kv: kv[0][0].lower()
        ):
            project_line = Text("  ")
            project_line.append(project_name, style="bold")
            if project_uuid:
                project_line.append(f"  [{project_uuid}]", style="muted")
            lines.append(project_line)

            by_env: dict[str, list[ResourceRow]] = {}
            for row in project_rows:
                by_env.setdefault(row.environment, []).append(row)

            for environment, env_rows in sorted(by_env.items()):
                lines.append(Text(f"    {environment}", style="muted"))
                for row in sorted(env_rows, key=lambda r: r.name.lower()):
                    resource_line = Text("      ")
                    resource_line.append(row.name)
                    resource_line.append(f"  {row.kind}", style="muted")
                    if row.uuid:
                        resource_line.append(f"  [{row.uuid}]", style="muted")
                    lines.append(resource_line)
        lines.append(Text(""))

    return Group(*lines)


def plain_resource_tree(rows: Iterable[ResourceRow]) -> str:
    r"""Tab-separated, one line per resource, fully qualified.

    ``server<TAB>project<TAB>project_uuid<TAB>environment<TAB>resource<TAB>kind<TAB>uuid``
    — greppable for CI and enough to drive ``plan``/``run`` by uuid from a script.
    """
    return "\n".join(
        f"{r.server or '?'}\t{r.project}\t{r.project_uuid}\t{r.environment}\t"
        f"{r.name}\t{r.kind}\t{r.uuid}"
        for r in sorted(
            rows, key=lambda r: (r.server.lower(), r.project.lower(), r.environment, r.name.lower())
        )
    )


def resource_row_dicts(rows: Iterable[ResourceRow]) -> list[dict[str, object]]:
    """Flat resource records for ``--json``."""
    return [
        {
            "server": r.server,
            "server_uuid": r.server_uuid,
            "project": r.project,
            "project_uuid": r.project_uuid,
            "environment": r.environment,
            "name": r.name,
            "uuid": r.uuid,
            "kind": r.kind,
        }
        for r in rows
    ]
