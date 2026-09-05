# Subtasks

A task can belong to another task. Task Hub carries that relationship between
services — and, just as importantly, protects it from the services that cannot
hold one.

---

## Which services can nest

Task Hub checked every one of these against a real account rather than trusting
the documentation, and the documentation was wrong about three of them.

| Service | Subtasks | What it actually does |
| --- | --- | --- |
| **Radicale** (built in) | Yes | Standard iCalendar, so any CalDAV app reads it |
| **CalDAV** servers | Yes | The same, on somebody else's server |
| **Google Tasks** | Yes | Accepts more than one level, though its own apps show one |
| **Todoist** | Yes | Several levels deep |
| **TickTick** | Yes | Real subtasks, plus its own checklist items |
| **Obsidian** | Reading only | An indented line under another is a subtask |
| **Microsoft To&nbsp;Do** | As steps | Its checklist items: a name and a tick, nothing more |
| **Things 3** | No | Has checklist items, but this connector only reads tasks |
| **Supernote To-Do** | Labels | Its tasks have no parent field, so the title carries it |

---

## What happens at a service that cannot nest

The Supernote is the clearest case. Its To-Do app has no idea a task can
contain another, so sending it a task and its eight steps would produce **nine
unrelated tasks in one list**, with nothing marking which is which. On a tablet
screen that is a mess.

So Task Hub sends one task, with the steps written into its note:

```
Passport expires in March

Steps:
[ ] Book flights
[x] Renew passport
[ ] Arrange dog sitter
```

One task on the device. Nothing lost. The list stays readable.

**Those brackets are text, not checkboxes.** Ticking one on the tablet by
editing the note does not mark that subtask done in Task Hub — the step list is
a readable copy of what the subtasks say, written one way only. If you want to
tick them off on the device, turn on the setting below and they arrive as real
tasks instead.

**Microsoft To Do gets real checklist items** instead, because it has them.
They hold a name and whether they are ticked — a due date on one is refused by
Microsoft outright — so a subtask's date stays in Task Hub rather than being
quietly dropped.

### If you would rather have them separately

Settings → **Send subtasks as separate tasks**. Every subtask is then sent as a
task in its own right, which lets you tick them off individually on the device.
The trade is the one above: a parent and its steps sit side by side in the same
list with nothing marking which is which.

Services that *do* support subtasks are unaffected either way.

---

## The rule that protects your hierarchy

This is the part worth understanding, because it is what stands between you and
losing structure you built.

A due date is a *value*. The worst a service that cannot store one can do is
fail to carry it. **A parent link is a relationship**, and a service that cannot
express one can do something much worse: report the task as having *no* parent.

If Task Hub believed that, a parent and its children sent to a flat list would
come home as unrelated tasks — and the structure would be gone in Radicale, and
from there in every other service you have connected, with nothing recording
that it ever existed. It could not be reconstructed.

So the rule is absolute: **a service that cannot hold a parent link never gets
to say a task has none.** It cannot invent one and it cannot clear one. Only
services that genuinely express containment are believed, and for those a
missing parent really does mean the task moved to the top level.

---

## Completion is never contagious

Todoist completes every subtask when you complete their parent. Google does
not. Task Hub does neither on its own.

Ticking off a parent in Task Hub marks that one task done, and nothing else.
Ticking off every subtask does not mark the parent done. This is deliberate: if
Task Hub inherited Todoist's behaviour, one tap there would silently tick tasks
off across every service you have connected.

If you complete a parent *inside* Todoist, Todoist will still complete its own
subtasks — that is Todoist's behaviour on its own screen, and Task Hub reads the
result rather than causing it.

---

## In the web interface

A task's steps sit indented beneath it, folded away behind a button on the
parent. They start folded — a list that opens showing every step of every piece
of work is longer than it is useful — and whatever you open is remembered.

**A piece of work is placed by its soonest step.** A task with no due date of
its own whose step is due today appears under **Today**, with its steps, rather
than under Someday. What needs doing this morning belongs where you look for
what needs doing this morning. A finished step no longer counts towards that.

Every task in a hierarchy also carries a badge naming the piece of work, and
pressing it opens that whole breakdown on a page of its own — parent and steps
in order, at whatever depth, each tickable where it sits. Finished steps stay in
the completed section, because that is what you open to un-tick something.

### Making them

Three ways, for three moments:

- **New task → Sub tasks** — type them one per line and the whole piece of work
  is created at once.
- **New task → Part of** — make the task you are creating a step of an existing
  one.
- **Editing a task → Add sub tasks** — adds more. That box is always empty and
  only ever adds, so saving a task without typing in it changes nothing. Existing
  steps are edited on their own rows.

---

---

## Repeats and clearing finished tasks

A task can repeat — daily, weekly, monthly or yearly — set when you create it or
edit it. A repeat rule written by another CalDAV client that this menu cannot
express is shown as **Custom** and left completely alone rather than flattened
into the nearest thing it can say.

**Clear completed** removes finished tasks, and it **skips anything that
repeats**. The rule lives on the task itself, not on each occurrence, so
deleting a ticked-off repeating task would not tidy away a finished thing — it
would end the whole series. Google makes this especially easy to get wrong: it
keeps the completed task and creates the next one itself, so what looks finished
may be its record of an occurrence.

Clearing reaches every service the tasks are synced to, which is the point —
clearing them here and leaving them everywhere else would tidy nothing — but it
is why the button asks first.

---

## Known limits

- **Obsidian is read-only for nesting.** Indentation in your notes is read as
  hierarchy, but Task Hub will never re-indent your files.
- **Plain checklist lines in Obsidian are not subtasks.** A line only counts as
  a task if it carries task metadata, so shopping lists and packing lists are
  not swept in — and cannot become anybody's parent.
- **Depth beyond one level is stored but not promised.** Radicale and Todoist
  hold arbitrary depth and Google's API accepted it, but Google's own apps
  appear to show only one level, so what a given app displays may be flatter
  than what Task Hub holds.
- **Things 3 checklist items are not read as subtasks.** They exist, but they
  are steps rather than tasks — no dates, no identity of their own — and the
  connector cannot write anything back regardless.
