"""Reading tasks out of Obsidian markdown.

The rule that matters most here is the one about what is *not* a task. A vault
is full of checklists -- shopping lists, packing lists, the steps of a recipe --
and treating those as tasks would push a hundred lines of somebody's groceries
into Todoist and Google. So the first block of tests is about lines that must be
ignored, and it is the block to be most suspicious of if it ever starts passing
too easily.
"""

from __future__ import annotations

import sys

from app.db.models import ItemStatus
from app.services.obsidian_md import (
    is_task, parse_line, source_reference, stable_id, strip_source_reference,
    content_fingerprint, to_record, with_source_reference,
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def task_for(line: str, global_filter: str = ""):
    """Parse a line and say whether it qualifies, in one step."""
    parsed = parse_line(line)
    return parsed, (parsed is not None and is_task(parsed, global_filter))


print("\nA plain checkbox is NOT a task")
for line in (
    "- [ ] milk",
    "- [ ] eggs",
    "    - [ ] pack the charger",
    "* [ ] wash up",
    "1. [ ] preheat the oven to 180",
    "- [x] already ticked, still not a task",
):
    _parsed, qualifies = task_for(line)
    check(f"ignored: {line.strip()[:34]}", not qualifies)

print("\nA line carrying a real task field IS a task")
for line, why in (
    ("- [ ] Renew the passport 📅 2026-09-12", "a due date"),
    ("- [ ] Book the dentist ⏳ 2026-09-14", "a scheduled date"),
    ("- [ ] Start the report 🛫 2026-09-01", "a start date"),
    ("- [ ] Water the plants 🔁 every week", "a recurrence"),
    ("- [ ] Call the bank ⏫", "a priority"),
    ("- [ ] Pay the invoice [due:: 2026-09-30]", "a Dataview due date"),
):
    _parsed, qualifies = task_for(line)
    check(f"counted, because it has {why}", qualifies, line)

print("\nA global filter, where the vault sets one, decides on its own")
# This is the Tasks plugin's own mechanism, so honouring it means Task Hub's
# task list and Obsidian's are the same list.
_p, yes = task_for("- [ ] Renew the passport #task", "#task")
check("a filtered line counts even with no other metadata", yes)

_p, no = task_for("- [ ] Renew the passport 📅 2026-09-12", "#task")
check("AN UNFILTERED LINE DOES NOT COUNT, EVEN WITH A DUE DATE", not no,
      "the filter has to be the only rule, or the two lists disagree")

_p, yes = task_for("- [ ] Renew the passport #task 📅 2026-09-12", "#task")
check("a filtered line with metadata counts", yes)

print("\nThe emoji fields are read correctly")
t = parse_line(
    "- [ ] Renew the passport ➕ 2026-08-01 🛫 2026-09-01 ⏳ 2026-09-05 "
    "📅 2026-09-12 🔺 🔁 every year 🆔 abc123"
)
check("description is just the description", t.description == "Renew the passport",
      repr(t.description))
check("created", t.value("created") == "2026-08-01", t.value("created"))
check("start", t.value("start") == "2026-09-01", t.value("start"))
check("scheduled", t.value("scheduled") == "2026-09-05", t.value("scheduled"))
check("due", t.value("due") == "2026-09-12", t.value("due"))
check("recurrence kept as its own words", t.value("recurrence") == "every year",
      t.value("recurrence"))
check("id", t.value("id") == "abc123", t.value("id"))
check("highest priority maps to iCalendar 1", t.priority == 1, str(t.priority))

print("\nThe six priority levels, including the one above normal")
for emoji, expected, label in (
    ("🔺", 1, "highest"), ("⏫", 3, "high"), ("🔼", 4, "medium"),
    ("🔽", 6, "low"), ("⏬", 9, "lowest"),
):
    p = parse_line(f"- [ ] Something 📅 2026-09-12 {emoji}")
    check(f"{label} -> {expected}", p.priority == expected, str(p.priority))
plain = parse_line("- [ ] Something 📅 2026-09-12")
check("no emoji means no priority, not a middling one", plain.priority == 0,
      str(plain.priority))

print("\nDataview inline fields are read too")
d = parse_line(
    "- [ ] Pay the invoice [due:: 2026-09-30] [priority:: high] "
    "[repeat:: every month] [completion:: 2026-09-29]"
)
check("description", d.description == "Pay the invoice", repr(d.description))
check("due", d.value("due") == "2026-09-30", d.value("due"))
check("priority word maps to the same scale", d.priority == 3, str(d.priority))
check("'repeat' is the recurrence field", d.value("recurrence") == "every month",
      d.value("recurrence"))
check("'completion' is the done field", d.value("done") == "2026-09-29",
      d.value("done"))
check("the vault's syntax is noticed", d.syntax == "dataview", d.syntax)

print("\nSomeone else's inline field is left alone")
other = parse_line("- [ ] Read the paper [author:: Hoare] 📅 2026-09-12")
check("it stays in the description rather than vanishing",
      "author:: Hoare" in other.description, repr(other.description))

print("\nStatus characters")
for char, expected in (
    (" ", ItemStatus.NEEDS_ACTION), ("x", ItemStatus.COMPLETED),
    ("X", ItemStatus.COMPLETED), ("/", ItemStatus.IN_PROCESS),
    ("-", ItemStatus.CANCELLED),
):
    p = parse_line(f"- [{char}] Something 📅 2026-09-12")
    check(f"[{char}] is {expected.value}", p.status == expected, str(p.status))

custom = parse_line("- [?] Something 📅 2026-09-12")
check("a custom status is not done", custom.status == ItemStatus.NEEDS_ACTION,
      str(custom.status))
check("AND THE CHARACTER ITSELF SURVIVES", custom.status_char == "?",
      "a writer must be able to put back exactly what it found")

print("\nThe title is the title, not the plumbing")
r = to_record(
    parse_line("- [ ] Renew the passport #task #admin 📅 2026-09-12 ⏫"),
    uid="u1", vault_name="Notes", relative_path="Admin/Passport.md",
    global_filter="#task",
)
check("the filter is not part of the name", "#task" not in r.title, r.title)
check("nor are the tags", "#admin" not in r.title, r.title)
check("the name survives intact", r.title == "Renew the passport", repr(r.title))
check("the filter is not offered as a tag either", "task" not in r.tags, str(r.tags))
check("but a real tag is", "admin" in r.tags, str(r.tags))

print("\nObsidian has no time of day, and none is invented")
check("the due date is read", str(r.due_date) == "2026-09-12", str(r.due_date))
check("NO TIME IS SET", r.due_time is None, str(r.due_time))
check("no timezone is set", r.due_tz is None, str(r.due_tz))
check("recurrence is not guessed into an RRULE", r.rrule is None, str(r.rrule))

print("\nEvery task says which note it came from")
check("the path is readable in the notes", "Admin/Passport.md" in (r.notes or ""),
      repr(r.notes))
check("and there is a link that opens the note",
      "obsidian://open?vault=Notes&file=Admin%2FPassport.md" in (r.notes or ""),
      repr(r.notes))

print("\nThe reference never doubles up and never eats the user's own notes")
ref = source_reference("Notes", "Admin/Passport.md")
once = with_source_reference("My own thoughts on this", ref)
twice = with_source_reference(once, ref)
check("adding it twice leaves one copy", twice.count("obsidian://") == 1,
      str(twice.count("obsidian://")))
check("THE USER'S OWN TEXT SURVIVES", "My own thoughts on this" in twice)
check("and it can be taken off again cleanly",
      strip_source_reference(twice) == "My own thoughts on this",
      repr(strip_source_reference(twice)))
check("stripping notes that never had one changes nothing",
      strip_source_reference("Just my notes") == "Just my notes")
check("notes that were only a reference come back empty, not blank-ish",
      strip_source_reference(ref) is None, repr(strip_source_reference(ref)))

print("\nIdentity survives the file being edited around the task")
line = "- [ ] Renew the passport 📅 2026-09-12"
first = stable_id("Admin/Passport.md", parse_line(line, line_number=3))
later = stable_id("Admin/Passport.md", parse_line(line, line_number=57))
check("A LINE MOVING DOWN THE FILE KEEPS ITS IDENTITY", first == later,
      "line numbers must not be part of it")

with_block = parse_line("- [ ] Renew the passport 📅 2026-09-12 ^a1b2c3")
check("a block reference is used when the user has one",
      stable_id("Admin/Passport.md", with_block) == "block:a1b2c3",
      stable_id("Admin/Passport.md", with_block))
check("and it is not left in the description",
      with_block.description == "Renew the passport", repr(with_block.description))

print("\nA neighbour being edited is not this task being edited")
# A markdown file has one modification time, so without this every task in a
# daily note looks freshly edited whenever any line in it is touched.
before = content_fingerprint(parse_line("- [ ] Renew the passport 📅 2026-09-12"))
after = content_fingerprint(parse_line("- [ ] Renew the passport 📅 2026-09-12"))
changed = content_fingerprint(parse_line("- [ ] Renew the passport 📅 2026-09-30"))
check("an untouched task fingerprints the same", before == after)
check("a real edit does not", before != changed)

print("\nLines that are not checklist items at all")
for line in ("Just a paragraph", "# A heading", "- a bullet with no box",
             "- [] no space in the brackets", ""):
    check(f"not parsed: {line[:30]!r}", parse_line(line) is None)

# --- TaskNotes ----------------------------------------------------------------

from app.services.obsidian_md import (          # noqa: E402
    TaskNotesConfig, is_tasknote, load_tasknotes_config, parse_frontmatter,
    tasknote_to_record,
)

TASKNOTE = """---
title: Weekly meeting
status: in-progress
due: 2026-09-15T09:30
scheduled: 2026-09-14
priority: high
recurrence: FREQ=WEEKLY;BYDAY=MO
contexts: [work]
tags: [task, meetings]
---
Some notes about the meeting.
"""

print("\nA TaskNotes file is read from its frontmatter")
front, body = parse_frontmatter(TASKNOTE)
check("the frontmatter is found", front.get("title") == "Weekly meeting", str(front))
check("and the body is separated from it",
      body.strip() == "Some notes about the meeting.", repr(body))

n = tasknote_to_record(front, uid="u2", vault_name="Notes",
                       relative_path="Tasks/Weekly meeting.md")
check("title", n.title == "Weekly meeting", n.title)
check("status maps to in-process", n.status == ItemStatus.IN_PROCESS, str(n.status))
check("priority uses the same 1-9 scale as inline tasks", n.priority == 3,
      str(n.priority))

print("\nUnlike an inline task, a TaskNote CAN carry a time of day")
check("the due date is read", str(n.due_date) == "2026-09-15", str(n.due_date))
check("AND SO IS THE TIME", str(n.due_time) == "09:30:00", str(n.due_time))
check("a date with no time gets no invented midnight", n.start_time is None,
      str(n.start_time))
check("its scheduled date is the start", str(n.start_date) == "2026-09-14",
      str(n.start_date))

print("\nTaskNotes recurrence is a real RRULE and passes straight through")
check("carried, not translated", n.rrule == "FREQ=WEEKLY;BYDAY=MO", str(n.rrule))

print("\nAn ordinary note is not a task")
for text in (
    "---\ntitle: Meeting notes\nauthor: Paul\n---\nWhat we discussed.",
    "---\ntags: [reading]\n---\nA book I liked.",
    "Just a note with no frontmatter at all.",
    "---\nstatus: draft\n---\nA blog post I am writing.",
):
    f, _ = parse_frontmatter(text)
    check(f"ignored: {text.splitlines()[1][:30] if len(text.splitlines()) > 1 else text[:30]!r}",
          not is_tasknote(f))

print("\nA vault that marks tasks with a tag uses that tag alone")
tagged = TaskNotesConfig(task_tag="task")
f, _ = parse_frontmatter(TASKNOTE)
check("a tagged note counts", is_tasknote(f, tagged))
check("and the marker tag is not offered as one of the task's own tags",
      "task" not in tasknote_to_record(
          f, uid="u", vault_name="V", relative_path="a.md", config=tagged).tags,
      str(tasknote_to_record(f, uid="u", vault_name="V",
                             relative_path="a.md", config=tagged).tags))

untagged, _ = parse_frontmatter(
    "---\ntitle: Something\nstatus: open\ndue: 2026-09-20\ntags: [notes]\n---\n"
)
check("AN UNTAGGED NOTE DOES NOT COUNT WHEN A TAG IS IN USE",
      not is_tasknote(untagged, tagged),
      "the vault's own rule has to be the only rule")
check("though it would count in a vault with no tag configured",
      is_tasknote(untagged))

print("\nRenamed properties are followed, not guessed")
# TaskNotes lets every property be renamed, so reading the plugin's own settings
# is the difference between seeing a field and silently losing it.
config = load_tasknotes_config({"fieldMapping": {"due": "deadline",
                                                 "status": "state"}})
check("the mapping is picked up", config.key("due") == "deadline", config.key("due"))
check("unmapped fields keep their default", config.key("priority") == "priority")
renamed, _ = parse_frontmatter(
    "---\ntitle: Renamed\nstate: open\ndeadline: 2026-10-01\n---\n"
)
check("a note using the renamed fields is still a task",
      is_tasknote(renamed, config))
rec = tasknote_to_record(renamed, uid="u3", vault_name="Notes",
                         relative_path="Tasks/Renamed.md", config=config)
check("AND ITS DUE DATE IS FOUND", str(rec.due_date) == "2026-10-01",
      str(rec.due_date))

print("\nA broken or hostile note does not stop the vault syncing")
bad, text = parse_frontmatter("---\ntitle: [unclosed\n  bracket\n---\nbody")
check("malformed YAML yields no frontmatter rather than an error", bad == {},
      str(bad))
check("and is therefore not a task", not is_tasknote(bad))
danger, _ = parse_frontmatter(
    "---\n!!python/object/apply:os.system ['echo hi']\n---\n"
)
check("a YAML tag that could execute code is refused", danger == {}, str(danger))

print("\nA TaskNote says where it came from too")
check("the reference is on it",
      "Tasks/Weekly meeting.md" in (n.notes or ""), repr(n.notes))
check("with a link that opens the note",
      "obsidian://open?vault=Notes&file=Tasks%2FWeekly%20meeting.md" in (n.notes or ""),
      repr(n.notes))

print("\nTasks from a vault wear an Obsidian badge, in purple")
from app.db.models import ServiceKind                    # noqa: E402
from app.web import deps                                  # noqa: E402

inline = to_record(parse_line("- [ ] Renew the passport 📅 2026-09-12"),
                   uid="u", vault_name="Notes", relative_path="a.md")
note, _ = parse_frontmatter("---\ntitle: T\nstatus: open\ndue: 2026-09-12\n---\n")
noted = tasknote_to_record(note, uid="u", vault_name="Notes", relative_path="b.md")

check("an inline task reports Obsidian as its origin",
      inline.origin_service == ServiceKind.OBSIDIAN, str(inline.origin_service))
check("and so does a TaskNote",
      noted.origin_service == ServiceKind.OBSIDIAN, str(noted.origin_service))

badge = deps.badge_for(ServiceKind.OBSIDIAN)
check("THE BADGE IS PURPLE", badge["colour"] == "purple", str(badge))
check("and it says Obsidian, not '3rd party'", badge["label"] == "Obsidian",
      str(badge))
check("the badge for a record read from a vault resolves the same way",
      deps.badge_for(inline.origin_service)["colour"] == "purple",
      str(deps.badge_for(inline.origin_service)))

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All Obsidian markdown tests passed.")
