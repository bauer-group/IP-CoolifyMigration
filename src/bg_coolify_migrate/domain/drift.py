"""Drift detection: rebuild drift, and configuration drift.

PURE module: no IO.

Two different problems share this module because both are "the target will not be
what the source is":

**1. Rebuild drift** — Coolify rebuilds a git-backed application on the target,
and a rebuild is not a migration. ``git_commit_sha`` cannot prevent this:
``check_git_if_build_needed()`` resolves ``git ls-remote refs/heads/<branch>``
and overwrites the commit (``ApplicationDeploymentJob.php:2329-2349``), and the
API never sets the ``rollback:`` flag that would bypass it. So drift must be
*detected and gated*, never assumed away. Three axes:

* ``CODE`` — branch HEAD has moved since the running image was built. Blocks:
  byte-exact data would land under different code, and if that code has already
  applied migrations to the data you have a genuine corruption scenario.
* ``TOPOLOGY`` — for ``build_pack=dockercompose`` the compose is re-read from
  git on every deploy. A renamed/added/removed volume means the old->new mapping
  computed from the source is quietly wrong. **Blocks** — this is data loss.
* ``BASE_IMAGE`` — Coolify forces ``docker build --pull``, so unpinned ``FROM``
  tags refresh on every build. Unfixable from here, so it only *warns*; the
  honest statement is that a rebuild is never byte-identical.

**2. Configuration drift** — after creating the target we diff it against the
source and PATCH what is patchable. What cannot be reconciled is reported rather
than silently lost, which is the whole bargain of the API-only constraint.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

#: Fields that MUST differ between source and target and are therefore excluded
#: from configuration drift. Diffing them would drown the real signal in noise.
EXPECTED_DIFFERENCES: frozenset[str] = frozenset(
    {
        "id",
        "uuid",
        "name",
        "created_at",
        "updated_at",
        "started_at",
        "status",
        "config_hash",
        "server_id",
        "server_uuid",
        "server_status",
        "destination_id",
        "destination_uuid",
        "destination_type",
        "environment_id",
        "environment_uuid",
        "project_id",
        "project_uuid",
        # Domains are deliberately re-pointed or released; the DNS gate owns them.
        "fqdn",
        "domains",
        "docker_compose_domains",
        # Regenerated per resource by Coolify.
        "manual_webhook_secret_github",
        "manual_webhook_secret_gitlab",
        "manual_webhook_secret_bitbucket",
        "manual_webhook_secret_gitea",
    }
)

#: ApplicationSetting fields that are settable via POST/PATCH but NOT readable
#: (GET /v1/applications/{uuid} does not eager-load the `settings` relation).
#: We cannot diff them, so they are surfaced as known-unknowns.
UNREADABLE_SETTINGS: frozenset[str] = frozenset(
    {
        "is_static",
        "is_spa",
        "is_auto_deploy_enabled",
        "is_force_https_enabled",
        "connect_to_docker_network",
        "is_build_server_enabled",
        "is_container_label_escape_enabled",
        "is_preserve_repository_enabled",
        "is_git_submodules_enabled",
        "is_git_lfs_enabled",
        "is_debug_enabled",
        "is_preview_deployments_enabled",
        "is_log_drain_enabled",
        "is_gpu_enabled",
        "is_include_timestamps",
        "is_consistent_container_name_enabled",
        "is_gzip_enabled",
        "is_stripprefix_enabled",
        "custom_internal_name",
        "is_env_sorting_enabled",
        "is_container_label_readonly_enabled",
        "disable_build_cache",
        "use_build_secrets",
        "inject_build_args_to_dockerfile",
        "include_source_commit_in_build",
        "docker_images_to_keep",
        "stop_grace_period",
        "is_raw_compose_deployment_enabled",
        "is_swarm_only_worker_nodes",
        "gpu_driver",
        "gpu_count",
        "gpu_device_ids",
        "gpu_options",
    }
)

#: The subset of UNREADABLE_SETTINGS that can be recovered by observing the
#: running container instead of asking the API — Traefik labels, network
#: membership and the container name are all facts Docker will tell us.
#: This fits the application-unaware "docker is the truth" philosophy.
RECOVERABLE_FROM_DOCKER: frozenset[str] = frozenset(
    {
        "is_force_https_enabled",
        "is_gzip_enabled",
        "is_stripprefix_enabled",
        "connect_to_docker_network",
        "is_consistent_container_name_enabled",
        "custom_internal_name",
    }
)


class DriftAxis(StrEnum):
    CODE = "code"
    TOPOLOGY = "topology"
    BASE_IMAGE = "base_image"


class Severity(StrEnum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"


class DriftFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    axis: DriftAxis
    severity: Severity
    summary: str
    detail: str = ""
    source_value: str | None = None
    target_value: str | None = None


class RebuildDriftReport(BaseModel):
    """Whether migrating this resource would ship different code than it runs."""

    model_config = ConfigDict(frozen=True)

    resource_name: str
    builds: bool
    findings: tuple[DriftFinding, ...] = ()

    @property
    def severity(self) -> Severity:
        if any(f.severity is Severity.BLOCK for f in self.findings):
            return Severity.BLOCK
        if any(f.severity is Severity.WARN for f in self.findings):
            return Severity.WARN
        return Severity.OK

    @property
    def is_blocked(self) -> bool:
        return self.severity is Severity.BLOCK

    @property
    def blocking(self) -> tuple[DriftFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCK)


class ConfigDriftReport(BaseModel):
    """What differs between the source and the freshly-created target."""

    model_config = ConfigDict(frozen=True)

    resource_name: str
    reconciled: tuple[str, ...] = ()
    """Fields that differed and were successfully PATCHed."""
    unreconciled: tuple[DriftFinding, ...] = ()
    """Fields that differ and could not be fixed. Reported, never hidden."""
    unknown: tuple[str, ...] = ()
    """Fields we cannot even read on the source (the settings gap)."""

    @property
    def is_clean(self) -> bool:
        return not self.unreconciled and not self.unknown


def assess_rebuild_drift(
    *,
    resource_name: str,
    builds: bool,
    running_commit: str | None = None,
    head_commit: str | None = None,
    running_topology: str | None = None,
    head_topology: str | None = None,
    unpinned_base_images: tuple[str, ...] = (),
) -> RebuildDriftReport:
    """Assess whether a rebuild on the target would ship something different.

    Args:
        resource_name: For the report.
        builds: Whether this resource actually builds — from
            ``kinds.always_builds`` OR ``compose.builds_from_source``. A resource
            that does not build cannot drift, so everything else is skipped.
        running_commit: The commit of the image the source is ACTUALLY running,
            read from the container's image tag ``{uuid}:{sha}``. This is the only
            trustworthy source: ``git_commit_sha`` is not updated by a normal
            deploy, and ``SOURCE_COMMIT`` is user-overridable and falls back to
            the literal string ``'unknown'``.
        head_commit: What ``git ls-remote refs/heads/<branch>`` returns now —
            i.e. what the target WILL build.
        running_topology: ``compose.topology_fingerprint`` of the compose in use.
        head_topology: ``compose.topology_fingerprint`` of the compose at HEAD.
        unpinned_base_images: ``FROM`` references without a digest.

    Returns:
        A report whose ``is_blocked`` decides whether the migration may proceed.
    """
    if not builds:
        return RebuildDriftReport(resource_name=resource_name, builds=False)

    findings: list[DriftFinding] = []

    # Axis 1: code.
    if running_commit and head_commit:
        if running_commit != head_commit:
            findings.append(
                DriftFinding(
                    axis=DriftAxis.CODE,
                    severity=Severity.BLOCK,
                    summary="branch HEAD has moved since the running image was built",
                    detail=(
                        "The target would rebuild from HEAD, not from the commit currently "
                        "running. Coolify's git_commit_sha cannot pin this: the deploy job "
                        "resolves `git ls-remote` and overwrites it. The mirrored data belongs "
                        "to the running commit."
                    ),
                    source_value=running_commit,
                    target_value=head_commit,
                )
            )
    elif running_commit or head_commit:
        findings.append(
            DriftFinding(
                axis=DriftAxis.CODE,
                severity=Severity.BLOCK,
                summary="cannot compare the running commit against branch HEAD",
                detail=(
                    "One side is unknown, so drift cannot be ruled out. Refusing rather than "
                    "assuming they match."
                ),
                source_value=running_commit,
                target_value=head_commit,
            )
        )

    # Axis 2: topology. The data-loss axis.
    if running_topology and head_topology and running_topology != head_topology:
        findings.append(
            DriftFinding(
                axis=DriftAxis.TOPOLOGY,
                severity=Severity.BLOCK,
                summary="the compose in git declares different volumes than the running stack",
                detail=(
                    "build_pack=dockercompose re-reads the compose from git on every deploy, so "
                    "the target would materialise a different set of volumes. The old->new "
                    "mapping computed from the source would be wrong and data would land nowhere."
                ),
                source_value=running_topology[:12],
                target_value=head_topology[:12],
            )
        )

    # Axis 3: base images. Cannot be fixed from here; be honest about it.
    if unpinned_base_images:
        findings.append(
            DriftFinding(
                axis=DriftAxis.BASE_IMAGE,
                severity=Severity.WARN,
                summary=f"{len(unpinned_base_images)} unpinned base image(s) will be re-pulled",
                detail=(
                    "Coolify forces `docker build --pull`, so floating FROM tags refresh on every "
                    "build. Even a same-commit rebuild is therefore not byte-identical. Pin the "
                    "FROM lines by digest if that matters: " + ", ".join(unpinned_base_images)
                ),
            )
        )

    return RebuildDriftReport(resource_name=resource_name, builds=True, findings=tuple(findings))


def normalise(config: dict[str, Any]) -> dict[str, Any]:
    """Strip fields that are expected to differ between source and target.

    Without this, every diff is dominated by uuids and timestamps and the real
    signal is invisible.
    """
    return {k: v for k, v in config.items() if k not in EXPECTED_DIFFERENCES}


def diff_config(
    *,
    resource_name: str,
    source: dict[str, Any],
    target: dict[str, Any],
    patchable: frozenset[str],
) -> ConfigDriftReport:
    """Diff a source resource against its freshly-created target.

    Args:
        resource_name: For the report.
        source: ``GET`` of the source resource.
        target: ``GET`` of the created target.
        patchable: Fields the target's PATCH endpoint accepts. Anything that
            differs and is NOT in here cannot be reconciled and must be reported.

    Returns:
        A report separating "we can fix this" from "we cannot, here it is".
    """
    src = normalise(source)
    tgt = normalise(target)

    reconciled: list[str] = []
    unreconciled: list[DriftFinding] = []

    for key in sorted(set(src) | set(tgt)):
        s_val = src.get(key)
        t_val = tgt.get(key)
        if s_val == t_val:
            continue
        if key in patchable:
            reconciled.append(key)
        else:
            unreconciled.append(
                DriftFinding(
                    axis=DriftAxis.CODE,
                    severity=Severity.WARN,
                    summary=f"{key} differs and the API cannot set it",
                    source_value=None if s_val is None else str(s_val)[:120],
                    target_value=None if t_val is None else str(t_val)[:120],
                )
            )

    # The settings gap: fields we cannot read on the source at all.
    unknown = tuple(sorted(UNREADABLE_SETTINGS - RECOVERABLE_FROM_DOCKER))

    return ConfigDriftReport(
        resource_name=resource_name,
        reconciled=tuple(reconciled),
        unreconciled=tuple(unreconciled),
        unknown=unknown,
    )
