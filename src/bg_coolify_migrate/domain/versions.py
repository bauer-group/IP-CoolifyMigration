"""Coolify release versions, compared as numbers.

PURE module: no IO.

Several rules this tool enforces exist only from a given Coolify release on —
a create field that 422s from 4.5.0, a storage field removed in 4.3.22. Gating
them on the version keeps a rule from refusing a migration on an older instance
where it cannot fail that way, which is the warning-as-furniture failure in a
more expensive form: a refusal that fires on a correct setup.

Compared on int tuples, never strings: ``4.3.10`` is newer than ``4.3.3``
numerically and OLDER lexically, and a string compare once called the exact
version this tool was validated against out of range.
"""

from __future__ import annotations

Version = tuple[int, ...]


def parse_version(raw: str | None) -> Version | None:
    """``"4.3.2"`` -> ``(4, 3, 2)``. ``None`` when it is not a plain version.

    Tolerates a leading ``v`` and a trailing suffix (``4.4.0-beta.1`` ->
    ``(4, 4, 0)``) because Coolify publishes long beta chains, and refuses to
    guess at anything else.
    """
    if raw is None:
        return None
    head = raw.strip().lstrip("v").split("-")[0].split("+")[0]
    parts = head.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def at_least(version: Version | None, minimum: Version) -> bool:
    """Whether a rule introduced in ``minimum`` applies to this instance.

    An UNKNOWN version counts as new enough. Every caller gates a check that
    protects a migration, and "we could not tell which Coolify this is" is not
    evidence that the protection is unnecessary — an unparseable version must
    fail toward checking, never toward skipping.
    """
    return version is None or version >= minimum
