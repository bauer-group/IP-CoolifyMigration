<!-- AUTO-GENERATED FILE. DO NOT EDIT. Edit docs/SECURITY.template.MD instead. Generated 2026-07-15. -->
# Security Policy

> **BAUER GROUP** | bauer-group/IP-CoolifyMigration

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1 | :x:                |

## Reporting a Vulnerability

Report security issues privately to **security@bauer-group.com** or via
[GitHub Security Advisories](https://github.com/bauer-group/IP-CoolifyMigration/security/advisories/new).

Please do not open a public issue for a vulnerability. We aim to acknowledge
within 2 business days and to ship a fix or mitigation within 30 days.

## Threat model

This tool is operated by an administrator against infrastructure they already
control. It is not a multi-tenant service and has no privilege boundary of its
own. That said, it handles material worth protecting:

- **A Coolify API token with `root` / `read:sensitive`** can read every secret of
  every project in the team. The tool requires such a token by design (Coolify
  silently omits secret values otherwise), which makes the token the single most
  sensitive input.
- **SSH access to both servers**, effectively as root.
- **`APP_KEY`** during instance migration, which decrypts Coolify's entire
  credential store.

## Security invariants

These are enforced in code and must never be weakened. See `AGENTS.md`.

1. **Never `chown`.** Ownership is preserved numerically (`--numeric-ids`).
   Coolify's own clone hardcodes `chown -R 1000:1000` and that is precisely what
   corrupts postgres/mysql/redis (uid 999) and clickhouse (uid 101) volumes.
2. **Never route volume data through the operator's workstation filesystem.**
3. **Never disable host-key checking.** No `StrictHostKeyChecking=no`.
4. **Never write SQL to `coolify-db`.** The REST API is the only write path.
5. **Never log secrets** — environment variable values, private keys, `APP_KEY`.
   Redaction is a structlog processor and is applied additively.
6. **Never start a target whose FQDN still resolves to the source.**
7. **Never delete the source before verification passes.**
8. **Never swallow a stop failure**, and never trust a stop endpoint — poll the
   Docker daemon.
9. **Never string-replace a UUID to derive a volume name.** Pair by `mount_path`.

## Credential handling

- Secrets are read from the environment or a `.env` file; never hardcoded, never
  committed, never written to the journal.
- Ephemeral SSH keys minted for a direct transfer are revoked in a guaranteed
  cleanup step, and the revocation is journalled so a crashed run still cleans up
  on the next invocation.
- The journal stores resource UUIDs and host addresses. It never stores secret
  values.

## Scanning

Secret scanning (gitleaks + GitGuardian) runs on every push via the
`security-management` reusable workflow. Dependencies are monitored by Dependabot.

---

_Last updated: 2026-07-15_
