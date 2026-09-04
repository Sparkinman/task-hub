"""Things Cloud's history format, pinned against what a live account returns.

Every payload below is the shape a real Things Cloud account on schema 301 sent
back, and every one of them broke something on the first connection this
connector ever made. It had been written against a description of an older
schema, and the failures were the kind that look like an empty account rather
than a fault:

- **The entity name is versioned.** To-dos arrive as ``Task7``; the connector
  accepted ``Task6``, ``Task``, ``Task2`` and ``Task3``. Signing in worked, the
  lists appeared, and every one of them was empty.
- **Notes are an object, not a string.** ``{"_t": "tx", "v": "the text"}``,
  where the text is in ``v``. Reading it as a string raised an AttributeError.
- **The lists were all the same list.** Inbox, Today and Anytime each returned
  the identical thirty-seven to-dos, because the history stream carries no
  reliable list membership -- so mapping two of them would have imported
  everything twice.

None of that was reachable without an account, which is the argument for the
label this connector carries.
"""

from __future__ import annotations

import sys

from app.connectors.things3 import _TASK_ENTITY, _notes
from app.db.models import CollectionKind, ServiceKind
from app.connectors.things3 import ThingsConnector

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


print("\nTo-dos are recognised whatever schema number Things is on")
for name in ("Task", "Task2", "Task3", "Task6", "Task7", "Task8", "Task12"):
    check(f"{name} is a to-do", bool(_TASK_ENTITY.fullmatch(name)))

print("\nAnd nothing else is swept in with them")
# Both of these arrive in the same stream, and both were in the live account.
for name in ("ChecklistItem3", "Tag4", "Area4", "TaskList", "Tasklike9", ""):
    check(f"{name or '(empty)'} is not a to-do",
          not _TASK_ENTITY.fullmatch(name))

print("\nNotes come back whatever shape they are wrapped in")
# The live shape, from a real account.
check("the object form yields its text",
      _notes({"_t": "tx", "ch": 454839628, "t": 1,
              "v": "Tap the calendar button below."}) == "Tap the calendar button below.")
check("an empty object form is no note",
      _notes({"_t": "tx", "ch": 0, "v": "", "t": 1}) is None)
# The older shape, which some account somewhere may still be on.
check("a plain string still works", _notes("  a note  ") == "a note")
check("an empty string is no note", _notes("   ") is None)
check("nothing is no note", _notes(None) is None)
# Anything unexpected must not become the word "dict" in somebody's task.
check("an unrecognised shape is no note, not a stringified object",
      _notes({"something": "else"}) is None)
check("a number is no note", _notes(12345) is None)

print("\nOnly one list is offered, because only one can be told apart")
connector = ThingsConnector(0, {"email": "e@example.com", "password": "p"})
connector._login = lambda: "history-key"  # type: ignore[method-assign]
lists = connector.list_remote_lists()
check("exactly one list", len(lists) == 1, str([l.name for l in lists]))
check("it is a task list", lists[0].kind == CollectionKind.TASKS)
check("it does not claim to be Inbox, Today or Anytime",
      lists[0].name not in ("Inbox", "Today", "Anytime"), lists[0].name)

print("\nThings is read-only, and says so where the engine looks")
caps = connector.capabilities(CollectionKind.TASKS)
check("it can write nothing", not caps.push_fields(), sorted(caps.push_fields()))
check("it cannot create", not caps.can_create)
check("it cannot delete", not caps.can_delete)
check("so the engine skips it for writes entirely",
      not caps.push_fields() and not caps.can_create and not caps.can_delete)
check("but it still reads the fields Things holds",
      {"title", "notes", "status", "due_date", "tags"} == set(caps.fields),
      sorted(caps.fields))

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All Things parsing tests passed.")
