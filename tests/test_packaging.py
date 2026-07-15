"""Guards against the two places a duplicated fact can rot.

Same reasoning as tests/test_api_fields.py checking our whitelists against
upstream: a list that is transcribed rather than derived is correct exactly once
unless something keeps it honest.

These run in the normal suite — they are pure file reads, no rig required.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
RUNNER_DOCKERFILE = ROOT / "tests" / "e2e" / "runner.Dockerfile"

#: On top of the runtime deps, the only things the e2e suite itself needs. The
#: rest of [dev] is lint and release tooling with no business in an image that
#: exists to run four tests.
RUNNER_EXTRA = {"pytest", "pytest-asyncio"}


def _pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _requirement_name(spec: str) -> str:
    return re.split(r"[><=!~\[]", spec)[0].strip().lower()


def _runner_specs() -> list[str]:
    """The `pip install` arguments in the runner image, verbatim."""
    text = RUNNER_DOCKERFILE.read_text(encoding="utf-8")
    block = text[text.index("RUN pip install") :]
    block = block[: block.index("\nENV ")]
    return re.findall(r'"([^"]+)"', block)


def test_runner_deps_match_pyproject() -> None:
    """The e2e image must install what the package declares — the same ranges.

    Not pedantry. These drifted the day they were written (`rich>=13.9,<15` in
    the image against `>=13.7.0,<16.0.0` in pyproject), which means the rig would
    have been proving the tool works against a version the tool does not permit,
    or missing a version it does. A rig that tests a different program than the
    one we ship is worse than no rig, because it is believed.
    """
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    declared = {_requirement_name(d): d for d in project["dependencies"]}

    installed = {_requirement_name(s): s for s in _runner_specs()}
    runtime_installed = {k: v for k, v in installed.items() if k not in RUNNER_EXTRA}

    assert runtime_installed == declared, (
        "tests/e2e/runner.Dockerfile has drifted from pyproject.toml.\n"
        "Copy the dependencies across verbatim; do not retype the ranges."
    )


def test_runner_installs_what_the_e2e_suite_needs() -> None:
    """pytest and pytest-asyncio, and nothing else out of [dev]."""
    installed = {_requirement_name(s) for s in _runner_specs()}
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    declared = {_requirement_name(d) for d in project["dependencies"]}

    assert installed - declared == RUNNER_EXTRA


def test_the_supported_floor_is_what_the_tools_check_against() -> None:
    """ruff and mypy must target the OLDEST supported version, not the newest.

    This looks like a version lagging behind and is the opposite. `target-version`
    and `python_version` say which version those tools check *against*; at the
    floor they reject 3.13+ syntax before it reaches a user on 3.12. Raising them
    to match the interpreter we develop on would end the backward compatibility
    silently, and the CI matrix would only notice if a test happened to cover the
    line in question.
    """
    config = _pyproject()
    project = config["project"]
    tool = config["tool"]
    assert isinstance(project, dict)
    assert isinstance(tool, dict)

    floor = re.search(r"(\d+)\.(\d+)", str(project["requires-python"]))
    assert floor, "requires-python does not name a version"
    major, minor = floor.groups()

    assert tool["ruff"]["target-version"] == f"py{major}{minor}"
    assert tool["mypy"]["python_version"] == f"{major}.{minor}"


def test_every_supported_version_is_classified_and_tested() -> None:
    """The classifiers, the CI matrix and requires-python must agree.

    Three places claiming a supported range; two agreeing and one not is how a
    version gets shipped untested.
    """
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    classified = {
        m.group(1)
        for c in project["classifiers"]
        if (m := re.fullmatch(r"Programming Language :: Python :: (\d+\.\d+)", c))
    }

    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    matrix_line = next(line for line in workflow.splitlines() if "python-version: [" in line)
    tested = set(re.findall(r"'(\d+\.\d+)'", matrix_line))

    assert classified == tested, (
        f"classifiers say {sorted(classified)} but CI tests {sorted(tested)}"
    )

    floor = re.search(r"(\d+\.\d+)", str(project["requires-python"]))
    assert floor and floor.group(1) == min(tested, key=lambda v: tuple(map(int, v.split(".")))), (
        "requires-python must name the lowest version CI actually tests"
    )
