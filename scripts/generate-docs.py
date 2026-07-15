#!/usr/bin/env python3
"""generate-docs.py — Render README.MD and SECURITY.MD from their templates.

The generated files carry a DO-NOT-EDIT banner and are regenerated on release by
the `bauer-group/automation-templates` documentation workflow. This script exists
so the same render can be produced locally (and so the working tree is valid for
`pip install -e .`, which needs README.MD to exist before CI ever runs).

Templates live in docs/*.template.MD and use {{PLACEHOLDER}} markers.

Usage
-----
    python scripts/generate-docs.py            # render all templates
    python scripts/generate-docs.py --check    # exit 1 if output is stale (CI)

Exit codes
----------
    0  rendered (or up to date with --check)
    1  a template is missing, or output is stale under --check
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

REPO_FULL_NAME = "bauer-group/IP-CoolifyMigration"
REPO_URL = f"https://github.com/{REPO_FULL_NAME}"
COMPANY_NAME = "BAUER GROUP"

# template -> generated output
TEMPLATES: dict[str, str] = {
    "README.template.MD": "README.MD",
    "SECURITY.template.MD": "SECURITY.MD",
}

BANNER = (
    "<!-- AUTO-GENERATED FILE. DO NOT EDIT. Edit docs/{template} instead. "
    "Generated {date}. -->\n"
)


def current_version() -> str:
    """Read __version__ from the package — the single source of truth."""
    init = ROOT / "src" / "bg_coolify_migrate" / "__init__.py"
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
    if not match:
        print("error: could not find __version__ in __init__.py", file=sys.stderr)
        raise SystemExit(1)
    return match.group(1)


def current_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip() or "main"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "main"


def supported_versions_table(version: str) -> str:
    """The SECURITY.MD support matrix: current minor supported, older not."""
    major_minor = ".".join(version.split(".")[:2])
    return f"| {major_minor}.x   | :white_check_mark: |\n| < {major_minor} | :x:                |"


def render(template_name: str, version: str) -> str:
    src = DOCS / template_name
    if not src.exists():
        print(f"error: missing template {src}", file=sys.stderr)
        raise SystemExit(1)
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    body = src.read_text(encoding="utf-8")
    replacements = {
        "{{COMPANY_NAME}}": COMPANY_NAME,
        "{{VERSION}}": version,
        "{{CURRENT_VERSION}}": version,
        "{{DATE}}": date,
        "{{LAST_UPDATED}}": date,
        "{{REPO_URL}}": REPO_URL,
        "{{REPO_FULL_NAME}}": REPO_FULL_NAME,
        "{{CURRENT_BRANCH}}": current_branch(),
        "{{SUPPORTED_VERSIONS_TABLE}}": supported_versions_table(version),
    }
    for marker, value in replacements.items():
        body = body.replace(marker, value)
    return BANNER.format(template=template_name, date=date) + body


def _strip_banner(text: str) -> str:
    """Drop the banner so --check ignores the embedded timestamp."""
    lines = text.splitlines(keepends=True)
    return "".join(lines[1:]) if lines and lines[0].startswith("<!-- AUTO-GENERATED") else text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if output is stale")
    args = parser.parse_args()

    version = current_version()
    stale: list[str] = []

    for template_name, output_name in TEMPLATES.items():
        rendered = render(template_name, version)
        target = ROOT / output_name
        if args.check:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            if _strip_banner(existing) != _strip_banner(rendered):
                stale.append(output_name)
            continue
        target.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"rendered {output_name} (v{version})")

    if stale:
        print(
            f"error: stale generated docs: {', '.join(stale)}\n"
            "run: python scripts/generate-docs.py",
            file=sys.stderr,
        )
        return 1
    if args.check:
        print("generated docs are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
