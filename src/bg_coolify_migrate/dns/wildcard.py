"""Server-bound wildcard domains.

PURE module: no IO.

A Coolify install with a per-server wildcard (the BAUER GROUP setup, and any
install that sets one) gives each server a wildcard base —
``app.0046-20.cloud.bauer-group.com`` — and generates every resource's default
URL underneath it: ``pdf-tool.app.0046-20.cloud.bauer-group.com``.

That URL is *bound to the server*. The wildcard's DNS record (``*.app.0046-20…``)
points at that one host, so the same hostname can never answer on a different
server. Migrating such a resource therefore cannot "cut over" the URL by
repointing DNS — the URL has to be *rewritten* onto the TARGET server's
wildcard: ``pdf-tool.app.0047-20.cloud.bauer-group.com``.

This is the exact opposite of a custom domain (``shop.example.com``), which is
server-independent and moves *with* the resource — by repointing its own DNS
record at the new host. Telling the two apart is the whole job of this module:
a host under the source server's wildcard is rewritten; anything else is a
custom domain the DNS gate reasons about.
"""

from __future__ import annotations

import re

from bg_coolify_migrate.dns.extract import normalise_host

#: A leading URL scheme, e.g. ``https://``. Same shape extract.normalise_host
#: strips, repeated here so the ``*.`` label underneath a scheme is reachable
#: before normalisation (which rejects a leading ``*`` and would return None).
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def wildcard_base(wildcard: str | None) -> str | None:
    """The bare, comparable host of a server's wildcard domain, or ``None``.

    Coolify stores it as a URL-ish string that may be ``https://*.app.x…``,
    ``*.app.x…``, ``https://app.x…`` or the bare host. Reduce it to the suffix
    we compare against: drop the scheme, then a leading ``*.`` wildcard label
    (``normalise_host`` rejects a leading ``*``, so both must go first).
    """
    if not wildcard:
        return None
    raw = _SCHEME_RE.sub("", wildcard.strip())
    if raw.startswith("*."):
        raw = raw[2:]
    return normalise_host(raw)


def under_wildcard(host: str, wildcard: str | None) -> bool:
    """Whether ``host`` is the wildcard base itself or a subdomain of it.

    ``pdf-tool.app.0046-20…`` is under ``app.0046-20…``; ``shop.example.com`` is
    not. The endswith test is anchored on a leading dot so ``notapp.0046-20…``
    cannot masquerade as being under ``app.0046-20…``.
    """
    base = wildcard_base(wildcard)
    if not base:
        return False
    return host == base or host.endswith("." + base)


def remap_host(
    host: str, source_wildcard: str | None, target_wildcard: str | None
) -> str | None:
    """Rewrite a source-wildcard host onto the target server's wildcard.

    ``pdf-tool.app.0046-20…`` with source base ``app.0046-20…`` and target base
    ``app.0047-20…`` becomes ``pdf-tool.app.0047-20…``.

    Returns ``None`` when ``host`` is not under the source wildcard (a custom
    domain, which is never rewritten) or when either base is missing — the
    caller then leaves the host untouched.
    """
    src = wildcard_base(source_wildcard)
    tgt = wildcard_base(target_wildcard)
    if not src or not tgt:
        return None
    if host == src:
        return tgt
    if host.endswith("." + src):
        # Keep the sub-label including its trailing dot: "pdf-tool." + base.
        label = host[: -len(src)]
        return label + tgt
    return None
