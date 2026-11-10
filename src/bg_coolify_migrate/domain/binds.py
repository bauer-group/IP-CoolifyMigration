"""Bind mounts: which host paths move, where they land, and what may be removed.

PURE module: no IO. ``transfer/binds.py`` asks the servers; this module decides
what the answers mean. Everything here is total over plain values, so every rule
is tested without a fake host.

**Why binds need their own rules.** A named volume lives under
``/var/lib/docker/volumes`` and nowhere else, so copying one can only ever touch
that volume. A bind mount is an arbitrary host path, so copying one is an
arbitrary write on the target server, and deleting one in a rollback is an
arbitrary ``rm -rf``. Each of these questions therefore has an explicit answer:

* **What does the target mount?** Read back, never predicted (invariant 10).
  Coolify records every compose bind as a file-storage row: the parsers create
  one for each local source (``./``, ``/``, ``~``, ``..``) except
  ``/var/run/docker.sock`` and ``/tmp`` (parsers.php applications/services
  v4.1.2:774-812/2161-2193, v4.5.0:1565-1603/2990-3022, v4.7.2:1613-1657/
  3062-3098), keyed on ``(mount_path, resource)``. A relative source resolves
  under the resource's own directory, ``/data/coolify/{applications|services}/
  {uuid}`` (``resolveComposeBindSource``, shared.php v4.7.2:2124-2132), so the
  TARGET's row for the same mount path names the target's directory. A plain
  application's host-path persistent storage and its file storages are rows
  too (``copy_storages`` POSTs them). So a source bind pairs with the target row
  at its mount path, and the copy goes to whatever path that row names.
* **Is it data?** A directory row is created with ``mkdir -p`` and never
  emptied: ``saveStorageOnServer`` only creates a missing directory
  (LocalFileVolume v4.7.2:333-405), a deploy writes only non-directory rows
  (ApplicationDeploymentJob v4.7.2:1546-1550), a service start writes only its
  compose and ``.env`` (Service::saveComposeConfigs v4.7.2:1648-1695). A file
  row WITH content is written from that content (``tee``, LocalFileVolume
  v4.7.2:390-392; a service start writes it where it is missing,
  EnsureContentFilesOnServer v4.7.2:51); everything else in the directory
  is the application's own data, and Coolify recreates it EMPTY on a new
  resource. Skipping all of ``/data/coolify`` lost it.
* **Does the TARGET's row hold that content?** Only where CREATE gives it one
  (:class:`StorageCarry`): a plain app's rows are POSTed with it, a compose
  app re-reads its compose (``content:`` included) from git, a template
  service is created from the catalog first. A raw-compose service is created
  from the source's stored ``docker_compose_raw``, which Coolify keeps WITHOUT
  ``content:`` (parsers.php v4.1.2:2740 .. v4.7.2:3709), so its target row is
  a contentless directory (v4.7.2:3051-3058): that file is mirrored instead. A
  database's rows are not carried at all. The rest is read back at the end of
  CREATE (:func:`content_problem`): a template or a repository can drop a file.
* **May we write there?** Only into a path that is absent, or an empty
  directory or file (Coolify's own storage job may have created it). Anything
  else may be another resource's data.
* **May a rollback delete it?** Only a path this run created, journalled before
  it was created, and never a path :func:`removable` refuses.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bg_coolify_migrate.domain.compose import PASSTHROUGH_HOST_PATHS
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.naming import COOLIFY_BASE_DIR, resource_config_dir

#: The three per-resource roots under ``/data/coolify`` (application_, service_
#: and database_configuration_dir, shared.php v4.7.2:82-97). A uuid is random,
#: so a path under ``{root}/{uuid}`` belongs to that resource whichever root it
#: is: a service on compose parsing version 3 resolves ``./`` under the
#: APPLICATIONS root (composeResourceDirectory, parsers.php v4.7.2:138-145).
RESOURCE_ROOTS: tuple[str, ...] = tuple(
    f"{COOLIFY_BASE_DIR}/{segment}" for segment in ("applications", "services", "databases")
)

#: The Docker socket under both of its names (``/var/run`` is a symlink to
#: ``/run``). Coolify passes ``/var/run/docker.sock`` through untouched; the
#: other spelling is the same socket.
DOCKER_SOCKETS = frozenset({"/var/run/docker.sock", "/run/docker.sock"})

#: Host trees that are the host's own, at and below the path: system binaries
#: and configuration, kernel and runtime filesystems, Docker's own state, and
#: root's home (SSH keys).
SYSTEM_TREES: tuple[str, ...] = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/lost+found",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/snap",
    "/sys",
    "/usr",
    "/var/run",
    "/var/lib/docker",
    "/var/lib/containerd",
)

#: Directories that hold MANY owners' data: the directory itself is the host's,
#: a path below it may be a resource's.
SYSTEM_DIRS = frozenset(
    {
        "/",
        "/data",
        "/home",
        "/media",
        "/mnt",
        "/opt",
        "/srv",
        "/tmp",
        "/var",
        "/var/lib",
        "/var/log",
    }
)

#: Journal key of one bind target COPY is about to fill (a ``step_started``
#: detail of COPY): ``{"path", "prior", "created"}``, written BEFORE the path is
#: touched. Its presence also makes COPY's compensations owed when COPY fails
#: (statemachine.owed_states).
BIND_TARGET = "bind_target"
#: Journal key of the bind targets a rollback removed (a ``rollback_step``
#: detail): a replayed rollback must not delete what someone created there since.
#: Written once per path, the moment its removal succeeded; every record
#: counts (:func:`owed_bind_targets`).
BINDS_REMOVED = "binds_removed"


class BindAction(StrEnum):
    """What the migration does with one bind mount."""

    MIGRATE = "migrate"
    SKIP = "skip"
    """Coolify itself writes it on the target; there is nothing to copy."""
    PASSTHROUGH = "passthrough"
    """The target host supplies its own (a socket, a read-only system file)."""
    REFUSE = "refuse"


class StorageCarry(StrEnum):
    """How a resource's storage rows reach its target."""

    COMPOSE = "compose"
    """Recreated by the target's own parse of a compose that keeps ``content:``:
    a compose app loads it from git; a template service is created from the
    catalog first, and the source's compose then keeps the catalog's rows."""
    RAW_COMPOSE = "raw_compose"
    """A raw-compose service: recreated by parsing the source's stored
    ``docker_compose_raw``, which Coolify keeps without ``content:`` and
    ``isDirectory`` (parsers.php v4.1.2:2740, v4.5.0:3638, v4.7.2:3701-3718).
    Every row is recreated, none holds content, and a bind the compose does not
    flag otherwise becomes a directory row (v4.7.2:3051-3058)."""
    API = "api"
    """POSTed by CREATE (``resources.copy_storages``) — except a file whose
    content the API shows as a placeholder, which it skips."""
    NONE = "none"
    """A database: CREATE carries none of its storage rows."""


#: Where a bind source was found on the host. Mirrors LocalFileVolume's own
#: REMOTE_FILE_STATE_SCRIPT (v4.7.2:53-73), with symlinks and empty files told
#: apart: a symlink is never written through, an empty file is no one's data.
STATE_MISSING = "missing"
STATE_EMPTY_DIR = "empty-directory"
STATE_DIR = "directory"
STATE_EMPTY_FILE = "empty-file"
STATE_FILE = "file"
STATE_SYMLINK = "symlink"
STATE_OTHER = "other"
STATES = frozenset(
    {
        STATE_MISSING,
        STATE_EMPTY_DIR,
        STATE_DIR,
        STATE_EMPTY_FILE,
        STATE_FILE,
        STATE_SYMLINK,
        STATE_OTHER,
    }
)

#: A bind's payload, as COPY and VERIFY must treat it.
KIND_DIR = "dir"
KIND_FILE = "file"


# ── paths ────────────────────────────────────────────────────────────────────


def normalise(path: object) -> str | None:
    """``path`` as an absolute canonical POSIX path, or None. PURE.

    Doubled and trailing slashes go; a ``.`` or ``..`` segment makes it None:
    such a path is not compared, written or deleted on a guess.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        return None
    return "/" + "/".join(parts)


def is_within(path: str, base: str) -> bool:
    """``path`` is ``base`` or below it. Both normalised. PURE."""
    if base == "/":
        return path.startswith("/")
    return path == base or path.startswith(f"{base}/")


def overlaps(a: str, b: str) -> bool:
    """One path contains the other. PURE."""
    return is_within(a, b) or is_within(b, a)


def own_dirs(uuid: str | None) -> tuple[str, ...]:
    """Every directory Coolify can keep for the resource ``uuid``. PURE."""
    if not uuid:
        return ()
    return tuple(f"{root}/{uuid}" for root in RESOURCE_ROOTS)


def workdir(kind: ResourceKind | None, uuid: str) -> str:
    """The directory a relative ``fs_path`` of this resource resolves in. PURE."""
    if kind is None:
        return f"{COOLIFY_BASE_DIR}/applications/{uuid}"
    return resource_config_dir(kind, uuid)


def own_dir_of(path: str, uuid: str | None) -> str | None:
    """The resource directory ``path`` lies strictly inside, or None. PURE."""
    for directory in own_dirs(uuid):
        if path != directory and is_within(path, directory):
            return directory
    return None


def is_system_path(path: str, *, uuid: str | None = None) -> bool:
    """Is ``path`` the host's own rather than a resource's? PURE.

    Below ``/data/coolify`` only the resource's OWN directory is the resource's:
    every other path there is Coolify's state (the proxy, SSH keys, backups) or
    another resource's directory.
    """
    if path in SYSTEM_DIRS or ".ssh" in path.split("/"):
        return True
    if any(is_within(path, tree) for tree in SYSTEM_TREES):
        return True
    if is_within(path, COOLIFY_BASE_DIR):
        return own_dir_of(path, uuid) is None
    return False


def resolve_fs_path(fs_path: object, base: str) -> str | None:
    """A storage row's ``fs_path`` as the host path Coolify mounts. PURE.

    LocalFileVolume::resolvedFsPath (v4.7.2:574-586): an absolute path is used
    as is, a relative one lives in the resource directory with a leading ``.``
    and ``/`` stripped. ``~`` depends on the SSH user's home and is None here.
    """
    if not isinstance(fs_path, str):
        return None
    path = fs_path.strip()
    if not path or path.startswith("~"):
        return None
    if not path.startswith("/"):
        if path.startswith("."):
            path = path[1:]
        path = path.lstrip("/")
        path = base.rstrip("/") + (f"/{path}" if path else "")
    return normalise(path)


def ancestors(path: str) -> list[str]:
    """Every proper ancestor of ``path`` below ``/``, outermost first. PURE."""
    parts = [p for p in path.split("/") if p]
    return ["/" + "/".join(parts[:i]) for i in range(1, len(parts))]


# ── storage rows ─────────────────────────────────────────────────────────────

#: What GET /storages shows instead of a file's content it will not return
#: (LocalFileVolume BINARY_PLACEHOLDER / TOO_LARGE_PLACEHOLDER, v4.7.2:19-21).
CONTENT_PLACEHOLDERS = frozenset({"[binary file]", "[file too large to display]"})


@dataclass(frozen=True, slots=True)
class StorageRow:
    """One storage row that mounts a host path."""

    mount_path: str
    host_path: str
    writes_content: bool = False
    """A file Coolify writes from content kept in the row at every deploy."""
    placeholder: bool = False
    """A file the API shows as binary or too large: its content cannot be carried."""


def storage_rows(storages: Mapping[str, Any] | None, *, base: str) -> list[StorageRow]:
    """The host-path rows of a ``/storages`` response. PURE.

    File storages always mount a host path (``fs_path``); a persistent storage
    only when ``host_path`` is set (StartPostgresql v4.7.2:229-235 and the
    deploy job mount ``host_path:mount_path``), otherwise it is a named volume.
    """
    if not storages:
        return []
    out: list[StorageRow] = []
    for entry in storages.get("file_storages") or []:
        if not isinstance(entry, dict):
            continue
        mount = normalise(entry.get("mount_path"))
        host = resolve_fs_path(entry.get("fs_path"), base)
        if mount is None or host is None:
            continue
        content = entry.get("content")
        is_dir = entry.get("is_directory") is True
        placeholder = isinstance(content, str) and content in CONTENT_PLACEHOLDERS
        writes = (
            not is_dir
            and entry.get("is_host_file") is not True
            and isinstance(content, str)
            and bool(content)
            and not placeholder
        )
        out.append(StorageRow(mount, host, writes_content=writes, placeholder=placeholder))
    for entry in storages.get("persistent_storages") or []:
        if not isinstance(entry, dict):
            continue
        mount = normalise(entry.get("mount_path"))
        host = normalise(entry.get("host_path"))
        if mount is not None and host is not None:
            out.append(StorageRow(mount, host))
    return out


def carry_for(kind: ResourceKind | None) -> StorageCarry:
    """How this kind's storage rows reach the target. PURE."""
    if kind is ResourceKind.DATABASE:
        return StorageCarry.NONE
    if kind is ResourceKind.SERVICE_COMPOSE:
        return StorageCarry.RAW_COMPOSE
    if kind in (ResourceKind.APP_GIT_COMPOSE, ResourceKind.SERVICE_TEMPLATE):
        return StorageCarry.COMPOSE
    return StorageCarry.API


# ── classification ───────────────────────────────────────────────────────────

_MIRRORED = "bind mount of host path {path}: mirrored path-to-path onto the target server"
_OWN_DATA = (
    "{path} in the resource's Coolify directory: data Coolify never regenerates (it only "
    "creates the directory), so it is mirrored into the target's own directory"
)
_PLACEHOLDER = (
    "{path} is a file whose content the API cannot carry (binary or over 5 MiB), so it "
    "is mirrored with rsync"
)


def classify(
    *,
    source: str,
    destination: str,
    read_only: bool,
    uuid: str | None,
    rows: Sequence[StorageRow] | None,
    carry: StorageCarry,
) -> tuple[BindAction, str]:
    """What happens to one bind mount of the source, and why. PURE.

    ``rows`` are the resource's own host-path storage rows; ``None`` when the
    API could not be read, in which case nothing is refused for a missing row
    (CREATE's read-back of the target is the proof), but a database's binds
    keep their own answer.
    """
    path = normalise(source)
    if path is None:
        return BindAction.REFUSE, (
            f"bind source {source!r} is not an absolute path, so it cannot be copied or "
            "compared safely"
        )
    if path in PASSTHROUGH_HOST_PATHS or path in DOCKER_SOCKETS:
        return BindAction.PASSTHROUGH, f"{path} is supplied by the target host, not migrated"

    if path in own_dirs(uuid):
        if read_only:
            return BindAction.SKIP, (
                f"{path} is the resource's whole Coolify directory (its generated compose, "
                ".env and repository checkout), regenerated at every deploy"
            )
        return BindAction.REFUSE, (
            f"writable bind of the resource's whole Coolify directory {path}: it mixes "
            "files Coolify regenerates with the application's data, and copying it would "
            "plant the source's compose and .env in the target's directory. Mount a "
            "subdirectory for the data instead"
        )

    inside_own = own_dir_of(path, uuid) is not None
    system = not inside_own and is_system_path(path, uuid=uuid)
    match = _matching_row(rows, destination=destination, path=path)
    if match is not None and match.writes_content:
        # Before the system-path rule: Coolify writes such a file wherever the
        # compose put it (isAdminControlledComposeMount, LocalFileVolume
        # v4.7.2:319-330), so it is the resource's, not the host's.
        return _content_file(path, carry=carry, system=system)
    if system:
        if read_only:
            return BindAction.PASSTHROUGH, (
                f"read-only bind of the host's own {path}: the target server supplies its own"
            )
        return BindAction.REFUSE, (
            f"writable bind of the host's own {path}: it is the server's (system files, "
            "runtime state, Docker's or Coolify's own data, or another resource's "
            "directory), not this resource's, and copying it would overwrite the target "
            "server's. Make it read-only if the container only reads it"
        )

    if carry is StorageCarry.NONE:
        if inside_own and match is None:
            return BindAction.SKIP, (
                f"{path} is written by Coolify from the database's settings at every "
                "start (custom configuration, init scripts, certificates)"
            )
        return BindAction.REFUSE, (
            f"bind mount of {path}: a database's storages are not carried to the target "
            "(CREATE recreates only the volume Coolify gives every database), so the "
            "target would not mount it and its data would be left behind"
        )
    if rows is not None and match is None:
        return BindAction.REFUSE, (
            f"bind mount of {path} at {destination} is declared in no Coolify storage "
            "(custom docker run options, or a compose bind with "
            "bind.create_host_path: false), so where the target mounts it cannot be "
            "read back and its data cannot be placed"
        )
    if match is not None and match.placeholder:
        if carry is StorageCarry.API:
            return BindAction.REFUSE, (
                f"{path} is a file mount whose content the API cannot carry (binary or "
                "over 5 MiB), so CREATE cannot recreate it and the target would not "
                "mount it. Turn it into a directory mount, or recreate it on the target "
                "by hand"
            )
        return BindAction.MIGRATE, _PLACEHOLDER.format(path=path)
    return BindAction.MIGRATE, (_OWN_DATA if inside_own else _MIRRORED).format(path=path)


def _content_file(path: str, *, carry: StorageCarry, system: bool) -> tuple[BindAction, str]:
    """A file Coolify writes from its storage row's content: does the TARGET get it? PURE."""
    if carry is StorageCarry.NONE:
        return BindAction.REFUSE, (
            f"{path} is a file mount whose content Coolify keeps in the database's storage "
            "row, and CREATE carries no storage row of a database, so the target would start "
            "without it. Remove the file mount on the source and add it to the target after "
            "the migration, or move the setting into the database's own configuration"
        )
    if carry is StorageCarry.RAW_COMPOSE:
        if system:
            return BindAction.REFUSE, (
                f"{path} is a file Coolify writes from its storage row's content, but the "
                "target service is created from the source's compose, which Coolify keeps "
                "without `content:`, so the target's row holds none - and a migration never "
                "writes the host's own paths on the target server. Mount it from the "
                "service's directory (./ in the compose) instead"
            )
        return BindAction.MIGRATE, (
            f"{path} is a file Coolify wrote from its storage row's content, but the target "
            "service is created from the source's compose, which Coolify keeps without "
            "`content:` - the target's row holds none, so the file is mirrored with rsync"
        )
    origin = (
        "CREATE recreates its storage row with the content"
        if carry is StorageCarry.API
        else "the target's own parse gives its row the content (the repository's compose "
        "for an application, the template for a service)"
    )
    return BindAction.SKIP, (
        f"{path} is a file Coolify writes from its storage row's content: {origin}, and "
        "Coolify writes it on the target from there (CREATE reads that row back)"
    )


def regenerated_content(
    rows: Sequence[StorageRow] | None, *, source: str, destination: str
) -> bool:
    """Is this bind a file Coolify writes from the content of its own row? PURE."""
    path = normalise(source)
    match = _matching_row(rows, destination=destination, path=path) if path else None
    return match is not None and match.writes_content


def content_problem(
    *,
    source: str,
    mount_path: str,
    target_rows: Sequence[StorageRow],
    source_uuid: str,
    target_uuid: str,
) -> str | None:
    """Why the target will not write a content file the source has, or None. PURE.

    The plan skips such a file because Coolify writes it on the target from the
    content of the TARGET's row — so that row is read back at the end of CREATE,
    while the source still serves. Paired as :func:`pair_binds` pairs: by mount
    path, narrowed by place where two parts of a service share it.
    """
    src, mount = normalise(source), normalise(mount_path)
    candidates = [r for r in target_rows if r.mount_path == mount]
    if len({r.host_path for r in candidates}) > 1 and src is not None:
        where = _location(src, source_uuid)
        candidates = [
            r for r in candidates if _location(r.host_path, target_uuid) == where
        ] or candidates
    if any(r.writes_content for r in candidates):
        return None
    held = "has no storage row there" if not candidates else "has a row there without content"
    return (
        f"{mount_path} is a file Coolify writes from its storage row's content ({source} on "
        f"the source), but the target {held}: the template or the repository's compose no "
        "longer declares it with its content, so the target would mount an empty directory "
        "there"
    )


def _matching_row(
    rows: Sequence[StorageRow] | None, *, destination: str, path: str
) -> StorageRow | None:
    if rows is None:
        return None
    mount = normalise(destination)
    for row in rows:
        if row.mount_path == mount and row.host_path == path:
            return row
    return None


# ── pairing ──────────────────────────────────────────────────────────────────


class BindPairingError(ValueError):
    """A source bind has no unambiguous place on the target. Always fatal."""


@dataclass(frozen=True, slots=True)
class BindPair:
    """A source host path and the target host path its bytes go to."""

    source_path: str
    target_path: str
    mount_path: str
    kind: str | None = None
    """:data:`KIND_DIR` or :data:`KIND_FILE`, once the source was looked at."""
    empty: bool = False
    """The source held nothing when DISCOVER looked: an empty source later is
    then no loss (:func:`source_lost`)."""


def _location(path: str, uuid: str) -> tuple[str, str]:
    """``("own", relative)`` inside the resource's directory, else ``("abs", path)``."""
    directory = own_dir_of(path, uuid)
    if directory is None:
        return "abs", path
    return "own", path[len(directory) + 1 :]


def pair_binds(
    sources: Sequence[tuple[str, str]],
    target_rows: Sequence[StorageRow],
    *,
    source_uuid: str,
    target_uuid: str,
) -> list[BindPair]:
    """Pair each source bind ``(host path, mount path)`` with the target's row. PURE.

    By MOUNT PATH, read back from what Coolify created for the target — the
    rule of invariant 10, never a uuid replaced in a path. Where two of the
    target's rows share a mount path (two parts of a service), the one at the
    same place wins: the same relative path in the resource directory, or the
    same absolute path.

    Raises:
        BindPairingError: No target row at the mount path, or no single one.
    """
    out: list[BindPair] = []
    for source, mount in sources:
        src = normalise(source)
        dest = normalise(mount)
        if src is None or dest is None:
            raise BindPairingError(f"bind {source!r} at {mount!r} is not an absolute path")
        candidates = [r for r in target_rows if r.mount_path == dest]
        paths = {r.host_path for r in candidates}
        if len(paths) > 1:
            where = _location(src, source_uuid)
            narrowed = {
                r.host_path for r in candidates if _location(r.host_path, target_uuid) == where
            }
            paths = narrowed or paths
        if not paths:
            declared = sorted(f"{r.host_path} @ {r.mount_path}" for r in target_rows)
            raise BindPairingError(
                f"bind {src} at {dest} has no counterpart on the target: it declares no "
                f"storage at {dest} (target declares: {declared or 'nothing'})"
            )
        if len(paths) > 1:
            raise BindPairingError(
                f"bind {src} at {dest} matches several target storages {sorted(paths)}"
            )
        out.append(BindPair(source_path=src, target_path=paths.pop(), mount_path=dest))
    return out


def merge_pairs(per_resource: Mapping[str, Sequence[BindPair]]) -> list[BindPair]:
    """One copy job per payload, across every resource of the plan. PURE.

    Two resources binding the same host path are one payload. Refused: one
    source going to two places, two sources into one place, and a target nested
    in another target in a way its source is not nested in that one's source
    (one copy's ``--delete`` would erase the other's).

    Raises:
        BindPairingError: On any of those conflicts.
    """
    by_source: dict[str, BindPair] = {}
    for pairs in per_resource.values():
        for pair in pairs:
            seen = by_source.get(pair.source_path)
            if seen is None:
                by_source[pair.source_path] = pair
            elif seen.target_path != pair.target_path:
                raise BindPairingError(
                    f"bind {pair.source_path} would go to both {seen.target_path} and "
                    f"{pair.target_path} on the target"
                )
    merged = list(by_source.values())
    for a in merged:
        for b in merged:
            if a is b:
                continue
            if a.target_path == b.target_path:
                raise BindPairingError(
                    f"binds {a.source_path} and {b.source_path} would both be copied to "
                    f"{a.target_path} on the target"
                )
            if is_within(b.target_path, a.target_path):
                rel = b.target_path[len(a.target_path) :]
                if b.source_path != a.source_path.rstrip("/") + rel:
                    raise BindPairingError(
                        f"bind target {b.target_path} lies inside {a.target_path}, but its "
                        f"source {b.source_path} is not the matching path inside "
                        f"{a.source_path}: one copy would delete the other"
                    )
    return merged


def pair_problem(pair: BindPair, *, source_uuid: str, target_uuid: str | None) -> str | None:
    """Why no plan of this version would copy ``pair``, or None. PURE.

    :func:`classify`'s host-path rule, applied again to a pair whatever made it.
    Above all to a pair DISCOVER journalled under an EARLIER version, which a
    resumed COPY reloads (``context.deserialise_bind_pairs``) and no PLAN of
    this version vetted: up to 2.14.0 every bind that was neither passed
    through nor under ``/data/coolify`` was journalled path-to-path, writable
    ``/etc/...``, ``/usr/...`` and ``/var/lib/docker/...`` included, and COPY
    would create and fill such a path on the target server. And to a target
    row naming a path the source's does not (a branch HEAD that moved since
    PLAN): the target is read back, never predicted, so only this keeps the
    copy out of the target host's own paths.

    The source must be the resource's: no system path, or strictly inside its
    own Coolify directory. The target must be the TARGET resource's by the same
    rule with ``target_uuid``: under ``/data/coolify`` only inside the target's
    own directory — not Coolify's state, another resource's directory or a tree
    named after the source's uuid — and nowhere there while its uuid is unknown.
    """
    for side, path, uuid in (
        ("source", pair.source_path, source_uuid),
        ("target", pair.target_path, target_uuid),
    ):
        if normalise(path) != path:
            return f"bind {pair.source_path} -> {pair.target_path}: its {side} is not canonical"
        if own_dir_of(path, uuid) is None and is_system_path(path, uuid=uuid):
            whose = "the resource's" if side == "source" else "the target resource's"
            return (
                f"bind {pair.source_path} -> {pair.target_path}: the {side} {path} is not "
                f"{whose} to {'copy' if side == 'source' else 'write'} (a system path, "
                "Coolify's own state or another resource's directory)"
            )
    return None


# ── host states ──────────────────────────────────────────────────────────────


def source_kind(state: str) -> str | None:
    """The payload kind of a source path in ``state``, or None if it is none. PURE."""
    if state in (STATE_DIR, STATE_EMPTY_DIR):
        return KIND_DIR
    if state in (STATE_FILE, STATE_EMPTY_FILE):
        return KIND_FILE
    return None


def is_empty_state(state: str) -> bool:
    """An empty directory or an empty file. PURE."""
    return state in (STATE_EMPTY_DIR, STATE_EMPTY_FILE)


def source_lost(path: str, state: str, *, kind: str | None, was_empty: bool) -> str | None:
    """Why a copied bind's source no longer holds what was copied, or None. PURE.

    The rollback guard's question once FINALIZE kept or renamed the source
    (``compensations.source_data_refusal``): every compensation after it aims
    at the target, and RESTART_SOURCE brings the source up on whatever its host
    path holds then. A missing path is recreated EMPTY — Coolify ``mkdir -p``s a
    directory row (LocalFileVolume v4.7.2:333-336), ``docker compose up``
    creates any missing bind source as a directory (EnsureContentFilesOnServer
    v4.7.2:11-12) — so the same holds for a path that is empty now where
    DISCOVER saw data: someone deleted it and a start recreated it. A source of
    another shape than the one copied is not it either.
    """
    found = source_kind(state)
    if found is None:
        return f"bind {path} ({'gone' if state == STATE_MISSING else f'now a {state}'})"
    if kind is not None and found != kind:
        return f"bind {path} (now a {found}, where a {kind} was copied)"
    if is_empty_state(state) and not was_empty:
        return f"bind {path} (empty now, where it held data when it was copied)"
    return None


def source_state_problem(path: str, state: str) -> str | None:
    """Why the source path cannot be copied, or None. PURE."""
    if source_kind(state) is not None:
        return None
    if state == STATE_MISSING:
        return f"{path} does not exist on the source server"
    if state == STATE_SYMLINK:
        return f"{path} is a symbolic link on the source server; bind its target instead"
    return f"{path} is not a regular file or directory (a socket or device holds no data)"


def target_state_problem(path: str, state: str, *, kind: str) -> str | None:
    """Why the target path may not be written, or None. PURE.

    Absent, or an empty directory or file — which Coolify's own storage job may
    have made for the new resource — is the target's alone. Anything else may
    be another resource's data and is never overwritten. An empty directory
    where the source has a file is Coolify's default (a bind whose row says
    nothing is taken for a directory, parsers.php v4.7.2:1568-1571); it is
    replaced.
    """
    if state == STATE_MISSING:
        return None
    if state == STATE_EMPTY_DIR:
        return None
    if state == STATE_EMPTY_FILE:
        if kind == KIND_FILE:
            return None
        return f"{path} is an empty file on the target server, but the source has a directory"
    if state in (STATE_DIR, STATE_FILE):
        return f"{path} already holds data on the target server - it may be another resource's"
    if state == STATE_SYMLINK:
        return f"{path} is a symbolic link on the target server; nothing is written through one"
    return f"{path} is a special file on the target server"


def resumed_target_problem(path: str, state: str, *, kind: str) -> str | None:
    """Why a target THIS run already began filling cannot be copied over, or None. PURE.

    Its content is this run's own partial copy, so it may hold data; only a
    shape the copy cannot replace stops it.
    """
    fits = {
        KIND_DIR: {STATE_MISSING, STATE_EMPTY_DIR, STATE_DIR},
        KIND_FILE: {STATE_MISSING, STATE_EMPTY_DIR, STATE_EMPTY_FILE, STATE_FILE},
    }
    if state in fits.get(kind, set()):
        return None
    return f"{path} on the target server is a {state}, where this run copies a {kind}"


def ancestor_problem(path: str, states: Mapping[str, str]) -> str | None:
    """Why the parents of a target path forbid writing it, or None. PURE.

    ``states`` maps each of :func:`ancestors` to its state. A symlink would
    redirect the write somewhere no check looked at; a file cannot hold one.
    """
    for parent in ancestors(path):
        state = states.get(parent, STATE_MISSING)
        if state == STATE_SYMLINK:
            return f"{parent}, a parent of {path}, is a symbolic link on the target server"
        if state not in (STATE_MISSING, STATE_DIR, STATE_EMPTY_DIR):
            return f"{parent}, a parent of {path}, is not a directory on the target server"
    return None


def missing_paths(path: str, states: Mapping[str, str]) -> list[str]:
    """Every ancestor of ``path``, and ``path`` itself, that does not exist. PURE.

    Outermost first: exactly what ``mkdir -p`` (or the copy) will create, and so
    what a rollback may remove again.
    """
    return [p for p in (*ancestors(path), path) if states.get(p, STATE_MISSING) == STATE_MISSING]


# ── who else mounts it ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HostBind:
    """One bind mount of one container on a server."""

    container: str
    source: str
    rw: bool = True
    labels: Mapping[str, str] = field(default_factory=dict)


def belongs_to(
    bind: HostBind, *, uuids: Iterable[str], label_sets: Iterable[Mapping[str, str]]
) -> bool:
    """Is this a container of the plan's own resources? PURE.

    By uuid in its name or any label value (a rename cannot move it, see
    ``docker.carries_uuid``) or by the label triple Coolify filters on itself.
    """
    if any(u and (u in bind.container or any(u in v for v in bind.labels.values())) for u in uuids):
        return True
    return any(
        all(bind.labels.get(k) == v for k, v in labels.items()) for labels in label_sets if labels
    )


#: Coolify's management mounts: its own container binds these roots whole
#: (docker-compose.prod.yml), and writes into a resource's directory only when
#: that resource is saved or deployed.
_COOLIFY_ROOTS = frozenset({COOLIFY_BASE_DIR, *RESOURCE_ROOTS})

#: The whole host. A container binding it is a host-wide agent (Portainer's
#: agent runs with ``-v /:/host``, a host backup with ``/:/source``), not a
#: resource sharing one path's data: :func:`host_wide_writers`.
HOST_ROOT = "/"


def shared_with(path: str, binds: Iterable[HostBind]) -> list[HostBind]:
    """The binds that share ``path``'s bytes with it. PURE.

    The same path or one inside it, read-only or not: that container sees the
    same data. A writable bind of a PARENT too — it can write into ``path`` —
    except Coolify's own management roots and the whole host (:data:`HOST_ROOT`).
    A read-only parent (cAdvisor's ``/:/rootfs:ro``) only reads, and does not
    count.

    A writable bind of ``/`` reaches every path of the server, so counting it
    here refused every bind migration on a server running such an agent, with
    a remedy ("migrate the resources that share it together") that does not
    fit. It is warned about instead (:func:`host_wide_writers`). Every other
    parent still refuses, ``/data``, ``/srv`` or ``/home`` included: such a bind
    is a container keeping its data in a tree that holds this path.
    """
    out: list[HostBind] = []
    for bind in binds:
        other = normalise(bind.source)
        if other is None:
            continue
        sees_it = is_within(other, path)
        writes_into_it = (
            is_within(path, other)
            and bind.rw
            and other not in _COOLIFY_ROOTS
            and other != HOST_ROOT
        )
        if sees_it or writes_into_it:
            out.append(bind)
    return out


def host_wide_writers(binds: Iterable[HostBind]) -> list[HostBind]:
    """The containers that mount the whole host writable, once each. PURE.

    :func:`shared_with` leaves them out; the run warns about each instead
    (:func:`host_wide_notice`). A read-only ``/`` is no writer.
    """
    seen: dict[str, HostBind] = {}
    for bind in binds:
        if bind.rw and normalise(bind.source) == HOST_ROOT:
            seen.setdefault(bind.container, bind)
    return list(seen.values())


def host_wide_notice(bind: HostBind, *, paths: Sequence[str], server: str) -> str:
    """The run notice for one container that mounts the whole host writable. PURE."""
    shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
    return (
        f"{bind.container} on {server} mounts the whole host writable ({HOST_ROOT}), so it "
        f"can write the bind mount(s) {shown} while the migration copies them. It looks "
        "like a host agent (Portainer's agent, a host backup) rather than a resource "
        "sharing that data, so the migration goes on - but what it writes there while "
        "the data is copied and verified can fail VERIFY or go missing from the copy. "
        "Make that mount read-only, or stop the container until the migration has "
        "finished."
    )


# ── rollback ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BindTarget:
    """A target path COPY filled, as journalled before it was touched."""

    path: str
    prior: str
    """Its state before this run: only :data:`STATE_MISSING` was created by it."""
    created: tuple[str, ...] = ()
    """The paths this run created for it (missing parents, outermost first, then
    the path itself when it was missing)."""


def removable(path: str, *, target_uuids: Iterable[str] = ()) -> bool:
    """May a rollback delete this path on the target server? PURE, and strict.

    Canonical and at least two levels deep; inside a target resource's own
    directory (never the directory itself), or outside ``/data/coolify`` and
    every system path.
    """
    canonical = normalise(path)
    if canonical is None or canonical != path or canonical.count("/") < 2:
        return False
    if any(own_dir_of(canonical, uuid) for uuid in target_uuids):
        return True
    if is_within(canonical, COOLIFY_BASE_DIR):
        return False
    return not is_system_path(canonical)


def owed_bind_targets(records: Iterable[tuple[str, Mapping[str, Any]]]) -> dict[str, BindTarget]:
    """The bind targets a rollback still owes, from the journal's ``(event, detail)``. PURE.

    In journal order: a COPY record adds its path, a rollback's removal record
    drops it. So a replayed rollback skips what an earlier one removed, and a
    path filled again by a later attempt is owed again. Junk is skipped: a row
    that cannot be read is a path nobody deletes.
    """
    owed: dict[str, BindTarget] = {}
    for event, detail in records:
        if event == "rollback_step":
            for path in detail.get(BINDS_REMOVED) or ():
                if isinstance(path, str):
                    owed.pop(path, None)
            continue
        entry = detail.get(BIND_TARGET)
        if not isinstance(entry, dict):
            continue
        path, prior = entry.get("path"), entry.get("prior")
        created = entry.get("created") or ()
        if not isinstance(path, str) or not isinstance(prior, str):
            continue
        owed[path] = BindTarget(
            path=path,
            prior=prior,
            created=tuple(p for p in created if isinstance(p, str)),
        )
    return owed
