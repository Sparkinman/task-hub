"""Parent and child tasks, and the rule that stops a flat service destroying them.

A parent link is not like the other fields. Every other one is a *value* -- a
due date, a priority -- and the worst a service can do with a value it cannot
hold is fail to carry it. A parent link is a *relationship*, and a service that
cannot express one can do something much worse: report the task as having no
parent, which does not merely fail to carry the hierarchy but destroys it, in
Radicale, for every other connected service at once.

That is not hypothetical on this project. Sending a parent and its children to a
list that cannot nest produces unrelated tasks there; letting that come home is
the same shape of damage as the fan-out duplication that once turned six tasks
into twenty.

So the interesting tests here are the negative ones: what a service is *not*
allowed to say. The Supernote figures throughout because its to-do API is the
proven case -- a real task row was read from a live account and carries no
parent field of any kind.
"""

from __future__ import annotations

import sys

from icalendar import Todo

from app.connectors.base import ALL_FIELDS, Capabilities
from app.db.models import Item, ItemStatus
from app.services.ical_model import CanonicalRecord, record_to_component
from app.services.ical_model import _parent_of  # noqa: PLC2701
from app.sync.engine import SyncEngine

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def todo_with(*related: tuple[str, str | None]) -> Todo:
    """A VTODO carrying the given (uid, reltype) RELATED-TO lines."""
    component = Todo()
    component.add("UID", "child-1")
    component.add("SUMMARY", "Book flights")
    for uid, reltype in related:
        from icalendar import vText

        value = vText(uid)
        if reltype is not None:
            value.params["RELTYPE"] = reltype
        component.add("RELATED-TO", value)
    return component


FLAT = Capabilities(fields=ALL_FIELDS, supports_parent=False)
NESTS = Capabilities(fields=ALL_FIELDS, supports_parent=True)


print("\nA parent link survives a trip through iCalendar")
record = CanonicalRecord(uid="child-1", title="Book flights", parent_uid="parent-1")
component = record_to_component(record)
ical = component.to_ical().decode()
check("RELATED-TO is written", "RELATED-TO" in ical, ical)
check("with RELTYPE=PARENT", "RELTYPE=PARENT" in ical, ical)
check("naming the parent", "parent-1" in ical, ical)
check("and reads back as the same parent", _parent_of(component) == "parent-1",
      str(_parent_of(component)))

print("\nA task with no parent says nothing at all")
# An empty RELATED-TO would be read by other clients as a relationship to the
# empty string, which is worse than silence.
plain = record_to_component(CanonicalRecord(uid="solo", title="Buy milk"))
check("no RELATED-TO is emitted", "RELATED-TO" not in plain.to_ical().decode())
check("and it reads back as top level", _parent_of(plain) is None)

print("\nRELATED-TO is not always a parent")
# PARENT, CHILD and SIBLING are all legal on the same component. Taking the
# first one would hang tasks off their own siblings.
check("a CHILD relationship is not mistaken for a parent",
      _parent_of(todo_with(("other-1", "CHILD"))) is None)
check("a SIBLING relationship is not either",
      _parent_of(todo_with(("other-1", "SIBLING"))) is None)
check("the parent is found among several relationships",
      _parent_of(todo_with(("kid-1", "CHILD"), ("mum-1", "PARENT"),
                           ("bro-1", "SIBLING"))) == "mum-1")
# RFC 5545 says an absent RELTYPE means PARENT, so ignoring those would drop
# hierarchy written by any client that leaves the default implicit.
check("an absent RELTYPE defaults to PARENT",
      _parent_of(todo_with(("mum-1", None))) == "mum-1")
check("lowercase is handled",
      _parent_of(todo_with(("mum-1", "parent"))) == "mum-1")
check("an empty value is ignored rather than stored",
      _parent_of(todo_with(("", "PARENT"))) is None)


print("\nA service that cannot nest may not set a parent")
item = Item(uid="child-1", title="Book flights", status=ItemStatus.NEEDS_ACTION)
item.parent_uid = None
SyncEngine.apply_parent(item, CanonicalRecord(uid="child-1", parent_uid="invented"), FLAT)
check("a flat service's claim of a parent is ignored", item.parent_uid is None,
      str(item.parent_uid))

print("\nAnd -- the one that matters -- may not clear one either")
# The failure this whole rule exists for. Send a parent and its children to a
# flat list, and they arrive as unrelated tasks; when that list reports back,
# every child looks parentless. Believing it destroys the hierarchy in Radicale
# and, from there, everywhere.
for reported in (None, "", "   "):
    item = Item(uid="child-1", title="Book flights", status=ItemStatus.NEEDS_ACTION)
    item.parent_uid = "parent-1"
    SyncEngine.apply_parent(
        item, CanonicalRecord(uid="child-1", parent_uid=reported), FLAT)
    check(f"a flat service reporting {reported!r} does not orphan the task",
          item.parent_uid == "parent-1", str(item.parent_uid))

print("\nA service that can nest is believed, in both directions")
item = Item(uid="child-1", title="Book flights", status=ItemStatus.NEEDS_ACTION)
item.parent_uid = None
SyncEngine.apply_parent(item, CanonicalRecord(uid="child-1", parent_uid="parent-1"), NESTS)
check("it can set a parent", item.parent_uid == "parent-1", str(item.parent_uid))
# For these, a missing parent genuinely means the task was moved to top level.
SyncEngine.apply_parent(item, CanonicalRecord(uid="child-1", parent_uid=None), NESTS)
check("and it can clear one", item.parent_uid is None, str(item.parent_uid))

print("\nThe capability is off unless a connector says otherwise")
# New connectors must opt in. Defaulting the other way would mean any connector
# written later silently gained the power to flatten hierarchies.
check("the default is no parent support",
      Capabilities(fields=ALL_FIELDS).supports_parent is False)
check("parenthood is not one of the value fields",
      "parent_uid" not in ALL_FIELDS and "parent" not in ALL_FIELDS,
      str(sorted(ALL_FIELDS)))

print("\nThe services that may nest, and the ones that may not")
from app.connectors.base import FULL_CAPABILITIES  # noqa: E402
from app.connectors.caldav_remote import CALDAV_CAPABILITIES  # noqa: E402
from app.connectors.supernote import SupernoteConnector  # noqa: E402
from app.db.models import CollectionKind  # noqa: E402

check("Radicale, which is the hub, may", FULL_CAPABILITIES.supports_parent)
check("CalDAV, which stores RELATED-TO, may", CALDAV_CAPABILITIES.supports_parent)
# Verified against a live account: a Supernote task row carries taskId,
# taskListId, title, detail, status, dueTime, completedTime, importance,
# recurrence, links and sort keys. There is no parent field of any kind.
supernote = SupernoteConnector.__new__(SupernoteConnector)
check("Supernote, whose tasks are flat, may not",
      not supernote.capabilities(CollectionKind.TASKS).supports_parent)

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll subtask tests passed.")
