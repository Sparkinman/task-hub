"""A stress run against the real connected accounts.

The sibling harness, :mod:`tests.stress`, replaces every service with an
in-memory stand-in. That makes it fast and repeatable, and it deliberately
cannot answer the questions a real account raises: whether a service throttles
under load, whether its API returns what its documentation claims when asked
two hundred times in a row, and what a sync pass actually costs in CPU and
memory when the answers come over a network instead of out of a dictionary.

This harness answers those. It talks to the genuine Google, Todoist, TickTick,
Radicale and Obsidian accounts that are connected, and everything else about it
is arranged so that talking to them is safe.

Three rules make it safe to point at somebody's real data:

**It never touches the live database.** The live database is copied to a
throwaway directory and stripped of every item, link and mapping, keeping only
the account credentials and settings. The copy is made through SQLite's backup
API rather than by copying the file, because the write-ahead log routinely
holds more of the database than the file does. The live instance carries on
untouched, and the copy is deleted at the end.

**Everything it creates goes in a container it made itself.** A throwaway task
list, project and calendar is created in each service, and the two hundred
tasks and two hundred events live only in those. Nothing is ever written to an
existing list, project or calendar. Cleanup is therefore a handful of deletes
of the containers rather than four hundred deletes of items, which is what
makes it reliable: one failed request cannot leave stray items behind.

**Cleanup does not depend on this script finishing.** Every container is
recorded in ``containers.json`` the moment it is created, before anything is
put in it. Running with ``--teardown`` reads that file and removes whatever it
names, so a run that is killed, crashes or times out is still cleaned up by a
second command that shares no state with the first.

Obsidian is a special case worth stating plainly. Its connector declares
``can_create=False`` and ``can_delete=False``, so Task Hub cannot write a file
into a vault under any circumstances, and the only change it can make to
somebody's notes is ticking a checkbox that is already there. Here it is
narrowed further still, to read-only, because no measurement is worth altering
a line of somebody's real notes. It also cannot be seeded: Obsidian's own sync
client reconciles the vault against the remote continuously and deletes
anything it does not recognise, which it was measured doing within two seconds
of a folder being created. So Obsidian joins by reading an existing test
folder, and the Work Vault is read exactly once, to time what reading a real
vault of that size costs.

Run it with:

    docker compose cp tests taskhub:/app/
    docker compose exec -e PYTHONPATH=/app -e TASKHUB_DATA_DIR=/tmp/stresslive \\
        -w /app taskhub python -m tests.stress_live

and, if it is interrupted:

    docker compose exec -e PYTHONPATH=/app -e TASKHUB_DATA_DIR=/tmp/stresslive \\
        -w /app taskhub python -m tests.stress_live --teardown
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# --- Guard rails, applied before anything imports the application -------------
#
# app.config reads TASKHUB_DATA_DIR at import time and every path in the
# application derives from it, so the isolation has to be established here or
# not at all.

LIVE_DATA = Path("/data")
DATA_DIR = Path(os.environ.get("TASKHUB_DATA_DIR", ""))

if not DATA_DIR or DATA_DIR.resolve() == LIVE_DATA.resolve():
    sys.exit(
        "Refusing to run: TASKHUB_DATA_DIR must be set to a throwaway directory "
        "that is not /data. This harness strips the database it is given."
    )

#: How many of each to create. Tasks are split between two sources so that more
#: than one service originates work, which is where reconciliation is hardest.
#: Overridable so that a change to this harness can be smoke-tested against the
#: real services at a scale that costs seconds rather than half an hour.
TASK_COUNT = int(os.environ.get("STRESS_TASKS", "200"))
EVENT_COUNT = int(os.environ.get("STRESS_EVENTS", "200"))

#: The folder in the personal vault that Obsidian is read from. It is an
#: existing test folder, and it is mapped read-only, because a vault cannot be
#: seeded from outside Obsidian: the sync client reconciles the vault against
#: the remote continuously and deletes anything it does not recognise, which it
#: was measured doing within two seconds of a folder being created. Reading a
#: real folder is the honest way to include Obsidian in a live run, and
#: read-only means Task Hub cannot alter a single line of anybody's notes.
VAULT_FOLDER = os.environ.get("STRESS_VAULT_FOLDER", "folder:Sync Testing")

#: A tag on everything this harness makes, so that anything it somehow fails to
#: remove can still be found and identified later by eye.
MARKER = "taskhub-stress"

RUN_ID = time.strftime("%Y%m%d-%H%M%S")
REGISTRY_PATH = DATA_DIR / "containers.json"


# --- The teardown registry ----------------------------------------------------


def registry_load() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def registry_add(entry: dict) -> None:
    """Record a container before anything is put in it.

    Written and flushed immediately rather than at the end of the run. The
    whole value of this file is that it survives the process dying, so it has
    to be correct at every instant rather than only once the run succeeds.
    """
    entries = registry_load()
    entries.append(entry)
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(entries, indent=2))


# --- Preparing an isolated database -------------------------------------------


#: Everything that describes synchronised data, as opposed to the accounts and
#: settings that describe how to reach the services. The copy keeps the latter
#: and discards the former, so the run starts from an empty world with working
#: credentials.
SYNC_TABLES = [
    "sync_log_entries",
    "sync_runs",
    "field_provenance",
    "item_links",
    "tombstones",
    "items",
    "list_mappings",
    "sync_groups",
    "remote_lists",
    "radicale_collections",
]


def prepare_isolated_data_dir() -> None:
    """Copy the live database and key into the throwaway directory, then strip it.

    The backup API is used rather than a file copy for the same reason the
    application's own backup does: at the time of writing the live database file
    held 2.7 MB while its write-ahead log held 4.5 MB, so a plain copy would
    produce a database missing most of its recent history -- which here would
    mean missing account credentials.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(f"file:{LIVE_DATA / 'taskhub.db'}?mode=ro", uri=True)
    target_path = DATA_DIR / "taskhub.db"
    target_path.unlink(missing_ok=True)
    target = sqlite3.connect(target_path)
    with target:
        source.backup(target)
    source.close()

    # The credentials are encrypted with this key. Without it every account in
    # the copy decrypts to an empty dictionary and nothing can connect.
    shutil.copy2(LIVE_DATA / "secret.key", DATA_DIR / "secret.key")

    with target:
        target.execute("PRAGMA foreign_keys = OFF")
        for table in SYNC_TABLES:
            target.execute(f"DELETE FROM {table}")
    target.execute("VACUUM")
    target.close()

    # The vaults are real directories managed by Obsidian's own client, and the
    # connector finds them through the data directory. Symlinking rather than
    # copying means the harness reads exactly what the live instance reads.
    link = DATA_DIR / "obsidian"
    if not link.exists():
        link.symlink_to(LIVE_DATA / "obsidian")


# Everything below this line may import the application.
if "--teardown" not in sys.argv:
    prepare_isolated_data_dir()

import datetime as dt  # noqa: E402
import gc  # noqa: E402
import logging  # noqa: E402
import statistics  # noqa: E402
import tracemalloc  # noqa: E402
from collections import Counter  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import httpx  # noqa: E402
import requests  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.connectors.base import ConnectorError  # noqa: E402
from app.crypto import decrypt_json  # noqa: E402
from app.db.models import (  # noqa: E402
    Account,
    AccountStatus,
    CollectionKind,
    Item,
    ItemLink,
    ListMapping,
    RemoteList as RemoteListRow,
    ServiceKind,
    SyncGroup,
    SyncLogEntry,
)
from app.db.session import get_engine, session_scope  # noqa: E402
from app.services.caldav_client import RadicaleClient  # noqa: E402
from app.services.ical_model import CanonicalRecord  # noqa: E402
from app.sync import engine as engine_module  # noqa: E402
from app.sync.engine import SyncEngine, build_connector  # noqa: E402

UTC = dt.timezone.utc


# --- Measurement --------------------------------------------------------------


class QueryCounter:
    """Counts SQL statements, to catch work that grows with the item count."""

    def __init__(self):
        self.count = 0
        self._listening = False
        self.by_statement: Counter[str] = Counter()

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1
        self.by_statement[" ".join(statement.split())[:150]] += 1

    def start(self):
        if not self._listening:
            event.listen(get_engine(), "before_cursor_execute", self._on_execute)
            self._listening = True

    def reset(self):
        self.count = 0
        self.by_statement.clear()

    def top(self, limit: int = 6) -> list[tuple[str, int]]:
        return self.by_statement.most_common(limit)


QUERIES = QueryCounter()


class CallCounter:
    """Counts and times every outbound HTTP request, grouped by service.

    Two libraries are patched because two are in use: the service connectors
    speak httpx, and the CalDAV client underneath Radicale speaks requests. A
    counter that watched only one would report a sync pass as costing half what
    it does.

    Failures are recorded rather than raised. A 429 from a real service is a
    result, not an accident, and the point of running against real accounts is
    to find out whether one arrives.
    """

    HOSTS = {
        "tasks.googleapis.com": "google",
        "www.googleapis.com": "google",
        "oauth2.googleapis.com": "google",
        "api.todoist.com": "todoist",
        "api.ticktick.com": "ticktick",
    }

    def __init__(self):
        self.calls: Counter[str] = Counter()
        self.seconds: Counter[str] = Counter()
        self.failures: list[str] = []
        self.rate_limited: Counter[str] = Counter()
        self._installed = False

    def _service_for(self, url: str) -> str:
        for host, name in self.HOSTS.items():
            if host in url:
                return name
        if "127.0.0.1" in url or "localhost" in url:
            return "radicale"
        return "other"

    def _record(self, url: str, status: int | None, elapsed: float, error: str | None):
        service = self._service_for(str(url))
        self.calls[service] += 1
        self.seconds[service] += elapsed
        if error:
            self.failures.append(f"{service}: {error}")
        elif status is not None and status >= 400:
            if status == 429:
                self.rate_limited[service] += 1
            self.failures.append(f"{service}: HTTP {status} from {url}")

    def install(self):
        if self._installed:
            return
        self._installed = True

        httpx_request = httpx.Client.request
        requests_request = requests.Session.request

        def wrapped_httpx(client, method, url, *args, **kwargs):
            started = time.perf_counter()
            try:
                response = httpx_request(client, method, url, *args, **kwargs)
            except Exception as exc:
                self._record(url, None, time.perf_counter() - started, repr(exc))
                raise
            self._record(url, response.status_code, time.perf_counter() - started, None)
            return response

        def wrapped_requests(session, method, url, *args, **kwargs):
            started = time.perf_counter()
            try:
                response = requests_request(session, method, url, *args, **kwargs)
            except Exception as exc:
                self._record(url, None, time.perf_counter() - started, repr(exc))
                raise
            self._record(url, response.status_code, time.perf_counter() - started, None)
            return response

        httpx.Client.request = wrapped_httpx
        requests.Session.request = wrapped_requests

    def snapshot(self) -> dict[str, int]:
        return dict(self.calls)

    def reset(self):
        self.calls.clear()
        self.seconds.clear()


CALLS = CallCounter()


class WarningCollector(logging.Handler):
    """Keeps every warning and error the application logged during the run.

    A sync pass that reports success while logging forty warnings has not
    succeeded in any sense the user would recognise, and those warnings are
    invisible in a summary that only counts items.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        try:
            self.records.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:  # noqa: BLE001 - a broken log line must not stop the run
            pass


WARNINGS = WarningCollector()


def rss_kb() -> int:
    """Resident memory of this process, from the kernel rather than from Python.

    ``tracemalloc`` measures what Python allocated; this measures what the
    operating system is actually holding, which includes the interpreter, the
    parsers and every buffer libraries keep out of Python's sight.
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


@dataclass
class PassMetrics:
    """What one sync pass cost."""

    index: int
    seconds: float
    cpu: float
    queries: int
    retained_bytes: int
    live_objects: int
    rss_kb: int
    pulled: int = 0
    pushed: int = 0
    errors: int = 0
    calls: dict[str, int] = field(default_factory=dict)

    @property
    def cpu_percent(self) -> float:
        return 100.0 * self.cpu / self.seconds if self.seconds else 0.0

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())


def run_pass(index: int) -> PassMetrics:
    """One full sync of every enabled group, measured."""
    gc.collect()
    QUERIES.reset()
    CALLS.reset()
    before_snapshot = tracemalloc.take_snapshot()
    cpu_before = cpu_seconds()
    started = time.perf_counter()

    with session_scope() as session:
        run = SyncEngine(session).run_sync(trigger="stress-live")
        pulled, pushed, errors = run.items_pulled, run.items_pushed, run.errors

    elapsed = time.perf_counter() - started
    cpu_used = cpu_seconds() - cpu_before
    gc.collect()
    after_snapshot = tracemalloc.take_snapshot()
    retained = sum(
        stat.size_diff for stat in after_snapshot.compare_to(before_snapshot, "filename")
    )

    return PassMetrics(
        index=index,
        seconds=elapsed,
        cpu=cpu_used,
        queries=QUERIES.count,
        retained_bytes=retained,
        live_objects=len(gc.get_objects()),
        rss_kb=rss_kb(),
        pulled=pulled,
        pushed=pushed,
        errors=errors,
        calls=CALLS.snapshot(),
    )


def print_table(metrics: list[PassMetrics]) -> None:
    print(f"\n  {'pass':<5}{'secs':>7}{'cpu s':>7}{'cpu%':>6}{'pulled':>8}{'pushed':>8}"
          f"{'errs':>6}{'calls':>7}{'queries':>9}{'retained':>11}{'objects':>9}{'rss MB':>9}")
    for m in metrics:
        print(f"  {m.index:<5}{m.seconds:>7.1f}{m.cpu:>7.1f}{m.cpu_percent:>5.0f}%"
              f"{m.pulled:>8}{m.pushed:>8}{m.errors:>6}{m.total_calls:>7}{m.queries:>9}"
              f"{m.retained_bytes/1024:>10.0f}K{m.live_objects:>9}{m.rss_kb/1024:>9.1f}")


def analyse(title: str, metrics: list[PassMetrics], item_count: int,
            expect_quiet: bool) -> list[str]:
    """Turn the per-pass numbers into findings."""
    problems: list[str] = []
    if not metrics:
        return problems

    if expect_quiet:
        noisy = [m for m in metrics if m.pushed]
        if noisy:
            problems.append(
                f"{title}: still writing with nothing changed — passes "
                f"{[m.index for m in noisy]} pushed {[m.pushed for m in noisy]}. A "
                "settled system must reach zero, or it rewrites every item for ever."
            )

    failed = [m for m in metrics if m.errors]
    if failed:
        problems.append(
            f"{title}: {sum(m.errors for m in failed)} error(s) recorded across "
            f"passes {[m.index for m in failed]}."
        )

    if len(metrics) >= 6:
        early = statistics.mean(m.live_objects for m in metrics[1:4])
        late = statistics.mean(m.live_objects for m in metrics[-3:])
        if late - early > 2000:
            problems.append(
                f"{title}: live object count grew by {late - early:.0f} across "
                "passes with no new data, which is what a leak looks like."
            )

        early_rss = statistics.mean(m.rss_kb for m in metrics[1:4])
        late_rss = statistics.mean(m.rss_kb for m in metrics[-3:])
        growth_mb = (late_rss - early_rss) / 1024
        print(f"\n  resident memory: {early_rss/1024:.1f} MB early, "
              f"{late_rss/1024:.1f} MB late  ({growth_mb:+.1f} MB)")
        if growth_mb > 25:
            problems.append(
                f"{title}: resident memory grew {growth_mb:.0f} MB across passes "
                "with no new data."
            )

        early_q = statistics.mean(m.queries for m in metrics[1:4])
        late_q = statistics.mean(m.queries for m in metrics[-3:])
        if late_q > early_q * 1.5 and late_q - early_q > 20:
            problems.append(
                f"{title}: queries per pass rose from {early_q:.0f} to {late_q:.0f} "
                "with no new data."
            )

    if item_count:
        per_item = statistics.mean(m.queries for m in metrics[-3:]) / item_count
        print(f"  queries per item per pass: {per_item:.2f}"
              f"   ({'fine' if per_item < 12 else 'HIGH — look for an N+1'})")
        if per_item >= 12:
            for statement, times in QUERIES.top():
                print(f"    {times:>5}x  {statement[:110]}")
            problems.append(
                f"{title}: {per_item:.1f} queries per item per pass suggests a "
                "query inside a per-item loop."
            )

    cpu_per_item = statistics.mean(m.cpu for m in metrics[-3:]) / (item_count or 1)
    print(f"  CPU per item per pass: {cpu_per_item*1000:.1f} ms")

    return problems


# --- Building the throwaway containers ----------------------------------------


def connector_for(session, service: ServiceKind, slot: int = 1):
    account = (
        session.query(Account)
        .filter(Account.service == service, Account.slot == slot)
        .one_or_none()
    )
    if account is None:
        return None, None
    return account, build_connector(session, account)


def create_containers(session) -> dict:
    """Make one throwaway list, project or calendar in each connected service.

    Each is registered for teardown before it is used. Creating a container per
    service, rather than writing into an existing one, is what makes the cleanup
    a single delete per service instead of four hundred deletes that each have
    their own chance of failing.
    """
    made: dict = {}
    name = f"TaskHub Stress {RUN_ID}"

    # -- Radicale: the lossless source ----------------------------------------
    account, connector = connector_for(session, ServiceKind.RADICALE)
    if connector is None:
        raise SystemExit("No Radicale account: this harness needs one as its source.")
    client = connector.client
    for key, collection_id, kind in (
        ("radicale_tasks", f"stress-tasks-{RUN_ID}", CollectionKind.TASKS),
        ("radicale_calendar", f"stress-cal-{RUN_ID}", CollectionKind.CALENDAR),
    ):
        registry_add({"service": "radicale", "kind": "collection", "id": collection_id})
        client.create_collection(collection_id, f"{name} {kind.value}", kind)
        made[key] = (account.id, collection_id, kind)
        print(f"  radicale   {kind.value:9} collection {collection_id}")

    # -- Google: a task list and a calendar of its own -------------------------
    account, connector = connector_for(session, ServiceKind.GOOGLE)
    if connector is not None:
        from app.connectors.google import CALENDAR_API, TASKS_API

        created = connector._request(
            "POST", f"{TASKS_API}/users/@me/lists", json={"title": name}
        ) or {}
        list_id = created.get("id", "")
        registry_add({"service": "google", "kind": "tasklist", "id": list_id})
        made["google_tasks"] = (account.id, list_id, CollectionKind.TASKS)
        print(f"  google     tasks     list {list_id}")

        created = connector._request(
            "POST", f"{CALENDAR_API}/calendars", json={"summary": name}
        ) or {}
        calendar_id = created.get("id", "")
        registry_add({"service": "google", "kind": "calendar", "id": calendar_id})
        made["google_calendar"] = (account.id, calendar_id, CollectionKind.CALENDAR)
        print(f"  google     calendar  {calendar_id}")

    # -- Todoist ---------------------------------------------------------------
    account, connector = connector_for(session, ServiceKind.TODOIST)
    if connector is not None:
        created = connector._request("POST", "/projects", json={"name": name}) or {}
        project_id = created.get("id", "")
        registry_add({"service": "todoist", "kind": "project", "id": project_id})
        made["todoist"] = (account.id, project_id, CollectionKind.TASKS)
        print(f"  todoist    tasks     project {project_id}")

    # -- TickTick --------------------------------------------------------------
    account, connector = connector_for(session, ServiceKind.TICKTICK)
    if connector is not None:
        created = connector._request("POST", "/project", json={"name": name}) or {}
        project_id = created.get("id", "")
        registry_add({"service": "ticktick", "kind": "project", "id": project_id})
        made["ticktick"] = (account.id, project_id, CollectionKind.TASKS)
        print(f"  ticktick   tasks     project {project_id}")

    # -- Apple: a calendar and a task list of its own in iCloud ---------------
    #
    # iCloud accepts MKCALENDAR for both component types, verified by creating
    # one of each and deleting them again, so Apple gets throwaway containers
    # like everybody else rather than being pointed at somebody's real
    # calendars. The account's own Reminders may well be upgraded and invisible
    # to CalDAV; that does not affect a list created here, which is CalDAV by
    # construction.
    account, connector = connector_for(session, ServiceKind.APPLE)
    if connector is not None:
        principal = connector._connect()
        for key, kind, components in (
            ("apple_tasks", CollectionKind.TASKS, ["VTODO"]),
            ("apple_calendar", CollectionKind.CALENDAR, ["VEVENT"]),
        ):
            try:
                collection = principal.make_calendar(
                    name=f"{name} {kind.value}",
                    supported_calendar_component_set=components,
                )
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                print(f"  apple      could not make a {kind.value} collection: {exc}")
                continue
            url = str(collection.url)
            registry_add({"service": "apple", "kind": "collection", "id": url})
            made[key] = (account.id, url, kind)
            print(f"  apple      {kind.value:9} {url}")

    # -- Obsidian: an existing folder, read only, nothing created --------------
    #
    # No container is made here and none is registered for teardown, because
    # nothing is created: the folder already exists and this run only reads it.
    # See VAULT_FOLDER for why a vault cannot be seeded from outside Obsidian.
    account = (
        session.query(Account)
        .filter(Account.service == ServiceKind.OBSIDIAN, Account.slot == 2)
        .one_or_none()
    )
    if account is not None:
        made["obsidian"] = (account.id, VAULT_FOLDER, CollectionKind.TASKS)
        print(f"  obsidian   tasks     {VAULT_FOLDER} (read only, existing)")

    return made


def wire_groups(session, made: dict) -> tuple[int, int]:
    """Put every container into one tasks group and one calendar group."""
    tasks_group = SyncGroup(name=f"Stress tasks {RUN_ID}",
                            kind=CollectionKind.TASKS, enabled=True)
    calendar_group = SyncGroup(name=f"Stress calendar {RUN_ID}",
                               kind=CollectionKind.CALENDAR, enabled=True)
    session.add_all([tasks_group, calendar_group])
    session.commit()

    for key, value in made.items():
        account_id, remote_id, kind = value
        row = RemoteListRow(
            account_id=account_id, remote_id=remote_id,
            name=f"stress {key}", kind=kind,
        )
        session.add(row)
        session.flush()
        group = tasks_group if kind == CollectionKind.TASKS else calendar_group
        # Obsidian alone is read-only here. Everything it could write is a tick
        # in somebody's real notes, and no measurement is worth that.
        session.add(ListMapping(
            remote_list_id=row.id, sync_group_id=group.id,
            read_enabled=True, write_enabled=key != "obsidian",
        ))
    session.commit()
    return tasks_group.id, calendar_group.id


# --- Seeding ------------------------------------------------------------------


def seed_caldav(client: RadicaleClient, collection_id: str) -> None:
    """Tasks that span several days, which is where date handling goes wrong."""
    today = dt.date.today()
    for n in range(TASK_COUNT):
        start = today + dt.timedelta(days=n % 7)
        client.save_record(collection_id, CanonicalRecord(
            uid=f"stress-{RUN_ID}-task-{n}",
            kind=CollectionKind.TASKS,
            title=f"Stress task {n}",
            notes=f"Runs from {start} for {2 + n % 5} days. {MARKER}",
            start_date=start,
            start_time=dt.time(9, 0),
            start_tz="Europe/London",
            due_date=start + dt.timedelta(days=2 + n % 5),
            due_time=dt.time(17, 30),
            due_tz="Europe/London",
            priority=(n % 4) + 1,
            tags=[f"tag{n % 3}", "stress"],
            location=f"Room {n % 10}",
        ))
        if n % 25 == 0:
            print(f"    seeded {n} tasks", flush=True)


def seed_events(client: RadicaleClient, collection_id: str) -> None:
    """Events crossing midnight and all-day spans, which come back wrong first."""
    today = dt.date.today()
    for n in range(EVENT_COUNT):
        start = today + dt.timedelta(days=n % 10)
        if n % 3 == 0:
            record = CanonicalRecord(
                uid=f"stress-{RUN_ID}-event-{n}", kind=CollectionKind.CALENDAR,
                title=f"All-day span {n}", start_date=start,
                end_date=start + dt.timedelta(days=1 + n % 4),
                location="Offsite", tags=["stress"], notes=MARKER,
            )
        else:
            record = CanonicalRecord(
                uid=f"stress-{RUN_ID}-event-{n}", kind=CollectionKind.CALENDAR,
                title=f"Overnight event {n}", notes=f"Crosses midnight. {MARKER}",
                start_date=start, start_time=dt.time(22, 0), start_tz="Europe/London",
                end_date=start + dt.timedelta(days=1 + n % 3),
                end_time=dt.time(6, 30), end_tz="Europe/London",
                location=f"Venue {n}", tags=["stress", "overnight"],
            )
        client.save_record(collection_id, record)
        if n % 25 == 0:
            print(f"    seeded {n} events", flush=True)


# --- Editing and deleting through the real connectors -------------------------


def links_by_account(session, group_id: int, limit: int) -> dict[int, list[tuple[str, str]]]:
    """A few (remote_list_id, remote_id) pairs per account, to edit or delete."""
    result: dict[int, list[tuple[str, str]]] = {}
    rows = (
        session.query(ItemLink, RemoteListRow)
        .join(RemoteListRow, ItemLink.remote_list_id == RemoteListRow.id)
        .filter(ItemLink.sync_group_id == group_id)
        .all()
    )
    for link, remote_list in rows:
        bucket = result.setdefault(link.account_id, [])
        if len(bucket) < limit:
            bucket.append((remote_list.remote_id, link.remote_id))
    return result


def edit_from_every_service(session, group_id: int, round_number: int) -> int:
    """Change the same tasks from every service in quick succession.

    This is the scenario that breaks naive engines: the edits are close enough
    together that modification times cannot order them, so the engine has to
    rely on its own provenance instead. Against real services it also exercises
    each one's update path under load, which the in-memory harness cannot.
    """
    edited = 0
    for account_id, targets in links_by_account(session, group_id, limit=3).items():
        account = session.get(Account, account_id)
        try:
            connector = build_connector(session, account)
        except ConnectorError as exc:
            print(f"    {account.service.value}: cannot build connector ({exc})")
            continue
        caps = connector.capabilities(CollectionKind.TASKS)
        if not caps.push_fields() - {"status"}:
            continue  # Obsidian and anything else that may only tick a box.
        for remote_list_id, remote_id in targets:
            record = CanonicalRecord(
                uid="", kind=CollectionKind.TASKS,
                title=f"Edited by {account.service.value} round {round_number}",
                notes=f"Touched at {time.time_ns()} {MARKER}",
                priority=(round_number % 4) + 1,
            )
            try:
                connector.update(remote_list_id, remote_id, record, CollectionKind.TASKS)
                edited += 1
            except Exception as exc:  # noqa: BLE001 - recorded, not fatal
                print(f"    {account.service.value} update failed: {exc}")
    return edited


def delete_from_source(client: RadicaleClient, collection_id: str,
                       kind: CollectionKind, count: int) -> list[str]:
    """Remove items from the source the way a person deleting them would."""
    removed = []
    prefix = "task" if kind == CollectionKind.TASKS else "event"
    for n in range(count):
        uid = f"stress-{RUN_ID}-{prefix}-{n}"
        if client.delete_record(collection_id, uid, kind):
            removed.append(uid)
    return removed


# --- Integrity ----------------------------------------------------------------


def check_integrity(session) -> list[str]:
    """States no correct sync should ever produce."""
    faults: list[str] = []

    orphans = (
        session.query(ItemLink)
        .filter(~ItemLink.item_id.in_(session.query(Item.id)))
        .count()
    )
    if orphans:
        faults.append(f"{orphans} link(s) point at items that no longer exist.")

    for item in session.query(Item).all():
        if not (item.title or "").strip():
            faults.append(f"item {item.id} has lost its title.")
        if item.start_date and item.end_date and item.end_date < item.start_date:
            faults.append(
                f"item {item.id} ends ({item.end_date}) before it starts "
                f"({item.start_date})."
            )
        if (item.start_date and item.start_date == item.end_date
                and item.start_time and item.end_time
                and item.end_time < item.start_time):
            faults.append(
                f"item {item.id} ends at {item.end_time}, before its start at "
                f"{item.start_time}, on the same day."
            )

    duplicates = (
        session.query(Item.uid, Item.sync_group_id)
        .group_by(Item.uid, Item.sync_group_id)
        .having(__import__("sqlalchemy").func.count(Item.id) > 1)
        .count()
    )
    if duplicates:
        faults.append(f"{duplicates} canonical uid(s) appear more than once in a group.")

    return faults


def spread(session, group_id: int) -> str:
    """How many items each account holds in a group, as one line."""
    counts: Counter[str] = Counter()
    rows = session.query(ItemLink).filter(ItemLink.sync_group_id == group_id).all()
    for link in rows:
        account = session.get(Account, link.account_id)
        counts[account.service.value if account else "?"] += 1
    return "  ".join(f"{name} {count}" for name, count in sorted(counts.items()))


# --- Teardown -----------------------------------------------------------------


def teardown(verbose: bool = True) -> list[str]:
    """Delete every container the registry names, whatever else happened.

    Deliberately reads the registry rather than any in-memory state, so it works
    identically whether the run finished, crashed or was killed. Each delete is
    attempted independently: one service being unreachable must not leave the
    others' containers behind.
    """
    entries = registry_load()
    if not entries:
        if verbose:
            print("Nothing recorded to remove.")
        return []

    failures: list[str] = []
    with session_scope() as session:
        for entry in entries:
            service, kind, identifier = entry["service"], entry["kind"], entry["id"]
            if not identifier:
                continue
            try:
                if service == "radicale":
                    _, connector = connector_for(session, ServiceKind.RADICALE)
                    connector.client.delete_collection(identifier)
                elif service == "google":
                    from app.connectors.google import CALENDAR_API, TASKS_API

                    _, connector = connector_for(session, ServiceKind.GOOGLE)
                    if kind == "tasklist":
                        connector._request(
                            "DELETE", f"{TASKS_API}/users/@me/lists/{identifier}")
                    else:
                        connector._request(
                            "DELETE", f"{CALENDAR_API}/calendars/{identifier}")
                elif service == "todoist":
                    _, connector = connector_for(session, ServiceKind.TODOIST)
                    connector._request("DELETE", f"/projects/{identifier}")
                elif service == "ticktick":
                    _, connector = connector_for(session, ServiceKind.TICKTICK)
                    connector._request("DELETE", f"/project/{identifier}")
                elif service == "apple":
                    # Through the connector's own handle rather than a fresh
                    # caldav.Calendar: it follows iCloud's discovery to the
                    # numbered shard the collection actually lives on, which a
                    # client left at the sign-in host cannot address at all.
                    _, apple = connector_for(session, ServiceKind.APPLE)
                    apple._calendar(identifier).delete()
                elif service == "obsidian":
                    shutil.rmtree(identifier, ignore_errors=True)
                if verbose:
                    print(f"  removed {service} {kind} {identifier}")
            except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                failures.append(f"{service} {kind} {identifier}: {exc}")
                if verbose:
                    print(f"  FAILED  {service} {kind} {identifier}: {exc}")

    if not failures:
        REGISTRY_PATH.unlink(missing_ok=True)
    return failures


def verify_clean(verbose: bool = True) -> list[str]:
    """Ask each service what it holds, and confirm nothing of ours is left.

    Teardown reporting success only means the delete requests were accepted.
    This asks the services themselves, which is the only answer that settles
    whether the accounts are actually clean.
    """
    leftovers: list[str] = []
    with session_scope() as session:
        for service in (ServiceKind.RADICALE, ServiceKind.GOOGLE,
                        ServiceKind.TODOIST, ServiceKind.TICKTICK,
                        ServiceKind.APPLE):
            try:
                _, connector = connector_for(session, service)
                if connector is None:
                    continue
                found = [
                    remote for remote in connector.list_remote_lists()
                    if "TaskHub Stress" in remote.name or remote.remote_id.startswith("stress-")
                ]
                if found:
                    for remote in found:
                        leftovers.append(
                            f"{service.value}: {remote.name!r} ({remote.remote_id})")
                elif verbose:
                    print(f"  {service.value:10} clean")
            except Exception as exc:  # noqa: BLE001
                leftovers.append(f"{service.value}: could not be checked ({exc})")

    if verbose and leftovers:
        print("  STILL PRESENT:")
        for line in leftovers:
            print(f"    {line}")
    return leftovers


# --- Driver -------------------------------------------------------------------


def main() -> int:
    if "--teardown" in sys.argv:
        print("Teardown only, from the registry")
        print("=" * 78)
        failures = teardown()
        print("\nVerifying with each service that nothing is left behind")
        leftovers = verify_clean()
        return 1 if failures or leftovers else 0

    print("Task Hub live stress run")
    print("=" * 78)
    print(f"run id {RUN_ID}   data dir {DATA_DIR}   "
          f"{TASK_COUNT} tasks, {EVENT_COUNT} events, seeded into CalDAV; "
          f"Obsidian joins read-only")

    logging.getLogger().addHandler(WARNINGS)
    tracemalloc.start()
    QUERIES.start()
    CALLS.install()

    # There is no CalDAV account to create: the copy already has the real one.
    engine_module.ensure_radicale_account = lambda session: None

    problems: list[str] = []
    made: dict = {}

    try:
        print("\nConnected accounts in the copied database:")
        with session_scope() as session:
            for account in session.query(Account).order_by(Account.id).all():
                usable = bool(decrypt_json(account.credentials))
                print(f"  {account.id} {account.service.value:10} slot {account.slot} "
                      f"{account.status.value:10} {account.label!r}"
                      f"{'' if usable else '   CREDENTIALS UNREADABLE'}")
                if not usable:
                    problems.append(
                        f"account {account.id} ({account.service.value}) could not be "
                        "decrypted; it took no part in this run."
                    )

        print("\n" + "-" * 78)
        print("Creating throwaway containers (recorded for teardown before use)")
        with session_scope() as session:
            made = create_containers(session)
            tasks_group, calendar_group = wire_groups(session, made)

        print("\n" + "-" * 78)
        print("Seeding")
        with session_scope() as session:
            _, radicale = connector_for(session, ServiceKind.RADICALE)
            client = radicale.client
            started = time.perf_counter()
            seed_caldav(client, made["radicale_tasks"][1])
            seed_events(client, made["radicale_calendar"][1])
            print(f"  CalDAV seeding took {time.perf_counter() - started:.1f}s")

        # -- Phase 1: the fan-out ---------------------------------------------
        print("\n" + "-" * 78)
        print("Phase 1 — everything propagates outward to every service")
        phase1: list[PassMetrics] = []
        for index in range(1, 7):
            metrics = run_pass(index)
            phase1.append(metrics)
            print(f"    pass {index}: {metrics.seconds:.0f}s  pulled {metrics.pulled}  "
                  f"pushed {metrics.pushed}  errors {metrics.errors}", flush=True)
            if index >= 2 and metrics.pushed == 0:
                break
        print_table(phase1)
        with session_scope() as session:
            item_count = session.query(Item).count()
            link_count = session.query(ItemLink).count()
            print(f"\n  canonical items: {item_count}   links: {link_count}")
            print(f"  tasks group:    {spread(session, tasks_group)}")
            print(f"  calendar group: {spread(session, calendar_group)}")
            problems += check_integrity(session)
        problems += analyse("Phase 1", phase1, item_count, expect_quiet=False)

        # -- Phase 2: does it go quiet? ---------------------------------------
        print("\n" + "-" * 78)
        print("Phase 2 — nothing changes; the system must settle and stay settled")
        phase2 = []
        for index in range(1, 7):
            phase2.append(run_pass(index))
            print(f"    pass {index}: {phase2[-1].seconds:.0f}s  "
                  f"pushed {phase2[-1].pushed}", flush=True)
        print_table(phase2)
        problems += analyse("Phase 2", phase2, item_count, expect_quiet=True)
        with session_scope() as session:
            problems += check_integrity(session)

        # -- Phase 3: everyone edits at once ----------------------------------
        print("\n" + "-" * 78)
        print("Phase 3 — every service edits the same tasks, then a sync reconciles")
        phase3 = []
        for round_number in range(1, 5):
            with session_scope() as session:
                edited = edit_from_every_service(session, tasks_group, round_number)
            metrics = run_pass(round_number)
            phase3.append(metrics)
            print(f"    round {round_number}: {edited} concurrent edits, "
                  f"{metrics.pushed} pushes, {metrics.errors} errors", flush=True)
        print_table(phase3)
        early = statistics.mean(m.pushed for m in phase3[:2])
        late = statistics.mean(m.pushed for m in phase3[-2:])
        print(f"\n  pushes per round: {early:.0f} early, {late:.0f} late")
        if late > early * 2 and late - early > 20:
            problems.append(
                f"Concurrent edits: pushes per round grew from {early:.0f} to "
                f"{late:.0f} for the same number of edits — an echo storm."
            )
        with session_scope() as session:
            problems += check_integrity(session)

        # -- Phase 4: deletion must propagate and stay deleted ----------------
        print("\n" + "-" * 78)
        print("Phase 4 — deletions from the source must not come back")
        # A minority on purpose. The engine treats a collection that comes back
        # empty as a temporary failure rather than a mass deletion, which is
        # correct and protective, and deleting everything would test that guard
        # instead of testing deletion.
        to_delete_tasks = max(1, min(10, TASK_COUNT // 10))
        to_delete_events = max(1, min(10, EVENT_COUNT // 10))
        with session_scope() as session:
            _, radicale = connector_for(session, ServiceKind.RADICALE)
            removed_tasks = delete_from_source(
                radicale.client, made["radicale_tasks"][1],
                CollectionKind.TASKS, to_delete_tasks)
            removed_events = delete_from_source(
                radicale.client, made["radicale_calendar"][1],
                CollectionKind.CALENDAR, to_delete_events)
        print(f"  deleted {len(removed_tasks)} tasks and {len(removed_events)} events "
              "at the source")

        phase4 = [run_pass(i) for i in range(1, 4)]
        print_table(phase4)
        with session_scope() as session:
            after = session.query(Item).count()
            print(f"\n  canonical items: {item_count} -> {after}")
            expected = item_count - len(removed_tasks) - len(removed_events)
            if after > expected:
                problems.append(
                    f"Phase 4: {after} items remain where {expected} were expected; "
                    "either the deletion did not propagate or something recreated it."
                )
            survivors = (
                session.query(Item)
                .filter(Item.uid.in_(removed_tasks + removed_events))
                .count()
            )
            if survivors:
                problems.append(f"Phase 4: {survivors} deleted item(s) came back.")
            problems += check_integrity(session)

        # -- Phase 5: what a real vault costs to read -------------------------
        print("\n" + "-" * 78)
        print("Phase 5 — reading the whole Work Vault, once, without writing to it")
        with session_scope() as session:
            account = (
                session.query(Account)
                .filter(Account.service == ServiceKind.OBSIDIAN, Account.slot == 1)
                .one_or_none()
            )
            if account is None:
                print("  no Work Vault account; skipped")
            else:
                connector = build_connector(session, account)
                started = time.perf_counter()
                cpu_before = cpu_seconds()
                try:
                    result = connector.pull("vault:", CollectionKind.TASKS)
                    print(f"  {len(result.items)} tasks read in "
                          f"{time.perf_counter() - started:.1f}s, "
                          f"{cpu_seconds() - cpu_before:.1f}s CPU")
                except Exception as exc:  # noqa: BLE001
                    print(f"  could not read the vault: {exc}")
                    problems.append(f"Work Vault read failed: {exc}")

        # -- Errors seen along the way ----------------------------------------
        print("\n" + "-" * 78)
        print("What the services said")
        for service, count in sorted(CALLS.calls.items()):
            print(f"  {service:10} {count} calls in the last pass")
        if CALLS.rate_limited:
            print(f"  rate limited: {dict(CALLS.rate_limited)}")
        if CALLS.failures:
            print(f"\n  {len(CALLS.failures)} failed request(s); first 15:")
            for line in CALLS.failures[:15]:
                print(f"    {line}")

        with session_scope() as session:
            entries = (
                session.query(SyncLogEntry)
                .order_by(SyncLogEntry.id.desc())
                .limit(400)
                .all()
            )
            failed = [e for e in entries if (e.level or "").lower() in ("error", "warning")]
            if failed:
                print(f"\n  {len(failed)} error/warning log entries; first 15:")
                for entry in failed[:15]:
                    print(f"    {entry.level}: {entry.message}")

        if WARNINGS.records:
            counted = Counter(r.split(":")[0] + ": " + r.split(":", 2)[-1][:80]
                              for r in WARNINGS.records)
            print(f"\n  {len(WARNINGS.records)} logged warnings/errors, "
                  f"{len(counted)} distinct; most frequent:")
            for text, count in counted.most_common(10):
                print(f"    {count:>4}x  {text}")

    except Exception as exc:  # noqa: BLE001 - report, then always clean up
        import traceback

        print("\nThe run stopped with an exception:")
        traceback.print_exc()
        problems.append(f"the run did not finish: {exc!r}")

    finally:
        print("\n" + "=" * 78)
        print("Teardown — removing every container this run created")
        failures = teardown()
        if failures:
            problems.append(
                f"{len(failures)} container(s) could not be removed: {failures}"
            )
        print("\nVerifying with each service that nothing is left behind")
        leftovers = verify_clean()
        if leftovers:
            problems.append(
                f"{len(leftovers)} stress container(s) still present after "
                f"teardown: {leftovers}"
            )

    current, peak = tracemalloc.get_traced_memory()
    print(f"\nPeak traced memory {peak/1024/1024:.1f} MB, still held "
          f"{current/1024/1024:.1f} MB, resident {rss_kb()/1024:.1f} MB")
    tracemalloc.stop()

    print("\n" + "=" * 78)
    if problems:
        print(f"{len(problems)} PROBLEM(S) FOUND\n")
        for problem in problems:
            print(f"  * {problem}")
        return 1

    print("No faults found: propagated to every service, settled to zero writes,")
    print("no leak, no invalid dates, no duplicates, no orphaned links, deletions")
    print("stayed deleted, and every container was removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
