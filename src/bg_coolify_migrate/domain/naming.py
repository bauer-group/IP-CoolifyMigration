"""Volume naming and old->new pairing.

PURE module: no IO.

**The single most important rule in this codebase lives here: never derive a
target volume name by string-replacing a UUID.** That is exactly how
``coolify-mover`` silently loses every service volume — it rewrites using the
sub-application's uuid while the volume name is prefixed with the *parent
service's* uuid, so the replace matches nothing, the DB row keeps the OLD name,
the data is copied to a NEW name, and Docker then auto-creates an empty volume at
deploy time. No error is raised at any point.

Volume names **cannot be preserved** across a migration, by construction:

* ``POST /{kind}/{uuid}/storages`` forces ``name = '{new_resource_uuid}-{name}'``.
* Standalone DB volumes are created by the model's ``created`` hook as
  ``'{engine}-data-{new_uuid}'``.
* Compose volumes are re-derived from the new uuid when Coolify parses the
  compose.

So the target names are whatever Coolify decides. The only correct algorithm is:
create the target, let Coolify materialise its own volumes, read them back, and
**pair source to target by ``mount_path``** — the one key that is stable across
the migration because it is a property of the container, not of Coolify.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from bg_coolify_migrate.domain.kinds import ENGINE_VOLUME_PREFIX, DatabaseEngine, ResourceKind

#: Where the local docker driver stores a named volume's contents.
DOCKER_VOLUME_ROOT = "/var/lib/docker/volumes"

#: Coolify's on-host configuration root.
COOLIFY_BASE_DIR = "/data/coolify"


class VolumePairingError(ValueError):
    """Source and target volumes could not be paired unambiguously.

    Always fatal. An unpaired volume means bytes with nowhere to go, or bytes
    going somewhere unintended — both are silent data loss if allowed through.
    """


#: Characters that Unicode NFKD does NOT decompose, but which Laravel's
#: ``Str::ascii`` (voku/portable-ascii) transliterates to multi-letter ASCII.
#:
#: NFKD alone silently DELETES these — 'Grüße' would slugify to 'grue' rather
#: than 'grusse', because 'ü' decomposes to u+diaeresis but 'ß' decomposes to
#: nothing at all and is dropped by ``encode('ascii', 'ignore')``. That matters
#: for a German-language estate, where 'ß' and 'ø'-class letters appear in real
#: service names.
#:
#: Note Coolify calls ``Str::slug($source, '-')`` with no language argument, so
#: the DEFAULT map applies: 'ä' -> 'a', not the German-locale 'ae'.
_TRANSLITERATE = str.maketrans(
    {
        "ß": "ss",
        "æ": "ae",
        "Æ": "ae",
        "œ": "oe",
        "Œ": "oe",
        "ø": "o",
        "Ø": "o",
        "đ": "d",
        "Đ": "d",
        "ð": "d",
        "Ð": "d",
        "þ": "th",
        "Þ": "th",
        "ł": "l",
        "Ł": "l",
        "ı": "i",  # noqa: RUF001 - dotless i is the point; it is what we transliterate
        "ŋ": "n",
        "ħ": "h",
        "ĸ": "k",
    }
)


def _ascii(value: str) -> str:
    """Laravel's ``Str::ascii``, near enough: transliterate, then drop the rest.

    The explicit table comes first because NFKD has no opinion on characters that
    are not decomposable accents — it passes `ß` straight through, and the
    ascii-encode then deletes it outright, turning `Grüße` into `grue`.
    """
    transliterated = value.translate(_TRANSLITERATE)
    normalised = unicodedata.normalize("NFKD", transliterated)
    return normalised.encode("ascii", "ignore").decode("ascii")


def slugify(value: str, separator: str = "-") -> str:
    """Laravel's ``Str::slug($value, '-')``, ported step for step.

    Load-bearing, and not only for predicting volume names: container discovery
    filters on `coolify.projectName` / `coolify.environmentName` /
    `coolify.resourceName`, which Coolify writes through this exact function. A
    slug that differs by one character matches no containers, and `docker ps`
    answers an empty list rather than an error — so the stack looks like it has
    no volumes and the migration cheerfully moves nothing.

    Mirrors the original's order, which is where the subtlety lives::

        $title = static::ascii($title, $language);
        $title = preg_replace('![_]+!u', '-', $title);          // flip
        $title = str_replace('@', '-at-', $title);              // dictionary
        $title = preg_replace('![^-\\pL\\pN\\s]+!u', '', lower($title));
        $title = preg_replace('![-\\s]+!u', '-', $title);
        return trim($title, '-');

    Note step four **removes** unwanted characters instead of replacing them, and
    only then does step five collapse runs. `a.b.c` becomes `abc`, not `a-b-c` —
    they coincide only when the stripped character happens to sit next to
    whitespace, which is why `Straße & Co` agrees either way and hid this.

    Verified against the running Laravel by test_slug_matches_laravel in the e2e
    suite. A unit test here could only check this against our own idea of
    Str::slug, which is the assumption that needs checking.
    """
    text = _ascii(value)

    flip = "_" if separator == "-" else "-"
    text = re.sub(f"[{re.escape(flip)}]+", separator, text)

    # Laravel's default dictionary. Not decoration: `me@host` slugs to
    # `me-at-host`, and dropping the `at` silently mismatches the label.
    text = text.replace("@", f"{separator}at{separator}")

    quoted = re.escape(separator)
    text = re.sub(rf"[^{quoted}a-z0-9\s]+", "", text.lower())
    text = re.sub(rf"[{quoted}\s]+", separator, text)
    return text.strip(separator)


def database_volume_name(engine: DatabaseEngine, uuid: str) -> str:
    """``{engine}-data-{uuid}`` as created by the model's ``created`` hook.

    Note the engine prefix is NOT always the API path segment: ``postgresql``'s
    volume is ``postgres-data-*``. :data:`ENGINE_VOLUME_PREFIX` holds the mapping.
    """
    return f"{ENGINE_VOLUME_PREFIX[engine]}-data-{uuid}"


def service_volume_name(service_uuid: str, source: str) -> str:
    """``{service_uuid}_{slug}`` — note the UNDERSCORE.

    Prefixed with the *parent service's* uuid even when the volume belongs to a
    ServiceApplication/ServiceDatabase child. Getting this wrong is the
    coolify-mover data-loss bug.
    """
    return f"{service_uuid}_{slugify(source)}"


def application_volume_name(app_uuid: str, name: str) -> str:
    """``{app_uuid}-{name}`` — note the HYPHEN.

    The separator genuinely differs from services. This asymmetry is why an
    application must never be converted into a service or vice versa.
    """
    return f"{app_uuid}-{name}"


def storage_api_volume_name(resource_uuid: str, name: str) -> str:
    """What ``POST /{kind}/{uuid}/storages`` will store, whatever name we send.

    Upstream unconditionally prefixes; sending ``data`` yields ``{uuid}-data``.
    """
    return f"{resource_uuid}-{name}"


def volume_data_path(volume_name: str) -> str:
    """Host path of a named volume's contents."""
    return f"{DOCKER_VOLUME_ROOT}/{volume_name}/_data"


def compose_volume_separator(kind: ResourceKind) -> str:
    """``_`` for services, ``-`` for applications, ``""`` for standalone DBs."""
    if kind in (ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE):
        return "_"
    if kind is ResourceKind.APP_GIT_COMPOSE:
        return "-"
    return ""


def postgres_mount_path(image: str) -> str:
    """Where Postgres keeps its data, which depends on the major version.

    Coolify's ``StandalonePostgresql::created`` hook parses the image tag with
    ``/:(?:pg)?(\\d+)/i`` and mounts ``/var/lib/postgresql`` for 18+ but
    ``/var/lib/postgresql/data`` below it. If the target is created without
    pinning ``image`` to the source's exact tag, the target can pick the *other*
    path and the mirrored bytes land where the server will not look for them.

    Unparseable tags fall back to the pre-18 path, matching upstream's own
    behaviour when the regex does not match.
    """
    match = re.search(r":(?:pg)?(\d+)", image, re.IGNORECASE)
    if match and int(match.group(1)) >= 18:
        return "/var/lib/postgresql"
    return "/var/lib/postgresql/data"


def resource_config_dir(kind: ResourceKind, uuid: str) -> str:
    """``/data/coolify/{applications|services|databases}/{uuid}``.

    Derived state — regenerated on every deploy and embedding the OLD uuid, so it
    must NOT be copied. Exposed so callers can explicitly recognise and skip it.
    """
    if kind is ResourceKind.DATABASE:
        segment = "databases"
    elif kind in (ResourceKind.SERVICE_TEMPLATE, ResourceKind.SERVICE_COMPOSE):
        segment = "services"
    else:
        segment = "applications"
    return f"{COOLIFY_BASE_DIR}/{segment}/{uuid}"


@dataclass(frozen=True, slots=True)
class VolumeEndpoint:
    """One side of a pairing: a volume and where its container sees it."""

    name: str
    mount_path: str
    container: str | None = None
    """Compose service name, when known. Disambiguates identical mount paths
    across different services of one stack (e.g. two services both at /data)."""


@dataclass(frozen=True, slots=True)
class VolumePair:
    """A resolved source->target volume mapping."""

    source: VolumeEndpoint
    target: VolumeEndpoint

    @property
    def source_path(self) -> str:
        return volume_data_path(self.source.name)

    @property
    def target_path(self) -> str:
        return volume_data_path(self.target.name)


def _key(ep: VolumeEndpoint, *, with_container: bool) -> tuple[str, ...]:
    return (ep.container or "", ep.mount_path) if with_container else (ep.mount_path,)


def pair_by_mount_path(
    source: list[VolumeEndpoint],
    target: list[VolumeEndpoint],
) -> list[VolumePair]:
    """Pair source volumes to target volumes by mount path.

    THE correct algorithm, and the reason this module exists. Names change across
    a migration by construction; mount paths do not, because they are declared by
    the compose/image rather than by Coolify.

    Pairs on ``(container, mount_path)`` when container names are known on both
    sides — necessary because one stack can legitimately mount two different
    volumes at the same path in two different services. Falls back to
    ``mount_path`` alone only when neither side carries container names.

    Raises:
        VolumePairingError: On any ambiguity (duplicate keys) or any unpaired
            volume on either side. Both are refused rather than guessed: an
            unpaired source volume is data left behind, and an unpaired target
            volume is a volume that will silently start empty.
    """
    with_container = all(ep.container for ep in source) and all(ep.container for ep in target)

    src_index: dict[tuple[str, ...], VolumeEndpoint] = {}
    for ep in source:
        k = _key(ep, with_container=with_container)
        if k in src_index:
            raise VolumePairingError(
                f"ambiguous source volumes: {src_index[k].name!r} and {ep.name!r} "
                f"both mount at {ep.mount_path!r}"
                + (f" in service {ep.container!r}" if ep.container else "")
                + ". Cannot pair safely."
            )
        src_index[k] = ep

    tgt_index: dict[tuple[str, ...], VolumeEndpoint] = {}
    for ep in target:
        k = _key(ep, with_container=with_container)
        if k in tgt_index:
            raise VolumePairingError(
                f"ambiguous target volumes: {tgt_index[k].name!r} and {ep.name!r} "
                f"both mount at {ep.mount_path!r}. Cannot pair safely."
            )
        tgt_index[k] = ep

    missing_target = sorted(src_index.keys() - tgt_index.keys())
    if missing_target:
        names = ", ".join(repr(src_index[k].name) for k in missing_target)
        raise VolumePairingError(
            f"source volume(s) with no counterpart on the target: {names}. "
            "Their data would be left behind. This usually means the target was "
            "created from a different compose than the source is running."
        )

    missing_source = sorted(tgt_index.keys() - src_index.keys())
    if missing_source:
        names = ", ".join(repr(tgt_index[k].name) for k in missing_source)
        raise VolumePairingError(
            f"target volume(s) with no counterpart on the source: {names}. "
            "They would start empty. This usually means the target was created "
            "from a different compose than the source is running."
        )

    return [VolumePair(source=src_index[k], target=tgt_index[k]) for k in sorted(src_index)]
