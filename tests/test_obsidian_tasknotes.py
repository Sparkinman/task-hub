"""Tests for reading a TaskNotes vault, against the shapes a real one produces.

Written from actual files created by the plugin rather than from its
documentation: no `title` field (the filename is the title), `scheduled` filled
in by default on every task, and containment expressed as a wikilink in
`projects` rather than by any parent field.
"""

from __future__ import annotations

import sys

from app.db.models import ItemStatus
from app.services.obsidian_md import (
    is_tasknote,
    load_tasknotes_config,
    parse_frontmatter,
    tasknote_project_links,
    tasknote_to_record,
    wikilink_target,
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


# The settings blob as the plugin actually writes it, trimmed to what is read.
SETTINGS = {
    "taskTag": "tasks",
    "taskIdentificationMethod": "tag",
    "tasksFolder": "TaskNotes/Tasks",
    "taskFilenameFormat": "zettel",
    "defaultTaskStatus": "open",
    "defaultTaskPriority": "normal",
    "fieldMapping": {
        "title": "title", "status": "status", "priority": "priority",
        "due": "due", "scheduled": "scheduled", "contexts": "contexts",
        "projects": "projects", "completedDate": "completedDate",
    },
    "customStatuses": [
        {"value": "none", "isCompleted": False},
        {"value": "open", "isCompleted": False},
        {"value": "in-progress", "isCompleted": False},
        {"value": "done", "isCompleted": True},
    ],
    "customPriorities": [
        {"value": "none", "weight": 0},
        {"value": "low", "weight": 1},
        {"value": "normal", "weight": 2},
        {"value": "high", "weight": 3},
    ],
}

CONFIG = load_tasknotes_config(SETTINGS)

SUBTASK = """---
status: open
priority: normal
scheduled: 2026-09-15
projects:
  - "[[A bUnch of blocked tasks]]"
dateCreated: 2026-09-15T16:01:17.943-06:00
tags:
  - tasks
  - task
---
"""

TIMED = """---
status: open
priority: normal
due: 2026-09-19T14:35
scheduled: 2026-09-15
tags:
  - tasks
---
"""

print("The vault's own vocabulary is read, not guessed")

check("the task tag is read", CONFIG.task_tag == "tasks", CONFIG.task_tag)
check("identification method is read", CONFIG.identify_by == "tag")
check("a status the plugin flags as completed is completed",
      CONFIG.status_for("done") == ItemStatus.COMPLETED)
check("a status it does not flag is not",
      CONFIG.status_for("open") == ItemStatus.NEEDS_ACTION)
check("an unknown status is treated as open, never as done",
      CONFIG.status_for("shipped") == ItemStatus.NEEDS_ACTION)
check("the tasks folder is read", CONFIG.tasks_folder == "TaskNotes/Tasks")

print("\nWriting back uses the vault's words, not ours")

check("open uses the vault's default rather than 'none'",
      CONFIG.value_for_status(ItemStatus.NEEDS_ACTION) == "open",
      CONFIG.value_for_status(ItemStatus.NEEDS_ACTION))
check("completed uses the vault's own word",
      CONFIG.value_for_status(ItemStatus.COMPLETED) == "done")
check("an unset priority writes nothing",
      CONFIG.value_for_priority(0) == "")

print("\nA task note, as the plugin really writes one")

front, _body = parse_frontmatter(TIMED)
check("it is recognised as a task", is_tasknote(front, CONFIG))
record = tasknote_to_record(
    front, uid="", vault_name="V",
    relative_path="TaskNotes/Tasks/tasknote with due date and time.md",
    config=CONFIG)
check("with no title field, the filename is the title",
      record.title == "tasknote with due date and time", record.title)
check("a due date carries its time of day",
      (record.due_date.isoformat(), str(record.due_time)) == ("2026-09-19", "14:35:00"),
      f"{record.due_date} {record.due_time}")

print("\nContainment is a link in projects, and only when it is a link")

front, _body = parse_frontmatter(SUBTASK)
check("the link target is found",
      tasknote_project_links(front, CONFIG) == ["A bUnch of blocked tasks"],
      str(tasknote_project_links(front, CONFIG)))
record = tasknote_to_record(
    front, uid="", vault_name="V",
    relative_path="TaskNotes/Tasks/Blocked subtask.md", config=CONFIG)
check("the parent's name does not also become a tag on the child",
      "A bUnch of blocked tasks" not in record.tags, str(record.tags))
check("other tags survive", "task" in record.tags, str(record.tags))

check("a plain word in projects is a label, not a parent",
      tasknote_project_links({"projects": ["Kitchen"]}, CONFIG) == [])
check("a plain word in projects is still a tag",
      "Kitchen" in tasknote_to_record(
          {"status": "open", "projects": ["Kitchen"], "tags": ["tasks"]},
          uid="", vault_name="V", relative_path="N.md", config=CONFIG).tags)

check("a display alias is dropped from a link",
      wikilink_target("[[Real Note|shown as this]]") == "Real Note")
check("a heading is dropped from a link",
      wikilink_target("[[Real Note#Section]]") == "Real Note")

print("\nA vault that identifies tasks by property rather than tag")

by_property = load_tasknotes_config({
    "taskIdentificationMethod": "property",
    "taskPropertyName": "type",
    "taskPropertyValue": "task",
})
check("a note with the property is a task",
      is_tasknote({"type": "task"}, by_property))
check("a note without it is not",
      not is_tasknote({"type": "reference"}, by_property))
check("and the tag is not consulted in that vault",
      not is_tasknote({"tags": ["tasks"]}, by_property))

print("\nWriting a task note")

import datetime as dt
import shutil
import tempfile
from pathlib import Path

from app.connectors.obsidian import ObsidianConnector
from app.db.models import CollectionKind
from app.services.ical_model import CanonicalRecord
from app.services.obsidian_md import record_to_tasknote, tasknote_filename

check("a filename is made from the title",
      tasknote_filename("Order the Lutron keypads") == "Order the Lutron keypads")
check("characters no filesystem takes are replaced",
      "/" not in tasknote_filename("Invoice 3/4") and
      ":" not in tasknote_filename("Call: Bob"))
check("link syntax is kept out of the name",
      "[" not in tasknote_filename("Read [[Some Note]]"))
check("an empty title still yields a name",
      tasknote_filename("   ") == "Untitled task")
check("a very long title is cut to something a filesystem will take",
      len(tasknote_filename("x" * 400)) <= 120)

written = record_to_tasknote(
    CanonicalRecord(uid="u", kind=CollectionKind.TASKS, title="A task",
                    due_date=dt.date(2026, 9, 22), due_time=dt.time(14, 35),
                    priority=3, tags=["work"]),
    config=CONFIG)
check("the vault's own status word is written", "status: open" in written, written)
check("the vault's own priority word is written", "priority: high" in written, written)
check("a due time is written with the date", "due: 2026-09-22T14:35" in written, written)
check("the task tag is written so the plugin sees it",
      "  - tasks" in written, written)
check("scheduled is never written, because it is never read",
      "scheduled:" not in written, written)

utc = record_to_tasknote(
    CanonicalRecord(uid="u", kind=CollectionKind.TASKS, title="A task",
                    due_date=dt.date(2026, 9, 22), due_time=dt.time(20, 35),
                    due_tz="UTC"),
    config=CONFIG)
check("a zoned time is written as UTC rather than as a floating one",
      "due: 2026-09-22T20:35Z" in utc, utc)

floating = record_to_tasknote(
    CanonicalRecord(uid="u", kind=CollectionKind.TASKS, title="A task",
                    due_date=dt.date(2026, 9, 22), due_time=dt.time(20, 35)),
    config=CONFIG)
check("a floating time is never given a zone it did not have",
      "due: 2026-09-22T20:35\n" in floating, floating)

print("\nWriting one into a vault, and reading it back")

raw = tempfile.mkdtemp()
try:
    vault = Path(raw) / "V"
    (vault / ".obsidian/plugins/tasknotes").mkdir(parents=True)
    (vault / ".obsidian/plugins/tasknotes/data.json").write_text(
        '{"taskTag": "tasks", "tasksFolder": "TaskNotes/Tasks", '
        '"defaultTaskStatus": "open"}', encoding="utf-8")

    class Bound(ObsidianConnector):
        @property
        def root(self) -> Path:
            return vault

    conn = Bound(account_id=1, credentials={"name": "V", "write_back": True})
    check("with TaskNotes installed, that is the format chosen",
          conn.writes_format() == "tasknotes", conn.writes_format())
    check("and creation needs no destination setting",
          conn.capabilities(CollectionKind.TASKS).can_create)

    parent = conn.create("vault:", CanonicalRecord(
        uid="p", kind=CollectionKind.TASKS, title="Master task",
        due_date=dt.date(2026, 9, 20)), CollectionKind.TASKS)
    check("a task note is created", parent.error is None, str(parent.error))
    check("it lands in the plugin's own tasks folder",
          parent.remote_id == "note:TaskNotes/Tasks/Master task.md", parent.remote_id)

    child = CanonicalRecord(uid="c", kind=CollectionKind.TASKS, title="A child task",
                            notes="With a description.",
                            due_date=dt.date(2026, 9, 21), due_time=dt.time(9, 30))
    child.parent_remote_id = parent.remote_id
    kid = conn.create("vault:", child, CollectionKind.TASKS)
    check("a subtask is created", kid.error is None, str(kid.error))

    back = {i.record.title: i for i in
            conn.pull("vault:", CollectionKind.TASKS, None).items}
    check("both come back", set(back) == {"Master task", "A child task"}, str(set(back)))
    check("the child's parent survives the round trip",
          back["A child task"].record.parent_remote_id == parent.remote_id,
          str(back["A child task"].record.parent_remote_id))
    check("the time of day survives, which inline could not carry",
          str(back["A child task"].record.due_time) == "09:30:00")
    check("the description survives, which inline could not carry either",
          "With a description." in (back["A child task"].record.notes or ""))

    again = conn.create("vault:", CanonicalRecord(
        uid="p2", kind=CollectionKind.TASKS, title="Master task"), CollectionKind.TASKS)
    check("a second task of the same name does not overwrite the first",
          again.remote_id != parent.remote_id, str(again.remote_id))
    check("and the first is still there",
          (vault / "TaskNotes/Tasks/Master task.md").is_file())

    off = Bound(account_id=1, credentials={"name": "V", "write_back": False})
    check("nothing is written with write-back off",
          off.create("vault:", CanonicalRecord(uid="x", kind=CollectionKind.TASKS,
                                               title="No"), CollectionKind.TASKS).error
          is not None)

    forced = Bound(account_id=1, credentials={
        "name": "V", "write_back": True, "create_format": "inline"})
    check("an explicit choice of inline is obeyed even with TaskNotes installed",
          forced.writes_format() == "inline")
    check("and inline then needs a note to write into",
          not forced.capabilities(CollectionKind.TASKS).can_create)
finally:
    shutil.rmtree(raw, ignore_errors=True)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All TaskNotes tests passed.")
