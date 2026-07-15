# Contributing

## Setup

```bash
uv venv --python 3.14
uv pip install -e ".[dev,docs]"
pre-commit install
```

Requires Python 3.12, 3.13 or 3.14. **Develop on 3.14**; the CI matrix runs all
three.

ruff and mypy are pinned to **3.12** on purpose, and raising them would quietly
end the backward compatibility rather than modernise anything: `target-version`
and `python_version` tell those tools which version to *check against*, not
which one you run. At 3.12 they reject 3.13+ syntax before it reaches a user on
the floor of the range.

## Quality gates

```bash
make all-checks     # or: .\Make.cmd all-checks
```

Runs ruff, mypy `strict`, and pytest with a **80% coverage gate**. All must pass
before a PR.

Integration tests need Docker and are excluded by default. They stand up two
real sshd containers and exercise real rsync — the only way to prove that a file
owned by uid 999, with an xattr and a hardlink, actually arrives intact:

```bash
python tests/integration/prepare.py
docker compose -f tests/integration/docker-compose.yml up -d --wait
pytest -m integration
docker compose -f tests/integration/docker-compose.yml down -v
```

The rig earns its keep: it found a data-loss bug (`--files-from` disables the
recursion `-a` implies, so chunked transfers copied directory entries and no
files, exiting 0) that no unit test could have caught.

## The one architectural rule

**Decisions go in `domain/`. It performs no IO, ever.**

This is not style. Both tools this project replaces are broken *because* their
logic is inseparable from their side effects, so neither can be tested — and both
silently lose data as a result. Everything that decides something (volume
pairing, drift, classification, rollback planning) is a pure function over a
captured snapshot, and is therefore verified exhaustively without a Coolify
instance, an SSH server or a Docker daemon.

If you find yourself wanting a mock to test a decision, the decision is in the
wrong layer.

## Security invariants

`AGENTS.md` lists ten non-negotiable invariants (never `chown`, never trust a
stop endpoint, never string-replace a UUID to derive a volume name, …). Each
encodes a specific, verified way that a predecessor tool destroys data.

**If a change would relax any of them, stop and open an issue first.**

## Testing expectations

- New logic in `domain/` → table-driven tests covering the edges.
- New API call → its field whitelist in `api/fields.py` **first**.
  `$allowedFields` is enforced upstream and unknown fields are a 422 per field.
- New IO in `discovery/` or `transfer/` → use the `FakeHost` fixture.
- New saga step → a chaos test asserting its compensation.

Prefer a test that encodes *why* over one that restates the code. Several tests
here name the upstream bug they prevent; that comment is the point.

## Commits

Conventional Commits, **past tense** subject, max 50 characters, no trailing
period:

```text
feat(dns): added authoritative resolver for the cutover gate
fix(transfer): kept hardlinked trees in a single rsync stream
docs(safety): documented the SIGKILL quiesce failure
```

Types: `feat` `fix` `docs` `style` `refactor` `perf` `test` `chore` `ci` `build`
`revert`. Breaking: `type!` or a `BREAKING CHANGE:` footer.

**Never include `Co-Authored-By:` or any AI attribution.**

One commit = one logical change. Each must work standalone. No WIP on `main`.

## Docs

`README.MD` and `SECURITY.MD` are **generated**. Edit `docs/README.template.MD`
or `docs/SECURITY.template.MD`, then:

```bash
python scripts/generate-docs.py
```

CI fails if they are stale.

## Releases

Automated by python-semantic-release on push to `main`. `feat` → minor, `fix`/
`perf` → patch. The version lives in `src/bg_coolify_migrate/__init__.py` and is
the single source of truth.

## Reporting a vulnerability

See [SECURITY.MD](SECURITY.MD). Do not open a public issue.
