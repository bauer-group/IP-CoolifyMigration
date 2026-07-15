# Safety model

What is guaranteed, how, and — just as importantly — what is not.

## The core claim

**Nothing is writing while we copy.** Everything else rests on this. If it is
false, byte-exact verification is worthless: we would have faithfully verified a
torn snapshot.

That is why the tool is *application-unaware*. We never ask "is this Postgres?"
We ask "is this container stopped?" Once it is, a volume is just bytes, and a
ClickHouse, a Redis and a service nobody has ever heard of are the same problem.

## Why we do not trust Coolify's stop endpoint

Three verified upstream behaviours make "call stop and proceed" unsafe:

1. **Every stop is asynchronous.** `action_stop` is a `dispatch(...)`; the HTTP
   call returns before anything has stopped.
2. **Applications: previews are not stopped.**
   `StopApplication::dispatch($application, false, ...)` — the second argument is
   `$previewDeployments`, and the API always passes `false`. Preview containers
   keep running and **keep writing**.
3. **Services: containers are found from DB records, not labels.** `StopService`
   stops names built from the parsed model, so a compose container Coolify never
   parsed is never stopped.

So we poll the daemon: `docker ps -a --filter label=coolify.*Id={id}`, and require
**every** container — previews included — to be `exited`.

Note the `-a`. Geczy's script uses bare `docker ps`, which is exactly why a
stopped container's volume gets silently skipped there.

## Why a SIGKILL is fatal, not a warning

A container that exits with 137 hit the stop timeout and was killed. A killed
database has not flushed. Mirroring an unflushed data directory byte-exactly just
gives you a faithful copy of corruption — so this is a hard failure with no
`--force`. Raise the resource's stop grace period and retry.

## Security invariants

Enforced in code. See `AGENTS.md` for the authoritative list.

1. **Never `chown`.** `rsync --numeric-ids` preserves uid/gid numerically.
   Coolify's own clone hardcodes `chown -R 1000:1000`, which corrupts every DB
   volume (postgres/mysql/redis = 999, clickhouse = 101). That single line is why
   upstream disabled the feature.
2. **Never route data through the operator's workstation filesystem.**
3. **Never disable host-key checking.**
4. **Never write SQL to `coolify-db`.**
5. **Never log or journal secrets.** The journal *raises* rather than redacting —
   a redaction hides the caller's bug.
6. **Never start a target whose FQDN still resolves to the source.**
7. **Never delete the source before verification passes.**
8. **Never swallow a stop failure.**
9. **Never string-replace a UUID to derive a volume name.**

## Volume pairing

Volume names **cannot** be preserved. `POST /storages` forces
`name = '{new_resource_uuid}-{name}'`, and the DB model hooks force
`{engine}-data-{new_uuid}`. The target's names are whatever Coolify decides.

So we create the target, let Coolify materialise its own volumes, read them back,
and pair source to target by **`mount_path`** — the one key that is stable,
because it is a property of the container rather than of Coolify.

The alternative — string-replacing the uuid — is how coolify-mover silently loses
every service volume: the volume is named after the *parent service's* uuid, the
tool replaces the *sub-application's*, the replace matches nothing, and Docker
auto-creates an empty volume at deploy with no error.

An ambiguous or unpaired volume is **refused**, never guessed at. An unpaired
source volume is data left behind; an unpaired target volume is one that starts
empty.

## Verification

Two manifests per side, both required:

- **Content** — SHA-256 of every regular file, hashed in parallel.
- **Metadata** — type, mode, uid, gid and symlink target of every entry.

Content alone is not enough. Two files with identical bytes but different
ownership hash identically, and ownership is what decides whether Postgres
starts. `sha256sum` also cannot read sockets or FIFOs, and a Postgres data
directory routinely contains one.

## Rollback

Every state that mutates anything has a compensating action, journalled to disk
with enough information to run after a total crash of your machine.

```text
CREATE_TARGET  -> delete the target
QUIESCE        -> restart the source
COPY           -> drop the target's volumes, revoke the ephemeral key
START_TARGET   -> stop the target
FINALIZE       -> restore the source's name and FQDN
```

Compensations run in reverse: the target must be stopped before its volumes are
dropped, and its volumes dropped before the resource is deleted, or we leak
volumes nothing references.

**Rollback is cheap because the source is never destroyed until FINALIZE.** That
is the one thing Geczy's script gets right, and it is the backbone of the whole
safety story.

If a compensation itself fails, the remaining ones still run — a failure to
delete the target must not also prevent restarting the source — and the run exits
`8` naming what is broken and where the journal is. We never compensate a
compensation.

## Accepted risks

Stated plainly rather than papered over.

- **A rebuild is never byte-identical.** `git_commit_sha` does not pin a deploy:
  `check_git_if_build_needed()` resolves `git ls-remote refs/heads/{branch}` and
  overwrites it, and the API never sets the `rollback:` flag that would bypass
  that. On top of which `docker build --pull` is forced, so unpinned `FROM` tags
  refresh on every build. We detect and gate the drift; we cannot remove it.
- **~25 `ApplicationSetting` fields are unreadable over the API.**
  `GET /applications/{uuid}` does not eager-load the `settings` relation, yet
  several of those fields are settable — a write-only asymmetry. We recover what
  Docker can prove (Traefik labels expose `is_force_https_enabled`,
  `is_gzip_enabled`, `is_stripprefix_enabled`; networks expose
  `connect_to_docker_network`) and surface the rest as explicit questions. We
  never guess.
- **Compose comments and formatting are destroyed** on the service path: Coolify
  round-trips `docker_compose_raw` through `Yaml::dump(Yaml::parse(...))`.
- **Registry credentials have no API surface.** They live in
  `~/.docker/config.json` on each server and must be provisioned out of band.
  Preflight checks that the image resolves before anything is stopped.
- **Anonymous volumes are refused.** Docker names them with a random 64-hex id
  that cannot be reproduced on the target, and there is no stable key to pair
  them by. Refusing beats silently starting with an empty volume.
