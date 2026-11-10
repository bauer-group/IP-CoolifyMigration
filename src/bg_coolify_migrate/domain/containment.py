"""Did anything start a stack this run holds still? PURE.

Between QUIESCE and FINALIZE a run holds TWO stacks still, and nothing in
Coolify keeps them that way:

* the SOURCE, stopped since QUIESCE. A git push to its branch with auto-deploy
  on (Application::isDeployable checks that flag alone, v4.7.2:1506), a pull
  request with previews on, a CI job calling ``POST /deploy?uuid`` or ``?tag``
  (DeployController checks neither flag nor status, v4.7.2:488-630), a deploy
  from the dashboard, or a deployment that was queued or building when the stop
  ran (StopApplication cancels none, v4.7.2:17-101) brings it back. It then
  writes to volumes the copy has already read, and routes the custom hosts it
  still holds — so everything users write there after VERIFY never reaches the
  target, silently.
* the TARGET, created and never started until START asks. It shares the
  source's repository (a GitHub App's repository id, a manual webhook's secrets)
  and its tags, so the same pushes and tag deploys start it early: it then serves
  custom hosts that still resolve to the source and asks ACME for them
  (invariant 7), and writes into volumes being copied.

So each checkpoint asks the daemons, and this module decides from what they
said. Three kinds of evidence, because Coolify's stop REMOVES containers
(``docker rm -f``) and a failed deploy removes the one it started
(ApplicationDeploymentJob v4.7.2:5601-5603):

* a container that is not stopped now;
* a stopped container whose ``State.StartedAt`` is at or after the stop —
  durable for as long as the container exists;
* a ``start`` in the daemon's event log, for a container that came and went.
  The daemon keeps only its last 256 events in memory (moby v27.5.1 and
  v28.3.3 daemon/events/events.go:12,26,113-118), so on a busy server this
  window may no longer reach back to the stop: :func:`event_log_gap` says when.

Times are the host's own clock throughout — ``date +%s`` read on the source
once the stop gate passed, ``StartedAt`` and event times written by that
daemon — so a skewed workstation cannot narrow the window.

One reader is no revival: Coolify's scheduled VOLUME backup (4.3.0+) runs
``docker run --rm --name volume-backup-<execution uuid> -v <volume>:/volume:ro``
whatever the resource's status — its scheduler has no 'running' skip, unlike the
task and database-backup schedulers (VolumeBackupJob v4.3.0:78-87,
v4.5.0:94-112, v4.7.2:101-123; ScheduledJobManager v4.3.0:382-430,
v4.7.2:531-600). It carries no Coolify label and no resource uuid, so only the
migrated-volume probe finds it, and it mounts the volume read-only. A container
found ONLY that way counts when it mounts a migrated volume writable
(:attr:`Seen.writes`); one that only reads cannot make the copy stale. Its
``stop_during_backup`` mode stops and restarts RUNNING containers only
(``docker ps -q``, v4.7.2:383-396,401-419), so it never starts a pin.

The SOURCE is judged from the moment the stop gate proved it down
(:func:`judged_from`); the TARGET from its creation: its uuid
did not exist before CREATE, so any start of a container carrying it, at any
time, is a start nobody here asked for. Containers found only by the target's
NAME labels count only while they run, because the names are not unique the
way a uuid is.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from bg_coolify_migrate.domain.deployments import IN_PROGRESS, QUEUED, Deployment

#: QUIESCE journals the SOURCE host's clock (unix seconds, ``date +%s``), read just
#: before it asks Coolify to stop anything, beside ``statemachine.STOP_REQUESTED``.
#: The window of ``quiesce.killed_since``; revivals are judged from it only when
#: :data:`STOPPED_CLOCK` is missing. A journal without either gets the state
#: check alone.
STOP_CLOCK = "stop_clock"

#: QUIESCE journals the source's clock again once its stop gate saw every
#: container of the stack stopped (``quiesce.wait_until_stopped``) and no data
#: holder was SIGKILLed. Revivals are judged from HERE, not from
#: :data:`STOP_CLOCK`: Coolify's stop is no single act. StopApplication stops a
#: compose application's containers one at a time, one SSH call and one grace
#: period each (StopApplication v4.1.2:38-42, v4.7.2:47-58), QUIESCE stops the plan's
#: resources one after another, and every Coolify container runs
#: ``restart: unless-stopped`` (constants.php v4.1.2:37, v4.7.2:62; parsers.php
#: v4.7.2:1508,2918). A container that exits on its own while it waits its turn
#: (its database went first, a periodic job) is restarted by Docker, then
#: stopped and removed by Coolify: a ``start`` after the stop request, in a
#: stack the gate then saw end stopped. Judged from the request, that read as a
#: revival at the end of COPY and rolled back a healthy migration, on every
#: attempt. Whatever ran before the gate passed wrote before the copy read.
STOPPED_CLOCK = "stopped_clock"

#: Journalled when a checkpoint after START finds the source revived and BLOCKS
#: (``errors.SourceRevivedAfterStart``): the source's clock then. The operator
#: was shown what started it, and a resume judges from this instant, so a source
#: they stopped again passes, and anything started after the block still counts.
REVIVAL_SEEN_CLOCK = "revival_seen_clock"

#: COPY journals ``{source volume: CreatedAt}`` for every source volume it copied,
#: once the end-of-copy checkpoint passed. A rollback after FINALIZE(keep/rename)
#: compares it (``compensations.source_data_refusal``): a volume pruned and
#: recreated EMPTY by a redeploy keeps its name, never its CreatedAt.
SOURCE_VOLUMES_CREATED = "source_volumes_created"

#: Container states that cannot write. ``gone`` is the discovery layer's state
#: for a container removed between ``docker ps`` and ``docker inspect``.
_STOPPED = frozenset({"exited", "created", "dead", "gone"})

#: Docker's zero time: what ``StartedAt`` reads for a container never started.
_NEVER = 1


@dataclass(frozen=True, slots=True)
class Watched:
    """One migrated resource as a checkpoint looks for it on one host."""

    name: str
    """The SOURCE resource's name — what the operator knows it by."""
    uuid: str
    """Its uuid on that host: the source's on the source, the target's on the target."""
    labels: Mapping[str, str]
    """Its project / environment / resource-name label triple on that host."""


@dataclass(frozen=True, slots=True)
class Seen:
    """One container a checkpoint found, as the daemon described it."""

    name: str
    state: str
    labels: Mapping[str, str]
    image: str = ""
    started_at: int | None = None
    """When it last started, on that host's clock; ``None`` if it never did or
    the daemon did not say (running containers are not inspected)."""
    writes: bool | None = None
    """Asked only of a container found by the migrated-volume probe alone
    (:func:`volume_only`): does it mount one of those volumes writable
    (:func:`mounts_writable`)? ``None`` when that could not be told — it then
    counts as a writer, like every container found any other way."""


@dataclass(frozen=True, slots=True)
class StartEvent:
    """One container ``start`` the daemon's event log still holds."""

    name: str
    time: int
    attributes: Mapping[str, str]
    """The container's labels, plus ``name`` and ``image`` (docker events)."""


@dataclass(frozen=True, slots=True)
class EventLog:
    starts: tuple[StartEvent, ...] = ()
    oldest: int | None = None
    """The oldest event of ANY kind the daemon still holds; ``None`` if it holds none."""


@dataclass(frozen=True, slots=True)
class Breach:
    resource: str
    container: str
    why: str
    uuid: str = ""
    """The watched uuid it belongs to on that host; empty for "the stack"."""

    def describe(self) -> str:
        return f"{self.resource}: {self.container} {self.why}"


# ── parsing what the daemon printed ─────────────────────────────────────────

_FRACTION = re.compile(r"\.\d+")


def parse_docker_time(raw: object) -> int | None:
    """A docker RFC 3339 timestamp as unix seconds; ``None`` for zero or junk.

    ``State.StartedAt`` is RFC 3339 with nanoseconds in UTC, and Docker's zero
    time ``0001-01-01T00:00:00Z`` for a container that never started. A volume's
    ``CreatedAt`` carries the daemon's own offset. The fraction is dropped before
    parsing: what Python accepts there differs between releases, and a second is
    all any comparison here needs.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _FRACTION.sub("", raw.strip(), count=1).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year <= _NEVER:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def started_at_of(state_json: str) -> int | None:
    """``StartedAt`` from ``docker inspect --format '{{json .State}}'``."""
    try:
        state = json.loads(state_json.strip() or "{}")
    except json.JSONDecodeError:
        return None
    return parse_docker_time(state.get("StartedAt")) if isinstance(state, dict) else None


def parse_event_log(stdout: str) -> EventLog:
    """What :func:`quiesce.start_events` printed: an ``oldest <unix>`` line, then
    one ``{{json .}}`` line per container ``start``. Anything else is skipped."""
    oldest: int | None = None
    starts: list[StartEvent] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("oldest "):
            value = line.removeprefix("oldest ").strip()
            oldest = int(value) if value.isdigit() else oldest
            continue
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("Type") != "container" or (event.get("Action") or event.get("status")) != (
            "start"
        ):
            continue
        actor = event.get("Actor")
        raw = actor.get("Attributes") if isinstance(actor, dict) else None
        attributes = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        when = event.get("time")
        if not isinstance(when, int):
            continue
        starts.append(StartEvent(name=attributes.get("name", ""), time=when, attributes=attributes))
    return EventLog(starts=tuple(starts), oldest=oldest)


def mounts_writable(mounts_json: str, volumes: Collection[str]) -> bool | None:
    """Does a container mount one of ``volumes`` writable? PURE.

    From ``docker inspect --format '{{json .Mounts}}'``: each entry's ``Name``
    is the volume's (empty for a bind) and ``RW`` false for a ``:ro`` mount.
    An entry without ``RW`` is Docker's default, read-write. ``None`` when it
    cannot be told — output that does not parse, or none of ``volumes`` among
    the mounts — and the caller counts that as a writer.
    """
    try:
        entries = json.loads(mounts_json.strip() or "null")
    except json.JSONDecodeError:
        return None
    if not isinstance(entries, list):
        return None
    found = False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("Name") not in volumes:
            continue
        if entry.get("RW") is not False:
            return True
        found = True
    return False if found else None


def parse_volume_created(stdout: str) -> dict[str, str]:
    """``docker volume inspect --format '{{.Name}} {{.CreatedAt}}'`` -> ``{name: CreatedAt}``."""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        name, _, created = line.strip().partition(" ")
        if name and created.strip():
            out[name] = created.strip()
    return out


# ── deciding ────────────────────────────────────────────────────────────────


def _clock_of(detail: Mapping[str, object], key: str) -> int | None:
    raw = detail.get(key)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def judged_from(quiesce_detail: Mapping[str, object]) -> int | None:
    """From when a source start counts as a revival, given QUIESCE's merged journal detail.

    The later of :data:`STOPPED_CLOCK` (the stop gate's proof) and
    :data:`REVIVAL_SEEN_CLOCK` (a block the operator has been shown). Without
    the gate's clock, the stop request's (:data:`STOP_CLOCK`): stricter, never
    looser. ``None`` when the journal holds none of them.
    """
    proven = _clock_of(quiesce_detail, STOPPED_CLOCK)
    base = proven if proven is not None else _clock_of(quiesce_detail, STOP_CLOCK)
    seen = _clock_of(quiesce_detail, REVIVAL_SEEN_CLOCK)
    if base is None or seen is None:
        return base if base is not None else seen
    return max(base, seen)


def carries(name: str, image: str, labels: Mapping[str, str], uuid: str) -> bool:
    """Does a container advertise ``uuid`` where a rename cannot reach it?

    The same test as ``discovery.docker.carries_uuid``: its name, its image, or a
    label VALUE (``coolify.name``, ``com.docker.compose.project``).
    """
    return bool(uuid) and (
        uuid in name or uuid in image or any(uuid in value for value in labels.values())
    )


def matches(labels: Mapping[str, str], wanted: Mapping[str, str]) -> bool:
    """Do ``labels`` carry every ``wanted`` label? An empty filter matches nothing."""
    return bool(wanted) and all(labels.get(key) == value for key, value in wanted.items())


def _owner(watched: Sequence[Watched], name: str, image: str, labels: Mapping[str, str]) -> (
    Watched | None
):
    by_uuid = next((w for w in watched if carries(name, image, labels, w.uuid)), None)
    return by_uuid or next((w for w in watched if matches(labels, w.labels)), None)


def volume_only(
    watched: Sequence[Watched],
    stack: Mapping[str, str],
    *,
    name: str,
    image: str,
    labels: Mapping[str, str],
) -> bool:
    """Is this a container only the migrated-volume probe can have found? PURE.

    Neither the plan's labels (``stack``) nor a watched uuid: what the label
    filter and the uuid probe find always carries one of them. Coolify's volume
    backup helper is such a container (module docstring); so is a debug shell
    someone ran on the volume.
    """
    return _owner(watched, name, image, labels) is None and not matches(labels, stack)


def _clock(when: int) -> str:
    return datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def source_breaches(
    watched: Sequence[Watched],
    seen: Iterable[Seen],
    events: EventLog | None,
    *,
    since: int | None,
    stack: Mapping[str, str],
) -> list[Breach]:
    """Every sign that the SOURCE ran after QUIESCE asked it to stop.

    ``seen`` comes from the plan's label filter (``stack``), the uuid probe and
    the migrated-volume probe, pins already left out. ``since`` is the source's
    clock :func:`judged_from` gives — once the stop gate proved the stack down,
    or a block after START since; without it (a journal older than the record)
    only a container running NOW counts.

    A container only the volume probe found (:func:`volume_only`) that mounts
    those volumes read-only (``writes`` False) is left out: Coolify's volume
    backup reads them on its schedule whatever the source's status, and a
    reader cannot make the copy stale. Every other container counts, writer or not.
    """
    breaches: list[Breach] = []
    reported: set[str] = set()
    for container in seen:
        owner = _owner(watched, container.name, container.image, container.labels)
        if owner is None and container.writes is False and not matches(container.labels, stack):
            continue
        resource = owner.name if owner else "the stack"
        if container.state not in _STOPPED:
            breaches.append(Breach(resource, container.name, f"is {container.state}"))
        elif since is not None and container.started_at is not None and (
            container.started_at >= since
        ):
            breaches.append(
                Breach(
                    resource,
                    container.name,
                    f"was started at {_clock(container.started_at)}, after the stop, "
                    "and has stopped again",
                )
            )
        else:
            continue
        reported.add(container.name)
    if events is None or since is None:
        return breaches
    for event in events.starts:
        if event.time < since or event.name in reported:
            continue
        owner = _owner(watched, event.name, event.attributes.get("image", ""), event.attributes)
        if owner is None and not matches(event.attributes, stack):
            continue
        breaches.append(
            Breach(
                owner.name if owner else "the stack",
                event.name or "a container",
                f"was started at {_clock(event.time)}, after the stop "
                "(the daemon's event log; the container is gone)",
            )
        )
        reported.add(event.name)
    return breaches


def target_breaches(
    watched: Sequence[Watched], seen: Iterable[Seen], events: EventLog | None
) -> list[Breach]:
    """Every sign that a TARGET ran before START asked it to.

    ``watched`` holds only the targets START has not asked for yet. Any start of
    a container carrying one's uuid counts, whenever it was: that uuid did not
    exist before CREATE. A container found only by the name labels counts while
    it runs.
    """
    breaches: list[Breach] = []
    reported: set[str] = set()
    for container in seen:
        owner = _owner(watched, container.name, container.image, container.labels)
        if owner is None:
            continue
        if container.state not in _STOPPED:
            why = f"is {container.state}"
        elif container.started_at is not None and carries(
            container.name, container.image, container.labels, owner.uuid
        ):
            why = f"was started at {_clock(container.started_at)} and has stopped again"
        else:
            continue
        breaches.append(Breach(owner.name, container.name, why, owner.uuid))
        reported.add(container.name)
    for event in (events.starts if events else ()):
        if event.name in reported:
            continue
        image = event.attributes.get("image", "")
        owner = next(
            (w for w in watched if carries(event.name, image, event.attributes, w.uuid)), None
        )
        if owner is None:
            continue
        breaches.append(
            Breach(
                owner.name,
                event.name or "a container",
                f"was started at {_clock(event.time)} "
                "(the daemon's event log; the container is gone)",
                owner.uuid,
            )
        )
        reported.add(event.name)
    return breaches


def event_log_gap(events: EventLog | None, since: int | None) -> int | None:
    """The time from which the event log is complete, when that is after ``since``.

    The daemon drops its oldest event once it holds 256. If the oldest one left
    is older than ``since``, nothing after ``since`` was dropped. If it is newer,
    a container that started and was removed again before it cannot be ruled
    out — one that still exists, running or not, is found all the same.
    """
    if events is None or since is None or events.oldest is None:
        return None
    return events.oldest if events.oldest > since else None


def unsettled(deployments: Iterable[Deployment]) -> list[Deployment]:
    """The deployments a stop cannot hold back: queued, in progress, or unread.

    Queued and in progress are Coolify's own cancellable set
    (DeployController::cancel_deployment v4.1.2:233-236, v4.7.2:251-254); an
    empty status is a row that could not be read, which proves nothing ended.
    StopApplication runs on the 'high' queue and cancels no deployment
    (v4.7.2:17), so one of these that lands after a rollback's ``/stop`` brings
    the target up again — serving the hosts it claimed beside the restarted
    source — and DeleteResourceJob cancels deployments of a preview only
    (v4.7.2:234-278), so its container can outlive the delete. A status this
    tool does not know cannot be cancelled either, and does not count.
    """
    return [d for d in deployments if d.status in (QUEUED, IN_PROGRESS, "")]


def volume_recreated(recorded: str, current: str) -> bool:
    """Is this not the volume COPY read? Compared as instants, so a daemon whose
    time zone changed is not mistaken for a new volume.

    A local volume's CreatedAt is the change time of its directory
    (``/var/lib/docker/volumes/<name>``, moby v27.5.1 volume/local/local.go:229-236),
    so a volume removed and created again under the same name — what a prune
    followed by a redeploy does — reads a new one.
    """
    before, now = parse_docker_time(recorded), parse_docker_time(current)
    if before is None or now is None:
        return recorded.strip() != current.strip()
    return before != now


# ── what the operator reads ─────────────────────────────────────────────────

_SOURCE_CAUSES = (
    "Something started the source after QUIESCE stopped it. The usual causes: a git "
    "push to its branch with auto-deploy on (or a pull request, with preview "
    "deployments on); a CI job calling Coolify's /deploy with its uuid or one of its "
    "tags; a deploy or restart from the dashboard, the API or MCP; or a deployment "
    "that was already queued or building when the stop ran - Coolify's stop cancels "
    "none.\n"
    "Whatever it wrote since then is on the source only, and the copy no longer "
    "matches it. "
)


def source_hint() -> str:
    """What happened, why it matters and what the rollback does next — before START.

    Only before START asked for a target: no user has written to the target yet,
    so rolling back costs nothing the source does not hold.
    """
    return (
        f"{_SOURCE_CAUSES}The run rolls back now: the target is stopped and deleted "
        "with its copied volumes, and the source restarted. Nothing deletes the source "
        "before FINALIZE, so what it wrote survives. Hold pushes and CI deploys of it, "
        "then migrate again."
    )


_FINALIZE_FATE = {
    "delete": "deletes the source, and those writes with it",
    "rename": "renames the source and leaves it stopped, with those writes on it",
    "keep": "leaves the source stopped, with those writes on it",
}


def revived_after_start_hint(*, policy: str, migration_id: str) -> str:
    """A revival found once START asked for a target: the two ways on, neither taken.

    No rollback runs by itself here. The target may have served users since
    START — the DNS gate let it start — and what they wrote is on the target
    only, while the rollback stops it and deletes it with its volumes
    (DROP_TARGET_VOLUMES, DELETE_TARGET_RESOURCE). Whose writes to give up, the
    revived source's or the target's, is the operator's call, so the run BLOCKS
    like the DNS gate: resumable, nothing undone (``errors.SourceRevivedAfterStart``).
    """
    fate = _FINALIZE_FATE.get(policy, "applies the finalize policy to the source")
    return (
        f"{_SOURCE_CAUSES}START has already asked Coolify to start the target, and the "
        "DNS gate let it: it may have served users since then, and what they wrote is "
        "on the target only. A rollback would delete that with the target, so this run "
        "does not roll back by itself. Nothing has been undone, and FINALIZE has not "
        "touched the source.\n"
        "To finish: stop the source in Coolify and hold pushes and CI deploys of it, "
        f"then coolify-migrate resume {migration_id} - FINALIZE then {fate}. The resume "
        "refuses again while a source container runs, or if one starts after this "
        "check.\n"
        f"To give up the target instead: coolify-migrate rollback {migration_id} - it "
        "deletes the target and everything written to it since START, and restarts "
        "the source."
    )


TARGET_HINT = (
    "Something started the target before START asked for it. The usual causes: a git "
    "push to its branch (a target is created with its source's repository, so a "
    "GitHub App or a manual webhook delivers the same push to both), a CI job calling "
    "Coolify's /deploy with its uuid or a tag it carries (CREATE copies the source's "
    "tags), or a deploy from the dashboard, the API or MCP.\n"
    "A target running before the DNS gate serves its custom hosts while they still "
    "resolve to the source and asks ACME for their certificates, and it may have "
    "written to the volumes being copied. The run rolls back now: the target is "
    "stopped and deleted with its volumes, and the source restarted if it was "
    "stopped. Hold pushes and CI deploys of the stack, then migrate again."
)


def breach_message(subject: str, checkpoint: str, breaches: Sequence[Breach]) -> str:
    """One line per breach, under a heading naming the checkpoint that found them."""
    return f"{subject} (found at {checkpoint}):\n" + "\n".join(
        f"  {b.describe()}" for b in breaches
    )
