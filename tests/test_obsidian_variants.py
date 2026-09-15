"""Every variant of both Obsidian task formats, vetted.

Written after a sweep over the real shapes both plugins produce, including the
awkward ones: metadata in the middle of a line, a mistyped year, a time of day
the inline format cannot hold, an offset on a TaskNotes date, and checklist
lines inside code blocks. Each case here is one that was either wrong once or is
close enough to a wrong one to be worth pinning.
"""

from __future__ import annotations

import sys

from app.connectors.obsidian import ObsidianConnector
from app.db.models import CollectionKind, ItemStatus
from app.services.obsidian_md import (
    TaskNotesConfig,
    is_task,
    load_tasknotes_config,
    parse_frontmatter,
    parse_line,
    stable_id,
    tasknote_to_record,
    to_record,
)

GF = "#todo"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def inline(line: str, gf: str = GF):
    task = parse_line(line, 0)
    if task is None or not is_task(task, gf):
        return None
    return to_record(task, uid="", vault_name="V", relative_path="N.md",
                     global_filter=gf)


print("Inline: statuses")

for char, expected in ((" ", ItemStatus.NEEDS_ACTION), ("x", ItemStatus.COMPLETED),
                       ("X", ItemStatus.COMPLETED), ("/", ItemStatus.IN_PROCESS),
                       ("-", ItemStatus.CANCELLED)):
    r = inline(f"- [{char}] #todo Thing 📅 2026-09-12")
    check(f"[{char}] maps to {expected.value}", r is not None and r.status == expected)

for char in ("?", "b", ">"):
    r = inline(f"- [{char}] #todo Thing 📅 2026-09-12")
    check(f"a custom status [{char}] is never read as done",
          r is not None and r.status != ItemStatus.COMPLETED)

print("\nInline: bullets and nesting")

for prefix in ("- ", "* ", "+ ", "1. ", "1) ", "  - ", "\t- "):
    check(f"bullet {prefix!r} is recognised",
          inline(f"{prefix}[ ] #todo Thing 📅 2026-09-12") is not None)

print("\nInline: priorities on iCalendar's scale")

for emoji, expected in (("🔺", 1), ("⏫", 3), ("🔼", 4), ("🔽", 6), ("⏬", 9)):
    r = inline(f"- [ ] #todo Thing {emoji} 📅 2026-09-12")
    check(f"{emoji} is {expected}", r is not None and r.priority == expected,
          str(r.priority if r else None))
check("no emoji means no priority",
      inline("- [ ] #todo Thing 📅 2026-09-12").priority == 0)
check("a priority emoji glued to a word is still read",
      inline("- [ ] #todo Thing⏫ 📅 2026-09-12").priority == 3)

print("\nInline: where the metadata sits")

check("metadata before the description still parses",
      str(inline("- [ ] #todo 📅 2026-09-12 Thing").due_date) == "2026-09-12")
check("and the description survives it",
      inline("- [ ] #todo 📅 2026-09-12 Thing").title == "Thing")
check("a trailing tag does not become part of the date",
      str(inline("- [ ] #todo Thing 📅 2026-09-12 #home").due_date) == "2026-09-12")
check("the trailing tag is kept as a tag",
      inline("- [ ] #todo Thing 📅 2026-09-12 #home").tags == ["home"])
check("the first of two due dates wins",
      str(inline("- [ ] #todo Thing 📅 2026-09-12 📅 2026-10-01").due_date)
      == "2026-09-12")

print("\nInline: dataview syntax is read as well as emoji")

check("dataview due", str(inline("- [ ] #todo Thing [due:: 2026-09-12]").due_date)
      == "2026-09-12")
check("dataview in parentheses",
      str(inline("- [ ] #todo Thing (due:: 2026-09-12)").due_date) == "2026-09-12")
check("dataview priority word", inline("- [ ] #todo Thing [priority:: high]").priority == 3)
check("dataview completion",
      inline("- [x] #todo Thing [completion:: 2026-09-13]").completed_at is not None)

print("\nInline: the description is left alone")

for label, line, expected in (
    ("a wikilink", "- [ ] #todo Call [[Bob Smith]] 📅 2026-09-12", "Call [[Bob Smith]]"),
    ("an alias", "- [ ] #todo Call [[Bob|B]] 📅 2026-09-12", "Call [[Bob|B]]"),
    ("bold", "- [ ] #todo **Urgent** thing 📅 2026-09-12", "**Urgent** thing"),
    ("inline code", "- [ ] #todo Run `make test` 📅 2026-09-12", "Run `make test`"),
    ("a footnote", "- [ ] #todo Thing[^1] 📅 2026-09-12", "Thing[^1]"),
    ("an emoji", "- [ ] #todo 🎂 Birthday 📅 2026-09-12", "🎂 Birthday"),
    ("a url with a fragment", "- [ ] #todo See https://x.com/a#b 📅 2026-09-12",
     "See https://x.com/a#b"),
):
    check(f"{label} survives", inline(line).title == expected, inline(line).title)

print("\nInline: malformed input loses as little as possible")

r = inline("- [ ] #todo Thing 📅 20206-09-18 more text")
check("a mistyped year does not swallow the description",
      r.title == "Thing more text", r.title)
check("and no date is invented from it", r.due_date is None)

for bad in ("09/12/2026", "2026-02-31", "tomorrow", ""):
    r = inline(f"- [ ] #todo Thing 📅 {bad}")
    check(f"an unreadable date {bad!r} leaves the title intact",
          r.title == "Thing", r.title)
    check(f"and invents no deadline from {bad!r}", r.due_date is None)

r = inline("- [ ] #todo Thing 📅 2026-09-19T14:35")
check("a time on an inline date is dropped, not left in the title",
      r.title == "Thing" and str(r.due_date) == "2026-09-19", r.title)

check("a line with no checkbox is not a task", parse_line("- Just a bullet", 0) is None)
check("a carriage return does not break parsing",
      inline("- [ ] #todo Thing 📅 2026-09-12\r").title == "Thing")

print("\nInline: without a global filter, a line must declare itself")

check("a plain checkbox is never a task", inline("- [ ] Buy milk", gf="") is None)
check("a due date makes it one", inline("- [ ] Buy milk 📅 2026-09-12", gf="") is not None)
check("a priority makes it one", inline("- [ ] Buy milk ⏫", gf="") is not None)
check("an id alone does not", inline("- [ ] Buy milk 🆔 abc", gf="") is None)
check("a dependency alone does not", inline("- [ ] Buy milk ⛔ abc", gf="") is None)
check("a created date alone does not",
      inline("- [ ] Buy milk ➕ 2026-09-01", gf="") is None)

print("\nInline: identity")

a = parse_line("- [ ] #todo Thing 📅 2026-09-12", 0)
b = parse_line("- [ ] #todo Thing 📅 2026-10-01", 0)
c = parse_line("- [x] #todo Thing 📅 2026-09-12", 0)
d = parse_line("- [ ] #todo Different 📅 2026-09-12", 0)
check("changing the due date keeps the identity",
      stable_id("N.md", a) == stable_id("N.md", b))
check("ticking it off keeps the identity",
      stable_id("N.md", a) == stable_id("N.md", c))
check("changing the words does not", stable_id("N.md", a) != stable_id("N.md", d))
check("the same words in another note do not collide",
      stable_id("A.md", a) != stable_id("B.md", a))

print("\nA checklist line inside a code block is an example, not a task")


class _Reader(ObsidianConnector):
    """Only _tasks_in is exercised, which never touches the filesystem."""


reader = _Reader(account_id=1, credentials={"name": "V"})
DOC = "\n".join([
    "- [ ] #todo A real task 📅 2026-09-12",
    "",
    "```markdown",
    "- [ ] #todo An example from the docs 📅 2026-09-13",
    "```",
    "",
    "~~~text",
    "- [ ] #todo A tilde-fenced example 📅 2026-09-14",
    "~~~",
    "",
    "- [ ] #todo Another real task 📅 2026-09-15",
])
titles = [i.record.title for i in
          reader._tasks_in(DOC, "Doc.md", GF, TaskNotesConfig(), [])]
check("the real tasks are read", titles == ["A real task", "Another real task"], str(titles))
check("neither fenced example is", not any("example" in t for t in titles), str(titles))

print("\nAn empty task is reported rather than silently named")

warnings: list[str] = []
reader._tasks_in("- [ ] #todo 📅 2026-09-12", "N.md", GF, TaskNotesConfig(), warnings)
check("a task with no description produces a warning",
      any("no description" in w for w in warnings), str(warnings))

# ------------------------------------------------------------------ TaskNotes

CFG = load_tasknotes_config({
    "taskTag": "tasks",
    "taskIdentificationMethod": "tag",
    "customStatuses": [
        {"value": "none", "isCompleted": False},
        {"value": "open", "isCompleted": False},
        {"value": "in-progress", "isCompleted": False},
        {"value": "done", "isCompleted": True},
    ],
    "customPriorities": [
        {"value": "none", "weight": 0}, {"value": "low", "weight": 1},
        {"value": "normal", "weight": 2}, {"value": "high", "weight": 3},
    ],
})


def note(front_yaml: str, path="TaskNotes/Tasks/A Task.md", cfg=CFG):
    front, _ = parse_frontmatter(f"---\n{front_yaml}\n---\n\nBody.\n")
    if not front:
        return None
    return tasknote_to_record(front, uid="", vault_name="V",
                              relative_path=path, config=cfg)


print("\nTaskNotes: a due time keeps its zone, or stays floating")

check("a floating time stays floating",
      (lambda r: str(r.due_time) == "14:35:00" and r.due_tz is None)(
          note("status: open\ndue: '2026-09-19T14:35'\ntags: [tasks]")))

r = note("status: open\ndue: '2026-09-19T14:35:00-06:00'\ntags: [tasks]")
check("an offset is converted to UTC rather than discarded",
      str(r.due_time) == "20:35:00" and r.due_tz == "UTC",
      f"{r.due_time} {r.due_tz}")

r = note("status: open\ndue: '2026-09-19T14:35:00+10:00'\ntags: [tasks]")
check("an eastern offset too", str(r.due_time) == "04:35:00" and r.due_tz == "UTC",
      f"{r.due_time} {r.due_tz}")

r = note("status: open\ndue: '2026-09-19T14:35:00Z'\ntags: [tasks]")
check("a Z suffix is understood", str(r.due_time) == "14:35:00" and r.due_tz == "UTC",
      f"{r.due_time} {r.due_tz}")

check("a date with no time gets no invented midnight",
      note("status: open\ndue: 2026-09-18\ntags: [tasks]").due_time is None)
check("an unreadable date invents nothing",
      note("status: open\ndue: not-a-date\ntags: [tasks]").due_date is None)

print("\nTaskNotes: scheduled is not a start date")

check("scheduled alone produces no start date",
      note("status: open\nscheduled: 2026-09-15\ntags: [tasks]").start_date is None)

print("\nTaskNotes: the title")

check("with no title field the filename is used",
      note("status: open\ntags: [tasks]").title == "A Task")
check("a title field wins when present",
      note("status: open\ntitle: Another\ntags: [tasks]").title == "Another")
check("an empty title field falls back to the filename",
      note("status: open\ntitle: ''\ntags: [tasks]").title == "A Task")
check("a deep path uses only the file's own name",
      note("status: open\ntags: [tasks]", path="A/B/C/Deep One.md").title == "Deep One")

print("\nTaskNotes: malformed frontmatter is survivable")

check("broken YAML yields no task, rather than an error",
      note("status: open\n  bad: [unclosed\ntags: [tasks]") is None)

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All Obsidian variant tests passed.")
