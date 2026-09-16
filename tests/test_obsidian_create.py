"""Tests for writing a task from another service into an Obsidian vault.

Creation is the one write that does not touch a line somebody else wrote, but it
has a failure mode the completion patch does not: a line that cannot be read
back afterwards is not merely lost. The next pass would find the task missing
from the vault and write it again, once per pass, for ever. So most of what is
checked here is that everything ``record_to_line`` can emit survives
``parse_line`` and still counts as a task.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

from app.connectors.obsidian import ObsidianConnector
from app.db.models import CollectionKind, ItemStatus
from app.services.ical_model import CanonicalRecord
from app.services.obsidian_md import (
    is_task,
    new_block_id,
    parse_line,
    record_to_line,
    stable_id,
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def record(**kwargs) -> CanonicalRecord:
    base = dict(uid="u1", kind=CollectionKind.TASKS, title="Buy milk")
    base.update(kwargs)
    return CanonicalRecord(**base)


def emitted(rec: CanonicalRecord, global_filter: str = "") -> str:
    return record_to_line(rec, block_id=new_block_id(), global_filter=global_filter)


print("Every emitted line reads back as a task")

cases = {
    "bare title, no fields at all": record(),
    "a due date": record(due_date=dt.date(2026, 9, 12)),
    "a start and a due date": record(
        start_date=dt.date(2026, 9, 10), due_date=dt.date(2026, 9, 12)),
    "high priority": record(priority=3),
    "normal priority (5) emits no emoji": record(priority=5),
    "tags": record(tags=["home", "#errand"]),
    "a recurrence rule": record(rrule="FREQ=WEEKLY"),
    "already completed": record(
        status=ItemStatus.COMPLETED,
        completed_at=dt.datetime(2026, 9, 11, tzinfo=dt.timezone.utc)),
    "an empty title": record(title=""),
}

for name, rec in cases.items():
    line = emitted(rec)
    parsed = parse_line(line, 0)
    ok = parsed is not None and is_task(parsed, "")
    check(f"{name}", ok, repr(line))

print("\nThe bare case is the one that needs the block reference")

bare = emitted(record())
parsed = parse_line(bare, 0)
check("a task with no date and no priority is still recognised",
      parsed is not None and is_task(parsed, ""), repr(bare))
check("and it is recognised because of the anchor, not by accident",
      parsed is not None and not is_task(
          parse_line(bare.rsplit(" ^", 1)[0], 0), ""),
      "a line without the anchor should not qualify")

print("\nNothing is invented")

line = emitted(record(priority=5))
check("priority 5 means normal, so no emoji is written",
      not any(e in line for e in ("🔺", "⏫", "🔼", "🔽", "⏬")), repr(line))

line = emitted(record(due_date=dt.date(2026, 9, 12), due_time=dt.time(14, 30)))
check("a time of day is dropped rather than approximated",
      "14:30" not in line and "📅 2026-09-12" in line, repr(line))

line = emitted(record(notes="Some long description"), global_filter="")
check("notes are dropped; there is nowhere on the line for them",
      "Some long description" not in line, repr(line))

print("\nThe vault's own global filter")

line = emitted(record(due_date=dt.date(2026, 9, 12)), global_filter="#task")
parsed = parse_line(line, 0)
check("the filter is written so the user's own queries find the task",
      parsed is not None and is_task(parsed, "#task"), repr(line))

print("\nIdentity survives what a text hash would not")

line = emitted(record(due_date=dt.date(2026, 9, 12)))
first = parse_line(line, 0)
reworded = line.replace("Buy milk", "Buy oat milk instead")
second = parse_line(reworded, 0)
check("rewording the task keeps its identity",
      stable_id("Inbox.md", first) == stable_id("Inbox.md", second))
check("moving the note keeps its identity",
      stable_id("A.md", first) == stable_id("B.md", first))

print("\nWriting into a vault")


with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    vault = tmp / "Test-Vault"
    vault.mkdir(parents=True)

    class Bound(ObsidianConnector):
        """Same connector, told where its vault is rather than looking it up."""

        @property
        def root(self) -> Path:
            return vault

    def build(**credentials):
        creds = {"name": "Test Vault", "sync_level": "full", "create_note": "Inbox.md"}
        creds.update(credentials)
        return Bound(account_id=1, credentials=creds)

    conn = build()
    outcome = conn.create("vault:", record(due_date=dt.date(2026, 9, 12)), CollectionKind.TASKS)
    check("a task is created", outcome.error is None, str(outcome.error))
    inbox = vault / "Inbox.md"
    check("the note was made", inbox.is_file())

    text = inbox.read_text(encoding="utf-8")
    check("the note explains itself", "Task Hub" in text, text[:80])
    check("the task is in it", "Buy milk" in text)

    # The remote id it returns must be the one a pull would produce, or the
    # engine's link points at nothing and the task is written again next pass.
    body_line = [l for l in text.splitlines() if "Buy milk" in l][0]
    parsed = parse_line(body_line, 0)
    expected = f"Inbox.md#{stable_id('Inbox.md', parsed)}"
    check("the id it reports is the id a pull would find",
          outcome.remote_id == expected, f"{outcome.remote_id!r} != {expected!r}")

    before = inbox.read_text(encoding="utf-8")
    conn2 = build()
    conn2.create("vault:", record(uid="u2", title="Second task"), CollectionKind.TASKS)
    after = inbox.read_text(encoding="utf-8")
    check("a second task is appended, leaving the first alone",
          after.startswith(before.rstrip("\n")) and "Second task" in after)

    print("\nRefusals")

    off = build(sync_level="read")
    check("creation is refused when the vault is read-only",
          off.create("vault:", record(), CollectionKind.TASKS).error is not None)
    check("and the capability says so too",
          not off.capabilities(CollectionKind.TASKS).can_create)

    nowhere = build(create_note="")
    check("creation is refused with no note chosen",
          nowhere.create("vault:", record(), CollectionKind.TASKS).error is not None)
    check("and the capability says so too",
          not nowhere.capabilities(CollectionKind.TASKS).can_create)

    check("with both set, creation is offered",
          build().capabilities(CollectionKind.TASKS).can_create)

    folder_chosen = build(create_note="Sync Testing")
    landed = folder_chosen.create("vault:", record(uid="f", title="From a folder"),
                                  CollectionKind.TASKS)
    check("choosing a folder puts tasks in a note Task Hub names",
          landed.remote_id.startswith("Sync Testing/From Task Hub.md#"),
          str(landed.remote_id))
    check("a note path is still honoured as a note path",
          build(create_note="Tasks/Inbox.md").create(
              "vault:", record(uid="g", title="Legacy"), CollectionKind.TASKS
          ).remote_id.startswith("Tasks/Inbox.md#"))

    outside = build(create_note="../escape.md")
    check("a note outside the vault is refused",
          outside.create("vault:", record(), CollectionKind.TASKS).error is not None)

    walled = build(create_note="Work/Inbox.md", write_folders=["folder:Personal"])
    check("a note outside the allowed folders is refused",
          walled.create("vault:", record(), CollectionKind.TASKS).error is not None)

    capped = build()
    capped._written = 999
    check("the per-pass cap applies to creation as well",
          capped.create("vault:", record(), CollectionKind.TASKS).error is not None)

    print("\nDeleting is still refused")
    check("delete refuses even with write-back on",
          build().delete("vault:", "Inbox.md#x", CollectionKind.TASKS).error is not None)
    check("and the capability says so",
          not build().capabilities(CollectionKind.TASKS).can_delete)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All Obsidian creation tests passed.")
