# Troubleshooting

## `doctor` says the token cannot read sensitive data (exit 10)

Create a new token in Coolify under **Keys & Tokens > API tokens** with `root` or
`read:sensitive`.

This is not a nicety. Coolify's `ApiSensitiveData` middleware omits `value`,
`real_value` and `docker_compose_raw` for tokens without that scope — **HTTP 200,
no error, no redaction marker, the keys are simply absent**. A migration would
recreate every environment variable empty and every service without its compose.

## The DNS gate blocked me (exit 3)

Working as designed. It is a resumable stop, not a failure — the target is created
and its data is verified.

```bash
# Apply the printed cutover checklist to your DNS, wait out the TTL, then:
coolify-migrate resume <id>
```

There is no `--force`, because starting early is *actively harmful*: Traefik on
the new host requests an ACME certificate, the HTTP-01 challenge is routed by DNS
to the **old** host, the challenge fails, and Let's Encrypt rate-limits 5 failed
validations per hostname per hour. A retry loop burns that budget, so even a
correct cutover an hour later cannot get a certificate.

### "resolves to neither source nor target"

A CDN or reverse proxy (typically Cloudflare's orange cloud) is in front. DNS
then tells us nothing about where the origin points, so we surface it rather than
guess. Check the CDN's origin setting before starting the target.

## Rebuild drift blocked me (exit 4)

The target would rebuild from branch HEAD, which is not what the source is
running — so byte-exact data would land under different code. If that code has
already applied migrations to the data, the mismatch is real corruption.

Options, in order of preference:

1. Deploy the source from current HEAD first, so the two agree, then migrate.
2. Accept it consciously: `--accept-rebuild-drift`.

You cannot pin the commit: `git_commit_sha` is settable but the deploy job
resolves `git ls-remote refs/heads/{branch}` and overwrites it. We report the
drift rather than pretend to prevent it.

### "the compose in git declares different volumes"

Topology drift, and the reason this blocks hard rather than warning. Coolify
re-reads a `dockercompose` application's compose from git on every deploy, so the
target would materialise a *different* volume set than the source has. The
old→new mapping would then be silently wrong and your data would land nowhere.

Reconcile the compose in git with what is deployed, then retry.

## Quiesce failed: SIGKILL (exit 5)

A container exited 137 — the stop timeout was hit and Docker killed it. A killed
database has not flushed, so its volume is a torn snapshot.

Raise the resource's stop grace period in Coolify, or raise `STOP_TIMEOUT`, and
retry. This is fatal by design: mirroring an unflushed data directory
byte-exactly just gives you a faithful copy of corruption.

## Quiesce failed: preview deployments present

Coolify's stop endpoint does **not** stop preview containers
(`StopApplication` filters `pullRequestId=0`), so they would keep writing while
volumes are mirrored.

Delete them first — they are rebuilt from the PR, so nothing of value is lost:

```bash
coolify-migrate run ... --delete-previews
# or manually: DELETE /v1/applications/{uuid}/previews/{pr_id}
```

## "anonymous volume: its id cannot be reproduced"

Your compose has a mount like `- /data` with no source. Docker names it with a
random 64-hex id that cannot be recreated on the target, and there is no stable
key to pair it by.

Give it a name in the compose:

```yaml
volumes:
  - app-data:/data     # instead of: - /data
```

Refusing beats silently starting with an empty volume.

## Verification failed (exit 7)

The copy does not match. The target was **not** started and the migration rolled
back; your source is untouched.

The report names every difference. `metadata_differs` with different uid/gid is
the important one — it means ownership changed, which is exactly what stops a
database from starting, and exactly what a content-only checksum would have
missed.

## Rollback failed (exit 8)

A compensating action could not run, so the system is in a state only you can
adjudicate. The error names the journal path.

**Your source has not been deleted** — that only ever happens at an explicit,
confirmed finalize. Inspect with:

```bash
coolify-migrate status <id>
```

## `422 This field is not allowed`

Our request whitelist has drifted from upstream Coolify. Coolify names the
offending field in the response.

Please open an issue with that field name. A scheduled CI job checks for this
weekly precisely so it breaks our CI rather than your migration — but Coolify may
have changed since the last run.

## Host key is not known

```bash
# Verify the fingerprint out of band first, then:
coolify-migrate run ... --trust-host-key
```

We never disable host-key checking. A *changed* key is still refused even with
`--trust-host-key`.

## The transfer only uses one stream

Expected if the volume contains hardlinks — the plan says so in its `reason`.
`rsync -H` only detects hardlinks within a single invocation, so splitting would
explode them into duplicate files. A backup volume using hardlinked snapshots can
go from 50 GB to several hundred. Correctness beats parallelism.
