"""Coolify's pin of its OWN host's SSH key, and what an instance move does to it.

PURE module: no IO.

From 4.7.0 Coolify pins the SSH host key of every server it manages and checks
it on every connection (``FEATURES["ssh_host_key_pinning"]``). Its own host — the
server record usually named ``localhost``, ip ``host.docker.internal`` — has no
exemption: learnHostKey skips only checking "off", an existing pin and a missing
settings row (SshMultiplexingHelper v4.7.0:731-783), and an instance upgraded to
4.7 pins whatever key its host presents (source "upgrade", :754).

F2 copies the database that holds that pin, but not ``/etc/ssh``: the new host's
sshd presents keys of its own. Coolify forgets a pin only when the server's ip
or port changes (Server.php v4.7.0:200-203, the placeholder-ip backfill :456) or
its Cloudflare-tunnel setting does (ServerSetting.php :275-276), and none of that
happens to ``localhost`` in a move. So the relocated instance reaches its own
host with the OLD key pinned, and what follows depends on the checking mode
(hostKeyCheckingMode, SshMultiplexingHelper v4.7.0:566-578):

* **warn**, the default: ssh still connects (StrictHostKeyChecking=no over the
  pinned known_hosts, :853-855). The connection check saves the new key as
  pending and reports it once (Server.php :2614-2654) — an alert after every
  relocation, which trains admins to click "trust" without looking.
* **enforce** (``SSH_HOST_KEY_CHECKING=enforce`` in the copied
  ``/data/coolify/source/.env``, or the server's own ``is_ssh_host_key_check_enforced``):
  the first connection check marks the host unreachable and unusable
  (ServerConnectionCheckJob v4.7.0:105-113). Nothing runs there — proxy,
  deployments, the start of every resource F2 moved — and all of it after the
  source has been fenced. BOOT cannot see it: the container's health check is
  HTTP only (docker-compose.prod.yml v4.7.0:47-48), and the decrypt probe never
  makes Coolify SSH anywhere.
* **off** (instance-wide, or the server's ``is_ssh_host_key_check_disabled``):
  nothing is compared, nothing to say.

A warning, never a refusal: one trust, after comparing, ends it, and the source
stays intact for a rollback. The tool trusts nothing itself: F2 uses no API
during the run, and a reset sent with an empty key would drop the pin and let
Coolify trust whatever key it sees next — the opposite of the point.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from bg_coolify_migrate.domain.coolify_versions import supports
from bg_coolify_migrate.domain.versions import Version

#: The instance-wide mode lives in Coolify's .env, which docker-compose.prod.yml
#: loads as the container's env_file (v4.7.0:32-33); config/constants.php :116
#: reads it as strtolower(trim(env('SSH_HOST_KEY_CHECKING', 'warn'))).
CHECKING_ENV = "SSH_HOST_KEY_CHECKING"

_MODES = frozenset({"warn", "enforce", "off"})
_CHECKING_RE = re.compile(rf"^[ \t]*(?:export[ \t]+)?{CHECKING_ENV}[ \t]*=(.*)$", re.MULTILINE)


def _env_value(raw: str) -> str:
    """One dotenv value: quoted up to its closing quote, else up to a `` #`` comment."""
    value = raw.strip()
    if value[:1] in ('"', "'"):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return re.split(r"[ \t]#", value, maxsplit=1)[0]


def instance_checking_mode(env_text: str | None) -> str | None:
    """The instance-wide checking mode, read from Coolify's .env. ``None``: not read.

    An absent or unknown value is "warn", as Coolify reads it (:566-578). A file
    that sets it more than once fails toward the warning rather than guessing
    which line the container's environment kept: "enforce" if any line says so,
    "off" only if every line does.
    """
    if env_text is None:
        return None
    modes = {_env_value(m).strip().lower() for m in _CHECKING_RE.findall(env_text)}
    modes = {m if m in _MODES else "warn" for m in modes}
    if "enforce" in modes:
        return "enforce"
    if modes == {"off"}:
        return "off"
    return "warn"


def checking_mode(instance_mode: str | None, settings: Mapping[str, Any]) -> str | None:
    """The mode Coolify applies to one server; ``None`` when it cannot be told.

    hostKeyCheckingMode (v4.7.0:566-578): an instance "off" or the server's own
    disable wins; then an instance "enforce" or the server's own enforce. A server
    can only make itself stricter than the instance, never weaker than "off". An
    unread instance mode with neither flag set could be any of the three.
    """
    if instance_mode == "off" or settings.get("is_ssh_host_key_check_disabled"):
        return "off"
    if instance_mode == "enforce" or settings.get("is_ssh_host_key_check_enforced"):
        # Under an unread instance mode this may really be "off"; enforce is the
        # outcome to warn about.
        return "enforce"
    return instance_mode


def _pinned(settings: Mapping[str, Any]) -> bool | None:
    """Whether a key is pinned. ``None`` when the record does not say.

    GET /servers and GET /servers/{uuid} carry the settings row, ``ssh_host_key``
    included (the public keys; not in ServerSetting::$hidden, v4.7.0:245-254),
    plus ``ssh_host_key_fingerprints`` computed from it (ServersController
    v4.7.0:56-59, both routes through removeSensitiveDataFromSettings :170,:249).
    """
    if "ssh_host_key_fingerprints" not in settings and "ssh_host_key" not in settings:
        return None
    return bool(settings.get("ssh_host_key_fingerprints") or settings.get("ssh_host_key"))


def self_host_key_warning(
    server: Mapping[str, Any], *, version: Version | None, instance_mode: str | None
) -> str | None:
    """The plan warning about Coolify's own host key, or ``None``.

    ``server`` is Coolify's record of its own host (with ``settings``). Silent
    before 4.7.0, when checking is off, and when nothing is pinned — Coolify then
    pins whatever the new host presents on first use. A record that does not say
    whether a key is pinned warns: "we could not tell" is no evidence it is safe.
    """
    if not supports("ssh_host_key_pinning", version):
        return None
    raw = server.get("settings")
    settings: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    mode = checking_mode(instance_mode, settings)
    if mode == "off" or _pinned(settings) is False:
        return None

    name = str(server.get("name") or "localhost")
    uuid = str(server.get("uuid") or "{uuid}")
    head = (
        f"Coolify pins the SSH host key of its own server {name!r}, and the copied "
        "database keeps the OLD host's key while the new host's sshd presents its own "
        "(F2 does not copy /etc/ssh). "
    )
    if mode == "enforce":
        effect = (
            "Checking is ENFORCED for it (SSH_HOST_KEY_CHECKING=enforce in "
            "/data/coolify/source/.env, or the server's own setting): until the new key "
            "is trusted the new instance cannot reach its own host — the proxy, "
            "deployments and the start of every resource moved there fail, after the "
            "source is fenced. "
        )
    elif mode == "warn":
        effect = (
            "In warn mode Coolify still connects, keeps the new key pending and reports "
            "a changed host key for its own server once. "
        )
    else:
        effect = (
            "In warn mode (the default) Coolify still connects and reports a changed "
            "host key once; under enforce (SSH_HOST_KEY_CHECKING=enforce, or the "
            "server's own setting) the new instance cannot reach its own host until "
            "the key is trusted — the proxy, deployments and every start there fail, "
            "after the source is fenced. "
        )
    remedy = (
        f"After the move, compare the pending key under Servers > {name} > Security "
        "with the target's own (`ssh-keygen -lf` on each /etc/ssh/ssh_host_*_key.pub "
        "there; match each SHA256 fingerprint) and trust it, or POST "
        f"/api/v1/servers/{uuid}/host-key/reset with `ssh_host_key` set to the "
        "target's public key lines (`write`, a team admin). Never send it empty: that "
        "drops the pin and Coolify trusts whatever key it sees next."
    )
    return head + effect + remedy
