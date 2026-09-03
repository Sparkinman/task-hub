"""Tests for the capability-aware merge engine.

These run without a network connection and without credentials, because the
merge rules are the part that must be right regardless of which services happen
to be connected. The first test is the one this whole design exists for.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.connectors.base import (
    ALL_FIELDS, F_DUE_DATE, F_DUE_TIME, F_NOTES, F_PRIORITY, F_STATUS, F_TITLE,
    Capabilities, RemoteItem,
)
from app.db.models import CollectionKind, ItemStatus, ServiceKind
from app.services.ical_model import CanonicalRecord
from app.sync.merge import (
    Provenance, baseline_fingerprints, content_hash, merge_remote, project,
)

UTC = dt.timezone.utc

# Google Tasks: keeps a date, discards the time, no priority, no tags.
GOOGLE_TASKS_CAPS = Capabilities(
    fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE})
)
# Todoist: keeps a real due time and priorities.
TODOIST_CAPS = Capabilities(
    fields=frozenset({F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_DUE_TIME, F_PRIORITY})
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def timed_task() -> CanonicalRecord:
    """A task due 5 March 2026 at 14:30 New York time."""
    return CanonicalRecord(
        uid="task-1",
        kind=CollectionKind.TASKS,
        title="Call the dentist",
        notes="Ask about the 3pm slot",
        status=ItemStatus.NEEDS_ACTION,
        due_date=dt.date(2026, 3, 5),
        due_time=dt.time(14, 30),
        due_tz="America/New_York",
        priority=1,
    )


def remote(record, caps, updated_at, deleted=False):
    return RemoteItem(
        remote_id="r1",
        record=record,
        fields_present=caps.present_fields(),
        remote_updated_at=updated_at,
    )


# --- The central requirement --------------------------------------------------

print("\nGoogle Tasks must not destroy a time it cannot store")

canonical = timed_task()
prov = {F_DUE_TIME: Provenance(ServiceKind.TODOIST, dt.datetime(2026, 3, 1, tzinfo=UTC))}

# Google reports the SAME date it already had. Nothing should change at all.
google_view = CanonicalRecord(
    uid="task-1", kind=CollectionKind.TASKS, title="Call the dentist",
    notes="Ask about the 3pm slot", due_date=dt.date(2026, 3, 5),
)
result = merge_remote(canonical, remote(google_view, GOOGLE_TASKS_CAPS,
                                        dt.datetime(2026, 3, 2, tzinfo=UTC)),
                      ServiceKind.GOOGLE, prov)
check("unchanged date from Google changes nothing", not result.changed, str(result.changes))
check("time survives an unchanged sync", canonical.due_time == dt.time(14, 30))

# Now the real case: the user moves the date in Google to the 7th.
google_view.due_date = dt.date(2026, 3, 7)
result = merge_remote(canonical, remote(google_view, GOOGLE_TASKS_CAPS,
                                        dt.datetime(2026, 3, 3, tzinfo=UTC)),
                      ServiceKind.GOOGLE, prov)
check("date edit from Google is applied", canonical.due_date == dt.date(2026, 3, 7))
check("TIME SET IN TODOIST SURVIVES A GOOGLE DATE EDIT",
      canonical.due_time == dt.time(14, 30), f"got {canonical.due_time}")
check("timezone survives too", canonical.due_tz == "America/New_York")
check("priority Google cannot see is untouched", canonical.priority == 1)

# Clearing the date in Google must clear the orphaned time as well.
google_view.due_date = None
merge_remote(canonical, remote(google_view, GOOGLE_TASKS_CAPS,
                               dt.datetime(2026, 3, 4, tzinfo=UTC)),
             ServiceKind.GOOGLE, prov)
check("clearing the date clears the orphaned time",
      canonical.due_date is None and canonical.due_time is None)


# --- Per-field conflict resolution -------------------------------------------

print("\nEdits to different fields in different services both survive")

canonical = timed_task()
prov = {}

# What Task Hub last pushed to Google -- including the note as it then stood.
google_baseline = baseline_fingerprints(canonical, GOOGLE_TASKS_CAPS)

# The user edits the note in Todoist at 10:00.
todoist_view = CanonicalRecord(uid="task-1", kind=CollectionKind.TASKS,
                               title="Call the dentist", notes="Ask for a morning slot",
                               due_date=dt.date(2026, 3, 5), due_time=dt.time(14, 30),
                               due_tz="America/New_York", priority=1)
merge_remote(canonical, remote(todoist_view, TODOIST_CAPS,
                               dt.datetime(2026, 3, 6, 10, tzinfo=UTC)),
             ServiceKind.TODOIST, prov,
             baseline=baseline_fingerprints(timed_task(), TODOIST_CAPS))

# At 11:00 the user moves the date in Google. Google stamps the WHOLE item as
# modified, and reports the old note back -- the one we pushed it earlier.
google_view = CanonicalRecord(uid="task-1", kind=CollectionKind.TASKS,
                              title="Call the dentist", notes="Ask about the 3pm slot",
                              due_date=dt.date(2026, 3, 9))
merge_remote(canonical, remote(google_view, GOOGLE_TASKS_CAPS,
                               dt.datetime(2026, 3, 6, 11, tzinfo=UTC)),
             ServiceKind.GOOGLE, prov, baseline=google_baseline)

check("newer note from Todoist is not clobbered by Google's stale echo",
      canonical.notes == "Ask for a morning slot", f"got {canonical.notes!r}")
check("date edited in Google survived", canonical.due_date == dt.date(2026, 3, 9))
check("time still intact after both edits", canonical.due_time == dt.time(14, 30))

# A genuine note edit in Google -- differing from the baseline -- must still land.
google_view.notes = "Reschedule entirely"
merge_remote(canonical, remote(google_view, GOOGLE_TASKS_CAPS,
                               dt.datetime(2026, 3, 6, 12, tzinfo=UTC)),
             ServiceKind.GOOGLE, prov, baseline=google_baseline)
check("a genuine note edit in Google IS applied",
      canonical.notes == "Reschedule entirely", f"got {canonical.notes!r}")


print("\nSame field edited in two services: the newer edit wins")

canonical = timed_task()
prov = {F_TITLE: Provenance(ServiceKind.TODOIST, dt.datetime(2026, 3, 8, tzinfo=UTC))}
stale = CanonicalRecord(uid="task-1", kind=CollectionKind.TASKS, title="Old title")
result = merge_remote(canonical, remote(stale, GOOGLE_TASKS_CAPS,
                                        dt.datetime(2026, 3, 7, tzinfo=UTC)),
                      ServiceKind.GOOGLE, prov)
check("older incoming edit is rejected", canonical.title == "Call the dentist")
check("the disagreement is recorded", F_TITLE in result.conflicts)

fresh = CanonicalRecord(uid="task-1", kind=CollectionKind.TASKS, title="New title")
merge_remote(canonical, remote(fresh, GOOGLE_TASKS_CAPS,
                               dt.datetime(2026, 3, 9, tzinfo=UTC)),
             ServiceKind.GOOGLE, prov)
check("newer incoming edit is accepted", canonical.title == "New title")


# --- Completion ---------------------------------------------------------------

print("\nCompletion is sticky-forward")

canonical = timed_task()
prov = {}
done = CanonicalRecord(uid="task-1", kind=CollectionKind.TASKS, title="Call the dentist",
                       status=ItemStatus.COMPLETED,
                       completed_at=dt.datetime(2026, 3, 6, tzinfo=UTC))
merge_remote(canonical, remote(done, GOOGLE_TASKS_CAPS,
                               dt.datetime(2026, 3, 6, tzinfo=UTC)),
             ServiceKind.GOOGLE, prov)
check("completion propagates", canonical.status == ItemStatus.COMPLETED)

# A service that has not caught up still reports it open, with an older stamp.
stale_open = CanonicalRecord(uid="task-1", kind=CollectionKind.TASKS,
                             title="Call the dentist", status=ItemStatus.NEEDS_ACTION)
merge_remote(canonical, remote(stale_open, TODOIST_CAPS,
                               dt.datetime(2026, 3, 5, tzinfo=UTC)),
             ServiceKind.TODOIST, prov)
check("a stale service cannot resurrect a completed task",
      canonical.status == ItemStatus.COMPLETED)

# A genuine, newer un-tick must be honoured.
real_reopen = CanonicalRecord(uid="task-1", kind=CollectionKind.TASKS,
                              title="Call the dentist", status=ItemStatus.NEEDS_ACTION)
merge_remote(canonical, remote(real_reopen, TODOIST_CAPS,
                               dt.datetime(2026, 3, 8, tzinfo=UTC)),
             ServiceKind.TODOIST, prov)
check("a genuine newer un-tick is honoured", canonical.status == ItemStatus.NEEDS_ACTION)
check("completed_at is cleared on reopen", canonical.completed_at is None)


# --- Projection and hashing ---------------------------------------------------

print("\nProjection and no-op suppression")

canonical = timed_task()
projected = project(canonical, GOOGLE_TASKS_CAPS)
check("Google projection keeps the date", projected.due_date == dt.date(2026, 3, 5))
check("Google projection drops the time", projected.due_time is None)
check("Google projection drops priority", projected.priority == 0)
check("the original is not mutated by projection", canonical.due_time == dt.time(14, 30))

h1 = content_hash(canonical, GOOGLE_TASKS_CAPS)
canonical.priority = 9
canonical.tags = ["errand"]
h2 = content_hash(canonical, GOOGLE_TASKS_CAPS)
check("changing a field Google cannot see does NOT force a rewrite", h1 == h2)

h3 = content_hash(canonical, TODOIST_CAPS)
canonical.priority = 3
h4 = content_hash(canonical, TODOIST_CAPS)
check("changing priority DOES force a rewrite to Todoist", h3 != h4)

canonical.due_time = dt.time(16, 0)
check("changing only the time does not force a rewrite to Google",
      content_hash(canonical, GOOGLE_TASKS_CAPS) == h2)


print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All merge tests passed.")
