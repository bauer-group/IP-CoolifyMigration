# Security Policy

> **{{COMPANY_NAME}}** | {{REPO_FULL_NAME}}

## Supported Versions

| Version      | Supported          |
| ------------ | ------------------ |
| Latest minor | :white_check_mark: |
| Older        | :x:                |

The current version is on the [releases page]({{REPO_URL}}/releases/latest).

## Reporting a Vulnerability

Report security issues privately via [GitHub Security Advisories]({{REPO_URL}}/security/advisories/new).

Please do not open a public issue for a vulnerability. We aim to acknowledge
within 2 business days and to ship a fix or mitigation within 30 days.

## Threat model

This tool is operated by an administrator against infrastructure they already
control. It is not a multi-tenant service and has no privilege boundary of its
own. That said, it handles material worth protecting:

- **A team-wide Coolify API token with `root`, or `read` + `read:sensitive` +
  `write` + `deploy` (plus `delete` from Coolify 4.6)**, issued by a team admin.
  It can read every secret of every project in the team and delete its resources.
  The tool requires such a token by design (Coolify silently omits secret values
  otherwise), which makes the token the single most sensitive input.
- **SSH access to both servers**, effectively as root: there is no sudo support.
- **A transfer key from the source to the target.** rsync runs on the servers, so
  the source has to log in to the target. The key is limited with `restrict` (no
  forwarding, no pty) but cannot be limited to rsync, so while it is installed a
  compromised source can run commands on the target.
- **Locked environment variables.** No API read returns their values, so the tool
  reads them from the files Coolify wrote on the source server.
- **`APP_KEY`** during instance migration, which decrypts Coolify's entire
  credential store.

## Security invariants

These are enforced in code and must never be weakened. The numbers match
`AGENTS.md`, which has the reasoning.

1. **Never `chown`.** Ownership is preserved numerically (`--numeric-ids`).
   Coolify's own clone hardcodes `chown -R 1000:1000` and that is precisely what
   corrupts postgres/mysql/redis (uid 999) and clickhouse (uid 101) volumes.
2. **Never route volume data through the operator's workstation filesystem.**
   rsync runs on the servers; a tunnel relays TCP only.
3. **Never disable host-key checking.** No `StrictHostKeyChecking=no`.
4. **Never write SQL to `coolify-db`.** The REST API is the only write path.
5. **Never log secrets** — environment variable values, private keys, `APP_KEY`.
   Redaction is a structlog processor and is applied additively.
6. **Never journal secrets.** The journal raises rather than redacting, so a
   caller's bug surfaces instead of being hidden.
7. **Never start a target whose FQDN still resolves to the source.**
8. **Never delete the source before verification passes.**
9. **Never swallow a stop failure**, and never trust a stop endpoint — poll the
   Docker daemon and require every container, previews included, to have exited
   without a SIGKILL.
10. **Never string-replace a UUID to derive a volume name.** Pair by `mount_path`.

## Credential handling

- Settings, the API token included, are read from the environment or a `.env`
  file; never hardcoded, never committed, never written to the journal.
- Recovered locked variables stay in memory and reach only the target, through
  the Coolify API. They are not written to the plan, the journal or a log, so a
  resumed run reads them again.
- A transfer key is minted per migration. Its private half sits in a mode-700
  directory on the source, its public half in the target's `authorized_keys`, and
  it stays there until the migration is finalized or rolled back (an instance
  migration revokes it when the old instance is fenced). Only its fingerprint is
  journalled.
- A crashed run still owes that revocation: resuming or rolling back performs it,
  and an abandoned run leaves the key installed until one of them does. A failed
  revocation does not stop the run; it is logged as an error naming the line to
  remove.
- The journal stores references: resource UUIDs and names, hostnames, host
  addresses and the key fingerprint. It never stores secret values.
- The saved plan next to it (`plan.json`) holds no environment variable values
  and no keys, but it does hold each resource's compose file, so a credential
  written literally into a compose file is stored there. It is written with your
  default file permissions: keep the state directory private.

## Scanning

Secret scanning is GitHub's own: secret scanning with push protection, validity
checks, non-provider patterns and AI detection are enabled on this repository.
Dependencies are scanned with Trivy on every push, pull request and weekly (the
`security.yml` workflow), and monitored by Dependabot.

---

_Last updated: {{LAST_UPDATED}}_
