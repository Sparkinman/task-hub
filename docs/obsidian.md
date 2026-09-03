# Obsidian

Task Hub reads tasks out of your Obsidian vault and syncs them to every other
service you have connected. A task you write in a daily note appears in Todoist,
Google Tasks and your calendar, carrying a link back to the note it came from.

**Obsidian is read-only.** Task Hub never writes into your vault. See
[Why read-only](#why-read-only) below — it is enforced by Obsidian's own client,
not by Task Hub promising to behave.

---

## What you need

An **Obsidian Sync subscription**. That is how the vault reaches Task Hub: it
signs in as another of your devices, exactly like a second laptop would, and
Obsidian sends it the vault.

There is no way around this. Obsidian Sync has no public API, and the Obsidian
team have said they do not intend to add one. If you keep your vault in Dropbox
or iCloud instead, Task Hub cannot reach it.

You do **not** need to install anything, run any commands, or leave a computer
switched on. Obsidian's own headless client is built into Task Hub.

---

## Setting it up

1. Open **Services → Obsidian**.
2. Enter your Obsidian account email and password. If you have two-factor
   authentication turned on, enter the current code as well.
3. **Tick every vault you want to sync** — you can choose more than one.
4. Task Hub downloads them and shows you how many tasks it found in each.

### More than one vault

You sign in to Obsidian once, and that one sign-in can sync as many vaults as
you like, up to ten. Obsidian's client keeps each vault's state separately, so
they do not interfere with each other.

Each vault you link becomes its own slot in Task Hub, which means each one has:

- its own folder mappings, so a work vault and a personal vault can feed
  entirely different collections;
- its own live sync, its own status and its own recent output;
- its own **Unlink this vault** button, which leaves the others and your
  sign-in alone.

You do not have to choose them all at once. The Obsidian page keeps offering
whichever vaults are not yet linked, so a second one can be added months later
without touching the first.

If your vaults use **different** end-to-end encryption passwords, link them one
at a time — the password box applies to every vault ticked at that moment.

Task Hub appears in your Obsidian sync history as a device named **Task Hub**,
so you can see it there and remove it whenever you like. If your plan limits how
many devices you can sync, it uses one of those slots.

---

## Keeping up with your vault

Once a vault is linked, Task Hub keeps a live connection to Obsidian Sync and
receives changes as they happen — the same way the Obsidian app on your laptop
does. Tick a task off on your phone and it is on the server moments later,
without waiting for the next sync.

The Obsidian page shows this as **Kept up to date: Live**. If it says anything
else, the copy on disk has stopped updating and what you see in Task Hub is
frozen at whatever it last downloaded. The most common reason is that the
sign-in expired, and the page says so and offers to sign in again.

**Download now** forces a full pass immediately. You rarely need it — it is
there for when you want to be certain, such as straight after a large
reorganisation of the vault.

### Why you will not see half a vault

Files arrive one at a time during a download, so for a few seconds a vault is a
directory that matches no real state of itself: a note can be genuinely missing
because it has not arrived yet. If Task Hub read the vault at that moment it
would see tasks missing and could conclude you had deleted them — and then
delete them from Todoist, Google and everywhere else.

So it does not read the vault during a pass. Sync waits for the download to
settle, and if it has not settled in time that round is skipped and tried again
on the next one. A sync that does nothing is always the right answer over a sync
that deletes your work.

---

## What counts as a task

This is the part worth reading carefully, because a vault is full of checkboxes
that are not tasks — shopping lists, packing lists, the steps in a recipe. If
Task Hub treated every one of those as a task, your Todoist would fill up with
somebody's groceries.

So **a plain checkbox is never synced**:

```markdown
- [ ] milk
- [ ] eggs
```

Neither of those becomes a task. A line has to say that it is one.

### If your vault uses a global filter

The [Obsidian Tasks](https://publish.obsidian.md/tasks/) plugin has a setting
called the **global filter** — usually `#task` — whose whole purpose is this
problem. If you have one set, Task Hub uses it and nothing else:

```markdown
- [ ] Renew the passport #task 📅 2026-09-12     ← synced
- [ ] Renew the passport 📅 2026-09-12           ← not synced, no #task
- [ ] milk                                        ← not synced
```

That is deliberate, even though the middle line has a due date. Using any other
rule would mean Task Hub's idea of your task list and Obsidian's own were
different lists, which is more confusing than either rule on its own.

### If your vault has no global filter

Then a line needs to carry a real task field — a due date, a scheduled date, a
start date, a priority or a recurrence:

```markdown
- [ ] Renew the passport 📅 2026-09-12     ← synced, it has a due date
- [ ] Call the bank ⏫                      ← synced, it has a priority
- [ ] milk                                  ← not synced
```

### TaskNotes

A [TaskNotes](https://github.com/callumalpass/tasknotes) file is always a task —
that is what the format is for. If your vault marks them with a tag, that tag
decides; otherwise a note needs a `status` and at least one of `due`,
`scheduled`, `priority` or `recurrence`, so your ordinary notes are left alone.

---

## The two formats

Task Hub reads both, and a vault can hold a mixture.

### Obsidian Tasks (a line in a note)

The [Tasks plugin](https://publish.obsidian.md/tasks/) puts a task's details on
the line itself, either as emoji or in
[Dataview](https://blacksmithgu.github.io/obsidian-dataview/)'s field syntax.
Both are read.

| Field | Emoji | Dataview |
| --- | --- | --- |
| Due | `📅 2026-09-12` | `[due:: 2026-09-12]` |
| Scheduled | `⏳ 2026-09-12` | `[scheduled:: 2026-09-12]` |
| Start | `🛫 2026-09-12` | `[start:: 2026-09-12]` |
| Created | `➕ 2026-09-01` | `[created:: 2026-09-01]` |
| Done | `✅ 2026-09-12` | `[completion:: 2026-09-12]` |
| Recurrence | `🔁 every week` | `[repeat:: every week]` |

Priority is a bare emoji with no value, and there are six levels. Note that
*medium* sits above normal, not below it — normal is simply the absence of any
emoji:

| 🔺 | ⏫ | 🔼 | *(none)* | 🔽 | ⏬ |
| --- | --- | --- | --- | --- | --- |
| Highest | High | Medium | Normal | Low | Lowest |

The plugin's own reference is the best place to read more:

- [Tasks: emoji format](https://publish.obsidian.md/tasks/Reference/Task+Formats/Tasks+Emoji+Format)
- [Tasks: Dataview format](https://publish.obsidian.md/tasks/Reference/Task+Formats/Dataview+Format)
- [Tasks: getting started](https://publish.obsidian.md/tasks/Getting+Started/Getting+Started)
- [Tasks: the global filter](https://publish.obsidian.md/tasks/Getting+Started/Global+Filter)

### TaskNotes (a file per task)

[TaskNotes](https://github.com/callumalpass/tasknotes) gives each task its own
note, with the details in YAML frontmatter:

```yaml
---
title: Weekly meeting
status: in-progress
due: 2026-09-15T09:30
scheduled: 2026-09-14
priority: high
recurrence: FREQ=WEEKLY;BYDAY=MO
contexts: [work]
---
```

TaskNotes lets you rename any of those properties. Task Hub reads your renamed
names from the plugin's own settings rather than assuming the defaults, so a
renamed field is still found.

- [TaskNotes documentation](https://callumalpass.github.io/tasknotes/)
- [TaskNotes: task properties](https://callumalpass.github.io/tasknotes/settings/task-properties/)

---

## What Obsidian can and cannot hold

**A task on a line has no time of day and no timezone.** There is nowhere in
`📅 2026-09-12` to put half past two. This is a limit of the format, not of Task
Hub.

That matters less than it sounds, because Task Hub treats a field a service
cannot express as *absent* rather than empty. A task due at 2:30pm in Todoist,
read into Obsidian and read back out again, keeps its 2:30pm — Obsidian is never
allowed to erase a time it could not have stored. It is the same rule that keeps
Google Tasks from doing the same thing.

**A TaskNotes file can hold a time**, because its dates are full timestamps. So
`due: 2026-09-15T09:30` survives the round trip intact.

**Recurrence differs between the two.** TaskNotes stores a real calendar rule
(`FREQ=WEEKLY;BYDAY=MO`) which passes straight through. The Tasks plugin stores
English (`every week when done`), which Task Hub reads but does not translate
into a calendar rule — guessing would produce something subtly different from
what you wrote.

---

## Finding your way back to the note

Every task carries a reference to where it came from, in its notes or
description field:

```
— Obsidian · Projects/Website.md
obsidian://open?vault=Notes&file=Projects%2FWebsite.md
```

The path is readable anywhere. The link opens the note itself in Obsidian, on
your computer or your phone. If you write your own notes on the task in Todoist,
they are kept — the reference sits below them and never overwrites anything.

---

## Why read-only

Obsidian tasks live in your own prose. A task line can hold `[[wikilinks]]`,
bold text, footnotes, indentation that makes it a sub-task of the line above,
and metadata belonging to plugins Task Hub has never heard of. Writing back into
that safely is a much harder problem than calling an API, and getting it wrong
damages your notes rather than a service's copy of a task.

So Task Hub does not write to your vault at all. And it does not merely promise
not to: Obsidian's client is configured in **mirror-remote** mode, which
downloads changes and *reverts local ones*. Even a bug in Task Hub that wrote
into the vault folder would be undone before it could reach your real vault.

What this costs you: **ticks do not come back**. If you complete a task in
Todoist, Obsidian still shows it unticked, and a task created in Google Tasks
never appears in your vault. Obsidian feeds the other services; nothing feeds
Obsidian.

Write-back may come later. It will be off by default, per-collection, and
turning it on will mean changing that sync mode — a visible switch, not a
checkbox buried in a form.

---

## Notes on security

Your Obsidian password is used once, to sign in, and is not stored — the client
keeps a session of its own afterwards. During that one sign-in the password is
passed to Obsidian's client as a command-line argument, because its prompts
cannot be driven any other way, which means it is briefly visible in the
container's process list.

Inside a normal Task Hub install this adds no real exposure: only Task Hub's own
processes run in that container, and anything able to read them can already read
the encryption key for every other credential you have saved. It is worth
knowing about if you run Task Hub somewhere other people have access to the same
container.

Your vault is stored unencrypted inside Task Hub's data volume, in the same
place as everything else. Back it up with the same care —
[backing up](getting-started.md#backing-up).
