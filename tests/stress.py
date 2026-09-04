"""A deliberately hostile workout for the sync engine.

Not part of ``run-tests.sh``: it takes minutes rather than seconds, and it is
here to be run when something needs proving rather than on every change. What
it looks for is the class of fault that a correctness test does not catch --
work that grows with each pass, queries that multiply with the number of items,
memory that is never given back, and systems that never settle.

Three things make it worth more than a synthetic loop:

**The capability profiles are the real ones.** Every fake service below asks an
actual connector class what it can store, so Google really cannot keep a time
of day here, Todoist really has no calendar, and Obsidian really refuses to
write anything but a completion. A hand-written profile would drift from the
code within a month and quietly stop testing anything.

**Everything is written at once.** Real services are edited by real people at
the same moment, and the interesting failures live in the overlap. Writers here
are staggered by a tenth of a millisecond, which is close enough to
simultaneous that ordering is decided by whatever the engine does rather than
by the clock.

**It measures rather than asserts.** A pass that works but allocates twice as
much as the one before it is a fault worth knowing about even though nothing
failed, so memory, query counts and timings are reported per pass and the
trends are what matter.

Run it with:

    docker compose exec -e PYTHONPATH=/app -e TASKHUB_DATA_DIR=/tmp/stress \\
        -w /app taskhub python -m tests.stress
"""

from __future__ import annotations

import datetime as dt
import gc
import statistics
import sys
import time
import tracemalloc
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import event

from app.connectors.base import Capabilities, Connector, PullResult, PushOutcome, RemoteItem
from app.connectors.base import RemoteList as RemoteListSpec
from app.db.models import (
    Account,
    AccountStatus,
    CollectionKind,
    Item,
    ItemLink,
    ListMapping,
    RemoteList as RemoteListRow,
    ServiceKind,
    SyncGroup,
)
from app.db.session import get_engine, init_db, session_scope
from app.services.ical_model import CanonicalRecord
from app.sync import engine as engine_module
from app.sync.engine import SyncEngine
from app.sync.merge import project

UTC = dt.timezone.utc

#: How close together concurrent edits are made. A tenth of a millisecond is
#: far below the resolution any real service reports modification times at, so
#: the engine cannot fall back on timestamps to order them and has to rely on
#: its own provenance tracking instead. That is precisely the code worth
#: stressing.
SIMULTANEITY_SECONDS = 0.0001

#: Sync passes per phase. Enough that a leak or a slowdown shows as a trend
#: rather than as noise from one unlucky pass.
PASSES_PER_PHASE = 12


# --- Measurement --------------------------------------------------------------


@dataclass
class PassMetrics:
    """What one sync pass cost, so that passes can be compared with each other."""

    index: int
    seconds: float
    queries: int
    #: Bytes tracemalloc says are still held after the pass, not the peak. A
    #: rising figure across otherwise identical passes is the leak signal.
    retained_bytes: int
    #: Live Python objects after a forced collection. Catches the case where
    #: allocation is flat but references are being kept.
    live_objects: int
    writes: dict[str, int] = field(default_factory=dict)

    @property
    def total_writes(self) -> int:
        return sum(self.writes.values())


class QueryCounter:
    """Counts SQL statements, to catch work that grows with the item count.

    An N+1 query is invisible in a correctness test -- the answers are right --
    and ruinous on a Raspberry Pi with a few thousand tasks. Counting per pass
    and comparing against the number of items turns it into something you can
    see.
    """

    def __init__(self):
        self.count = 0
        self._listening = False
        #: Statement text -> how many times it ran this pass. Counting the
        #: shape of each query, not just how many there were, is what turns
        #: "this is slow" into "this line is the problem".
        self.by_statement: Counter[str] = Counter()

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1
        # Collapsed onto one line and clipped: the interest is in which query
        # repeats, not in reading the whole of it here.
        self.by_statement[" ".join(statement.split())[:150]] += 1

    def start(self):
        if not self._listening:
            event.listen(get_engine(), "before_cursor_execute", self._on_execute)
            self._listening = True

    def reset(self):
        self.count = 0
        self.by_statement.clear()

    def top(self, limit: int = 8) -> list[tuple[str, int]]:
        return self.by_statement.most_common(limit)


QUERIES = QueryCounter()


# --- The fake services --------------------------------------------------------


class RecordingService(Connector):
    """An in-memory stand-in for one real service, with that service's limits.

    It behaves as the real thing does in the ways that matter to the engine: it
    can only report fields it is able to store, it stamps a modification time
    on every write, and it refuses operations its capabilities say it cannot
    perform. What it does not do is talk to anything, so a stress run can make
    tens of thousands of calls without touching a real account.
    """

    def __init__(self, key: str, caps_by_kind: dict, service: ServiceKind):
        super().__init__(account_id=0, credentials={}, sync_state={})
        self.key = key
        self.service = service
        self.name = key
        self._caps_by_kind = caps_by_kind
        #: remote_id -> record, surviving across passes like a real service.
        self.store: dict[str, CanonicalRecord] = {}
        self.stamps: dict[str, dt.datetime] = {}
        self.writes = 0
        self.reads = 0
        self.rejected_writes = 0

    def capabilities(self, kind):
        return self._caps_by_kind[kind]

    def verify(self):
        return self.key

    def list_remote_lists(self):
        return [
            RemoteListSpec(remote_id="list1", name=f"{self.key} list",
                           kind=CollectionKind.TASKS),
            RemoteListSpec(remote_id="cal1", name=f"{self.key} calendar",
                           kind=CollectionKind.CALENDAR),
        ]

    def pull(self, remote_list_id, kind, since=None, state=None):
        self.reads += 1
        caps = self._caps_by_kind[kind]
        items = []
        for remote_id, record in self.store.items():
            if record.kind != kind:
                continue
            # A real service reports only what it can hold. Projecting through
            # the capability set here is what makes a lossy service genuinely
            # lossy in this harness rather than merely labelled as one.
            reported = project(record, caps)
            if not caps.stores_uid:
                reported.uid = ""
            items.append(
                RemoteItem(
                    remote_id=remote_id,
                    record=reported,
                    fields_present=caps.present_fields(),
                    remote_updated_at=self.stamps.get(remote_id),
                )
            )
        return PullResult(items=items, incremental=False)

    def create(self, remote_list_id, record, kind):
        if not self._caps_by_kind[kind].can_create:
            self.rejected_writes += 1
            raise AssertionError(
                f"{self.key} was asked to create in {kind.value}, which its "
                "capabilities say it cannot do. The engine should not have "
                "asked."
            )
        self.writes += 1
        remote_id = f"{self.key}-{len(self.store) + 1}"
        self.store[remote_id] = record
        self.stamps[remote_id] = dt.datetime.now(UTC)
        return PushOutcome(remote_id=remote_id, remote_updated_at=self.stamps[remote_id])

    def update(self, remote_list_id, remote_id, record, kind):
        self.writes += 1
        self.store[remote_id] = record
        self.stamps[remote_id] = dt.datetime.now(UTC)
        return PushOutcome(remote_id=remote_id, remote_updated_at=self.stamps[remote_id])

    def delete(self, remote_list_id, remote_id, kind):
        if not self._caps_by_kind[kind].can_delete:
            self.rejected_writes += 1
            raise AssertionError(
                f"{self.key} was asked to delete, which it cannot do."
            )
        self.store.pop(remote_id, None)
        self.stamps.pop(remote_id, None)
        return PushOutcome(remote_id=remote_id)

    # -- Used by the harness rather than the engine ---------------------------

    def edit(self, remote_id: str, **changes) -> None:
        """Change a record the way a person using that service would.

        Stamped with the current time, because that is what a real service
        does, and staggered by the caller when several services are edited at
        once.
        """
        record = self.store[remote_id]
        for attribute, value in changes.items():
            setattr(record, attribute, value)
        self.stamps[remote_id] = dt.datetime.now(UTC)


def real_capabilities() -> dict[str, dict]:
    """Ask every real connector class what it can store.

    Constructed with placeholder credentials, which is enough because the
    capability declaration is static per connector -- it describes the service,
    not the account. Nothing here reaches the network.
    """
    import importlib

    specs = [
        ("google", "GoogleConnector", ServiceKind.GOOGLE,
         {"access_token": "x", "refresh_token": "y"}, {"client_id": "c", "client_secret": "s"}),
        ("todoist", "TodoistConnector", ServiceKind.TODOIST,
         {"api_token": "x"}, {}),
        ("ticktick", "TickTickConnector", ServiceKind.TICKTICK,
         {"access_token": "x"}, {}),
        ("microsoft", "MicrosoftConnector", ServiceKind.MICROSOFT,
         {"access_token": "x", "refresh_token": "y"}, {"client_id": "c", "client_secret": "s"}),
        ("obsidian", "ObsidianConnector", ServiceKind.OBSIDIAN,
         {"email": "e", "password": "p", "vault": "v", "write_back": True}, {}),
        ("things3", "ThingsConnector", ServiceKind.THINGS3,
         {"email": "e", "password": "p"}, {}),
        ("caldav_remote", "AppleConnector", ServiceKind.APPLE,
         {"username": "u", "password": "p"}, {}),
        ("radicale_local", "RadicaleConnector", ServiceKind.RADICALE,
         {"username": "u", "password": "p"}, {}),
    ]

    profiles: dict[str, dict] = {}
    for module_name, class_name, service, credentials, extra in specs:
        cls = getattr(importlib.import_module(f"app.connectors.{module_name}"), class_name)
        try:
            instance = cls(account_id=0, credentials=credentials, sync_state={}, **extra)
        except Exception as exc:  # pragma: no cover - reported, not fatal
            print(f"  ! could not read {class_name}: {exc}")
            continue
        profiles[class_name] = {
            "service": service,
            "caps": {
                CollectionKind.TASKS: instance.capabilities(CollectionKind.TASKS),
                CollectionKind.CALENDAR: instance.capabilities(CollectionKind.CALENDAR),
            },
        }
    return profiles


# --- Building the world -------------------------------------------------------

SERVICES: dict[int, RecordingService] = {}


def build_world(profiles: dict[str, dict]) -> tuple[list[RecordingService], list[int]]:
    """One account per service, in a tasks group and a calendar group.

    Every service joins both groups even when it cannot store calendar entries
    at all. That is deliberate: a connector with an empty capability set being
    asked to write is exactly the fault that once made every run report
    ``partial``, and it will not resurface unnoticed if the stress run keeps
    asking.
    """
    with session_scope() as session:
        groups = {}
        for kind in (CollectionKind.TASKS, CollectionKind.CALENDAR):
            group = SyncGroup(name=f"Stress {kind.value}", kind=kind, enabled=True)
            session.add(group)
            groups[kind] = group
        session.commit()

        services: list[RecordingService] = []
        for slot, (class_name, profile) in enumerate(profiles.items(), start=1):
            account = Account(
                service=profile["service"], slot=slot, label=class_name,
                status=AccountStatus.CONNECTED,
            )
            session.add(account)
            session.commit()

            fake = RecordingService(class_name, profile["caps"], profile["service"])
            SERVICES[account.id] = fake
            services.append(fake)

            for kind, remote_id in (
                (CollectionKind.TASKS, "list1"),
                (CollectionKind.CALENDAR, "cal1"),
            ):
                row = RemoteListRow(
                    account_id=account.id, remote_id=remote_id,
                    name=f"{class_name} {kind.value}", kind=kind,
                )
                session.add(row)
                session.flush()
                session.add(ListMapping(
                    remote_list_id=row.id, sync_group_id=groups[kind].id,
                    read_enabled=True, write_enabled=True,
                ))
            session.commit()

        return services, [g.id for g in groups.values()]


def run_pass(index: int, services: list[RecordingService]) -> PassMetrics:
    """One full sync, measured."""
    for service in services:
        service.writes = 0

    gc.collect()
    QUERIES.reset()
    before_snapshot = tracemalloc.take_snapshot()
    started = time.perf_counter()

    with session_scope() as session:
        SyncEngine(session).run_sync(trigger="stress")

    elapsed = time.perf_counter() - started
    gc.collect()
    after_snapshot = tracemalloc.take_snapshot()
    retained = sum(
        stat.size_diff for stat in after_snapshot.compare_to(before_snapshot, "filename")
    )

    return PassMetrics(
        index=index,
        seconds=elapsed,
        queries=QUERIES.count,
        retained_bytes=retained,
        live_objects=len(gc.get_objects()),
        writes={s.key: s.writes for s in services},
    )


def report(title: str, metrics: list[PassMetrics], item_count: int) -> list[str]:
    """Print the trend across passes and return anything that looks wrong."""
    problems: list[str] = []
    print(f"\n  {'pass':<6}{'seconds':>9}{'queries':>9}{'writes':>8}{'retained':>12}{'objects':>10}")
    for m in metrics:
        print(f"  {m.index:<6}{m.seconds:>9.3f}{m.queries:>9}{m.total_writes:>8}"
              f"{m.retained_bytes/1024:>11.0f}K{m.live_objects:>10}")

    settled = [m for m in metrics[2:]]
    if settled and any(m.total_writes for m in settled):
        offenders = {k: sum(m.writes.get(k, 0) for m in settled) for m in settled for k in m.writes}
        noisy = {k: v for k, v in offenders.items() if v}
        problems.append(
            f"{title}: still writing after settling — {noisy}. A stable system "
            "must reach zero writes; anything else rewrites every item for ever."
        )

    if len(metrics) >= 6:
        early = statistics.mean(m.live_objects for m in metrics[1:4])
        late = statistics.mean(m.live_objects for m in metrics[-3:])
        growth = late - early
        if growth > 2000:
            problems.append(
                f"{title}: live object count grew by {growth:.0f} across passes "
                "with no new data, which is what a leak looks like."
            )

        early_q = statistics.mean(m.queries for m in metrics[1:4])
        late_q = statistics.mean(m.queries for m in metrics[-3:])
        if late_q > early_q * 1.5 and late_q - early_q > 20:
            problems.append(
                f"{title}: queries per pass rose from {early_q:.0f} to {late_q:.0f} "
                "with no new data."
            )

    if item_count and metrics:
        per_item = statistics.mean(m.queries for m in metrics[-3:]) / item_count
        print(f"\n  queries per item per pass: {per_item:.1f}"
              f"   ({'fine' if per_item < 12 else 'HIGH — look for an N+1'})")
        if per_item >= 12:
            print("\n  the queries that repeat most, from the last pass:")
            for statement, times in QUERIES.top():
                print(f"    {times:>5}x  {statement[:110]}")
        if per_item >= 12:
            problems.append(
                f"{title}: {per_item:.1f} queries per item per pass suggests a "
                "query inside a per-item loop."
            )

    return problems


# --- The scenarios ------------------------------------------------------------


def seed_tasks(source: RecordingService, count: int) -> None:
    """Tasks that span several days, which is where date handling goes wrong.

    A task with a start date, a due date some days later, a time of day and a
    real timezone exercises every field the merge treats as a group. Most
    services can hold only some of it, so each one loses something different --
    and the point of the exercise is that no service's loss is allowed to
    propagate to any other.
    """
    today = dt.date.today()
    for n in range(count):
        start = today + dt.timedelta(days=n % 7)
        source.store[f"task-{n}"] = CanonicalRecord(
            uid=f"stress-task-{n}",
            kind=CollectionKind.TASKS,
            title=f"Multi-day task {n}",
            notes=f"Runs from {start} for {2 + n % 5} days.",
            start_date=start,
            start_time=dt.time(9, 0),
            start_tz="Europe/London",
            due_date=start + dt.timedelta(days=2 + n % 5),
            due_time=dt.time(17, 30),
            due_tz="Europe/London",
            priority=(n % 4) + 1,
            tags=[f"tag{n % 3}", "stress"],
            location=f"Room {n % 10}",
        )
        source.stamps[f"task-{n}"] = dt.datetime.now(UTC)


def seed_events(source: RecordingService, count: int) -> None:
    """Calendar entries that run across day boundaries, plus all-day ones.

    Multi-day events are the case most likely to come back wrong, because an
    event that ends before it starts is a state several services will accept
    without complaint and no client can display.
    """
    today = dt.date.today()
    for n in range(count):
        start = today + dt.timedelta(days=n % 10)
        if n % 3 == 0:
            # All-day, spanning several days: no times at all.
            source.store[f"event-{n}"] = CanonicalRecord(
                uid=f"stress-event-{n}", kind=CollectionKind.CALENDAR,
                title=f"All-day span {n}", start_date=start,
                end_date=start + dt.timedelta(days=1 + n % 4),
                location="Offsite", tags=["stress"],
            )
        else:
            # Timed and crossing midnight, in a timezone that observes DST.
            source.store[f"event-{n}"] = CanonicalRecord(
                uid=f"stress-event-{n}", kind=CollectionKind.CALENDAR,
                title=f"Overnight event {n}", notes="Crosses midnight.",
                start_date=start, start_time=dt.time(22, 0), start_tz="Europe/London",
                end_date=start + dt.timedelta(days=1 + n % 3),
                end_time=dt.time(6, 30), end_tz="Europe/London",
                location=f"Venue {n}", tags=["stress", "overnight"],
            )
        source.stamps[f"event-{n}"] = dt.datetime.now(UTC)


def edit_everywhere_at_once(services: list[RecordingService], round_number: int) -> int:
    """Edit the same items from several services a tenth of a millisecond apart.

    This is the scenario that breaks naive sync engines. Each service is edited
    in turn with almost no gap, so their modification times are effectively
    identical and cannot be used to decide who wins. What must happen is that
    each service's *own* change is accepted for the fields it can express, and
    no service's echo of a value it was merely told about overwrites a newer
    edit made elsewhere.
    """
    edited = 0
    for offset, service in enumerate(services):
        writable = service.capabilities(CollectionKind.TASKS).push_fields()
        targets = [rid for rid, rec in service.store.items()
                   if rec.kind == CollectionKind.TASKS][:3]
        for remote_id in targets:
            changes = {}
            if "title" in writable:
                changes["title"] = f"Edited by {service.key} round {round_number}"
            if "notes" in writable:
                changes["notes"] = f"Touched at {time.time_ns()}"
            if "priority" in writable:
                changes["priority"] = (round_number + offset) % 4 + 1
            if not changes:
                continue
            service.edit(remote_id, **changes)
            edited += 1
            time.sleep(SIMULTANEITY_SECONDS)
    return edited


def count_items() -> tuple[int, int]:
    with session_scope() as session:
        return (
            session.query(Item).count(),
            session.query(ItemLink).count(),
        )


def check_integrity(services: list[RecordingService]) -> list[str]:
    """Look for states no correct sync should ever produce."""
    faults: list[str] = []

    for service in services:
        if service.rejected_writes:
            faults.append(
                f"{service.key} was asked to perform {service.rejected_writes} "
                "operation(s) its capabilities forbid."
            )
        for remote_id, record in service.store.items():
            if record.kind != CollectionKind.CALENDAR:
                continue
            if record.start_date and record.end_date and record.end_date < record.start_date:
                faults.append(
                    f"{service.key}/{remote_id} ends ({record.end_date}) before "
                    f"it starts ({record.start_date})."
                )
            if (record.start_date == record.end_date and record.start_time
                    and record.end_time and record.end_time < record.start_time):
                faults.append(
                    f"{service.key}/{remote_id} ends at {record.end_time}, before "
                    f"its start at {record.start_time}, on the same day."
                )
        for remote_id, record in service.store.items():
            if not (record.title or "").strip():
                faults.append(f"{service.key}/{remote_id} has lost its title.")

    with session_scope() as session:
        orphans = session.query(ItemLink).filter(
            ~ItemLink.item_id.in_(session.query(Item.id))
        ).count()
        if orphans:
            faults.append(f"{orphans} link(s) point at items that no longer exist.")

    return faults


# --- Driver -------------------------------------------------------------------


def main() -> int:
    print("Task Hub stress run")
    print("=" * 74)

    tracemalloc.start()
    init_db()
    QUERIES.start()

    # The engine builds real connectors from stored credentials; here it must
    # hand back the in-memory stand-ins instead. Radicale account creation is
    # skipped for the same reason -- there is no CalDAV server in this run.
    engine_module.build_connector = lambda session, account: SERVICES[account.id]
    engine_module.ensure_radicale_account = lambda session: None

    print("\nCapability profiles, read from the real connector classes:")
    profiles = real_capabilities()
    for name, profile in profiles.items():
        tasks = profile["caps"][CollectionKind.TASKS]
        cal = profile["caps"][CollectionKind.CALENDAR]
        print(f"  {name:22} tasks {len(tasks.fields):2} fields, "
              f"calendar {len(cal.fields):2} fields, "
              f"writes {len(tasks.push_fields()):2}")

    services, _ = build_world(profiles)
    source = next(s for s in services if s.key == "RadicaleConnector")
    problems: list[str] = []

    # -- Phase 1: multi-day tasks and events propagate outward ----------------
    print("\n" + "-" * 74)
    print("Phase 1 — 40 multi-day tasks and 30 multi-day events, from one source")
    seed_tasks(source, 40)
    seed_events(source, 30)

    metrics = [run_pass(i, services) for i in range(1, PASSES_PER_PHASE + 1)]
    items, links = count_items()
    print(f"\n  canonical items: {items}   links: {links}")
    problems += report("Phase 1", metrics, items)
    problems += check_integrity(services)

    # -- Phase 2: everyone edits at once --------------------------------------
    print("\n" + "-" * 74)
    print(f"Phase 2 — every service edits the same tasks {SIMULTANEITY_SECONDS*1000:.1f}ms apart")
    phase2: list[PassMetrics] = []
    for round_number in range(1, PASSES_PER_PHASE + 1):
        edited = edit_everywhere_at_once(services, round_number)
        phase2.append(run_pass(round_number, services))
    print(f"\n  {edited} concurrent edits per round")
    print(f"  {'pass':<6}{'seconds':>9}{'queries':>9}{'writes':>8}{'retained':>12}{'objects':>10}")
    for m in phase2:
        print(f"  {m.index:<6}{m.seconds:>9.3f}{m.queries:>9}{m.total_writes:>8}"
              f"{m.retained_bytes/1024:>11.0f}K{m.live_objects:>10}")
    # Writes are expected here -- edits are being made every round. What is
    # checked is that the volume stays proportional to the edits rather than
    # snowballing, and that nothing invalid results.
    if len(phase2) >= 6:
        early = statistics.mean(m.total_writes for m in phase2[1:4])
        late = statistics.mean(m.total_writes for m in phase2[-3:])
        print(f"\n  writes per round: {early:.0f} early, {late:.0f} late")
        if late > early * 2 and late - early > 20:
            problems.append(
                f"Concurrent edits: writes per round grew from {early:.0f} to "
                f"{late:.0f} for the same number of edits — an echo storm."
            )
    problems += check_integrity(services)

    # -- Phase 3: does it settle once editing stops? --------------------------
    print("\n" + "-" * 74)
    print("Phase 3 — editing stops; the system must go quiet")
    phase3 = [run_pass(i, services) for i in range(1, 7)]
    problems += report("Phase 3", phase3, count_items()[0])
    problems += check_integrity(services)

    # -- Phase 4: deletion propagates, and nothing comes back -----------------
    print("\n" + "-" * 74)
    print("Phase 4 — deletions from one service must not resurrect")
    doomed = [rid for rid, rec in source.store.items()
              if rec.kind == CollectionKind.TASKS][:5]
    for remote_id in doomed:
        source.store.pop(remote_id, None)
        source.stamps.pop(remote_id, None)
    before_total = sum(len(s.store) for s in services)
    for i in range(1, 5):
        run_pass(i, services)
    after_total = sum(len(s.store) for s in services)
    print(f"  items across all services: {before_total} -> {after_total}")
    if after_total >= before_total:
        problems.append(
            "Phase 4: deleting 5 tasks did not reduce the total across services; "
            "either the deletion did not propagate or something recreated them."
        )
    resurrected = [rid for rid in doomed if rid in source.store]
    if resurrected:
        problems.append(f"Phase 4: deleted tasks came back: {resurrected}")
    problems += check_integrity(services)

    # -- Verdict --------------------------------------------------------------
    print("\n" + "=" * 74)
    current, peak = tracemalloc.get_traced_memory()
    print(f"Peak traced memory: {peak/1024/1024:.1f} MB   still held: {current/1024/1024:.1f} MB")
    tracemalloc.stop()

    if problems:
        print(f"\n{len(problems)} PROBLEM(S) FOUND\n")
        for problem in problems:
            print(f"  * {problem}")
        return 1

    print("\nNo faults found: settles to zero writes, no leak, no invalid dates,")
    print("no orphaned links, and no service asked to do what it cannot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
