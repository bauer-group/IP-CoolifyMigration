"""Docker image references and how stable their tags are.

PURE module: no IO.

**Why this matters for a migration.** We build the target exactly as the source
is configured — same image reference, same tag. But a tag is a *pointer*, not a
version. If the source runs `postgres:16` and the target pulls `postgres:16`
tomorrow, the two can be different images.

Usually that is fine and unremarkable: a patch release. Sometimes it is not, and
the interesting case is specific to what this tool does. We copy the data
directory **byte-exactly** and then start a possibly-newer engine against it:

* ``postgres:16`` -> 16.1 to 16.4 : same on-disk format. Fine.
* ``postgres:latest`` crossing a major (16 -> 17) : the data directory is
  **incompatible**. Postgres refuses to start with "database files are
  incompatible with server". Nothing is lost — the source is untouched — but the
  migration fails at the healthcheck for a reason that is easy to misread.

So we classify the reference, say what could happen, and let the operator decide.
That is a judgement about *their* stack, not one we can make for them.

There is a second, subtler trap for floating tags, and it is Coolify's rather
than ours: ``StandalonePostgresql::created`` picks the volume mount path by
regexing the tag (``/:(?:pg)?(\\d+)/i``), sending 18+ to ``/var/lib/postgresql``
and everything else to ``/var/lib/postgresql/data``. A tag of ``latest`` matches
nothing, so it gets the pre-18 path — even if ``latest`` is now 18 and the
container expects the other one. :func:`mount_path_is_guessable` flags it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: Tags that explicitly track a moving target.
_MOVING_TAGS = frozenset(
    {"latest", "edge", "main", "master", "stable", "nightly", "dev", "develop", "rolling"}
)

#: `16`, `v8` — a bare major. Moves on every minor release.
_BARE_MAJOR_RE = re.compile(r"^v?\d+$")
#: `16.4`, `v1.2` — major.minor. Moves on patch releases.
_MAJOR_MINOR_RE = re.compile(r"^v?\d+\.\d+(?:-[\w.]+)?$")
#: `16.4.1`, `1.2.3-alpine` — fully qualified.
_FULL_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[\w.]+)?$")

#: Coolify's own regex for deciding a Postgres volume's mount path.
_COOLIFY_PG_VERSION_RE = re.compile(r":(?:pg)?(\d+)", re.IGNORECASE)


class TagStability(StrEnum):
    """How likely this reference is to resolve to a different image later."""

    PINNED = "pinned"
    """Digest-pinned (``@sha256:...``). Cannot change. Nothing to warn about."""

    EXACT = "exact"
    """A fully-qualified version (``16.4.1``). Conventionally immutable, though a
    registry can technically re-push it."""

    PATCH_FLOATING = "patch_floating"
    """``16.4`` — picks up patch releases. Same on-disk format; low risk."""

    MINOR_FLOATING = "minor_floating"
    """``16`` — picks up minor releases within a major. Still the same on-disk
    format for every engine we care about; low risk."""

    MOVING = "moving"
    """``latest``, or no tag at all. Can cross a MAJOR version, which for a
    database means the copied data directory may be unreadable by the new
    engine. This is the one worth stopping to think about."""


@dataclass(frozen=True, slots=True)
class ImageRef:
    """A parsed docker image reference."""

    raw: str
    registry: str | None
    name: str
    tag: str | None
    digest: str | None

    @property
    def effective_tag(self) -> str:
        """The tag docker will actually use. No tag means ``latest``."""
        return self.tag or "latest"

    @property
    def stability(self) -> TagStability:
        return classify_tag(self.tag, has_digest=bool(self.digest))

    @property
    def is_floating(self) -> bool:
        """True if the target could pull a different image than the source runs."""
        return self.stability not in (TagStability.PINNED, TagStability.EXACT)

    def __str__(self) -> str:
        return self.raw


def parse(reference: str) -> ImageRef:
    """Parse ``[registry[:port]/]name[:tag][@digest]``.

    The awkward part is telling a registry port from a tag: both are a colon.
    Docker's own rule — a colon before the last slash is a port — is what we use.
    """
    raw = reference.strip()
    rest = raw
    digest: str | None = None

    if "@" in rest:
        rest, _, digest_part = rest.rpartition("@")
        digest = digest_part or None

    registry: str | None = None
    tag: str | None = None

    last_slash = rest.rfind("/")
    last_colon = rest.rfind(":")
    if last_colon > last_slash:
        # A colon after the final slash is a tag, not a registry port.
        rest, _, tag_part = rest.rpartition(":")
        tag = tag_part or None

    if last_slash != -1:
        candidate, _, name = rest.partition("/")
        # A first segment is a registry only if it looks like a host: it has a
        # dot, a port, or is literally localhost. Otherwise it is an org name.
        if "." in candidate or ":" in candidate or candidate == "localhost":
            registry = candidate
        else:
            name = rest
    else:
        name = rest

    return ImageRef(raw=raw, registry=registry, name=name, tag=tag, digest=digest)


def classify_tag(tag: str | None, *, has_digest: bool = False) -> TagStability:
    """How stable is this tag? PURE."""
    if has_digest:
        return TagStability.PINNED
    if tag is None or tag.lower() in _MOVING_TAGS:
        return TagStability.MOVING
    if _FULL_VERSION_RE.match(tag):
        return TagStability.EXACT
    if _MAJOR_MINOR_RE.match(tag):
        return TagStability.PATCH_FLOATING
    if _BARE_MAJOR_RE.match(tag):
        return TagStability.MINOR_FLOATING
    # A named tag like `16-alpine` or `production`. It carries some intent but
    # nothing enforces immutability, so treat it as patch-floating: worth
    # mentioning, not worth alarming about.
    return TagStability.PATCH_FLOATING


def risk_note(ref: ImageRef, *, is_database: bool = False) -> str | None:
    """A plain-language description of what this tag could do. PURE.

    Returns ``None`` when there is nothing worth saying. The wording is
    deliberately concrete: "may pull a newer image" is not actionable, "may cross
    a major version and refuse to start on the copied data" is.
    """
    stability = ref.stability

    if stability in (TagStability.PINNED, TagStability.EXACT):
        return None

    if stability is TagStability.MOVING:
        if is_database:
            return (
                f"{ref.raw} is a moving tag: the target may pull a NEWER MAJOR version "
                "than the source is running. Database data directories are not compatible "
                "across majors, so the engine may refuse to start on the copied data "
                "(your source stays untouched either way). Pin the tag if unsure."
            )
        return (
            f"{ref.raw} is a moving tag: the target will pull whatever it points at now, "
            "which may not be what the source is running."
        )

    if stability is TagStability.MINOR_FLOATING:
        return (
            f"{ref.raw} tracks minor releases within a major: the target may pull a newer "
            "minor than the source is running. Normally compatible."
        )

    return (
        f"{ref.raw} tracks patch releases: the target may pull a newer patch than the "
        "source is running. Normally compatible."
    )


def mount_path_is_guessable(image: str) -> bool:
    """Whether Coolify can read a Postgres major version out of this tag.

    ``StandalonePostgresql::created`` regexes the tag to choose between
    ``/var/lib/postgresql`` (18+) and ``/var/lib/postgresql/data``. When the
    regex finds nothing — ``postgres:latest``, ``postgres`` — it silently takes
    the pre-18 path, which is wrong if the tag actually resolves to 18+.

    Returns False when the tag defeats that regex, so callers can warn.
    """
    return bool(_COOLIFY_PG_VERSION_RE.search(image))


def same_image(a: str | None, b: str | None) -> bool:
    """Whether two references name the same image AND tag. PURE."""
    if not a or not b:
        return False
    ref_a, ref_b = parse(a), parse(b)
    return (
        ref_a.registry == ref_b.registry
        and ref_a.name == ref_b.name
        and ref_a.effective_tag == ref_b.effective_tag
    )
