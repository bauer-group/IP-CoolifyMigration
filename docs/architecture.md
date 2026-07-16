# Architecture

## Pure cores, thin IO shells

This is the load-bearing design decision, not a stylistic preference.

Both tools this replaces are broken *because* their logic is inseparable from
their side effects. coolify-mover's volume-name derivation, its stop handling and
its rsync invocation are all inlined into one procedure that talks to Postgres,
SSH and the filesystem — so none of it can be tested, and all of it is wrong.

So: every decision lives in `domain/`, which performs no IO and is total over
captured snapshots. The volume-pairing algebra, the drift gates, the rollback
planner and the compose analysis are all verified exhaustively without a Coolify
instance, an SSH server or a Docker daemon.

```text
cli (Typer)  ->  wizard / dashboard (Rich)
                      |
                 engine (saga: plan -> execute -> verify -> compensate)
                      |            [performs what domain/statemachine decides]
        +-------------+-------------+-------------+
        |             |             |             |
     api/          discovery/    transfer/      dns/
   (httpx)        (docker+api)   (asyncssh)   (dnspython)
        \-------------- journal/ (JSONL, fsync) --------------/
```

| Layer | Purity | Responsibility |
| --- | --- | --- |
| `domain/` | **PURE** | Every decision. No IO, ever. |
| `api/` | IO | Coolify REST, with per-endpoint request whitelists |
| `discovery/` | IO | Docker facts; the label-based quiesce gate |
| `transfer/` | IO | asyncssh, rsync, checksum manifests |
| `dns/` | IO | Authoritative resolution |
| `journal/` | IO | Append-only crash-safe state |
| `engine/` | Executor | Walks the state machine, compensates on failure |
| `ui/` | Render | Surfaces the `reason` domain objects already carry |

If you need a mock to test a decision, the decision is in the wrong layer.

## The domain modules

| Module | Answers |
| --- | --- |
| `kinds.py` | What kind of resource is this? Which API route recreates it? |
| `compose.py` | Does it build (`build:` vs `image:`)? What does it mount? |
| `naming.py` | Which source volume maps to which target volume? |
| `manifest.py` | What are we copying, what are we skipping, and why? |
| `drift.py` | Would the rebuild ship different code? |
| `plan.py` | What is the strategy, and is anything blocking? |
| `statemachine.py` | What happens next, and what undoes it? |

## Why "does it build?" is not a property of the kind

A Coolify service and a `build_pack=dockercompose` application are both compose,
and either may declare `build:` instead of `image:`. So `builds` is an *input* to
strategy selection, read from the compose document, not derived from the kind:

```python
select_strategy(kind, builds=..., has_volumes=...)
```

The same `ResourceKind.SERVICE_COMPOSE` yields `COPY_DATA` (zero drift) or
`REBUILD` (gated) depending on its YAML. Conflating the two would silently skip
the drift gate for every compose stack that builds from source.

## The F1 state machine

```text
PREFLIGHT      token scope; reachability; rsync/docker present; disk vs REAL size;
               previews detected; images resolvable; REBUILD-DRIFT GATE
   |
PLAN           build the plan + provisional manifest   [dry-run stops here]
   |
CREATE_TARGET  API only, instant_deploy=false          undo: delete target
   |
QUIESCE        stop, then POLL the daemon by label     undo: restart source
   |
DISCOVER       authoritative post-stop reconciliation;
               pair old->new by mount_path
   |
COPY           parallel rsync -aHAXS --numeric-ids     undo: drop target volumes
   |
VERIFY         content + metadata manifests both sides
   |
DNS_GATE       custom domain -> source?  STOP, exit 3, resumable
               (server-bound wildcard URLs rewritten, never gate;
                --accept-dns downgrades the stop to a warning)
   |
START_TARGET   deploy                                  undo: stop target
   |
HEALTHCHECK    containers healthy
   |
FINALIZE       keep | rename | delete                  <- the only irreversible step
```

Three orderings are deliberate and worth the argument:

- **`CREATE_TARGET` before `QUIESCE`.** A failed create then costs zero downtime.
  Decisively: the target must exist before volumes can be paired, because pairing
  reads back what Coolify actually created.
- **`DISCOVER` after `QUIESCE`.** Discovery before the stop is provisional — a
  running stack can still create volumes. The authoritative manifest is the one
  taken when nothing can write.
- **`DNS_GATE` between `VERIFY` and `START_TARGET`.** Earlier wastes a good
  transfer; later has already caused the ACME damage the gate exists to prevent.

## Testing strategy

| Layer | How | Why it is meaningful |
| --- | --- | --- |
| `domain/` | Table-driven, zero IO | The decisions that hold the correctness |
| `api/` | respx against recorded fixtures | Contract, incl. the token-scope trap |
| `discovery/`, `transfer/` | `FakeHost` with scripted daemon output | The quiesce gate is testable without a server |
| `engine/` | **Chaos suite** | Kill mid-COPY; assert resume and rollback converge |
| `transfer/` | **Integration rig**: two real sshd containers | Only real rsync can prove real bytes |
| whitelists | Scheduled CI vs upstream `openapi.json` | API drift breaks *our* CI, not your migration |

### The chaos suite

Kills the process at every state and asserts that resume and rollback converge.
Possible only because the rollback planner is pure: `rollback_plan(completed)`
is a total function, so "what happens if we die at COPY?" is a table test, not an
experiment. Neither predecessor has any journal, resume or rollback to test.

### The integration rig

```bash
python tests/integration/prepare.py
docker compose -f tests/integration/docker-compose.yml up -d --wait
pytest -m integration
docker compose -f tests/integration/docker-compose.yml down -v
```

Two Alpine containers running real `sshd` with real `rsync`. Each test
corresponds to a flag `coolify-mover` omits, and asserts the actual on-disk
result: a file owned by **uid 999** stays 999; a **hardlink** pair still shares an
inode; an **xattr** survives; a **sparse** file stays sparse; a **dangling
symlink** is not resolved; and a wrong `chown` *is caught by verification*.

This is not belt-and-braces. It immediately found a data-loss bug that no amount
of unit testing could:

> **`--files-from` turns OFF the recursion that `-a` implies.** A chunked
> transfer was copying directory *entries* and no files — with rsync exiting 0
> and the tree looking correct. It would have hit only the large volumes that get
> chunked in the first place.

The lesson generalises: a pure core proves the *decisions*, and only real IO
proves the *effects*. Both are needed, and neither substitutes for the other.
