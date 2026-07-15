"""Drift detection: what the target will be that the source is not.

PURE module: no IO.

**The design rule: we build the target exactly as the source is configured, then
report what could still differ and let the operator decide.**

This is deliberately advisory rather than obstructive. New image versions and
moved branches are *normal*; whether they are compatible is a judgement about a
specific stack, and the operator has context we do not. So drift produces
warnings and a question, never a refusal. The one thing we owe them is that the
question is *concrete* — "may pull a newer image" is not actionable, "may cross a
major version and refuse to start on the copied data" is.

Four axes:

* ``IMAGE`` — a floating tag (``latest``, ``16``). The target pulls the same tag
  the source uses, which may now resolve to a different image. For a database
  crossing a major, the byte-exactly copied data directory can be unreadable by
  the newer engine.
* ``CODE`` — branch HEAD has moved since the running image was built.
  ``git_commit_sha`` cannot prevent this: ``check_git_if_build_needed()``
  resolves ``git ls-remote refs/heads/<branch>`` and overwrites the commit
  (``ApplicationDeploymentJob.php:2329-2349``), and the API never sets the
  ``rollback:`` flag that would bypass it. So it is reported, not prevented.
* ``TOPOLOGY`` — for ``build_pack=dockercompose`` the compose is re-read from
  git on every deploy, so the target may declare different volumes.
* ``BASE_IMAGE`` — Coolify forces ``docker build --pull``, so unpinned ``FROM``
  tags refresh on every build.

Note what is NOT here: a hard gate on topology. ``naming.pair_by_mount_path`` is
the real protection and it is strictly better — it pairs by mount path, so a
volume *renamed* in git still maps correctly (a fingerprint comparison would
have blocked that harmless case), while a volume genuinely added, removed or
re-pathed raises ``VolumePairingError`` at DISCOVER. The precise check lives
where the decision is made.

**Configuration drift** is a separate concern that shares the module: after
creating the target we diff it against the source and PATCH what is patchable.
What cannot be reconciled is reported rather than silently lost, which is the
whole bargain of the API-only constraint.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from bg_coolify_migrate.domain import images as image_mod

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
    IMAGE = "image"
    CODE = "code"
    TOPOLOGY = "topology"
    BASE_IMAGE = "base_image"


class Severity(StrEnum):
    OK = "ok"

    NOTICE = "notice"
    """Worth saying, not worth interrupting for. A patch-floating tag."""

    WARN = "warn"
    """The operator should decide before proceeding. Prompts interactively;
    needs ``--accept-drift`` when unattended."""

    BLOCK = "block"
    """Reserved. Nothing in this module produces it: drift is a judgement about
    the operator's stack, not ours to refuse. Real inconsistencies (an unpaired
    volume) are caught where they are detected, not here."""


class DriftFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    axis: DriftAxis
    severity: Severity
    summary: str
    detail: str = ""
    source_value: str | None = None
    target_value: str | None = None


class RebuildDriftReport(BaseModel):
    """What the target may end up running that the source is not."""

    model_config = ConfigDict(frozen=True)

    resource_name: str
    builds: bool
    findings: tuple[DriftFinding, ...] = ()

    @property
    def severity(self) -> Severity:
        for level in (Severity.BLOCK, Severity.WARN, Severity.NOTICE):
            if any(f.severity is level for f in self.findings):
                return level
        return Severity.OK

    @property
    def needs_decision(self) -> tuple[DriftFinding, ...]:
        """Findings the operator should adjudicate before we proceed.

        Not "blocking": we do not refuse. We ask. Unattended, ``--accept-drift``
        answers the question in advance.
        """
        return tuple(f for f in self.findings if f.severity in (Severity.BLOCK, Severity.WARN))

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.needs_decision)

    @property
    def notices(self) -> tuple[DriftFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.NOTICE)

    def summary_lines(self) -> list[str]:
        """One line per finding, for a report or a prompt."""
        return [f"{f.axis.value}: {f.summary}" for f in self.findings]


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


def assess_image_drift(
    *, resource_name: str, images: tuple[str, ...], is_database: bool = False
) -> tuple[DriftFinding, ...]:
    """Classify the image tags this resource will pull. PURE.

    We build the target with the SAME image reference the source uses. That is
    the correct thing to do — but a tag is a pointer, so "the same reference" can
    still mean "a different image".

    A moving tag on a database is the case worth stopping for: we copy the data
    directory byte-exactly, and a newer MAJOR engine may simply refuse to read
    it. Nothing is lost (the source is untouched), but the operator should know
    before, not from a healthcheck failure after.
    """
    findings: list[DriftFinding] = []

    for image in images:
        ref = image_mod.parse(image)
        note = image_mod.risk_note(ref, is_database=is_database)
        if note is None:
            continue

        moving = ref.stability is image_mod.TagStability.MOVING
        findings.append(
            DriftFinding(
                axis=DriftAxis.IMAGE,
                # A moving tag on a database can make the data unreadable; a patch
                # bump is routine. Do not spend the operator's attention equally.
                severity=Severity.WARN if (moving and is_database) else Severity.NOTICE,
                summary=(
                    f"{ref.raw} is a moving tag; the target may pull a newer major version"
                    if moving
                    else f"{ref.raw} may resolve to a newer image than the source runs"
                ),
                detail=note,
                source_value=ref.effective_tag,
                target_value=ref.effective_tag,
            )
        )

        if is_database and not image_mod.mount_path_is_guessable(image):
            # Coolify's own trap, not ours: the created hook regexes the tag to
            # choose the volume mount path and silently takes the pre-18 path
            # when it finds no number.
            findings.append(
                DriftFinding(
                    axis=DriftAxis.IMAGE,
                    severity=Severity.WARN,
                    summary=f"Coolify cannot read a version out of {ref.raw}",
                    detail=(
                        "Coolify picks a Postgres volume's mount path by regexing the tag for a "
                        "number, defaulting to the pre-18 path when it finds none. With this tag "
                        "it will guess — and guess wrong if the image is actually 18+. Pin the "
                        "tag to a version (e.g. postgres:16) to remove the guess."
                    ),
                )
            )

    return tuple(findings)


def assess_rebuild_drift(
    *,
    resource_name: str,
    builds: bool,
    running_commit: str | None = None,
    head_commit: str | None = None,
    running_topology: str | None = None,
    head_topology: str | None = None,
    unpinned_base_images: tuple[str, ...] = (),
    images: tuple[str, ...] = (),
    is_database: bool = False,
) -> RebuildDriftReport:
    """Assess what the target may run that the source does not.

    Advisory throughout. Nothing here refuses a migration: whether a newer image
    or a newer commit is compatible is a judgement about the operator's stack.
    We make the question concrete and let them answer it.

    Args:
        resource_name: For the report.
        builds: Whether this resource actually builds — from
            ``kinds.always_builds`` OR ``compose.builds_from_source``. A resource
            that does not build cannot drift on code, but its IMAGES still can.
        running_commit: The commit of the image the source is ACTUALLY running,
            read from the container's image tag ``{uuid}:{sha}``. The only
            trustworthy source: ``git_commit_sha`` is not updated by a normal
            deploy, and ``SOURCE_COMMIT`` is user-overridable and falls back to
            the literal string ``'unknown'``.
        head_commit: What ``git ls-remote refs/heads/<branch>`` returns now —
            i.e. what the target WILL build.
        running_topology: ``compose.topology_fingerprint`` of the compose in use.
        head_topology: ``compose.topology_fingerprint`` of the compose at HEAD.
        unpinned_base_images: ``FROM`` references without a digest.
        images: Image references this resource will pull.
        is_database: Raises the stakes of a moving tag — data directories are not
            compatible across engine majors.
    """
    findings: list[DriftFinding] = list(
        assess_image_drift(resource_name=resource_name, images=images, is_database=is_database)
    )

    if not builds:
        return RebuildDriftReport(
            resource_name=resource_name, builds=False, findings=tuple(findings)
        )

    # Axis: code.
    #
    # THREE states, not two: both known and equal (silent), both known and
    # different (ask), one unknown (ask, for a different reason). Flattening the
    # first two into a single condition makes the CLEAN case fall through to the
    # "cannot compare" branch.
    if running_commit and head_commit:
        if running_commit != head_commit:
            findings.append(
                DriftFinding(
                    axis=DriftAxis.CODE,
                    severity=Severity.WARN,
                    summary="branch HEAD has moved since the running image was built",
                    detail=(
                        "The target rebuilds from HEAD, not from the commit currently running — "
                        "Coolify's git_commit_sha cannot pin a deploy, because the job resolves "
                        "`git ls-remote` and overwrites it. Your data is copied byte-exactly and "
                        "would then run under this newer code. Usually fine; worth a thought if "
                        "the delta contains schema migrations."
                    ),
                    source_value=running_commit,
                    target_value=head_commit,
                )
            )
    elif running_commit or head_commit:
        findings.append(
            DriftFinding(
                axis=DriftAxis.CODE,
                severity=Severity.WARN,
                summary="cannot compare the running commit against branch HEAD",
                detail=(
                    "One side is unknown, so we cannot tell you whether the target would build "
                    "the code that is running. Proceeding is reasonable; we just cannot say it "
                    "is identical."
                ),
                source_value=running_commit,
                target_value=head_commit,
            )
        )

    # Axis: topology. Advisory only — pair_by_mount_path is the real check, and a
    # more precise one: it maps a renamed volume correctly and raises on one that
    # is genuinely added, removed or re-pathed.
    if running_topology and head_topology and running_topology != head_topology:
        findings.append(
            DriftFinding(
                axis=DriftAxis.TOPOLOGY,
                severity=Severity.WARN,
                summary="the compose in git differs from the one the stack is running",
                detail=(
                    "build_pack=dockercompose re-reads the compose from git on every deploy, so "
                    "the target may declare different services or volumes. A renamed volume is "
                    "handled correctly (we pair by mount path), but one that was added, removed "
                    "or re-pathed will stop the migration at DISCOVER rather than guess."
                ),
                source_value=running_topology[:12],
                target_value=head_topology[:12],
            )
        )

    # Axis: base images.
    if unpinned_base_images:
        findings.append(
            DriftFinding(
                axis=DriftAxis.BASE_IMAGE,
                severity=Severity.NOTICE,
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
