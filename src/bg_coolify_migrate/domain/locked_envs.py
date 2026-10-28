"""Locked (``is_shown_once``) variables, read back from the files Coolify writes.

PURE module: no IO. The caller reads one file from the source server
(:mod:`bg_coolify_migrate.discovery.envfiles`) and hands its text in; everything
here is a total function over that text and the captured env list.

Why this exists. A locked variable's value is in no API read: the controllers
``makeHidden(['value', 'real_value'])`` it after the sensitive check, so a root
token does not help (applications and services from 4.2.0, databases from
4.4.0; v4.5.0 ApplicationsController.php:142-144, ServicesController.php:77-79,
DatabasesController.php:3792-3812). The target was therefore created without
it — and for a credential the target generates itself (a service's
``SERVICE_PASSWORD_*``, a Redis ``REDIS_PASSWORD``) it then held a different
secret than the copied data was initialised with.

Coolify itself writes the value to the source server, every time it deploys:

* applications — ``/data/coolify/applications/{uuid}/.env`` (runtime rows only;
  v4.5.0 ApplicationDeploymentJob.php:1740-1960, 2035-2067);
* services — ``/data/coolify/services/{uuid}/.env`` (every row; v4.5.0
  Service.php:1636-1683);
* databases — the ``environment:`` list of
  ``/data/coolify/databases/{uuid}/docker-compose.yml`` (v4.5.0
  StartPostgresql.php:261-273, StartRedis.php:255-283).

Each line is ``KEY=ENC(resolved)``, where ENC is a known encoding chosen per
row: the exact escaping of 4.5 (``escapeComposeEnvFileValue`` /
``escapeDollarSign``) for rows saved on 4.5+, the legacy form otherwise
(:func:`legacy_format`). We decode, then RE-ENCODE and require the line back
byte for byte. A wrong guess, a secret-manager line, a truncated multi-line
record all fail that round trip and come out as "unrecoverable" — never as a
different value. The container's environment is the wrong source: compose has
already interpolated ``${SERVICE_FQDN_X}`` to the SOURCE host there, and QUIESCE
removes the containers anyway.

What is recovered is the value Coolify LAST WROTE, not necessarily the stored
one. Before a single locked record is trusted the file is checked against the
resource's current configuration (:func:`recover`): the unlocked rows in the
same file must encode exactly as the API's ``real_value`` predicts, an
application's file must not come from a preview deployment, and a dotenv file
must split into records without leftovers. The round trip cannot see a legacy
value cut at one of its own newlines — the cut-off first line re-encodes to
itself — so a dotenv record that does not end in a closing double quote is
trusted only when the next line provably starts a record of its own
(:func:`_open_ended`).

Values live ONLY in :class:`Secrets`, whose ``repr``/``str`` show key names.
Reasons and plan lines name keys and paths, never values — they are printed,
journaled and saved in plan.json, and invariant 5 does not bend for a helpful
message.

Indistinguishable cases, carried as their RESOLVED value (documented, accepted):
a secret-manager ``{{vault.X}}`` value that upstream deliberately keeps out of
the DB can encode exactly like a plain row (an application exact row without
``$``, a legacy literal row, a database row without ``$``). The secret-manager
link is visible through no API, so nothing here can tell them apart.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from bg_coolify_migrate.dns.wildcard import under_wildcard
from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.kinds import DatabaseEngine
from bg_coolify_migrate.domain.versions import Version, parse_version

#: The keys behind the ``runtime_buildpack_control`` feature
#: (EnvironmentVariable::RUNTIME_BUILDPACK_CONTROL_VARIABLES).
_RUNTIME_BUILDPACK_CONTROL = frozenset(
    {
        "NIXPACKS_PHP_ROOT_DIR",
        "NIXPACKS_PHP_FALLBACK_PATH",
        "NIXPACKS_SPA_OUTPUT_DIR",
        "RAILPACK_SKIP_MIGRATIONS",
    }
)

#: app/Support/RemoteSecretReferences.php::PATTERN (v4.4.0+).
VAULT_REFERENCE = re.compile(r"\{\{\s*vault\.([A-Za-z0-9_]+)\s*\}\}")

#: Keys the deploy job MAY write into an application's .env itself, in addition
#: to the rows (generate_coolify_env_variables + coolifyDefaultEnvironmentVariables,
#: v4.5.0 ApplicationDeploymentJob.php:3941-4040 and shared.php:4546-4562; PORT
#: and HOST at :1825-1834). A record under any other key is not Coolify's. Which
#: of them a given file holds depends on the build pack, the compose parsing
#: version, settings and the release (:func:`_app_generated`).
_APP_GENERATED = frozenset(
    {
        "SOURCE_COMMIT",
        "PORT",
        "HOST",
        "COOLIFY_FQDN",
        "COOLIFY_URL",
        "COOLIFY_BRANCH",
        "COOLIFY_RESOURCE_UUID",
        "COOLIFY_CONTAINER_NAME",
        "COOLIFY_APP_NAME",
        "COOLIFY_PROJECT_NAME",
        "COOLIFY_ENVIRONMENT_NAME",
        "COOLIFY_SERVER_IP",
    }
)
_COMPOSE_GENERATED_PREFIXES = ("SERVICE_FQDN_", "SERVICE_URL_", "SERVICE_NAME_")
_LEFT_TO_TARGET = re.compile(r"^SERVICE_(FQDN|URL)_")
_RECORD_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
#: The generated HOST line, verbatim on every release in range (v4.1.2
#: ADJ.php:1396, v4.3.10:1415, v4.5.0:1832), and PORT's ``$ports[0]``.
_GENERATED_HOST = "0.0.0.0"
_GENERATED_PORT = re.compile(r"[0-9]+")
_RECORD_START = re.compile(r"([^=\n]+)=")
#: addPreviewDeploymentSuffix (v4.5.0 shared.php:5026-5029) and
#: generateApplicationContainerName (docker.php:573-588).
_PREVIEW_SUFFIX = re.compile(r"-pr-(\d+)$")
#: PHP trim()/ltrim() default character set.
_PHP_WHITESPACE = " \t\n\r\0\x0b"

BUILD_TIME_ONLY = "build-time only: Coolify never writes it to the server"
PREVIEW_ONLY = "a preview variable: production deploys never write it"
SHARED_REFERENCE = (
    "a {{...}} shared-variable reference: the server file holds only its resolved "
    "value; unlock it on the source (the reference text is not the secret) or set "
    "it on the target by hand"
)


class FileKind(StrEnum):
    """How a config file delimits its records."""

    DOTENV = "dotenv"
    """``KEY=value`` lines, read by docker compose's dotenv parser."""

    COMPOSE = "compose"
    """A generated compose file; records are the ``environment:`` list items."""


def config_path(collection: str, uuid: str) -> tuple[str, FileKind]:
    """Where Coolify writes this resource's env values on its server.

    ``application_configuration_dir()`` and friends are ``/data/coolify/{kind}``
    (v4.5.0 bootstrap/helpers/shared.php:86-97, unchanged since v4.1.2:69-84).

    Raises:
        ValueError: For a uuid that is not plain alphanumerics — it is put into a
            path, and nothing but Coolify's own cuid shape is accepted there.
    """
    if not re.fullmatch(r"[A-Za-z0-9]+", uuid):
        raise ValueError("refusing to build a config path from a non-alphanumeric uuid")
    if collection == "applications":
        return f"/data/coolify/applications/{uuid}/.env", FileKind.DOTENV
    if collection == "services":
        return f"/data/coolify/services/{uuid}/.env", FileKind.DOTENV
    if collection == "databases":
        return f"/data/coolify/databases/{uuid}/docker-compose.yml", FileKind.COMPOSE
    raise ValueError(f"no config file for collection {collection!r}")


class Secrets:
    """Recovered values. The ONLY holder of them, and it never shows one.

    Deliberately not a mapping: no ``items()``, no ``values()``, no ``__eq__``,
    no pydantic. ``repr``/``str`` list key names, so a Secrets that lands in an
    assertion, a log field or a traceback discloses nothing.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Secrets(keys={list(self.keys())!r})"

    __str__ = __repr__


@dataclass(frozen=True, eq=False)
class SourceConfig:
    """One config file as read from the source server — or why it could not be."""

    path: str
    kind: FileKind
    text: str | None = field(default=None, repr=False)
    unavailable: str | None = None


@dataclass(frozen=True, eq=False)
class LockedRecovery:
    """What :func:`recover` made of a resource's locked rows. Values in Secrets only."""

    recovered: Secrets = field(default_factory=Secrets)
    unrecoverable: Mapping[str, str] = field(default_factory=dict)
    """Production key -> why it could not be read back. Never a value."""
    left_to_target: tuple[str, ...] = ()
    """Generated by the target itself: a service's SERVICE_FQDN_*/SERVICE_URL_*, a
    dockercompose app's SERVICE_FQDN_*/URL_*/NAME_* (:func:`_left_to_target`)."""
    from_api: tuple[str, ...] = ()
    """Recovered from the source database's connection URL, not from the file."""
    previews: tuple[str, ...] = ()
    """Locked PREVIEW rows. Never recovered: production deploys never write them."""


# ── upstream's view of a row ─────────────────────────────────────────────────


def is_locked(env: Mapping[str, Any]) -> bool:
    """A locked row whose value the API withheld.

    ``'value' not in env``, mirroring the copy path (api/resources.py): the hide
    is ``makeHidden``, so the key is ABSENT. A present ``null`` is a row that is
    genuinely null — on 4.1.x every locked value is still returned.
    """
    return bool(env.get("key")) and bool(env.get("is_shown_once")) and "value" not in env


def saved_before_exact_escaping(env: Mapping[str, Any]) -> bool:
    """Whether a row keeps the legacy escaping.

    ``uses_legacy_escaping`` itself is in $hidden, but the creating hook stamps
    every row's ``version`` with the Coolify version that saved it, and that IS
    returned (v4.5.0 EnvironmentVariable.php:145-147). The 4.5.0 migration set
    the flag on every row that existed then; no code path clears it short of
    delete-and-re-add. A row without a version predates it. A stamp of 4.5 or
    later cannot exist on an older instance, so the stamp alone decides.
    """
    stamped = parse_version(str(env["version"])) if env.get("version") else None
    return stamped is None or not supports("exact_env_escaping", stamped)


def is_json(value: str) -> bool:
    """PHP ``json_validate`` (8.3): strict JSON, no NaN/Infinity constants.

    Python's json accepts NaN/Infinity/-Infinity; PHP does not, and the choice
    decides which encoding a row got. A disagreement in either direction makes
    the round trip fail closed rather than return a wrong value.
    """
    if not value:
        return False

    def _reject(_constant: str) -> Any:
        raise ValueError("not JSON")

    try:
        json.loads(value, parse_constant=_reject)
    except (ValueError, RecursionError):
        return False
    return True


def escape_env_variables(value: str) -> str:
    """bootstrap/helpers/docker.php::escapeEnvVariables (v4.5.0:1991-1997, v4.1.2:1267).

    Sequential ``str_replace`` of ``\\ CR TAB NUL " '``; no replacement text
    contains a later search character, so a per-character map is equivalent.
    Newlines and ``$`` are left alone.
    """
    return value.translate(_ENV_ESCAPES)


_ENV_ESCAPES = str.maketrans(
    {"\\": "\\\\", "\r": "\\r", "\t": "\\t", "\0": "\\0", '"': '\\"', "'": "\\'"}
)
_ENV_UNESCAPES = {"\\": "\\", "r": "\r", "t": "\t", "0": "\0", '"': '"', "'": "'"}


def escape_compose_env_file_value(value: str, allow_interpolation: bool) -> str:
    """docker.php::escapeComposeEnvFileValue (v4.5.0:2028-2037)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if not allow_interpolation:
        escaped = escaped.replace("$", "\\$")
    return f'"{escaped}"'


def escape_dollar_sign(value: str) -> str:
    """docker.php::escapeDollarSign (v4.5.0:1998-2004)."""
    return value.replace("$", "$$")


def legacy_format(env: Mapping[str, Any], value: str) -> str:
    """The pre-4.5 line: HasSecretManager::legacyFormatEnvironmentVariableValue.

    JSON object/array raw; literal or multiline wrapped in single quotes
    WITHOUT escaping; everything else through escapeEnvVariables (v4.5.0
    HasSecretManager.php:174-183, EnvironmentVariable.php:358-367; the same
    shape at v4.1.2 EnvironmentVariable.php:258-293).
    """
    if value.startswith(("{", "[")) and is_json(value):
        return value
    if env.get("is_literal") or env.get("is_multiline"):
        return f"'{value}'"
    return escape_env_variables(value)


def allows_interpolation(env: Mapping[str, Any], value: str) -> bool:
    """HasSecretManager::environmentVariableAllowsInterpolation (v4.5.0:224-232).

    Only plain values: not JSON (judged after PHP's ``ltrim``), not literal, not
    multiline. The secret-manager clause cannot be seen from outside.
    """
    stripped = value.lstrip(_PHP_WHITESPACE)
    json_like = stripped[:1] in ("{", "[") and is_json(value)
    return not json_like and not env.get("is_literal") and not env.get("is_multiline")


def encode(env: Mapping[str, Any], value: str, kind: FileKind) -> str:
    """The text Coolify writes after ``KEY=`` for this row and resolved value."""
    if saved_before_exact_escaping(env):
        return legacy_format(env, value)
    allow = allows_interpolation(env, value)
    if kind is FileKind.DOTENV:
        return escape_compose_env_file_value(value, allow)
    return value if allow else escape_dollar_sign(value)


def decode(raw: str, env: Mapping[str, Any], kind: FileKind) -> str | None:
    """The value whose encoding is exactly ``raw``, or None.

    Collects every plausible decoding — the legacy form has three shapes, and
    which one a line took depends on the VALUE (JSON or not) — and keeps those
    that re-encode to ``raw``. For the encodings above the survivors never
    disagree, but a line that two different values could have produced would be
    refused, not guessed: this function returns a value or nothing, never a
    second-best one.
    """
    candidates: set[str] = set()
    if not saved_before_exact_escaping(env):
        if kind is FileKind.DOTENV:
            unquoted = _unescape_compose_env_file_value(raw)
            if unquoted is not None:
                candidates.add(unquoted)
        else:
            candidates.update({raw, raw.replace("$$", "$")})
    else:
        if raw.startswith(("{", "[")) and is_json(raw):
            candidates.add(raw)
        if len(raw) >= 2 and raw[0] == raw[-1] == "'":
            candidates.add(raw[1:-1])
        unescaped = _unescape_env_variables(raw)
        if unescaped is not None:
            candidates.add(unescaped)
    exact = {v for v in candidates if v and encode(env, v, kind) == raw}
    return exact.pop() if len(exact) == 1 else None


def _unescape_compose_env_file_value(raw: str) -> str | None:
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return None
    inner, out, i = raw[1:-1], [], 0
    while i < len(inner):
        char = inner[i]
        if char == "\\":
            if i + 1 >= len(inner) or inner[i + 1] not in '\\"$':
                return None
            out.append(inner[i + 1])
            i += 2
            continue
        if char == '"':
            return None
        out.append(char)
        i += 1
    return "".join(out)


def _unescape_env_variables(raw: str) -> str | None:
    out, i = [], 0
    while i < len(raw):
        char = raw[i]
        if char == "\\":
            if i + 1 >= len(raw) or raw[i + 1] not in _ENV_UNESCAPES:
                return None
            out.append(_ENV_UNESCAPES[raw[i + 1]])
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


# ── reading the files ────────────────────────────────────────────────────────


@dataclass(frozen=True, eq=False)
class _Record:
    raw: str = field(repr=False)
    closed: bool
    """A double-quoted value whose closing quote the scanner found: delimited
    exactly whatever the neighbouring lines look like."""


@dataclass(eq=False)
class _Parsed:
    records: dict[str, list[_Record]] = field(default_factory=dict, repr=False)
    anomaly: bool = False
    sequence: list[tuple[str, _Record]] = field(default_factory=list, repr=False)
    """Every record in file order (dotenv only)."""
    open_ended: set[str] = field(default_factory=set)
    """Keys with a record whose end nothing proves (:func:`_open_ended`)."""


def single_copy(stdout: str) -> str | None:
    """The file text from ``cat P; printf '\\000'; cat P``, or None if the reads differ.

    The application .env and a database's compose are written with ``tee``,
    non-atomically (v4.5.0 ApplicationDeploymentJob.php:2063,
    StartPostgresql.php:213). A read overlapping a deploy can see a truncated
    last record — and a truncated legacy value still passes the round trip.
    Two reads that agree byte for byte did not overlap a rewrite.
    """
    if len(stdout) % 2 == 0:
        return None
    half = len(stdout) // 2
    first, separator, second = stdout[:half], stdout[half], stdout[half + 1 :]
    return first if separator == "\0" and first == second else None


def _parse_dotenv(text: str) -> _Parsed:
    """Split a Coolify-written dotenv file into records, flagging every leftover.

    A record starts with ``KEY=`` at the start of a line. A value opening with
    ``"`` runs to the first unescaped ``"`` (the exact and secret-manager
    forms escape every inner quote); one opening with ``'`` to the first ``'``
    followed by a newline or the end (the legacy literal form); anything else
    to the end of its line. A line that starts no record, or text after a
    closing quote, is an anomaly: some legacy value spilled over lines.
    """
    parsed = _Parsed()
    pos, end = 0, len(text)
    while pos < end:
        eol = text.find("\n", pos)
        line_end = end if eol == -1 else eol
        start = _RECORD_START.match(text, pos, line_end)
        if start is None:
            parsed.anomaly = True
            pos = line_end + 1
            continue
        key, vstart = start.group(1), start.end()
        closed = False
        if vstart < end and text[vstart] == '"':
            close = _closing_double_quote(text, vstart + 1)
            if close is None:
                raw, pos, parsed.anomaly = text[vstart:], end, True
            else:
                tail_end = text.find("\n", close + 1)
                tail_end = end if tail_end == -1 else tail_end
                if tail_end == close + 1:
                    raw, closed = text[vstart : close + 1], True
                else:
                    raw, parsed.anomaly = text[vstart:tail_end], True
                pos = tail_end + 1
        elif vstart < end and text[vstart] == "'":
            close = _closing_single_quote(text, vstart + 1)
            if close is None:
                raw, pos, parsed.anomaly = text[vstart:line_end], line_end + 1, True
            else:
                raw, pos = text[vstart : close + 1], close + 2
        else:
            raw, pos = text[vstart:line_end], line_end + 1
        record = _Record(raw=raw, closed=closed)
        parsed.records.setdefault(key, []).append(record)
        parsed.sequence.append((key, record))
    return parsed


def _closing_double_quote(text: str, i: int) -> int | None:
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return None


def _closing_single_quote(text: str, i: int) -> int | None:
    while (found := text.find("'", i)) != -1:
        if found + 1 == len(text) or text[found + 1] == "\n":
            return found
        i = found + 1
    return None


class _Unparseable(ValueError):
    pass


def _parse_compose_environment(text: str, uuid: str) -> _Parsed:
    """The ``environment:`` list of a Start*-generated compose (Yaml::dump).

    YAML delimits every item exactly, so there is no anomaly here — only a
    document that does not have the shape Start*.php writes.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _Unparseable from exc
    service = doc.get("services", {}).get(uuid) if isinstance(doc, dict) else None
    env = service.get("environment") if isinstance(service, dict) else None
    items: Iterable[tuple[str, Any]]
    if isinstance(env, list):
        items = [_split_item(item) for item in env]
    elif isinstance(env, dict):
        items = env.items()
    else:
        raise _Unparseable
    parsed = _Parsed()
    for key, raw in items:
        if not isinstance(raw, str):
            raise _Unparseable
        parsed.records.setdefault(str(key), []).append(_Record(raw=raw, closed=True))
    return parsed


def _split_item(item: object) -> tuple[str, str]:
    if not isinstance(item, str) or "=" not in item:
        raise _Unparseable
    key, _, raw = item.partition("=")
    return key, raw


# ── what Coolify writes where ────────────────────────────────────────────────


def _left_to_target(key: str, *, collection: str, compose_app: bool) -> bool:
    """A locked row the target generates itself, so there is nothing to carry.

    Only where Coolify generates these names: a dockercompose application's
    deploy writes SERVICE_FQDN_/URL_ from its domains and SERVICE_NAME_ from its
    compose and never writes such a row (v4.5.0 ADJ.php:1749-1790), and a
    service rebuilds its SERVICE_FQDN_/URL_ rows from the sub-application fqdn.
    On any other application, and on a database, the row is the operator's own:
    written verbatim (ADJ.php:1805-1811; StartPostgresql pushes every runtime
    row) and regenerated by nothing — Application.php:362-384 even deletes these
    rows when an app leaves dockercompose. Those are recovered like any other.
    """
    if collection == "applications" and compose_app:
        return key.startswith(_COMPOSE_GENERATED_PREFIXES)
    return collection == "services" and _LEFT_TO_TARGET.match(key) is not None


def _like_prefix(key: str, prefix: str) -> bool:
    """SQL ``key LIKE '{prefix}_%'``: the ``_`` is a wildcard, so one more char."""
    return key.startswith(prefix) and len(key) > len(prefix)


def _is_buildpack_control(key: str) -> bool:
    """EnvironmentVariable::scopeWithoutBuildpackControlVariables (LIKE semantics)."""
    return _like_prefix(key, "NIXPACKS") or _like_prefix(key, "RAILPACK")


def _app_writes(env: Mapping[str, Any], *, version: Version | None, compose_app: bool) -> bool:
    """Whether a production row reaches an application's runtime .env.

    ``runtime_environment_variables`` excludes buildpack control keys (all of
    them before 4.5; all but four from 4.5), the job keeps ``is_runtime`` rows,
    and a dockercompose app regenerates SERVICE_FQDN_/URL_/NAME_ instead of
    writing its rows (v4.5.0 Application.php:1174-1179, ADJ.php:1749-1752).
    """
    if env.get("is_preview") or env.get("is_runtime") is False:
        return False
    key = str(env.get("key"))
    if _is_buildpack_control(key) and not (
        supports("runtime_buildpack_control", version) and key in _RUNTIME_BUILDPACK_CONTROL
    ):
        return False
    return not (compose_app and key.startswith(_COMPOSE_GENERATED_PREFIXES))


def _compose_service_keys(compose: str | None, prefixes: Sequence[str]) -> frozenset[str] | None:
    """SERVICE_NAME_/FQDN_/URL_ keys for the compose's services; None if unknown.

    Upper-cased with ``-`` and ``.`` turned into ``_`` (Service.php:1658,
    normalizeComposeServiceName domains.php:345), plus the bare upper-case form
    v4.1.2 still used for SERVICE_URL_/FQDN_ (ADJ v4.1.2:1342-1343).
    """
    if not compose:
        return None
    try:
        doc = yaml.safe_load(compose)
    except yaml.YAMLError:
        return None
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        return None
    names = {str(n).replace("-", "_").replace(".", "_").upper() for n in services} | {
        str(n).upper() for n in services
    }
    return frozenset(f"{prefix}{name}" for prefix in prefixes for name in names)


def _expected_key(
    key: str, *, rows: frozenset[str], generated: frozenset[str], generated_prefixes: Sequence[str]
) -> bool:
    if key in rows:
        return True
    if not _RECORD_KEY.fullmatch(key):
        return False
    return key in generated or key.startswith(tuple(generated_prefixes))


def _app_generated(production_keys: frozenset[str], *, compose_app: bool) -> frozenset[str]:
    """The generated keys an application's .env can hold at all.

    PORT and HOST are written only when no production row (build-only ones
    included) has that key, and PORT never for a dockercompose app (v4.1.2
    ADJ.php:1390-1397, v4.3.10:1409-1416, v4.5.0:1824-1833). The rest hinge on
    the compose parsing version, settings and the release, none of which an API
    read pins down for every version in range — they stay possible.
    """
    impossible = production_keys & {"PORT", "HOST"}
    if compose_app:
        impossible |= {"PORT"}
    return _APP_GENERATED - impossible


def _written_for_sure(written: Sequence[Mapping[str, Any]], *, omits_null: bool) -> frozenset[str]:
    """Row keys whose line the file holds whenever it was written for this config.

    A service's and a dockercompose app's file leave out a null row the compose
    references (dockerComposeEnvironmentVariableShouldBeUnset, v4.5.0
    Service.php:1676, ADJ.php:1954-1961): an unlocked null row there may be
    absent. A locked row is taken as written — it would have to be null as well
    as hidden for that not to hold.
    """
    return frozenset(
        str(e["key"])
        for e in written
        if not (
            omits_null
            and "value" in e
            and e["value"] is None
            and not e.get("preserve_compose_null")
        )
    )


def _open_ended(parsed: _Parsed, starts_a_record: Callable[[str, _Record], bool]) -> set[str]:
    """Keys of dotenv records whose END nothing proves.

    A record not closed by a double quote ends at the next newline (legacy plain
    or JSON) or at a single quote before one (legacy literal/multiline) — and a
    legacy value may hold exactly such a newline itself, so the next line may be
    the rest of the value. The round trip cannot tell: the cut-off part
    re-encodes to itself. What proves the end is the end of the file, or a next
    line Coolify certainly wrote (``starts_a_record``): its key then occurs once
    only because that line is its own record — a continuation would have made a
    second copy, i.e. an anomaly. A line Coolify only MAY have written proves
    nothing (a generated COOLIFY_*/SERVICE_*/PORT line, a null row the compose
    may omit): the record before it is not trusted, whatever it decodes to.
    """
    return {
        key
        for (key, record), (next_key, next_record) in pairwise(parsed.sequence)
        if not record.closed and not starts_a_record(next_key, next_record)
    }


def _expected_raw(env: Mapping[str, Any], real_value: str, kind: FileKind) -> str:
    """What Coolify writes for an UNLOCKED row, predicted from the API's real_value.

    For a legacy row ``real_value`` IS Coolify's own legacy-formatted resolved
    value (v4.5.0 EnvironmentVariable.php:265-283; v4.1.2:171-209); for an exact
    row it is the resolved value, which the writer then escapes.
    """
    if saved_before_exact_escaping(env):
        return real_value
    allow = allows_interpolation(env, real_value)
    if kind is FileKind.DOTENV:
        return escape_compose_env_file_value(real_value, allow)
    return real_value if allow else escape_dollar_sign(real_value)


def _stale(written: Sequence[Mapping[str, Any]], parsed: _Parsed, kind: FileKind) -> bool:
    """Whether any unlocked row disagrees with its record.

    Rows without a readable real_value, and secret-manager rows (whose resolved
    secret the API never returns), cannot be predicted and are skipped. A blank
    row may be left out of a compose app's or a service's file
    (dockerComposeEnvironmentVariableShouldBeUnset, v4.5.0 Service.php:1675),
    but if it is there it must be blank.
    """
    for env in written:
        if "value" not in env:
            continue
        key, value, real = str(env["key"]), env.get("value"), env.get("real_value")
        if any(isinstance(v, str) and VAULT_REFERENCE.search(v) for v in (value, real)):
            continue
        records = parsed.records.get(key, [])
        if value in (None, ""):
            if any(r.raw not in ("", '""', "''") for r in records):
                return True
            continue
        if not isinstance(real, str) or not real:
            continue
        if not records:
            return True
        if len(records) > 1 or (parsed.anomaly and not records[0].closed):
            continue  # unreliable here; the anomaly itself already fails closed
        if records[0].raw != _expected_raw(env, real, kind):
            return True
    return False


def _preview_marker(parsed: _Parsed) -> int | None:
    """The pull request whose deploy last wrote this application's .env, if any.

    configuration_dir does not depend on pull_request_id (v4.5.0 ADJ.php:282),
    so a preview deploy OVERWRITES the production .env with the preview's
    variables. Its COOLIFY_CONTAINER_NAME (or a dockercompose app's
    SERVICE_NAME_*) carries the ``-pr-N`` suffix.
    """
    for key, records in parsed.records.items():
        if key != "COOLIFY_CONTAINER_NAME" and not key.startswith("SERVICE_NAME_"):
            continue
        for record in records:
            raw = record.raw
            value = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
            if match := _PREVIEW_SUFFIX.search(value):
                return int(match.group(1))
    return None


@dataclass(frozen=True, eq=False)
class _FileVerdict:
    parsed: _Parsed | None
    problem: str | None


def _inspect(
    source: SourceConfig,
    production: Sequence[Mapping[str, Any]],
    *,
    collection: str,
    uuid: str,
    version: Version | None,
    compose_app: bool,
    compose: str | None,
    ports_exposes: str | None = None,
) -> _FileVerdict:
    """Read the file and decide whether ANY locked record of it may be trusted."""
    path = source.path
    if source.unavailable:
        return _FileVerdict(None, source.unavailable)
    if source.text is None:
        return _FileVerdict(None, f"{path} was not read")

    if source.kind is FileKind.COMPOSE:
        try:
            parsed = _parse_compose_environment(source.text, uuid)
        except _Unparseable:
            return _FileVerdict(None, f"{path} could not be parsed")
        written = list(production)
    else:
        parsed = _parse_dotenv(source.text)
        is_app = collection == "applications"
        if is_app:
            written = [
                e for e in production if _app_writes(e, version=version, compose_app=compose_app)
            ]
            generated = _app_generated(
                frozenset(str(e["key"]) for e in production), compose_app=compose_app
            )
            prefixes: tuple[str, ...] = ()
            if compose_app:
                exact_names = _compose_service_keys(compose, _COMPOSE_GENERATED_PREFIXES)
                generated = generated | (exact_names or frozenset())
                prefixes = _COMPOSE_GENERATED_PREFIXES if exact_names is None else ()
        else:
            written = list(production)
            names = _compose_service_keys(compose, ("SERVICE_NAME_",))
            generated = names or frozenset()
            prefixes = ("SERVICE_NAME_",) if names is None else ()
        rows = frozenset(str(e["key"]) for e in written)
        for key, records in parsed.records.items():
            expected = _expected_key(
                key, rows=rows, generated=generated, generated_prefixes=prefixes
            )
            if len(records) > 1 or not expected:
                parsed.anomaly = True

        certain = _written_for_sure(written, omits_null=not is_app or compose_app)
        # HOST, and PORT once ports_exposes is known, are written whenever they
        # are possible at all (main_port(): ports_exposes, or [80] for a static
        # site) — with the exact shape the deploy job gives them.
        host_written = is_app and "HOST" in generated
        port_written = is_app and "PORT" in generated and isinstance(ports_exposes, str)

        def starts_a_record(key: str, record: _Record) -> bool:
            if key in certain:
                return True
            if key == "HOST" and host_written:
                return record.raw == _GENERATED_HOST
            if key == "PORT" and port_written:
                return _GENERATED_PORT.fullmatch(record.raw) is not None
            return False

        parsed.open_ended = _open_ended(parsed, starts_a_record)

    if collection == "applications" and (pr := _preview_marker(parsed)) is not None:
        return _FileVerdict(
            None,
            f"the source's last deployment was pull request #{pr}, so {path} holds the "
            "preview's values; redeploy production once, then plan again",
        )
    if _stale(written, parsed, source.kind):
        return _FileVerdict(
            None,
            f"{path} does not match the resource's current configuration (written by an "
            "older deploy, or a variable changed since without a redeploy/restart); "
            "redeploy or restart the source once, then plan again",
        )
    return _FileVerdict(parsed, None)


def _from_file(
    env: Mapping[str, Any], verdict: _FileVerdict, source: SourceConfig
) -> tuple[str | None, str]:
    """``(value, "")`` or ``(None, reason)`` for one locked production row."""
    if verdict.problem is not None or verdict.parsed is None:
        return None, verdict.problem or f"{source.path} was not read"
    path, key = source.path, str(env["key"])
    records = verdict.parsed.records.get(key, [])
    if not records:
        return None, f"not in {path}"
    if len(records) > 1:
        return None, f"its line in {path} is ambiguous (the key occurs {len(records)} times)"
    record = records[0]
    if verdict.parsed.anomaly and not record.closed:
        return None, f"its line in {path} cannot be delimited exactly"
    if key in verdict.parsed.open_ended:
        return None, (
            f"its line in {path} cannot be delimited exactly (the next line is one "
            "Coolify may not have written, so it may continue this value)"
        )
    value = decode(record.raw, env, source.kind)
    if value is None:
        return None, (
            f"its line in {path} is not in the encoding Coolify uses for this variable "
            "(a secret-manager value?) and cannot be read back exactly"
        )
    return value, ""


def redis_password_from_url(url: object) -> str | None:
    """The server password inside a Redis ``internal_db_url``, or None.

    The one API path to a locked value — up to 4.5.0: StandaloneRedis appends
    internal_db_url, the controller makes it visible to sensitive callers, and it
    embeds ``rawurlencode(connectionPassword())`` (v4.5.0 StandaloneRedis.php:59,
    309-318, 402-436; DatabasesController.php:213-234; same at v4.4.0). From
    4.5.1 the controller nulls both connection URLs for a sensitive caller when
    the credential is locked (DatabasesController v4.5.1:262-270,
    StandaloneRedis::isCredentialLocked :421-424; the ``locked_db_url_withheld``
    feature), so a locked REDIS_PASSWORD then comes from the file alone, with
    nothing to cross-check it against. rawurlencode is exact RFC 3986, so
    ``unquote`` (never ``unquote_plus``) inverts it. The username part exists for
    redis >= 6 and is itself encoded, so the first ``:`` of the userinfo is the
    separator. A ``{{...}}`` is a secret-manager or shared reference shown instead
    of the password — not the password.
    """
    if not isinstance(url, str) or "@" not in url:
        return None
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return None
    userinfo = netloc.rpartition("@")[0]
    encoded = userinfo.split(":", 1)[1] if ":" in userinfo else userinfo
    password = unquote(encoded)
    return None if not password or "{{" in password else password


def recover(
    envs: Sequence[Mapping[str, Any]],
    source: SourceConfig,
    *,
    collection: str,
    uuid: str,
    version: Version | None = None,
    compose_app: bool = False,
    compose: str | None = None,
    engine: DatabaseEngine | None = None,
    internal_db_url: str | None = None,
    ports_exposes: str | None = None,
) -> LockedRecovery:
    """Read back every locked production row the source server holds a value for.

    Per row, first match wins: a name the target generates itself is left to it
    (:func:`_left_to_target`); a shared reference, and an application row
    Coolify never writes to the server, are unrecoverable on their face; then
    the FILE must be trusted as a whole (readable, parseable, not a preview's,
    not stale); then the row's own record must exist once, be delimited exactly
    and decode exactly. A Redis REDIS_PASSWORD comes from the API URL first,
    cross-checked with the file — up to 4.5.0; from 4.5.1 the API withholds that
    URL (:func:`redis_password_from_url`) and the file is the only source.

    ``ports_exposes`` (applications) is the resource's own; a known one makes
    the generated PORT line certain, so a record before it can be delimited.
    """
    production = [e for e in envs if e.get("key") and not e.get("is_preview")]
    previews = sorted({str(e["key"]) for e in envs if is_locked(e) and e.get("is_preview")})
    recovered: dict[str, str] = {}
    unrecoverable: dict[str, str] = {}
    left: list[str] = []
    from_api: list[str] = []

    pending: list[Mapping[str, Any]] = []
    for env in production:
        if not is_locked(env):
            continue
        key = str(env["key"])
        if _left_to_target(key, collection=collection, compose_app=compose_app):
            left.append(key)
        elif env.get("is_shared"):
            unrecoverable[key] = SHARED_REFERENCE
        elif collection == "applications" and not _app_writes(
            env, version=version, compose_app=compose_app
        ):
            unrecoverable[key] = BUILD_TIME_ONLY
        else:
            pending.append(env)

    if pending:
        verdict = _inspect(
            source,
            production,
            collection=collection,
            uuid=uuid,
            version=version,
            compose_app=compose_app,
            compose=compose,
            ports_exposes=ports_exposes,
        )
        for env in pending:
            key = str(env["key"])
            value, reason = _from_file(env, verdict, source)
            from_url = engine is DatabaseEngine.REDIS and key == "REDIS_PASSWORD"
            api_value = redis_password_from_url(internal_db_url) if from_url else None
            if from_url and api_value is None and supports(
                "locked_db_url_withheld", version, if_unknown=False
            ):
                # Not a disagreement and not a token problem: this release never
                # shows the URL of a locked credential. Say so, or the reason
                # reads as if the file had merely been unlucky.
                reason = (
                    "Coolify >= 4.5.1 withholds the connection URL of a locked "
                    f"credential, so {source.path} is the only source, and {reason}"
                )
            if api_value is not None:
                if value is not None and value != api_value:
                    unrecoverable[key] = (
                        f"the source database's API connection URL and {source.path} disagree"
                    )
                else:
                    recovered[key] = api_value
                    from_api.append(key)
            elif value is None:
                unrecoverable[key] = reason
            else:
                recovered[key] = value

    return LockedRecovery(
        recovered=Secrets(recovered),
        unrecoverable=dict(sorted(unrecoverable.items())),
        left_to_target=tuple(sorted(left)),
        from_api=tuple(sorted(from_api)),
        previews=tuple(previews),
    )


def merged_view(
    envs: Sequence[Mapping[str, Any]], recovery: LockedRecovery
) -> list[dict[str, Any]]:
    """The env list as if the recovered rows had been readable. IN MEMORY ONLY.

    For the fidelity checks, which then see a recovered REDIS_PASSWORD or a
    legacy-escaped credential like any other value. Never persisted, never
    logged; callers drop it after the checks.
    """
    out: list[dict[str, Any]] = []
    for env in envs:
        row = dict(env)
        key = str(env.get("key") or "")
        if is_locked(env) and not env.get("is_preview") and key in recovery.recovered:
            row["value"] = recovery.recovered.get(key)
        out.append(row)
    return out


# ── what the plan says ───────────────────────────────────────────────────────

_HOST_LIKE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


def names_source_server(value: str, *, wildcard: str | None, ip: str | None) -> bool:
    """Whether a value mentions a host under the source wildcard, or its IP.

    Coolify's sslip.io/nip.io fallback domains encode the IP with dashes or
    dots, so they count too.
    """
    address = (ip or "").strip().lower()
    for token in _HOST_LIKE.findall(value):
        host = token.lower().rstrip(".")
        if wildcard and under_wildcard(host, wildcard):
            return True
        if not address:
            continue
        if host == address:
            return True
        if host.endswith((".sslip.io", ".nip.io")) and (
            address in host or address.replace(".", "-") in host
        ):
            return True
    return bool(address) and ":" in address and address in value.lower()


def source_server_keys(
    recovery: LockedRecovery, *, wildcard: str | None, ip: str | None
) -> tuple[str, ...]:
    """Recovered keys whose value points at the source server. Keys only."""
    secrets = recovery.recovered
    names = secrets.keys()
    return tuple(
        key
        for key in names
        if names_source_server(secrets.get(key) or "", wildcard=wildcard, ip=ip)
    )


def recovered_lines(
    recovery: LockedRecovery, *, path: str, source_server_hits: Sequence[str] = ()
) -> list[str]:
    """Plan warnings for what WILL be carried. Key names and paths only."""
    out: list[str] = []
    names = recovery.recovered.keys()
    from_file = [k for k in names if k not in recovery.from_api]
    if from_file:
        out.append(
            f"locked (is_shown_once) variable(s) {from_file} will be read from {path} on "
            "the source server and set on the target, still locked. They carry the value "
            "Coolify last wrote on the source server (applications: at their last deploy; "
            "services and databases: at their last start or save); a {{...}} "
            "shared-variable reference inside one arrives resolved."
        )
    if recovery.from_api:
        out.append(
            f"locked (is_shown_once) variable(s) {list(recovery.from_api)} will be read "
            "from the source database's connection URL (Coolify's internal_db_url) and "
            "set on the target, still locked."
        )
    out.extend(
        f"the recovered value of {key} names the source server's wildcard domain or IP; "
        "the target receives it unchanged — update it after the cutover if it should "
        "point at the target."
        for key in source_server_hits
    )
    if recovery.left_to_target:
        out.append(
            f"locked variable(s) {list(recovery.left_to_target)} are generated by the "
            "target itself (from its own domains and compose services) and are not carried."
        )
    return out
