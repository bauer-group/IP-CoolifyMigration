"""Compose document analysis.

PURE module: no IO. Takes a compose document (already decoded from base64) and
answers the questions the migration actually needs:

* Which services **build from source** rather than pull an image? This is the
  question that makes ``build_pack`` insufficient on its own — a service or a
  dockercompose application whose compose says ``build:`` is rebuilt on the
  target exactly like a nixpacks app.
* Which mounts exist, and of what class? Coolify skips ``driver_opts.type=cifs``
  and passes ``/var/run/docker.sock`` and ``/tmp`` straight through; we mirror
  those decisions rather than inventing our own.
* What is the **topology fingerprint**? For ``build_pack=dockercompose`` the
  compose is re-read from git on every deploy, so the file that will be deployed
  on the target may declare *different volumes* than the one currently running.
  Comparing fingerprints turns that from a silent data-loss bug into a blocked
  migration.

Both compose short syntax (``name:/path:ro``) and long syntax
(``{type: volume, source: x, target: /path}``) are handled, because real stacks
mix them freely.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import yaml

#: Host paths Coolify passes through untouched rather than treating as data.
#: Mirrors upstream's own special-casing; migrating these would be actively wrong
#: (the target has its own docker socket, and /tmp is scratch by definition).
PASSTHROUGH_HOST_PATHS = frozenset({"/var/run/docker.sock", "/tmp"})


class MountClass(StrEnum):
    """How a mount must be treated by the migration."""

    NAMED = "named"
    """A named docker volume. Mirrored byte-for-byte."""

    ANONYMOUS = "anonymous"
    """No source given — docker invents a 64-hex id that CANNOT be reproduced
    on the target. Refused by default rather than silently losing the data."""

    BIND = "bind"
    """A host path. Mirrored, but the path may need rewriting if it embeds a uuid."""

    TMPFS = "tmpfs"
    """RAM-backed and empty by contract. Skipped."""

    PASSTHROUGH = "passthrough"
    """docker.sock / /tmp. Skipped — the target supplies its own."""

    CIFS = "cifs"
    """Network-attached; Coolify skips these and so do we — the share is already
    shared, and copying it would duplicate rather than move."""


class ComposeError(ValueError):
    """The compose document could not be understood.

    Deliberately fatal: a compose we cannot parse is a compose whose volumes we
    cannot enumerate, and migrating a stack whose data we have not fully
    enumerated is exactly the failure mode this tool exists to prevent.
    """


@dataclass(frozen=True, slots=True)
class ComposeMount:
    """One mount declared by one service."""

    service: str
    mount_class: MountClass
    source: str | None
    """The volume name or host path. ``None`` for anonymous volumes."""
    target: str
    """The in-container path. This is the STABLE key we pair old->new volumes on,
    because names always change across a migration but mount paths do not."""
    read_only: bool = False


def parse(raw: str) -> dict[str, Any]:
    """Parse a compose document.

    Args:
        raw: The decoded YAML text (callers base64-decode Coolify's
            ``docker_compose_raw`` first).

    Raises:
        ComposeError: If the document is not valid YAML or not a mapping.
    """
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ComposeError(f"compose is not valid YAML: {exc}") from exc
    if doc is None:
        raise ComposeError("compose is empty")
    if not isinstance(doc, dict):
        raise ComposeError(f"compose must be a mapping, got {type(doc).__name__}")
    return doc


def services(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The ``services:`` mapping, tolerating absence and null entries."""
    raw = doc.get("services") or {}
    if not isinstance(raw, dict):
        raise ComposeError("`services` must be a mapping")
    return {name: (body or {}) for name, body in raw.items() if isinstance(body, dict | None)}


def build_services(doc: dict[str, Any]) -> list[str]:
    """Names of services that build from source instead of pulling an image.

    THE function that makes "does this resource build?" answerable. A service
    counts as building when it declares ``build:`` — whether short form
    (``build: ./app``) or long form (``build: {context: ., dockerfile: X}``).

    A service may legally declare *both* ``build:`` and ``image:`` (the image
    then names the build output); that still builds, so ``build:`` wins.
    """
    return sorted(name for name, body in services(doc).items() if body.get("build") is not None)


def builds_from_source(doc: dict[str, Any]) -> bool:
    """True if any service in this compose builds from source."""
    return bool(build_services(doc))


def declared_volume_names(doc: dict[str, Any]) -> list[str]:
    """Top-level ``volumes:`` keys, honouring an explicit ``name:`` override.

    Coolify emits explicit ``name:`` keys so there is no compose-project prefix
    on the resulting docker volume — the key and the real name can differ, and
    the real name is what exists under /var/lib/docker/volumes.
    """
    raw = doc.get("volumes") or {}
    if not isinstance(raw, dict):
        raise ComposeError("`volumes` must be a mapping")
    out: list[str] = []
    for key, body in raw.items():
        if isinstance(body, dict) and body.get("name"):
            out.append(str(body["name"]))
        else:
            out.append(str(key))
    return sorted(out)


def _volume_driver_types(doc: dict[str, Any]) -> dict[str, str | None]:
    """Top-level volume key -> ``driver_opts.type`` (for cifs detection)."""
    raw = doc.get("volumes") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str | None] = {}
    for key, body in raw.items():
        opts = (body or {}).get("driver_opts") if isinstance(body, dict) else None
        out[str(key)] = str(opts.get("type")) if isinstance(opts, dict) and opts.get("type") else None
    return out


def _classify_source(source: str | None, driver_types: dict[str, str | None]) -> MountClass:
    if source is None:
        return MountClass.ANONYMOUS
    if source in PASSTHROUGH_HOST_PATHS:
        return MountClass.PASSTHROUGH
    if driver_types.get(source) == "cifs":
        return MountClass.CIFS
    # A source containing a path separator or starting with . or ~ is a bind.
    # Everything else is a docker volume name (docker's own rule).
    if source.startswith(("/", "./", "../", "~")):
        return MountClass.BIND
    return MountClass.NAMED


def _parse_short_mount(service: str, spec: str, driver_types: dict[str, str | None]) -> ComposeMount:
    """Parse ``[source:]target[:mode]``.

    Windows-style drive letters are not a concern (these composes run on Linux),
    but a bare ``/data`` (anonymous volume) must not be mistaken for a bind.
    """
    parts = spec.split(":")
    read_only = parts[-1] in ("ro", "readonly") if len(parts) >= 2 else False
    if read_only or (len(parts) >= 3 and parts[-1] in ("rw", "z", "Z", "cached", "delegated", "consistent")):
        parts = parts[:-1]

    if len(parts) == 1:
        # `- /data` -> anonymous volume mounted at /data
        return ComposeMount(service, MountClass.ANONYMOUS, None, parts[0], read_only)
    if len(parts) == 2:
        source, target = parts
        return ComposeMount(service, _classify_source(source, driver_types), source, target, read_only)
    raise ComposeError(f"service {service!r}: cannot parse volume spec {spec!r}")


def _parse_long_mount(
    service: str, spec: dict[str, Any], driver_types: dict[str, str | None]
) -> ComposeMount:
    target = spec.get("target")
    if not target:
        raise ComposeError(f"service {service!r}: long-syntax mount without `target`")
    source = spec.get("source")
    declared = str(spec.get("type") or "").lower()
    read_only = bool(spec.get("read_only"))

    if declared == "tmpfs":
        return ComposeMount(service, MountClass.TMPFS, None, str(target), read_only)
    if declared == "bind":
        cls = MountClass.PASSTHROUGH if source in PASSTHROUGH_HOST_PATHS else MountClass.BIND
        return ComposeMount(service, cls, str(source) if source else None, str(target), read_only)
    # Declared as a volume, or omitted entirely -> infer the class from the source.
    src = str(source) if source else None
    return ComposeMount(service, _classify_source(src, driver_types), src, str(target), read_only)


def mounts(doc: dict[str, Any]) -> list[ComposeMount]:
    """Every mount declared by every service, classified.

    Includes ``tmpfs:`` entries (as :attr:`MountClass.TMPFS`) so that a caller
    enumerating "everything this stack mounts" sees them and can consciously skip
    them, rather than them being invisible.
    """
    driver_types = _volume_driver_types(doc)
    out: list[ComposeMount] = []
    for name, body in services(doc).items():
        for spec in body.get("volumes") or []:
            if isinstance(spec, str):
                out.append(_parse_short_mount(name, spec, driver_types))
            elif isinstance(spec, dict):
                out.append(_parse_long_mount(name, spec, driver_types))
            else:
                raise ComposeError(f"service {name!r}: unsupported volume entry {spec!r}")
        for spec in body.get("tmpfs") or []:
            target = spec.split(":")[0] if isinstance(spec, str) else str(spec)
            out.append(ComposeMount(name, MountClass.TMPFS, None, target, False))
    return out


def data_mounts(doc: dict[str, Any]) -> list[ComposeMount]:
    """Only the mounts whose bytes must actually move."""
    keep = {MountClass.NAMED, MountClass.BIND, MountClass.ANONYMOUS}
    return [m for m in mounts(doc) if m.mount_class in keep]


def has_anonymous_volumes(doc: dict[str, Any]) -> list[ComposeMount]:
    """Anonymous volumes, which cannot be faithfully reproduced on the target.

    Docker names these with a random 64-hex id. There is no way to recreate that
    id on the target, and no stable key to pair them by other than the mount
    path — which is ambiguous when one service declares several. Callers refuse
    by default and surface these to the operator.
    """
    return [m for m in mounts(doc) if m.mount_class is MountClass.ANONYMOUS]


def topology_fingerprint(doc: dict[str, Any]) -> str:
    """A stable hash over the volume-relevant structure of a compose document.

    Used by the drift gate. For ``build_pack=dockercompose`` Coolify re-reads the
    compose from git at deploy time, so the target may materialise a *different*
    set of volumes than the source is running. If that happens, the old->new
    volume mapping computed from the source is quietly wrong and data lands
    nowhere — so a fingerprint mismatch is a hard block, not a warning.

    Deliberately ignores everything that does not affect which bytes live where:
    image tags, ports, env, labels, healthchecks, restart policies. Renaming a
    service DOES change the fingerprint, because Coolify derives volume names
    from service names.
    """
    shape = {
        "volumes": declared_volume_names(doc),
        "mounts": sorted(
            (m.service, m.mount_class.value, m.source or "", m.target) for m in mounts(doc)
        ),
        "builds": build_services(doc),
    }
    blob = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
