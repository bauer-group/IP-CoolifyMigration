"""Locked variables: the plan-time review and the CREATE-time recovery.

The decisions live in ``domain/locked_envs.py`` (what can be read back, and
how exactly) and ``domain/fidelity.py`` (what an unrecoverable one means for the
target). This module only fetches the inputs and wires the two together, at the
two moments they are needed:

* PLAN — :func:`review_envs` reads the source's config file, recovers what it
  can, runs the fidelity env rules on the result and returns LINES: warnings,
  refusals and the names of the keys it promises to carry. The values die with
  the call; nothing that reaches a ResourcePlan or plan.json holds one.
* CREATE — :func:`recover_for_create` reads everything AGAIN, before the step's
  first write. A promised key that can no longer be read, or a credential the
  target would regenerate, refuses with nothing changed. The Secrets it returns
  live in the step's locals until copy_envs has sent them; they are never put
  on the context and never journaled.

Recovery is therefore never persisted, and a resumed CREATE re-runs it from the
files. That is all it says about a resume: the targets an interrupted CREATE had
already created — env rows, recovered values included — are not this module's
to find or undo; that is the rollback's handling of an interrupted attempt.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.discovery import envfiles
from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain import locked_envs as le
from bg_coolify_migrate.domain.kinds import DatabaseEngine, ResourceKind
from bg_coolify_migrate.domain.plan import ResourcePlan, ResourceSnapshot
from bg_coolify_migrate.domain.versions import Version
from bg_coolify_migrate.errors import EnvRecoveryError, MigrationError
from bg_coolify_migrate.transfer.ssh import RemoteHost

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EnvReview:
    """What the plan says about one resource's env vars. Names and lines only."""

    warnings: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    recover_env_keys: tuple[str, ...] = ()


def _has_locked(envs: Sequence[dict[str, Any]]) -> bool:
    return any(le.is_locked(e) and not e.get("is_preview") for e in envs)


async def _recover(
    source_host: RemoteHost,
    snapshot: ResourceSnapshot,
    envs: Sequence[dict[str, Any]],
    *,
    version: Version | None,
    full: dict[str, Any],
) -> tuple[le.LockedRecovery, str]:
    """Read the file and recover. Returns the recovery and the file's path."""
    source = await envfiles.read_source_config(source_host, snapshot.collection, snapshot.uuid)
    ports = full.get("ports_exposes") if snapshot.collection == "applications" else None
    recovery = le.recover(
        envs,
        source,
        collection=snapshot.collection,
        uuid=snapshot.uuid,
        version=version,
        compose_app=snapshot.kind is ResourceKind.APP_GIT_COMPOSE,
        compose=snapshot.docker_compose_raw,
        engine=snapshot.engine,
        internal_db_url=_db_url(full, snapshot),
        ports_exposes=ports if isinstance(ports, str) else None,
    )
    return recovery, source.path


def _db_url(full: dict[str, Any], snapshot: ResourceSnapshot) -> str | None:
    """internal_db_url — only for the one engine that needs it, never stored.

    ``None`` for a locked credential from 4.5.1, which withholds the URL
    (locked_envs.redis_password_from_url): the file is the only source then.
    """
    url = full.get("internal_db_url") if snapshot.engine is DatabaseEngine.REDIS else None
    return url if isinstance(url, str) else None


def _needs_full(snapshot: ResourceSnapshot) -> bool:
    """Whether recovery reads the resource itself: Redis's URL, an app's ports."""
    return snapshot.engine is DatabaseEngine.REDIS or snapshot.collection == "applications"


async def review_envs(
    source_host: RemoteHost,
    *,
    snapshot: ResourceSnapshot,
    full: dict[str, Any],
    envs: list[dict[str, Any]],
    version: Version | None,
    compose: str | None,
    source_ip: str | None = None,
    source_wildcard: str | None = None,
) -> EnvReview:
    """The env-variable part of a resource's plan. Reads only.

    Fail-soft like the rest of the planner: a file that cannot be read, or a
    recovery that raises, leaves the locked keys unrecovered (with the reason)
    and the normal refusal/warning classes decide what that means.
    """
    view: list[dict[str, Any]] = envs
    unrecovered: dict[str, str] | None = None
    lines: list[str] = []
    promised: tuple[str, ...] = ()

    if _has_locked(envs):
        failure: str | None = None
        try:
            recovery, path = await _recover(source_host, snapshot, envs, version=version, full=full)
        except Exception as exc:  # a pure function failing must not stop the plan
            failure = type(exc).__name__
        if failure is not None:
            log.warning("planner.locked_envs_failed", uuid=snapshot.uuid, error=failure)
            recovery, path = (
                le.LockedRecovery(
                    unrecoverable={
                        str(e["key"]): f"reading it back failed ({failure})"
                        for e in envs
                        if le.is_locked(e) and not e.get("is_preview")
                    }
                ),
                "",
            )
        # SERVICE_FQDN_/URL_ rows the target generates itself are neither carried
        # nor missing on the target: keep them out of the "created WITHOUT" list.
        generated = set(recovery.left_to_target)
        view = [
            e
            for e in le.merged_view(envs, recovery)
            if e.get("is_preview") or str(e.get("key")) not in generated
        ]
        unrecovered = dict(recovery.unrecoverable)
        lines = le.recovered_lines(
            recovery,
            path=path,
            source_server_hits=le.source_server_keys(
                recovery, wildcard=source_wildcard, ip=source_ip
            ),
        )
        promised = recovery.recovered.keys()
        del recovery

    compose_app = snapshot.kind is ResourceKind.APP_GIT_COMPOSE
    refusals = fidelity.env_refusals(
        view,
        collection=snapshot.collection,
        engine=snapshot.engine,
        version=version,
        compose_app=compose_app,
        unrecovered=unrecovered,
    )
    warnings = fidelity.env_warnings(
        view,
        collection=snapshot.collection,
        engine=snapshot.engine,
        compose=compose,
        version=version,
        compose_app=compose_app,
        unrecovered=unrecovered,
        builds=snapshot.collection == "applications" and snapshot.builds,
    )
    del view
    return EnvReview(
        warnings=(*warnings, *lines), refusals=tuple(refusals), recover_env_keys=promised
    )


_NOTHING_CHANGED = (
    "Nothing has been changed and the source is untouched: this check runs before "
    "CREATE_TARGET writes anything."
)


async def _recover_for_create_one(
    api: CoolifyClient,
    source_host: RemoteHost,
    snapshot: ResourceSnapshot,
    version: Version | None,
) -> le.LockedRecovery | None:
    envs = await api.get_envs(snapshot.collection, snapshot.uuid)
    if not _has_locked(envs):
        return None
    full = (
        await api.get_resource(snapshot.collection, snapshot.uuid) if _needs_full(snapshot) else {}
    )
    recovery, _ = await _recover(source_host, snapshot, envs, version=version, full=full)
    return recovery


async def _barrier(
    api: CoolifyClient,
    source_host: RemoteHost,
    snapshot: ResourceSnapshot,
    version: Version | None,
) -> le.LockedRecovery | None:
    """Re-raise anything unexpected as a value-free EnvRecoveryError.

    Raised AFTER the except block, so the original exception — whose message or
    arguments might carry a value — is neither the cause nor the context of
    what gets journaled and printed.
    """
    failure: str | None = None
    try:
        return await _recover_for_create_one(api, source_host, snapshot, version)
    except MigrationError:
        raise
    except Exception as exc:
        failure = type(exc).__name__
    raise EnvRecoveryError(
        f"{snapshot.name}: reading its locked variables back from the source server "
        f"failed ({failure})",
        hint=_NOTHING_CHANGED,
    )


async def recover_for_create(
    api: CoolifyClient,
    source_host: RemoteHost,
    resources: Sequence[ResourcePlan],
    *,
    version: Version | None = None,
) -> dict[str, le.Secrets]:
    """Every resource's recovered locked values, keyed by source uuid.

    Refuses — before CREATE's first write — when a key the plan promised can no
    longer be read, or when the target would regenerate an unrecoverable
    credential (the plan may predate a re-lock). Other unrecoverable keys are
    dropped exactly as the plan warned, and logged by name. ``version`` is the
    release the plan was made for; unknown applies every rule.
    """
    out: dict[str, le.Secrets] = {}
    for resource in resources:
        snapshot = resource.snapshot
        recovery = await _barrier(api, source_host, snapshot, version)
        if recovery is None:
            continue
        compose_app = snapshot.kind is ResourceKind.APP_GIT_COMPOSE
        broken = {
            key: reason
            for key, reason in recovery.unrecoverable.items()
            if key in resource.recover_env_keys
            or fidelity.target_regenerates(
                key, collection=snapshot.collection, engine=snapshot.engine, compose_app=compose_app
            )
        }
        if broken:
            listed = "; ".join(f"{k}: {r}" for k, r in sorted(broken.items()))
            raise EnvRecoveryError(
                f"{snapshot.name}: locked variable(s) {sorted(broken)} can no longer be read "
                f"back from the source server ({listed})",
                hint=(
                    "The plan promised to carry them, or the target would generate its own "
                    "value while the copied data keeps the source's. Fix the cause named "
                    "above (or unlock them on the source), then run again.\n" + _NOTHING_CHANGED
                ),
            )
        if dropped := sorted(set(recovery.unrecoverable) | set(recovery.previews)):
            log.warning("api.envs.locked_dropped", resource=snapshot.name, keys=dropped)
        if len(recovery.recovered):
            log.info(
                "api.envs.locked_recovered",
                resource=snapshot.name,
                keys=list(recovery.recovered.keys()),
            )
            out[snapshot.uuid] = recovery.recovered
    return out
