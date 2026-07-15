"""The interactive wizard.

Only ever reached on a TTY: in a pipe or CI the CLI requires explicit arguments
instead, because a prompt in CI is a hang.

Design rule: **the wizard chooses, it never decides.** Every question maps to a
CLI flag, so anything you can do here you can also script. A wizard that can do
something the flags cannot is a wizard you have to use.
"""

from __future__ import annotations

from typing import Any

import questionary
from rich.console import Group, RenderableType

from bg_coolify_migrate.domain.plan import MigrationPlan
from bg_coolify_migrate.domain.statemachine import FinalizePolicy
from bg_coolify_migrate.ui import report as report_mod
from bg_coolify_migrate.ui.console import get_console, human_bytes

_STYLE = questionary.Style(
    [
        ("qmark", "fg:cyan bold"),
        ("question", "bold"),
        ("answer", "fg:cyan bold"),
        ("pointer", "fg:cyan bold"),
        ("highlighted", "fg:cyan bold"),
        ("selected", "fg:green"),
        ("instruction", "fg:#888888"),
    ]
)


class Cancelled(Exception):
    """The operator pressed Ctrl+C or chose to abort."""


def _ask(prompt: Any) -> Any:
    answer = prompt.ask()
    if answer is None:
        raise Cancelled
    return answer


def choose_server(servers: list[dict[str, Any]], *, message: str, exclude: str | None = None) -> str:
    """Pick a server. Returns its uuid."""
    choices = [
        questionary.Choice(
            title=f"{s.get('name')}  ({s.get('ip')})",
            value=str(s.get("uuid")),
        )
        for s in servers
        if s.get("uuid") != exclude
    ]
    if not choices:
        raise Cancelled("no eligible servers")
    return str(_ask(questionary.select(message, choices=choices, style=_STYLE)))


def choose_project(projects: list[dict[str, Any]]) -> str:
    choices = [
        questionary.Choice(title=str(p.get("name")), value=str(p.get("name")))
        for p in projects
    ]
    if not choices:
        raise Cancelled("no projects visible")
    return str(_ask(questionary.select("Which project?", choices=choices, style=_STYLE)))


def choose_environment(environments: list[str]) -> str:
    if not environments:
        return "production"
    if len(environments) == 1:
        return environments[0]
    return str(
        _ask(questionary.select("Which environment?", choices=environments, style=_STYLE))
    )


def choose_finalize_policy() -> FinalizePolicy:
    """What happens to the source once the target is verified healthy.

    `rename` is default and listed first because it is the reversible one.
    `delete` is described honestly rather than sold.
    """
    choice = _ask(
        questionary.select(
            "When the target is verified and healthy, the source should be:",
            choices=[
                questionary.Choice(
                    title="renamed and kept stopped  (reversible, releases the FQDN)",
                    value=FinalizePolicy.RENAME,
                ),
                questionary.Choice(
                    title="left exactly as it is  (safest; you clean up by hand)",
                    value=FinalizePolicy.KEEP,
                ),
                questionary.Choice(
                    title="deleted with its volumes  (IRREVERSIBLE)",
                    value=FinalizePolicy.DELETE,
                ),
            ],
            style=_STYLE,
        )
    )
    return FinalizePolicy(choice)


def confirm_drift(plan: MigrationPlan) -> bool:
    """Ask about anything the target may run that the source does not.

    Separate from :func:`confirm_plan` and asked FIRST, because it is a different
    kind of question. "Do you want to migrate?" is about intent; "is postgres:16
    resolving to a newer minor acceptable for your data?" is about compatibility,
    and only the operator can answer it.

    We do not refuse — new image versions and moved branches are normal. We make
    the question concrete and let them decide.
    """
    undecided = [r for r in plan.resources if r.needs_confirmation]
    if not undecided:
        return True

    console = get_console()
    console.print(
        "\n[warn]The target will be built exactly as the source is configured, "
        "but it may not end up running exactly the same thing:[/warn]\n"
    )
    for resource in undecided:
        panel = report_mod.drift_panel(resource.drift) if resource.drift else None
        if panel is not None:
            console.print(panel)

    console.print(
        "Your data is copied byte-exactly either way, and your source stays untouched "
        "until the very last step.",
        style="muted",
    )
    return bool(
        _ask(questionary.confirm("Continue with the above?", default=False, style=_STYLE))
    )


def confirm_plan(plan: MigrationPlan) -> bool:
    """Show the plan and ask. Returns False if the operator declines."""
    console = get_console()
    renderables: list[RenderableType] = [
        report_mod.plan_summary(plan),
        report_mod.resources_table(plan),
    ]

    for resource in plan.resources:
        if resource.manifest.items:
            renderables.append(
                report_mod.manifest_table(
                    resource.manifest, title=f"Volumes - {resource.snapshot.name}"
                )
            )

    warnings = report_mod.warnings_panel(plan)
    if warnings is not None:
        renderables.append(warnings)

    blocking = report_mod.blocking_panel(plan)
    if blocking is not None:
        renderables.append(blocking)

    console.print(Group(*renderables))

    if plan.is_blocked:
        console.print("\n[err]This migration cannot proceed.[/err] Nothing has been changed.")
        return False

    console.print(
        f"\nThis will STOP {plan.project}/{plan.environment} on "
        f"[host]{plan.source_server.name}[/host] and move "
        f"[count]{human_bytes(plan.total_bytes)}[/count] to "
        f"[host]{plan.target_server.name}[/host].",
    )
    if not bool(_ask(questionary.confirm("Proceed?", default=False, style=_STYLE))):
        return False

    # Asked last, so the operator has seen the whole plan before adjudicating the
    # compatibility question.
    return confirm_drift(plan)


def confirm_destructive(plan: MigrationPlan) -> bool:
    """Typed confirmation for `--finalize delete`.

    A yes/no prompt is too cheap for an irreversible action: it is one keystroke
    from muscle memory. Typing the project name is a deliberate speed bump, and
    it also proves the operator knows *which* project they are about to destroy.
    """
    if plan.finalize_policy is not FinalizePolicy.DELETE:
        return True

    console = get_console()
    console.print(
        f"\n[err]--finalize delete[/err] will DELETE the source resources and their "
        f"volumes on [host]{plan.source_server.name}[/host] after the target is verified.\n"
        "This is the only irreversible step. Everything else can be rolled back.",
    )
    typed = _ask(
        questionary.text(
            f"Type the project name ({plan.project}) to confirm:",
            style=_STYLE,
        )
    )
    if typed.strip() != plan.project:
        console.print("[warn]Names do not match - aborting.[/warn]")
        return False
    return True
