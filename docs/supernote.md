# Supernote setup — complete walkthrough

Connects three things on your Supernote tablet, none of which anything else
outside Supernote's own apps can reach:

| | Direction | What it is |
| --- | --- | --- |
| **To-Do** | both ways | The tablet's built-in task app |
| **Digest** | both ways | Passages you highlight out of documents and notebooks |
| **Notebooks** | read only | Your handwriting, backed up as PDFs you can read anywhere |

The **Notes** and **Digests** pages appear in Task Hub's sidebar once you have
connected Supernote, and not before.

Read the honest summary first. This connector is built differently from every
other one in Task Hub, including the other unofficial ones, and you should know
how before you rely on it.

---

## What you are getting, plainly

**Ratta publishes no API for Supernote.** None at all — no developer portal, no
documentation, no OAuth, no support channel. The two open-source Supernote
projects that exist both cover file storage only, and neither touches to-dos.

So the addresses this connector uses were read out of the Supernote Partner app
itself, by unpacking it and reading the compiled code inside. They were then
checked against a live account, and they work. But nothing about them is
published, versioned or promised to anybody.

Four consequences follow, and none of them are hidden in a footnote.

**1. It is unofficial, and Ratta can terminate it at any time.** There is no
agreement here and nothing to appeal to. Ratta may change these addresses,
restrict them, or block them outright whenever they choose, without notice and
without owing anyone an explanation — a Partner app release could do it and
nobody would announce it. The first sign would be a Supernote sync that fails.

Every other service keeps syncing normally when that happens, disconnecting
Supernote changes nothing else, and anything already synced stays where it is.

**2. It syncs both ways.** Tasks you add, edit, tick off or delete in Task Hub
reach the tablet, and changes made on the tablet come back. Each write operation
was worked out against a live account rather than guessed, which mattered:
saving an edit the obvious way turns out to create a *second copy* of the task
rather than changing it, and does so without reporting any error. Task Hub does
the one thing that actually updates a task.

The lists themselves are only read. Task Hub maps to lists you have already made
on the tablet and never creates or deletes one there.

**3. Signing in needs a code from your email, and lasts thirty days.** Supernote
will not issue a session without emailing you a six-character code. The session
it gives back is good for thirty days and there is no way at all to renew it in
the background — Supernote provides no mechanism, and several likely ones were
tried and do not exist. So roughly once a month you will need to sign in again.
Task Hub reads the expiry date out of the session itself and warns you a week
before, on the Supernote page and on the overview, so it is a diary note rather
than a surprise.

**4. Your password is not stored.** It is used once, to ask Supernote for a
session, and then discarded. Keeping it would gain nothing, because it cannot
renew a session on its own — Supernote would just email another code.

---

## Getting every calendar onto the tablet

Supernote's calendar app subscribes to **one** calendar. That is a real
limitation if your appointments live in more than one place, and it is one Task
Hub removes without any Supernote-specific trickery.

Point as many calendars as you like at a single Task Hub collection — Google,
Outlook, iCloud, any CalDAV server — then subscribe the tablet to that one
collection. The device sees everything, in one calendar, kept up to date.

Set it up on the **Sync** page: tick each calendar you want against the same
collection. [Getting started](getting-started.md) covers the mapping table, and
[Connecting your own apps](third-party-apps.md) covers giving the tablet the
address.

This is the ordinary merge behaviour every service gets, so the same approach
works for anything else that only accepts one calendar.

---

## This is not the same as the CalDAV route

Task Hub can already see Supernote tasks a second way, and it is easy to
confuse the two.

| | **This connector** | **The CalDAV route** |
| --- | --- | --- |
| What it reads | The **To-Do app built into** Supernote | Whatever a CalDAV app **installed on** the tablet syncs |
| Where the tasks show | Supernote's own To-Do app, and the Partner app on your phone | Inside the third-party app you installed |
| Needs an app installing on the tablet | No | Yes |
| Direction | Two-way | Two-way |
| Official | No | Yes — CalDAV is a standard |

If the tasks you want are the ones you see in Supernote's own To-Do app, and in
the Partner app on your phone, this is the connector you want. If you have
installed a CalDAV client on the tablet and want that, see
[Connecting your own apps](third-party-apps.md) instead.

There is no harm in using both.

---

## What syncs, and what does not

**Comes across:**

- Your to-do lists, as separate lists you can map to any collection
- Each task's title
- Its notes, where you have written any
- Whether it is done
- Its due date

Everything in that list syncs in both directions — an edit in Task Hub reaches
the tablet just as an edit on the tablet reaches Task Hub.

**Does not come across:**

- **A time of day.** Supernote's To-Do app only lets you choose a date, so
  there is no time to read. This matters in a good way: because Task Hub knows
  Supernote cannot hold a time, a task syncing from Supernote can never wipe a
  time you set in Todoist, Google or anywhere else.
- **Repeats.** Supernote stores repeating to-dos, but the format has not been
  confirmed, so a repeating task syncs as a single one rather than as a wrong
  repeating one.
- **Priority.** The field exists in Supernote's data and was empty on every task
  tested, so nothing is read from it yet.
- **Reminders.** Supernote tracks these separately from the due date.

### Tasks written on a page of a notebook

A task made by circling handwriting carries a link back to the page it came
from — the notebook icon beside it on the tablet, which jumps straight there.

Task Hub keeps that link intact. An edit made here sends Supernote its own
record with only the fields Task Hub owns laid over it, so the link survives
being ticked off, renamed or given a due date.

**And it offers the same jump.** If that notebook is one you have backed up,
the task carries a small notebook link on the Tasks page that opens the PDF at
the right page — no tablet required.

**Only when the notebook is here.** If it is in a folder you have not backed
up, or you removed the copy, the reference still reads as text under the task
— *"From 20260821_041013.note, page 2"*, so you can still see where it came
from — but nothing looks clickable. A link that goes nowhere invites a tap and
answers with an error for something that was never wrong.

### Tasks that belong to no list

Supernote lets a task exist without being in any list — it shows in the To-Do
app's **All** view and in none of the named lists. Task Hub gathers these into a
list of its own called **Inbox** — the same name the tablet uses — which
appears alongside your real lists once there is something in it.

This exists so nothing on your account is invisible to Task Hub. Filtering
tasks by list — the obvious way to build this — silently loses them, and a task
you cannot see is worse than one you have chosen not to sync.

Give this list a collection of its own, or leave write-back off for the
Supernote lists sharing its collection. An unfiled task belongs to no list, so
writing it back to one would leave the original outside every list and a copy
inside one. Task Hub refuses to do that and says so, but the tidiest
arrangement avoids the situation entirely.

Task Hub never writes into this list, and never treats a task leaving it as a
deletion. Filing one of these tasks into a real list on the tablet is an
ordinary thing to do, and it makes the task disappear from this view while the
task itself is perfectly fine — so treating that as a deletion would delete your
task from every other service because you tidied it up.

---

## Digests: your highlights, both ways

A digest is a passage you have dragged out of a PDF or a notebook on the
tablet. Task Hub mirrors them, shows them as readable PDFs, and — unlike the
notebook backup — **writes back**: anything you add here appears in the Digest
app on your Supernote.

**Choosing what to mirror.** On the Supernote page, under *Digests*, tick the
libraries you want. Ticking none mirrors everything, including digests filed in
no library at all, which the tablet allows.

**What comes across:** the passage, the file and page it came from, any typed
note, and which library it belongs to. A handwritten comment stays on the
tablet — the page says so against the digest rather than quietly showing you
half of it.

**Reading them as a PDF.** Task Hub sets the document itself, because a digest
is text rather than a file: there is nothing to download until it is typeset.
Each library has a **View PDF** and a **Download**, and there is an *All as
PDF* for the lot.

**Adding, editing and deleting.** All three reach Supernote. Deleting really
does delete — from the tablet as well as from Task Hub — which is the opposite
of the notebook backup, where *Remove* only clears Task Hub's copy. The
confirmation says which you are doing.

**When it refreshes.** Alongside the notebook backup, on the same schedule.
Listing every digest on an account is one small request, so it costs nothing
to include.

---

## Backing up your notebooks as PDFs

Separate from the to-do sync, and read-only: Task Hub copies notebooks *out* of
folders you choose and never writes anything back into them.

**What it does.** For each notebook in a folder you tick, Task Hub asks
Supernote's own converter to render it as a PDF and keeps the result. You read
it under **Notes**, and you can download a copy onto whatever device you are
using — phone, tablet or laptop.

Supernote's converter is used rather than an open-source one on purpose.
`.note` is an undocumented binary format that changes with the firmware, and
Ratta's converter is the only one guaranteed to understand the version your
tablet is writing today.

**Setting it up.** On the Supernote page, under *Back up notebooks as PDFs*:

1. Tick the folders you want. Ticking a folder includes everything inside it,
   however deeply nested.
2. Choose how often, and whether it runs automatically.
3. Press **Save backup settings**, then **Back up now** to fetch the first copy.

**Why it is slow on purpose.** The fastest setting is once every 30 minutes,
and the default is every 6 hours. Converting a notebook is real work on
Supernote's servers, on an API they never published and owe nobody. Task Hub
also only converts notebooks that have actually changed — Supernote reports a
checksum for every file, so opening a notebook without editing it costs
nothing — pauses between conversions, and does at most 25 in one pass. A first
backup of a large account therefore arrives over several runs rather than as
one long burst. All of that is deliberate: being a good guest is what keeps
this working.

**Large notebooks may say "still converting".** Supernote queues big ones —
year planners especially — and renders them in the background. That is not a
failure and needs nothing from you; they appear on a later backup.

**Removing a copy you do not want.** Each notebook on the Notes page has a
**Remove** button. It deletes Task Hub's copy and nothing else — the notebook
stays on your tablet and in Supernote's cloud, untouched.

Task Hub remembers the decision, which it has to: the notebook is still sitting
in a folder you chose to back up, so without a record of it the very next pass
would fetch it again and the button would appear not to work. Removed notebooks
are listed at the bottom of the Notes page with a **Restore** button if you
change your mind, and *Clear this list* forgets them entirely — which means they
will be backed up again on the next pass.

To stop backing up a whole folder, untick it on the Supernote page instead.
That removes every notebook in it in one go.

**What it is not.** It is a backup, not a sync. Editing a PDF here is not
possible, nothing travels back to the tablet, and deleting a notebook on the
tablet removes it from Task Hub on the next pass.

---

## Setting it up

You need your Supernote Cloud email address and password — the same ones you
use in the Supernote Partner app — and access to that email account, because a
code is sent to it.

**1.** Open **Services → Supernote** in Task Hub.

**2.** Enter your Supernote email address and password, and press
**Send me a code**.

**3.** Check your email. Supernote sends a six-character code, letters and
numbers. It expires after a few minutes, so do this straight away.

**4.** Type the code into the box that has appeared, and press **Finish signing
in**.

Task Hub immediately reads your lists back to prove the session works, and tells
you the date it expires. If something is wrong, you find out now rather than at
the next scheduled sync.

**5.** Press **Refresh lists** to fetch your to-do lists.

**6.** Map each list to a collection, the same way as any other service. See
[Getting started](getting-started.md) if you have not done this before.

> **One thing to get right when write-back is on.** Ticking a collection makes
> that list two-way. If you tick *several* Supernote lists into the same
> collection, then every new task in that collection is created in **every one
> of those lists** — one task becomes four. That is how syncing works for any
> service, not something peculiar to Supernote, but Supernote makes it easy to
> hit because one account gives you several lists at once. Send write-back to a
> single list and read from the rest.

### If the code does not arrive

- **Check the newest email.** If you tried more than once, each attempt sends
  its own code and only the most recent works.
- **Wait a moment and look in spam.** It comes from Supernote, not from Task
  Hub.
- **Start again** with *Cancel and start again* if more than a few minutes have
  passed. Codes expire quickly.

### If your password is refused

Sign in at [cloud.supernote.com](https://cloud.supernote.com/) in a browser. If
it fails there too, the password is the problem rather than Task Hub. Note that
your Supernote Cloud password is its own thing — not the password of the email
account you registered with, which is a common mix-up.

---

## Signing in again, every thirty days

When a session is within a week of running out, a warning appears on the
Supernote page and on the overview, naming the date. Signing in again is exactly
the same three steps as the first time, and your list mappings are kept — you do
not set anything up twice.

If a session does run out before you get to it, nothing is lost. Supernote
syncing simply stops until you sign in again, and every other service carries on
untouched.

---

## If it stops working

Because this rests on addresses Ratta never published, "it broke" is a real
possibility rather than a theoretical one. Signs, and what they mean:

- **"Supernote Cloud rejected this session"** — the thirty days are up, or you
  signed out elsewhere. Sign in again.
- **"Supernote Cloud answered with something that was not JSON"** — the API has
  probably changed. This is the one that needs the connector updating; there is
  nothing you can do from the interface.
- **Lists appear but are empty** — check the tasks are in the To-Do app on the
  tablet and have synced to the Partner app on your phone. If the Partner app
  cannot see them either, they have not reached Supernote's servers yet and
  Task Hub cannot see them either.

In every case, the rest of Task Hub keeps working. Supernote failing has never
been able to stop another service syncing, by design.

---

## The plugin that runs on the tablet

Separate from everything above, there is a **[Task Hub Supernote
plugin](https://github.com/Sparkinman/task-hub-supernote-plugin)** for the device itself: it captures handwriting
directly into CalDAV tasks and gives you task lists, calendar views and a daily
agenda on the tablet.

It reaches Task Hub over CalDAV like any other client, so it sits alongside this
connector rather than replacing it — items it creates arrive stamped as
Supernote, which is where that origin badge in the task list comes from.

---

## Further reading

- [The Task Hub Supernote plugin](https://github.com/Sparkinman/task-hub-supernote-plugin) — the companion that runs
  on the tablet
- [Supernote's own To-Do app guide](https://support.supernote.com/the-to-do-app)
  — what the app on the tablet can do
- [Supernote support](https://support.supernote.com/) — the official help site
- [What works today](compatibility.md) — every service and its current state
- [Connecting your own apps](third-party-apps.md) — the CalDAV route described
  above
