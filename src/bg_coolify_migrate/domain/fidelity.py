"""What an API re-creation cannot carry to the target, decided at plan time.

PURE module: no IO. Every function reads something already captured — a GET, an
env list, a compose document — and returns human-readable lines. The planner
turns REFUSALS into hard blocks and WARNINGS into plan warnings.

Why this exists: Coolify 4.3.11-4.5.0 moved state to places the REST API cannot
reach, or started refusing on create what it had stored for years. The drift
canary saw eight new create fields; an audit of the release tags (2026-10-26,
v4.3.10 -> v4.5.0) found the rest. Each rule cites the release it starts in and
is gated on it, so it never refuses a migration on an instance where it cannot
fail that way.

Refusal vs warning follows AGENTS.md. Refuse what is not a judgement: a create
that WILL 422, a deploy that WILL fail after the source is stopped, data that
would silently be left behind. Warn about a setting the target will not share,
because whether that matters is the operator's call about their stack.

No line produced here ever contains a secret's VALUE — key names and lengths
only. These lines are printed and journaled, and invariant 5 does not bend for a
helpful error message.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
from typing import Any
from urllib.parse import urlsplit

from bg_coolify_migrate.domain import compose as compose_mod
from bg_coolify_migrate.domain.kinds import DatabaseEngine
from bg_coolify_migrate.domain.versions import Version, at_least, parse_version

# ── where each rule starts ───────────────────────────────────────────────────

#: An explicit domain port moves into the hidden applications.domain_port_overrides.
PORT_OVERRIDES_HIDDEN_SINCE: Version = (4, 3, 15)
#: `host_path` leaves every storage create array (backported to the 4.3 line).
HOST_PATH_REMOVED_SINCE: Version = (4, 3, 22)
#: ManualWebhookSecret: a new secret must be >= 16 characters.
WEBHOOK_SECRET_MIN_LENGTH_SINCE: Version = (4, 4, 0)
#: {{vault.KEY}} references, resolved from a link no GET returns.
SECRET_MANAGER_SINCE: Version = (4, 4, 0)
#: External and driver_opts compose volumes are used as written, not renamed.
COMPOSE_VOLUMES_AS_WRITTEN_SINCE: Version = (4, 4, 0)
#: Docker compose is stored verbatim instead of Yaml::dump(Yaml::parse()).
COMPOSE_STORED_VERBATIM_SINCE: Version = (4, 4, 0)
#: Redis/KeyDB/Dragonfly rows that predate it keep the unquoted --requirepass.
REDIS_PASSWORD_QUOTING_SINCE: Version = (4, 4, 0)
#: http_basic_auth_password gains max:72 on create.
BASIC_AUTH_MAX_LENGTH_SINCE: Version = (4, 5, 0)
#: New env rows use exact escaping; rows saved before keep the legacy escaping.
EXACT_ENV_ESCAPING_SINCE: Version = (4, 5, 0)
#: Both compose parsers refuse `scale` / `deploy.replicas` above 1.
COMPOSE_SCALE_CHECK_SINCE: Version = (4, 5, 0)
#: convertDockerRunToCompose maps flags it used to ignore.
RUN_OPTIONS_PARSER_SINCE: Version = (4, 5, 0)

_WEBHOOK_SECRET_FIELDS = (
    "manual_webhook_secret_github",
    "manual_webhook_secret_gitlab",
    "manual_webhook_secret_bitbucket",
    "manual_webhook_secret_gitea",
)
#: app/Rules/ManualWebhookSecret.php::MIN_LENGTH (v4.4.0+), counted with mb_strlen.
_WEBHOOK_SECRET_MIN_LENGTH = 16
#: ApplicationsController v4.5.0:1354 `'string|nullable|max:72'` (mb_strlen).
_BASIC_AUTH_MAX_LENGTH = 72
#: bootstrap/helpers/api.php sharedDataApplications, identical v4.1.2-v4.5.0:
#: `['nullable', 'string', 'max:1000', 'regex:/^[a-zA-Z0-9 \-_.\/:=@,+]+$/']`.
_HEALTH_CHECK_COMMAND = re.compile(r"[a-zA-Z0-9 \-_./:=@,+]{1,1000}")

#: app/Support/RemoteSecretReferences.php::PATTERN (v4.4.0+).
_VAULT_REFERENCE = re.compile(r"\{\{\s*vault\.([A-Za-z0-9_]+)\s*\}\}")

_REDIS_FAMILY = frozenset({DatabaseEngine.REDIS, DatabaseEngine.KEYDB, DatabaseEngine.DRAGONFLY})


def _settings(source: dict[str, Any]) -> dict[str, Any]:
    settings = source.get("settings")
    return settings if isinstance(settings, dict) else {}


# ── applications ─────────────────────────────────────────────────────────────


def application_refusals(source: dict[str, Any], *, version: Version | None) -> list[str]:
    """What makes this application's create 422, or its routing silently wrong."""
    out: list[str] = []

    if at_least(version, WEBHOOK_SECRET_MIN_LENGTH_SINCE):
        short = [
            f"{field} ({len(value)} characters)"
            for field in _WEBHOOK_SECRET_FIELDS
            if isinstance(value := source.get(field), str)
            and 0 < len(value) < _WEBHOOK_SECRET_MIN_LENGTH
        ]
        if short:
            out.append(
                f"manual webhook secret(s) shorter than {_WEBHOOK_SECRET_MIN_LENGTH} "
                f"characters: {', '.join(short)}. Coolify >= 4.4 keeps such a secret on "
                "the application that already has it, but refuses it on create, so the "
                "target would be rejected outright. Rotate it on the source — and in the "
                "git host's webhook settings — to 16+ characters, or clear it."
            )

    if at_least(version, BASIC_AUTH_MAX_LENGTH_SINCE):
        password = source.get("http_basic_auth_password")
        if isinstance(password, str) and len(password) > _BASIC_AUTH_MAX_LENGTH:
            out.append(
                f"the HTTP basic auth password is {len(password)} characters; Coolify "
                f">= 4.5 refuses more than {_BASIC_AUTH_MAX_LENGTH} on create (it was "
                "never checked before), so the target would be rejected outright. "
                "Shorten it on the source — or clear it if basic auth is off — then "
                "migrate. Truncating it here would silently change the credential."
            )

    command = source.get("health_check_command")
    if isinstance(command, str) and command and not _HEALTH_CHECK_COMMAND.fullmatch(command):
        # The UI's "Enable healthcheck" button saves without validating, so a
        # stored command can fail the API's own rule (identical v4.1.2-v4.5.0).
        out.append(
            f"the health check command ({len(command)} characters) contains characters "
            "Coolify's API refuses on create — it allows only letters, digits, spaces and "
            "- _ . / : = @ , + and at most 1000 characters — so the target would be "
            "rejected outright. Simplify it on the source (e.g. move it into a script "
            "in the image), then migrate."
        )

    if at_least(version, PORT_OVERRIDES_HIDDEN_SINCE):
        hidden = hidden_port_overrides(source)
        if hidden:
            exposed = (
                80 if _settings(source).get("is_static") else _first_port(source.get("ports_exposes"))
            )
            out.append(
                f"the source routes a domain to port(s) {hidden}, but ports_exposes "
                f"starts with {exposed}. Since 4.3.15 Coolify keeps a domain's explicit "
                "port in a hidden column no API read returns, so the target would route "
                f"every domain to {exposed} — and parking the source's domains would "
                "erase that port on the source as well, beyond what a rollback restores. "
                "If every domain uses one port, put it first in ports_exposes on the "
                "source; otherwise migrate this application by hand."
            )

    return out


def application_warnings(source: dict[str, Any], *, version: Version | None) -> list[str]:
    """Settings the target will not share. Silent on a stock application."""
    out: list[str] = []
    settings = _settings(source)

    if source.get("is_http_basic_auth_enabled") and (
        source.get("http_basic_auth_hash_algorithm") == "argon2id"
    ):
        out.append(
            "HTTP basic auth hashes with argon2id on the source; the target hashes the "
            "same password with bcrypt, so credentials keep working. argon2id cannot be "
            "sent on create — it is rejected unless the target server runs Caddy >= "
            "2.11 — so re-select it on the target by hand if it does."
        )
    if resolver := settings.get("traefik_certificate_resolver"):
        out.append(
            f"traefik_certificate_resolver={resolver!r} cannot be migrated, so the target "
            "uses its server's default resolver and may obtain its certificate "
            "differently. Resolver names are defined per server: make sure the target "
            "server's Traefik has it, then set it on the target by hand."
        )
    if settings.get("is_permanent_redirect_enabled") is True:
        out.append(
            "permanent redirects (301/308) cannot be migrated; the target answers the "
            "HTTP-to-HTTPS and www redirects with temporary 302/307 until you re-enable "
            "it by hand."
        )
    if settings.get("include_coolify_default_variables") is False:
        out.append(_DEFAULT_VARIABLES_WARNING)
    if settings.get("is_container_label_readonly_enabled") is False and source.get("custom_labels"):
        out.append(
            "the source has readonly labels OFF, so its container labels may be "
            "hand-edited. The setting cannot be migrated, and a target created with it "
            "on regenerates its labels from its own domains — hand-made routers, "
            "middlewares or headers are lost. Re-apply them on the target by hand."
        )
    if settings.get("is_static") is True:
        out.append(
            "the source is a static site, and that setting rides under `settings`, which "
            "the create body never carries: the target is built and run as a regular "
            "application instead of being served by Coolify's static web server. Mark it "
            "static on the target by hand before switching traffic over."
        )
    if source.get("is_maintenance_enabled") is True:
        out.append(_MAINTENANCE_WARNING)
    if at_least(version, RUN_OPTIONS_PARSER_SINCE) and (
        flags := newly_mapped_run_options(source.get("custom_docker_run_options"))
    ):
        out.append(
            f"custom_docker_run_options use {flags}, which Coolify ignored before 4.5 "
            "and applies from 4.5 on. The target — and the source's own next deploy — "
            "runs with them; check it behaves as the running source does."
        )
    return out


_DEFAULT_VARIABLES_WARNING = (
    "include_coolify_default_variables is off on the source and cannot be migrated: "
    "the target's containers get COOLIFY_APP_NAME, COOLIFY_PROJECT_NAME, "
    "COOLIFY_ENVIRONMENT_NAME and COOLIFY_SERVER_IP until you turn it off by hand."
)

_MAINTENANCE_WARNING = (
    "the source is in maintenance mode, which cannot be migrated: the target comes up "
    "serving the real application instead of the maintenance page. Re-enable it on the "
    "target by hand before switching traffic if that is not what you want."
)


def _first_port(raw: object) -> int | None:
    for part in str(raw or "").split(","):
        if part.strip().isdigit():
            return int(part.strip())
    return None


def _label_text(raw: object) -> str:
    """custom_labels as text. The UI stores them base64, the API stores plaintext."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    if "traefik." in raw or "caddy" in raw:
        return raw
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return raw


_TRAEFIK_PORT = re.compile(r"\.loadbalancer\.server\.port=(\d+)")
_CADDY_PORT = re.compile(r"\{\{\s*upstreams\s+(\d+)\s*\}\}")


def hidden_port_overrides(source: dict[str, Any]) -> list[int]:
    """Ports the proxy routes to that neither ports_exposes[0] nor the fqdn explain.

    Coolify (>= 4.3.15) strips an explicit ``:port`` out of every domain it saves
    into ``domain_port_overrides`` — in ``$hidden``, made visible by no controller —
    and routes each domain to its override, falling back to the FIRST exposed port.
    The GET therefore shows a portless fqdn, and the only place the routing
    survives is the generated proxy labels. A port the labels route to that is
    neither the fallback nor written in the fqdn is an override the target cannot
    get. Compose applications route per service and are not checked here.
    """
    if source.get("build_pack") == "dockercompose":
        return []
    # generateLabelsApplication (v4.5.0 docker.php:1229) routes a static site to 80
    # whatever ports_exposes says — switching an app to static never rewrites it.
    exposed = 80 if _settings(source).get("is_static") else _first_port(source.get("ports_exposes"))
    if exposed is None:
        return []
    labels = _label_text(source.get("custom_labels"))
    routed = {int(p) for p in _TRAEFIK_PORT.findall(labels)} | {
        int(p) for p in _CADDY_PORT.findall(labels)
    }
    explicit: set[int] = set()
    for url in str(source.get("fqdn") or "").split(","):
        try:
            if (port := urlsplit(url.strip()).port) is not None:
                explicit.add(port)
        except ValueError:
            continue
    return sorted(routed - {exposed} - explicit)


#: Flags convertDockerRunToCompose maps from v4.5.0 (bootstrap/helpers/constants.php
#: DOCKER_RUN_OPTION_MAPPING) that every earlier release silently ignored.
_NEWLY_MAPPED_FLAGS = frozenset(
    {"--add-host", "--pids-limit", "--read-only", "--runtime", "--tmpfs", "--interactive", "--tty"}
)
#: docker.php v4.5.0:1797 maps any `^-[dit]+$` token carrying i or t (-it, -dit, -td).
_NEWLY_MAPPED_SHORT = re.compile(r"-[dit]*[it][dit]*")
#: The only flags whose `=false` the old parser read as true (docker.php v4.5.0:1704).
_FALSE_NOW_HONOURED = frozenset({"--init", "--privileged", "--read-only"})


def newly_mapped_run_options(raw: object) -> list[str]:
    """The custom docker run flags whose meaning changed in Coolify 4.5.0."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    found: set[str] = set()
    for token in tokens:
        flag, _, value = token.partition("=")
        if flag in _FALSE_NOW_HONOURED and value.lower() == "false":
            # `--init=false` and friends were read as TRUE before 4.5.
            found.add(token)
        elif flag in _NEWLY_MAPPED_FLAGS or _NEWLY_MAPPED_SHORT.fullmatch(token):
            found.add(flag)
    return sorted(found)


# ── services and databases ───────────────────────────────────────────────────


def service_warnings(source: dict[str, Any]) -> list[str]:
    """Service-level and per-sub-application settings the target will not share."""
    out: list[str] = []
    if source.get("include_coolify_default_variables") is False:
        out.append(_DEFAULT_VARIABLES_WARNING)
    if source.get("is_maintenance_enabled") is True:
        out.append(_MAINTENANCE_WARNING)
    tls = sorted(
        str(app.get("name"))
        for app in source.get("applications") or []
        if isinstance(app, dict)
        and (app.get("traefik_certificate_resolver") or app.get("is_permanent_redirect_enabled"))
    )
    if tls:
        out.append(
            f"sub-application(s) {tls} set a certificate resolver or permanent redirects, "
            "which can be set only per sub-application after create and are not "
            "migrated: the target uses its server's default resolver and temporary "
            "redirects until you re-apply them by hand."
        )
    return out


def database_warnings(
    source: dict[str, Any], *, engine: DatabaseEngine | None = None, tls_carried: bool = False
) -> list[str]:
    out: list[str] = []
    if source.get("include_coolify_default_variables") is False:
        out.append(_DEFAULT_VARIABLES_WARNING)
    if lost := database_tls_lost_warning(source, engine=engine, carried=tls_carried):
        out.append(lost)
    if identity := database_tls_identity_warning(source, engine=engine):
        out.append(identity)
    return out


def unsupported_engine_refusal(source: dict[str, Any]) -> str:
    """For a database whose engine this tool cannot recreate (e.g. SQLite, 4.4+)."""
    kind = source.get("type") or source.get("database_type") or source.get("image") or "unknown"
    return (
        f"database engine {kind!r} is not supported. SQLite (Coolify >= 4.4) keeps its "
        "data in a volume applications mount directly, and this tool cannot recreate "
        "that pairing through the API. Migrate it by hand."
    )


# ── database TLS ─────────────────────────────────────────────────────────────
#
# Standalone database TLS is two columns, readable in every GET since
# v4.0.0-beta.399 (fillable, not $hidden, no cast — StandalonePostgresql
# v4.5.0:49-50,71-91) and settable through NO create or update $allowedFields and
# no dedicated route at any tag v4.1.2-v4.5.0. The only API path that carries them
# is POST /databases/{uuid}/clone (v4.3.0+, routes/api.php:340), a whole-row
# server-side copy this tool does not create through. Every create helper only
# fill()s validated request fields (bootstrap/helpers/databases.php v4.5.0:83-95),
# so a target always gets the column default — false for every engine since
# beta.400 (migration 2025_03_26_104103). The reverse flip (source off, target on)
# therefore cannot happen.
#
# Read here from enable_ssl / ssl_mode / the engine ONLY — never from file
# storages: on a TLS source those carry server.key, and the storages GET returns
# its content to a read:sensitive token (DatabasesController v4.5.0:138-145).
# The plan is written by runner.save_plan without the journal's secret guard, so
# how these lines are built is the only thing keeping a secret out of them.

#: The TLS columns. Kept out of api/fields.py on purpose (no array accepts them);
#: test_database_tls_is_still_unsettable fails the day a release does.
DATABASE_TLS_FIELDS: frozenset[str] = frozenset({"enable_ssl", "ssl_mode"})

#: Only these engines have an ssl_mode column (migration 2025_01_27_102616).
_SSL_MODE_ENGINES = frozenset(
    {DatabaseEngine.POSTGRESQL, DatabaseEngine.MYSQL, DatabaseEngine.MONGODB}
)
_SSL_MODE_SHAPE = re.compile(r"[A-Za-z_-]{1,32}")


def _tls_on(value: object) -> bool:
    """No model casts enable_ssl, so accept what PDO may hand back. Absent is off."""
    return value is True or value in (1, "1", "t", "true")


def _ssl_mode(source: dict[str, Any], engine: DatabaseEngine | None) -> str | None:
    mode = source.get("ssl_mode") if engine in _SSL_MODE_ENGINES else None
    return mode if isinstance(mode, str) and _SSL_MODE_SHAPE.fullmatch(mode) else None


#: What a client of the source sees once the target serves plaintext. Evidence
#: per engine at v4.5.0: StartMysql:189-197 / StartMariadb:186-193
#: (--require-secure-transport=1), StartMongodb:205-241 (--tlsMode),
#: StartRedis:313 / StartKeydb:309 (--tls-port 6380), StartDragonfly:204-213
#: (--tls on the main port, no plaintext — FlushDatabase:18-19 agrees),
#: StartPostgresql:192-198 (ssl=on only; the mode is enforced client-side).
_TLS_LOST_CONSEQUENCE: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRESQL: (
        "Clients connecting with sslmode=require, verify-ca or verify-full — Coolify's "
        "own connection URL carries the mode — fail against it; prefer/allow fall back "
        "to plaintext unnoticed. If pg_hba.conf in the data directory uses hostssl "
        "rules, the target refuses EVERY TCP client while its health check still passes."
    ),
    DatabaseEngine.MYSQL: (
        "The source refuses plaintext (require_secure_transport); the target accepts "
        "it, and clients with ssl-mode=VERIFY_CA/VERIFY_IDENTITY fail."
    ),
    DatabaseEngine.MARIADB: (
        "The source refuses plaintext (require_secure_transport); the target accepts "
        "it, and clients that require TLS fail."
    ),
    DatabaseEngine.MONGODB: (
        "Clients with tls=true — Coolify's own connection URL carries it — fail to "
        "connect. Coolify switched TLS on by migration for every MongoDB that existed "
        "at v4.0.0-beta.399 or was created before beta.400, so it may be on here "
        "without anyone having chosen it."
    ),
    DatabaseEngine.REDIS: (
        "The source serves TLS on port 6380 next to 6379; rediss://…:6380 clients get "
        "connection refused."
    ),
    DatabaseEngine.KEYDB: (
        "The source serves TLS on port 6380 next to 6379; rediss://…:6380 clients get "
        "connection refused."
    ),
    DatabaseEngine.DRAGONFLY: (
        "The source serves TLS on its main port 6379 (no plaintext); the target serves "
        "plaintext there, so rediss:// clients fail and plaintext clients now connect."
    ),
}


def database_tls_lost_warning(
    source: dict[str, Any], *, engine: DatabaseEngine | None, carried: bool
) -> str | None:
    """The source serves TLS and the target will not. None when TLS is carried.

    A WARNING, not a refusal: the database deploys and runs fine without TLS, and
    whether its clients need TLS is the operator's call about their stack. The
    target is never public either way (create_database forces is_public=False).
    """
    if carried or not _tls_on(source.get("enable_ssl")):
        return None
    mode = _ssl_mode(source, engine)
    consequence = (engine is not None and _TLS_LOST_CONSEQUENCE.get(engine)) or (
        "Clients that require TLS fail to connect."
    )
    return (
        f"TLS is on for this database{f' (ssl_mode={mode!r})' if mode else ''}, but no "
        "create or update field and no dedicated route carries enable_ssl/ssl_mode "
        "(Coolify 4.1.2-4.5.0), so the target starts WITHOUT TLS. "
        f"{consequence} "
        # HasDatabaseStatusInfo::instantSaveSSL (v4.5.0:105-118) only saves the
        # column, and StartPostgresql & co. apply it at the next start — so a
        # toggle on the stopped target before START costs no outage at all.
        "The target is created stopped before the source is quiesced. Toggle SSL on "
        f"it in the UI as soon as it appears{f' (same mode, {mode!r})' if mode else ''} "
        "so its first start already serves TLS; otherwise toggle it and restart it "
        "right after cutover."
    )


def database_tls_identity_warning(
    source: dict[str, Any], *, engine: DatabaseEngine | None
) -> str | None:
    """The target's certificate identity differs, however TLS gets there.

    Kept apart from the lost warning on purpose: carrying TLS (by hand today, by
    a clone-based create some day) must not silence this. StartPostgresql
    v4.5.0:47-87 takes the CA of the DESTINATION server (Server::ensureCaCertificate)
    and issues CN = the database uuid, SAN = DNS:<uuid> + IP:<that server's IP>
    (SslHelper v4.5.0:53,76,162); each server's CA lives at
    /data/coolify/ssl/coolify-ca.crt on that server (SslHelper v4.5.0:243-256).
    """
    if not _tls_on(source.get("enable_ssl")):
        return None
    mode = _ssl_mode(source, engine)
    client_certs = (
        " The source also demands client certificates (ssl_mode='verify-full'): "
        "clients must present ones signed by the target server's CA instead."
        if engine is DatabaseEngine.MONGODB and mode == "verify-full"
        else ""
    )
    return (
        "once TLS is on for the target, its certificate is issued by the TARGET "
        "server's CA (/data/coolify/ssl/coolify-ca.crt on that server) for the "
        "target's new uuid and IP — not by the source server's. Clients that verify "
        "the server (verify-ca/verify-full, ssl-ca, tlsCAFile) or pin the source "
        "server's CA must be given the target's." + client_certs
    )


#: Directives in a carried engine config that switch TLS on by themselves. Fixed
#: labels, so a refusal names WHICH directive without echoing the config (whose
#: other lines may hold a password). Anchored to non-comment lines, and the
#: explicit-off forms (tls-port 0, require_secure_transport=OFF, mode: disabled)
#: do not count — those cannot fail the target. Each pattern follows its engine's
#: own grammar, not the canonical spelling: postgresql.conf makes the ``=``
#: optional and takes any unambiguous boolean prefix (``t``, ``ye``; never the
#: ambiguous ``o``), Redis lowercases directive names, my.cnf accepts a
#: ``loose-`` prefix, and mongod.conf is YAML, so flow style is valid.
_Directives = tuple[tuple[str, re.Pattern[str]], ...]

_POSTGRES_TLS: _Directives = (
    (
        "ssl",
        re.compile(
            r"^\s*ssl(?:\s*=\s*|\s+)'?(on|t|tr|tru|true|y|ye|yes|1)'?\s*(#|$)", re.M | re.I
        ),
    ),
)
#: MySQL and MariaDB read the same my.cnf options.
_MY_CNF_TLS: _Directives = (
    ("ssl-cert", re.compile(r"^\s*(?:loose[-_])?ssl[-_]cert\s*=", re.M | re.I)),
    ("ssl-key", re.compile(r"^\s*(?:loose[-_])?ssl[-_]key\s*=", re.M | re.I)),
    ("ssl-ca", re.compile(r"^\s*(?:loose[-_])?ssl[-_]ca\s*=", re.M | re.I)),
    (
        "require_secure_transport",
        re.compile(
            r"^\s*(?:loose[-_])?require[-_]secure[-_]transport\s*(=\s*(on|1|true)\b|$)",
            re.M | re.I,
        ),
    ),
)
#: Redis and its fork KeyDB share the keys.
_REDIS_CONF_TLS: _Directives = (
    ("tls-port", re.compile(r"^\s*tls-port\s+['\"]?[1-9]", re.M | re.I)),
    ("tls-cert-file", re.compile(r"^\s*tls-cert-file\s", re.M | re.I)),
    ("tls-key-file", re.compile(r"^\s*tls-key-file\s", re.M | re.I)),
)
#: mongod.conf YAML; the ssl/PEMKeyFile spellings are mongod's pre-4.2 aliases.
_MONGO_CONF_TLS: _Directives = (
    (
        "net.tls.mode",
        re.compile(r"^[^#\n]*\bmode\s*:\s*['\"]?(require|prefer|allow)(TLS|SSL)\b", re.M),
    ),
    ("certificateKeyFile", re.compile(r"^[^#\n]*\b(certificateKeyFile|PEMKeyFile)\s*:", re.M)),
)

#: Engine -> (the carried config field, its TLS directives). Dragonfly has no
#: config field and ClickHouse no TLS at all (supportsSsl() is false).
_TLS_CONFIG_DIRECTIVES: dict[DatabaseEngine, tuple[str, _Directives]] = {
    DatabaseEngine.POSTGRESQL: ("postgres_conf", _POSTGRES_TLS),
    DatabaseEngine.MYSQL: ("mysql_conf", _MY_CNF_TLS),
    DatabaseEngine.MARIADB: ("mariadb_conf", _MY_CNF_TLS),
    DatabaseEngine.MONGODB: ("mongo_conf", _MONGO_CONF_TLS),
    DatabaseEngine.REDIS: ("redis_conf", _REDIS_CONF_TLS),
    DatabaseEngine.KEYDB: ("keydb_conf", _REDIS_CONF_TLS),
}


def database_tls_config_refusals(
    source: dict[str, Any], *, engine: DatabaseEngine | None
) -> list[str]:
    """A TLS source whose carried engine config turns TLS on by itself.

    The *_conf fields are carried verbatim, but the certificate mounts that
    Coolify's SSL setting adds are not (and the setting itself never arrives), so
    the target points its engine at certificate files that do not exist there: it
    fails to start — or refuses every client — after the source is stopped. That
    is not a judgement, so it refuses. Silent unless the source has TLS on: a
    config that enables TLS against files in its own data volume works on the
    target, because the volume is copied.
    """
    if engine is None or not _tls_on(source.get("enable_ssl")):
        return []
    spec = _TLS_CONFIG_DIRECTIVES.get(engine)
    if spec is None:
        return []
    field, directives = spec
    config = source.get(field)
    if not isinstance(config, str) or not config.strip():
        return []
    found = [label for label, pattern in directives if pattern.search(config)]
    if not found:
        return []
    return [
        f"TLS is on for this database and its custom configuration turns TLS on by "
        f"itself ({field} sets {', '.join(found)}). The target gets that configuration "
        "verbatim but neither the SSL setting nor the source's certificate files, so "
        "after the source is stopped it would fail to start or refuse every client. "
        "Remove the TLS lines from the source's custom configuration — Coolify's SSL "
        "setting applies TLS on its own — then migrate; or migrate this database by hand."
    ]


# ── compose ──────────────────────────────────────────────────────────────────


def compose_refusals(raw: str | None, *, version: Version | None) -> list[str]:
    """Compose shapes a 4.4+/4.5+ target cannot reproduce or will not accept."""
    if not raw:
        return []
    try:
        doc = compose_mod.parse(raw)
    except compose_mod.ComposeError:
        # The planner already logs an unparseable compose; refusing here would
        # duplicate a diagnosis that is not this module's to make.
        return []

    out: list[str] = []
    volumes = doc.get("volumes")
    if at_least(version, COMPOSE_VOLUMES_AS_WRITTEN_SINCE) and isinstance(volumes, dict):
        for key in sorted(volumes, key=str):
            decl = volumes[key]
            if not isinstance(decl, dict):
                continue
            if _is_external(decl):
                # parsers.php::composeExternalVolumeDockerName (v4.5.0:614).
                external = decl["external"]
                name = (
                    decl.get("name")
                    or (external.get("name") if isinstance(external, dict) else None)
                    or key
                )
                out.append(
                    f"compose volume {key!r} is external. Coolify >= 4.4 uses it under "
                    f"its literal name {name!r} instead of a per-resource volume and "
                    "records no storage for it, so there is nothing on the target to "
                    "pair it with — and it may be shared with other stacks there. "
                    "Migrate it by hand."
                )
            elif decl.get("driver_opts") or decl.get("driver") not in (None, "", "local"):
                out.append(
                    f"compose volume {key!r} sets driver/driver_opts. From Coolify 4.4 "
                    "they are applied, so the bytes live where the driver puts them (a "
                    "bind device or a network share), not in the volume's own directory: "
                    "the copy would move nothing and verification would compare two "
                    "empty directories and pass. Migrate it by hand."
                )

    if at_least(version, COMPOSE_SCALE_CHECK_SINCE):
        for name, service in sorted(compose_mod.services(doc).items()):
            deploy = service.get("deploy")
            for label, value in (
                ("scale", service.get("scale")),
                ("deploy.replicas", deploy.get("replicas") if isinstance(deploy, dict) else None),
            ):
                if _int_above_one(value):
                    out.append(
                        f"compose service {name!r} sets {label} to {value}. Coolify >= 4.5 "
                        "refuses that while parsing — after the target row is saved, "
                        "leaving a half-created resource. Remove it on the source first."
                    )
    return out


def _is_external(decl: dict[str, Any]) -> bool:
    """bootstrap/helpers/parsers.php::isComposeExternalVolume (v4.5.0:456)."""
    if "external" not in decl:
        return False
    external = decl["external"]
    if isinstance(external, dict):
        return True
    return external is True or str(external).strip().lower() in ("1", "true", "on", "yes")


def _int_above_one(value: object) -> bool:
    try:
        return int(str(value)) > 1
    except ValueError:
        return False


def compose_redump_warning(*, version: Version | None) -> list[str]:
    """Only true before 4.4: from then on the compose is stored verbatim."""
    if at_least(version, COMPOSE_STORED_VERBATIM_SINCE):
        return []
    return [
        "Coolify re-dumps compose through Yaml::dump(Yaml::parse(...)); "
        "comments and formatting will be lost on the target"
    ]


# ── storages ─────────────────────────────────────────────────────────────────


def storage_refusals(storages: dict[str, Any] | None, *, version: Version | None) -> list[str]:
    """Persistent storages this Coolify can no longer create through its API.

    Only for resources whose storages are POSTed (plain applications) — compose
    and database volumes are materialised by Coolify itself.
    """
    if not storages or not at_least(version, HOST_PATH_REMOVED_SINCE):
        return []
    out: list[str] = []
    for entry in storages.get("persistent_storages") or []:
        if not isinstance(entry, dict):
            continue
        host_path = entry.get("host_path")
        if isinstance(host_path, str) and host_path.strip():
            out.append(
                f"persistent storage {entry.get('name')!r} at {entry.get('mount_path')!r} "
                f"binds host path {host_path!r}. Coolify >= 4.3.22 can no longer create a "
                "host-path storage through its API, and recreating it as a plain volume "
                "would mount an empty volume over the mirrored data. Turn it into a "
                "directory file mount on the source, or create it on the target by hand."
            )
    return out


# ── environment variables ────────────────────────────────────────────────────

_GENERATED_SERVICE_SECRET = re.compile(r"^SERVICE_(PASSWORD|USER|BASE64|REALBASE64)")
_CREDENTIAL_KEY = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE", re.IGNORECASE)


def _locked(env: dict[str, Any]) -> bool:
    return bool(env.get("key")) and bool(env.get("is_shown_once")) and env.get("value") is None


def _target_regenerates(key: str, *, collection: str, engine: DatabaseEngine | None) -> bool:
    """A locked key the target will hold a DIFFERENT value for, not merely lack.

    A new service generates its own SERVICE_PASSWORD_* / SERVICE_USER_* (and the
    bulk upsert that would overwrite them skips a value it cannot read); a Redis
    takes its server password from REDIS_PASSWORD and gets a random one. NOT
    KeyDB/Dragonfly: their create makes no REDIS_PASSWORD row, so a target
    without the locked one falls back to the carried keydb/dragonfly_password.
    """
    if collection == "services":
        return bool(_GENERATED_SERVICE_SECRET.match(key))
    return engine is DatabaseEngine.REDIS and key == "REDIS_PASSWORD"


def env_refusals(
    envs: list[dict[str, Any]],
    *,
    collection: str,
    engine: DatabaseEngine | None,
    version: Version | None,
) -> list[str]:
    """Environment variables whose copy would fail the target after the stop."""
    out: list[str] = []

    if at_least(version, SECRET_MANAGER_SINCE):
        referencing = sorted(
            str(e["key"])
            for e in envs
            if e.get("key")
            and not e.get("is_literal")
            and isinstance(e.get("value"), str)
            and _VAULT_REFERENCE.search(e["value"])
        )
        if referencing:
            out.append(
                f"environment variable(s) {referencing} reference a secret manager "
                "({{vault.KEY}}). The link to the secret manager is returned by no API "
                "read, so the target would be created without it and its first deploy "
                "would fail — after the source had already been stopped. Remove the "
                "references, or migrate this resource by hand."
            )

    regenerated = sorted(
        str(e["key"])
        for e in envs
        if _locked(e) and _target_regenerates(str(e["key"]), collection=collection, engine=engine)
    )
    if regenerated:
        out.append(
            f"locked (is_shown_once) variable(s) {regenerated} cannot be read through any "
            "API, so the target would run with its OWN freshly generated value while the "
            "copied data was initialised with the source's — every client would fail to "
            "authenticate. Delete and re-add them unlocked with the same value (the "
            "running container's environment shows it), then migrate."
        )
    return out


def env_warnings(
    envs: list[dict[str, Any]],
    *,
    collection: str,
    engine: DatabaseEngine | None,
    compose: str | None,
    version: Version | None,
) -> list[str]:
    """Environment variables the target will get differently. Keys only, never values."""
    out: list[str] = []

    locked = sorted(
        str(e["key"])
        for e in envs
        if _locked(e)
        and not _target_regenerates(str(e["key"]), collection=collection, engine=engine)
    )
    if locked:
        credentials = [k for k in locked if _CREDENTIAL_KEY.search(k)]
        out.append(
            f"locked (is_shown_once) variable(s) {locked} cannot be read through any API, "
            "so the target is created WITHOUT them. Set them on the target by hand "
            "before switching traffic over."
            + (f" {credentials} look like credentials." if credentials else "")
        )

    if at_least(version, SECRET_MANAGER_SINCE):
        literal = sorted(
            str(e["key"])
            for e in envs
            if e.get("key")
            and e.get("is_literal")
            and isinstance(e.get("value"), str)
            and _VAULT_REFERENCE.search(e["value"])
        )
        if literal:
            out.append(
                f"literal variable(s) {literal} contain a {{{{vault.KEY}}}} reference. If "
                "the source links a secret manager it resolves them; the target has no "
                "link and passes the reference text through as the value."
            )

    if at_least(version, EXACT_ENV_ESCAPING_SINCE):
        changed = sorted(
            str(e["key"])
            for e in envs
            if e.get("key")
            and _saved_before_exact_escaping(e)
            and legacy_escaping_changes_value(e, collection=collection, engine=engine)
        )
        if changed:
            credentials = [k for k in changed if _CREDENTIAL_KEY.search(k)]
            out.append(
                f"variable(s) {changed} were saved before Coolify 4.5 and keep its old "
                "escaping on the source, but are created anew on the target, where Coolify "
                "passes them exactly — so the container sees a different value "
                "(backslashes, quotes, a ' #' tail, quoting of literal values). Check them "
                "after the first start."
                + (
                    f" {credentials} look like credentials: if the copied data was "
                    "initialised with the old form, authentication will fail."
                    if credentials
                    else ""
                )
            )

    if at_least(version, REDIS_PASSWORD_QUOTING_SINCE) and engine in _REDIS_FAMILY:
        out.extend(_redis_password_warnings(envs, engine=engine))

    if compose and at_least(version, EXACT_ENV_ESCAPING_SINCE):
        empty = sorted(
            str(e["key"])
            for e in envs
            if e.get("key")
            and (e.get("value") == "" or (e.get("value") is None and e.get("preserve_compose_null")))
            and _compose_distinguishes_unset(str(e["key"]), compose)
        )
        if empty:
            out.append(
                f"variable(s) {empty} are empty on the source, which Coolify still writes "
                "as KEY= there. The target stores them as unset — its API turns '' into "
                "null — and leaves them out, and this compose reads them in a way that "
                "tells the two apart (${KEY-default}, ${KEY?}, or a bare pass-through). "
                "A ${KEY?} makes the target refuse to start; give them a value first."
            )
    return out


def _saved_before_exact_escaping(env: dict[str, Any]) -> bool:
    """Whether a row keeps the legacy escaping. PURE.

    `uses_legacy_escaping` itself is in $hidden, but the creating hook stamps every
    row's `version` with the Coolify version that saved it, and that IS returned.
    The 4.5.0 migration set the flag on every row that existed then; no code path
    clears it short of delete-and-re-add. A row without a version predates it.
    """
    stamped = parse_version(str(env["version"])) if env.get("version") else None
    return stamped is None or stamped < EXACT_ENV_ESCAPING_SINCE


#: bootstrap/helpers/docker.php::escapeEnvVariables (v4.5.0:1991) — the characters
#: whose presence makes escapeEnvVariables($value) !== $value.
_LEGACY_ESCAPED = frozenset({"\\", "\r", "\t", "\x00", '"', "'"})


def legacy_escaping_changes_value(
    env: dict[str, Any], *, collection: str, engine: DatabaseEngine | None
) -> bool:
    """Port of HasSecretManager::legacyEscapingChangesValue (v4.5.0:189-219). PURE.

    Whether a legacy-escaped row puts a different value into the container than an
    exact one with the same saved value would. Reads the stored `value`: upstream
    reads the resolved one, so a {{team.X}}-style shared reference is judged by
    its reference text — the one approximation here.
    """
    value = env.get("value")
    if not isinstance(value, str) or not value.strip() or _VAULT_REFERENCE.search(value):
        return False
    app_or_service = collection in ("applications", "services")

    if env.get("key") == "REDIS_PASSWORD" and engine in _REDIS_FAMILY and "$" in value:
        return True
    if value.startswith(("{", "[")) and _is_json(value):
        return "$" in value or (collection != "applications" and "\n" in value)
    if env.get("is_literal") or env.get("is_multiline"):
        # Service and application .env files strip the old single quotes;
        # database environment lists keep them.
        return "'" in value if app_or_service else True
    return bool(_LEGACY_ESCAPED & set(value)) or (app_or_service and " #" in value)


def _is_json(value: str) -> bool:
    try:
        json.loads(value)
    except ValueError:
        return False
    return True


#: StartRedis::requirePassArgument (v4.5.0:334-341, flag since v4.4.0): a legacy row
#: keeps the UNQUOTED --requirepass when the password contains one of these, and
#: Compose then splits it.
_SHELL_SPLITTING = frozenset("\\'\";&|<> \t\r\n")


def _redis_password_warnings(
    envs: list[dict[str, Any]], *, engine: DatabaseEngine | None
) -> list[str]:
    """legacy_password_quoting (v4.4.0): $hidden, true for every pre-4.4 row, false
    for every row created since — so always false on the target."""
    value = next((e.get("value") for e in envs if e.get("key") == "REDIS_PASSWORD"), None)
    if not isinstance(value, str) or not value:
        return []
    if engine is DatabaseEngine.REDIS:
        if _SHELL_SPLITTING & set(value) or "{{" in value:
            return [
                "REDIS_PASSWORD contains shell or quote characters (or a {{...}} "
                "reference). A Redis created before Coolify 4.4 passes it to "
                "--requirepass unquoted, so its server password is not the stored value; "
                "the target quotes it and enforces the stored value exactly. Clients "
                "configured with the source's effective password will fail to "
                "authenticate — check them after the first start."
            ]
        return []
    return [
        "a REDIS_PASSWORD variable is set. A KeyDB/Dragonfly created before Coolify 4.4 "
        "ignores it in favour of its own password field; the target uses it AS the "
        "server password. If the two differ, clients of the target authenticate "
        "differently — make them equal on the source first."
    ]


def _compose_distinguishes_unset(key: str, compose: str) -> bool:
    """Does this compose read KEY in a way where unset and '' differ?

    ``${KEY-default}`` and ``${KEY?err}`` (no colon), and a bare pass-through
    (``- KEY`` or ``KEY:`` with no value). The colon forms treat both alike.
    """
    k = re.escape(key)
    return bool(
        re.search(rf"\$\{{{k}[-?]", compose)
        or re.search(rf"^\s*-\s*{k}\s*$", compose, re.MULTILINE)
        or re.search(rf"^\s*{k}:\s*$", compose, re.MULTILINE)
    )


# ── servers ──────────────────────────────────────────────────────────────────


def server_refusals(
    target_server: dict[str, Any], *, collection: str, source: dict[str, Any]
) -> list[str]:
    """Server roles (Coolify >= 4.4) that make the target's deploy impossible."""
    role = _settings(target_server).get("server_role")
    name = target_server.get("name") or target_server.get("uuid")
    if role == "build":
        return [
            f"target server {name!r} is a build server, and Coolify refuses to host "
            "resources on one. Pick a server whose role is 'deployment' or 'both'."
        ]
    if (
        role == "deployment"
        and collection == "applications"
        and source.get("build_pack") not in ("dockerimage", "dockercompose", None)
        and not source.get("docker_registry_image_name")
    ):
        return [
            f"target server {name!r} is set to deployments only, so this application "
            "must be built on a build server and pulled by image name — and it has no "
            "docker_registry_image_name. Its deploy would fail after the source is "
            "stopped. Set an image name on the source first, or pick another server."
        ]
    return []


def server_warnings(source_server: dict[str, Any], target_server: dict[str, Any]) -> list[str]:
    """Server-level settings that change what every migrated resource gets."""
    src, tgt = _settings(source_server), _settings(target_server)
    if "traefik_certificate_resolver" not in src or "traefik_certificate_resolver" not in tgt:
        return []
    if src["traefik_certificate_resolver"] == tgt["traefik_certificate_resolver"]:
        return []
    return [
        f"the source server issues certificates with resolver "
        f"{src['traefik_certificate_resolver']!r} and the target server with "
        f"{tgt['traefik_certificate_resolver']!r} (null means the proxy's own "
        "certificates): every migrated domain will get its certificate differently."
    ]
