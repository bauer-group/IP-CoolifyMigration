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

So each checkpoint asks the daemons and Coolify, and this module decides from
what they said. Four kinds of evidence, because Coolify's stop REMOVES
containers (``docker rm -f``) and a failed deploy removes the one it started
(ApplicationDeploymentJob v4.7.2:5601-5603):

* a container that is not stopped now;
* a stopped container whose ``State.StartedAt`` is at or after the stop —
  durable for as long as the container exists;
* a ``start`` in the daemon's event log, for a container that came and went.
  The daemon keeps only its last 256 events in memory (moby v27.5.1 and
  v28.3.3 daemon/events/events.go:12,26,113-118), and every Docker health
  probe logs three (exec_create, exec_start, exec_die): on a host whose other
  stacks probe every few seconds this window rarely reaches back to the stop,
  and a daemon restarted since holds none at all. :func:`event_log_gap` says when;
* for an APPLICATION, a row in its deployment history
  (``GET /deployments/applications/{uuid}``) that the stop did not see ended
  (:func:`deployment_breaches`). Durable where the event log is not: Coolify
  keeps the rows for 60 days (CleanupDatabase v4.1.2:23-26,52-57,
  v4.7.2:29-31,47) and a failed deploy leaves its row. Compared by uuid and
  by Coolify's own order, never by a time, so no clock is involved at all.
  QUIESCE waits out every source deployment before the stop
  (``engine/deploy_triggers.await_source_deployments``), so a row this tool
  did not see ended is never its own.

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

#: QUIESCE journals, with :data:`STOP_CLOCK`, the deployments every source
#: APPLICATION had ENDED by then: ``{source app uuid: [deployment uuid, ...]}``
#: (:func:`ended_deployments`). Read right after the wait for its deployments
#: to end, before the mount capture and the pins; one still queued or running
#: then refuses the stop (``engine/containment.deployments_before_stop``). A row the record
#: does not hold is a deployment of the stopped source (:func:`deployment_breaches`).
#: A journal without the record (from before it) gets no deployment check.
SOURCE_DEPLOYMENTS = "source_deployments"

#: Journalled with :data:`REVIVAL_SEEN_CLOCK` at a block after START: every
#: source deployment that check read and that had ENDED, added to an earlier
#: block's (:func:`block_record`). The operator was shown them, so a resume does
#: not block on them again; one queued after the block still counts.
REVIVAL_SEEN_DEPLOYMENTS = "revival_seen_deployments"

#: Journalled beside :data:`REVIVAL_SEEN_DEPLOYMENTS`: the deployments a block
#: after START showed while they were still queued, running or unread. Never
#: accounted, for the reason the stop's record holds ended rows only: one that
#: had not ended at the block runs after it. Each counts while it is pending, and
#: ONCE more after it ended, whatever row sorts above it — a newer row the block
#: saw ended (:func:`deployment_breaches`); the next block then accounts for it.
REVIVAL_PENDING_DEPLOYMENTS = "revival_pending_deployments"

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
    read_at: int | None = None
    """The host's clock when the log was read (the ``--until`` of both queries);
    ``None`` when the output did not say."""


@dataclass(frozen=True, slots=True)
class Breach:
    resource: str
    container: str
    why: str
    uuid: str = ""
    """The watched uuid it belongs to on that host; empty for "the stack"."""

    def describe(self) -> str:
        return f"{self.resource}: {self.container} {self.why}"


@dataclass(frozen=True, slots=True)
class Unanswered:
    """One read a check sent a server's Docker daemon that got no answer.

    Not a breach: a check that cannot say there was none. It never passes —
    before START the run rolls back, after START it blocks
    (``engine/containment.assert_contained``).
    """

    side: str
    """``source`` or ``target``."""
    server: str
    what: str
    """What was asked, e.g. ``its docker event log``."""
    why: str
    """What the failed read said, one line."""

    def describe(self) -> str:
        return f"the {self.side} server {self.server!r}: {self.what} ({self.why})"


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
    """What :func:`quiesce.start_events` printed: an ``until <unix>`` line, an
    ``oldest <unix>`` line, then one ``{{json .}}`` line per container ``start``.
    Anything else is skipped."""
    oldest: int | None = None
    read_at: int | None = None
    starts: list[StartEvent] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("oldest "):
            value = line.removeprefix("oldest ").strip()
            oldest = int(value) if value.isdigit() else oldest
            continue
        if line.startswith("until "):
            value = line.removeprefix("until ").strip()
            read_at = int(value) if value.isdigit() else read_at
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
    return EventLog(starts=tuple(starts), oldest=oldest, read_at=read_at)


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


def parse_daemon_id(stdout: str) -> str | None:
    """``docker info --format '{{.ID}}'`` -> the daemon's engine ID; ``None`` if it gave none.

    A UUID the daemon writes once into ``engine-id`` in its data root and reads
    back at every start (moby v27.5.1 daemon/id.go:12-35, unchanged in v28.3.3;
    daemon/info.go v27.5.1:53): one daemon, one ID, whichever address it is
    reached by. ``<no value>`` is what the template prints for a missing field.
    """
    line = next((raw.strip() for raw in stdout.splitlines() if raw.strip()), "")
    return line if line and line != "<no value>" else None


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

    A daemon that holds no event at all is no complete log: the buffer lives in
    memory, so it was restarted since (the stop alone left events). Then the
    log reaches back to nothing before it was read: ``read_at``, or ``since + 1``
    when the output did not say.
    """
    if events is None or since is None:
        return None
    if events.oldest is None:
        return events.read_at if events.read_at is not None else since + 1
    return events.oldest if events.oldest > since else None


#: Coolify's cancellable statuses (below), and a row that could not be read.
_PENDING = frozenset({QUEUED, IN_PROGRESS, ""})


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
    return [d for d in deployments if d.status in _PENDING]


# ── the deployment history: the witness no clock and no buffer can lose ─────


def ended_deployments(deployments: Iterable[Deployment]) -> list[str]:
    """The uuids of the deployments that had ENDED: what QUIESCE journals at the stop.

    One still queued or running is left out on purpose: Coolify's stop cancels
    none, so it runs after the stop. QUIESCE refuses the stop over it
    (``engine/containment.deployments_before_stop``); left out, it would be
    reported by the first checkpoint all the same (:func:`deployment_breaches`).
    """
    return sorted({d.uuid for d in deployments if d.uuid and d.status not in _PENDING})


def _uuid_lists(raw: object) -> dict[str, list[str]] | None:
    if not isinstance(raw, dict):
        return None
    return {
        str(app): [u for u in uuids if isinstance(u, str)]
        for app, uuids in raw.items()
        if isinstance(uuids, list)
    }


def accounted_deployments(
    quiesce_detail: Mapping[str, object],
) -> dict[str, frozenset[str]] | None:
    """Per source application, the deployments a checkpoint need not report.

    Those that had ended at the stop (:data:`SOURCE_DEPLOYMENTS`) and those a
    block after START has shown ended (:data:`REVIVAL_SEEN_DEPLOYMENTS`). ``None``
    when the journal holds no stop record: then there is nothing to compare with.
    """
    base = _uuid_lists(quiesce_detail.get(SOURCE_DEPLOYMENTS))
    if base is None:
        return None
    shown = _uuid_lists(quiesce_detail.get(REVIVAL_SEEN_DEPLOYMENTS)) or {}
    return {app: frozenset({*uuids, *shown.get(app, ())}) for app, uuids in base.items()}


def pending_at_block(quiesce_detail: Mapping[str, object]) -> dict[str, frozenset[str]]:
    """Per source application, what a block after START showed still pending
    (:data:`REVIVAL_PENDING_DEPLOYMENTS`); empty before any block."""
    pending = _uuid_lists(quiesce_detail.get(REVIVAL_PENDING_DEPLOYMENTS)) or {}
    return {app: frozenset(uuids) for app, uuids in pending.items()}


def block_record(
    quiesce_detail: Mapping[str, object], history: Mapping[str, Sequence[Deployment]]
) -> dict[str, dict[str, list[str]]]:
    """What a block after START journals of the rows its check read. PURE.

    Merged with an earlier block's record, because each record replaces the
    last (``Journal.undo_info``):

    * :data:`REVIVAL_SEEN_DEPLOYMENTS` gains every row that had ENDED — shown,
      so accounted from now on. That includes a row an earlier block showed
      pending: this check reported it ended (:func:`deployment_breaches`).
    * :data:`REVIVAL_PENDING_DEPLOYMENTS` holds every row still queued, running
      or unread, and keeps an earlier block's pending row this read did not list.
      A row the stop or a block already saw ended is never pending again: an
      unread status for it is a read that failed, not a deployment that resumed.

    An application this check did not read keeps what it had.
    """
    shown = _uuid_lists(quiesce_detail.get(REVIVAL_SEEN_DEPLOYMENTS)) or {}
    seen = {app: set(uuids) for app, uuids in shown.items()}
    pending = {app: set(uuids) for app, uuids in pending_at_block(quiesce_detail).items()}
    stopped = _uuid_lists(quiesce_detail.get(SOURCE_DEPLOYMENTS)) or {}
    for app, rows in history.items():
        ended = {d.uuid for d in rows if d.uuid and d.status not in _PENDING}
        known = seen.setdefault(app, set())
        known.update(ended)
        still = {d.uuid for d in rows if d.uuid and d.status in _PENDING}
        pending[app] = (pending.get(app, set()) | still) - known - set(stopped.get(app, ()))
    return {
        REVIVAL_SEEN_DEPLOYMENTS: {app: sorted(u) for app, u in sorted(seen.items())},
        REVIVAL_PENDING_DEPLOYMENTS: {app: sorted(u) for app, u in sorted(pending.items())},
    }


def deployment_breaches(
    applications: Sequence[Watched],
    accounted: Mapping[str, frozenset[str]],
    history: Mapping[str, Sequence[Deployment]],
    *,
    pending: Mapping[str, frozenset[str]] | None = None,
) -> list[Breach]:
    """Every deployment of a source application the stop did not see ended.

    ``history`` holds each application's newest rows as Coolify lists them,
    newest first (``GET /deployments/applications/{uuid}``: created_at
    descending, v4.1.2 Application.php:1084-1099; with id as the tiebreak at
    v4.7.2:1489-1490). A row counts when it

    * is still queued or running, or could not be read — it starts the source,
      or is starting it, and nothing here may wait for it; or
    * was ``pending`` when a block after START showed it (:func:`pending_at_block`)
      and has ended since — it may have started the source after that check,
      and its container may be gone. Wherever it sorts: the block may have seen
      a NEWER row ended, which is accounted and sits above it; or
    * is not ``accounted`` and no accounted row sorts above it: queued after the
      stop, or not ended by it. A row BELOW an accounted one is older than it,
      so it existed at the stop: one the stop's read did not reach (its page
      ends at ``take`` rows), surfaced by two rows created in the same second
      sorting the other way — v4.1.2 orders on created_at alone. Old rows are
      pruned by age only (CleanupDatabase v4.7.2:47), so none resurfaces otherwise.

    Statuses only, never a log: the client keeps nothing else of a row.
    An application missing from ``accounted`` has no record and is not judged.
    """
    breaches: list[Breach] = []
    for application in applications:
        known = accounted.get(application.uuid)
        rows = history.get(application.uuid)
        if known is None or rows is None:
            continue
        was_pending = (pending or {}).get(application.uuid, frozenset())
        below_known = False
        for row in rows:
            fresh = row.uuid not in known and not below_known
            if row.uuid in known:
                below_known = True
            if row.status in _PENDING:
                why = (
                    f"is {row.status} (Coolify's deployment history): it starts the "
                    "stopped source, or is starting it"
                    if row.status
                    else "has a status Coolify did not report (Coolify's deployment "
                    "history): it may still start the source"
                )
            elif row.uuid in was_pending and row.uuid not in known:
                why = (
                    f"had not ended when this run last blocked, and is {row.status} "
                    "now (Coolify's deployment history): it may have started the source "
                    "after that check, and whatever it started may be gone"
                )
            elif fresh:
                why = (
                    f"came after the stop, or had not ended by it, and is {row.status} "
                    "(Coolify's deployment history; whatever it started may be gone)"
                )
            else:
                continue
            breaches.append(Breach(application.name, f"deployment {row.uuid}", why))
    return breaches


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
        "To finish: stop the source in Coolify, cancel any of its deployments still "
        "queued or in progress, and hold pushes and CI deploys of it, then "
        f"coolify-migrate resume {migration_id} - FINALIZE then {fate}. The resume "
        "refuses again while a source container runs or a source deployment has not "
        "ended, or if one starts after this check - and once more for a deployment "
        "still queued or running here, after it ends, cancelled or not: it may have "
        "started the source after this check.\n"
        f"To give up the target instead: coolify-migrate rollback {migration_id} - it "
        "deletes the target and everything written to it since START, and restarts "
        "the source."
    )


_HISTORY_WITNESS = (
    "A deploy of the source that came up and was removed again - a failed redeploy - "
    "leaves its row in Coolify's deployment history long after the daemon's event log "
    "has forgotten it, so without that history this check cannot say that nothing "
    "started the source."
)


def unread_history_message(unread: Sequence[tuple[str, str]], checkpoint: str = "") -> str:
    """A check whose deployment histories (``(application, why)``) could not be read."""
    names = ", ".join(name for name, _ in unread)
    where = f" (at {checkpoint})" if checkpoint else ""
    return (
        f"could not read the deployment history of {names} to check that nothing "
        f"deployed the stopped source{where}"
    )


def _coolify_said(unread: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"Coolify said of {name}: {why}" for name, why in unread)


def unread_history_hint(unread: Sequence[tuple[str, str]]) -> str:
    """Why an unread history never passes — before START: the run rolls back."""
    return f"{_HISTORY_WITNESS}\n{_coolify_said(unread)}"


def unread_breaches(unread: Sequence[tuple[str, str]]) -> list[Breach]:
    """Unread histories as lines under a breach found in the same check, so a
    revival already found is reported with what could not be checked beside it."""
    return [
        Breach(name, "its deployment history", f"could not be read either ({why})")
        for name, why in unread
    ]


def unanswered_breaches(unanswered: Sequence[Unanswered]) -> list[Breach]:
    """Server reads that got no answer, as lines under a breach found in the same
    check — the rule of :func:`unread_breaches`."""
    return [
        Breach(f"the {u.side} server {u.server!r}", u.what, f"could not be read either ({u.why})")
        for u in unanswered
    ]


_SIDES = (("source", "the stopped source"), ("target", "an unstarted target"))


def unchecked_message(
    unanswered: Sequence[Unanswered],
    unread: Sequence[tuple[str, str]] = (),
    checkpoint: str = "",
) -> str:
    """A check that could not be finished: the server reads that got no answer,
    and the deployment histories Coolify did not give beside them. Histories
    alone keep their own message (:func:`unread_history_message`)."""
    if not unanswered:
        return unread_history_message(unread, checkpoint)
    sides = {u.side for u in unanswered}
    subject = " or ".join(text for side, text in _SIDES if side in sides)
    where = f" (at {checkpoint})" if checkpoint else ""
    lines = [u.describe() for u in unanswered] + [b.describe() for b in unread_breaches(unread)]
    return f"could not check that nothing started {subject}{where} - a server did not answer:\n" + (
        "\n".join(f"  {line}" for line in lines)
    )


def resume_when(
    unanswered: Sequence[Unanswered], unread: Sequence[tuple[str, str]] = ()
) -> str:
    """Who must answer again before a resume can finish the check: the words of a block."""
    sides = [side for side, _ in _SIDES if any(u.side == side for u in unanswered)]
    parties = [f"the {' and '.join(sides)} server{'s' if len(sides) > 1 else ''}"] if sides else []
    if unread or not parties:
        parties.append("Coolify")
    verb = "answer" if len(parties) > 1 or len(sides) > 1 else "answers"
    return f"once {' and '.join(parties)} {verb} again"


_DAEMON_WITNESS = (
    "Only a server's Docker daemon shows what started there since the stop - a container "
    "running now, the StartedAt of one stopped again, the start in its event log of one "
    "that came and went - so without its answer this check cannot say that nothing started."
)


def unchecked_after_start_hint(
    *,
    policy: str,
    migration_id: str,
    unread: Sequence[tuple[str, str]] = (),
    unanswered: Sequence[Unanswered] = (),
) -> str:
    """A check that could not be finished once START asked for a target: a block.

    An unread deployment history, or a server read that got no answer — the
    container listings, a container's inspect, the event log, on the source or
    on a target START has not asked for yet. Not proof that anything started:
    a check that cannot say that nothing did. Before START that rolls back
    (:func:`unread_history_hint`, or the read's own error); after it the target
    may hold users' writes, which the rollback deletes, so a Coolify or an SSH
    connection that does not answer for a while (a restart, an update, a
    network blip) must not decide that. Nothing is journalled: nothing was shown.
    """
    fate = _FINALIZE_FATE.get(policy, "applies the finalize policy to the source")
    witness = [_DAEMON_WITNESS] if unanswered else []
    if unread:
        witness.append(f"{_HISTORY_WITNESS}\n{_coolify_said(unread)}")
    when = resume_when(unanswered, unread)
    return (
        "\n".join(witness) + "\n"
        "START has already asked Coolify to start the target, and the DNS gate let it: it "
        "may have served users since then, and what they wrote is on the target only. A "
        "rollback would delete that with the target, so this run does not roll back over a "
        "check it could not finish. Nothing has been undone, and FINALIZE has not touched "
        "the source.\n"
        f"{when[0].upper()}{when[1:]}: coolify-migrate resume {migration_id} - it checks "
        f"again, and FINALIZE then {fate}.\n"
        f"To give up the target instead: coolify-migrate rollback {migration_id} - it "
        "deletes the target and everything written to it since START, and restarts the "
        "source."
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


def shared_daemon_refusal(
    source_id: str | None, target_id: str | None, *, source: str, target: str
) -> str | None:
    """Two server records, one Docker daemon. PURE.

    The plan's same-server refusal compares server uuids, and Coolify gives one
    SSH endpoint one server record (ServersController v4.7.2:637,
    ``sshEndpointInUse``) — but one machine reached two ways (localhost and its
    public IP, or two SSH ports) is two records. Every source-side checkpoint
    would then find the running target under the source's labels, the stack's
    project, environment and name, and take it for the source started again.
    Refused only when BOTH daemons answered with the same ID; an unanswered
    probe proves nothing and refuses nothing.
    """
    if not source_id or not target_id or source_id != target_id:
        return None
    return (
        f"the source server {source!r} and the target server {target!r} are one Docker "
        f"daemon (both report engine ID {source_id}): Coolify knows this machine twice, "
        "under two addresses. A migration between them never leaves it - the checks that "
        "prove the source stays stopped would see the target's containers as the source "
        "started again, every custom domain resolves to both sides, and bind mounts would "
        "be copied onto themselves. Pick a target server on another machine."
    )


SHARED_DAEMON_HINT = (
    "Compare `docker info --format '{{.ID}}'` on both servers. If they really are two "
    "machines, one was cloned from the other together with Docker's data root "
    "(/var/lib/docker by default) and kept its engine-id file: remove that file on one "
    "of them and restart Docker there, which writes a new ID. Nothing has been changed."
)


def breach_message(subject: str, checkpoint: str, breaches: Sequence[Breach]) -> str:
    """One line per breach, under a heading naming the checkpoint that found them."""
    return f"{subject} (found at {checkpoint}):\n" + "\n".join(
        f"  {b.describe()}" for b in breaches
    )
