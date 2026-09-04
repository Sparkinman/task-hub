# Things 3 setup — complete walkthrough

Connects **Things 3** by Cultured Code, one way: Things → Task Hub.

Read the honest summary before you set it up, because this connector is
different from every other one in Task Hub.

---

## What you are getting, plainly

**Cultured Code publishes no API for Things Cloud.** There is no developer
portal, no OAuth, no documentation, and no support channel for this. Things has
a well-designed URL scheme for *adding* items on a Mac or iPhone, but nothing
that lets a server elsewhere read your list — and reading is the whole point of
a sync.

So this connector talks to the same endpoint the Things apps themselves use,
worked out by the community through observation. Three consequences follow, and
none of them are hidden in a footnote:

**1. It is read-only.** Task Hub imports your Things to-dos; it never writes
back. Writing to an undocumented endpoint means guessing at a format for a
database that holds your real work, and a wrong guess corrupts it. Reading
cannot. If you tick a task in Things it will show as complete in Task Hub, but a
task completed in Google will *not* be ticked in Things — you would still do
that yourself.

**2. It can stop working without warning.** Cultured Code owes nobody
compatibility here. A change to their backend could break this on any given day,
and there would be no announcement. Task Hub contains the damage: a Things
failure marks that one account as needing attention and every other service
syncs normally.

**3. It needs your Things Cloud email and password.** There is no OAuth to
delegate to, so there is no way to give limited access. The password is
encrypted at rest with the same key as every other credential and is never
written to logs — but it is your actual account password, and you should decide
whether that trade is worth it. If it is not, the alternative below is genuinely
good.

**It has never been tested against a live account.** Every other connector was
written against a documented API and exercised for real. This one was written
against a description. It verifies that it can sign in and read the moment you
connect, so a wrong assumption shows up immediately rather than as quiet
nonsense later.

---

## The alternative worth considering first

Things 3 can subscribe to a calendar, and Task Hub publishes one. That gives you
Task Hub's tasks visible inside Things with no password shared and nothing that
can break — at the cost of them being read-only *in Things*, which is the mirror
image of the trade above.

Many people are better served by the mirror image: keep Things as the app you
use on your Mac and iPhone, and let Task Hub hold everything else.

Alternatively, several people run Things beside Task Hub without connecting them
at all, using Apple Reminders (see `apple.md`) as the bridge, since Things can
import from Reminders on its own.

---

# Setting it up

## Step 1 — Confirm Things Cloud is on

1. On your Mac: **Things** → **Settings** → **Things Cloud**. It should show
   your email address and "Up to date".
2. On iPhone or iPad: **Settings** (in Things) → **Things Cloud**.

If Things Cloud has never been enabled, there is nothing on the server to read.
Turn it on and let it finish its first sync.

## Step 2 — Have your credentials ready

You need the **email address** your Things Cloud account uses and **its
password**. If you have forgotten it, reset it at
**https://culturedcode.com/things/cloud/** — Things on your devices will ask you
to sign in again afterwards.

## Step 3 — Connect

3. In Task Hub: **Services** → **Things 3**.
4. Click **Connect** on an empty slot.
5. Enter the email address and password.
6. Click **Save**.

Task Hub signs in immediately and reads your account. If the endpoint has
changed or the details are wrong, you are told now.

## Step 4 — Choose what to import

7. Click **Refresh lists**. Your Things areas and projects appear, along with
   the built-in lists.
8. Tick the Radicale collection each should import into.
9. **Write back stays off** and cannot be turned on. This is deliberate, and
   the page says so.
10. Click **Save sync settings for this account**, then **Sync now**.

---

# What comes across

| | |
|---|---|
| Title | Yes |
| Notes | Yes |
| **When** (the day a to-do is scheduled) | Yes, as the due date |
| Completion | Yes |
| Tags | Yes |
| **Time of day** | **No** — Things schedules to a day, with reminders kept separately |
| Checklists | No — Things's sub-items have no equivalent elsewhere |
| Headings inside a project | No |
| Areas and projects | As the list an item belongs to |

Things schedules a to-do to a **day**, not a time; its reminders are a separate
feature. The connector therefore does not claim to hold a time of day, which is
what stops a Things import from clearing a 2:30pm you set in Todoist or on your
phone.

## Deletion

Things's history is a stream of changes, not a complete list, so an item's
absence does not reliably mean it was deleted. Task Hub therefore **never
deletes anything on the strength of a Things pull**. If you delete a to-do in
Things it stays in Task Hub until you delete it there too.

This is the cautious choice on purpose: with an endpoint nobody documents, a
misread response that means "here is a partial list" must never be able to erase
your tasks everywhere.

---

# Troubleshooting

### "Things Cloud rejected these details"

Check the email and password by signing out and back in inside Things itself. If
they work there and not here, the sign-in endpoint has probably changed — see
below.

### "Things Cloud sent an unreadable response" / "Could not reach Things Cloud"

Most likely Cultured Code has changed something. There is no fix from your side.
Task Hub keeps syncing everything else; you can disconnect the Things account to
stop the warnings, and the tasks already imported stay where they are.

### Nothing was imported

Confirm Things Cloud is switched on and synced (Step 1), and that the account
actually holds to-dos — a Things installation used purely locally has an empty
cloud account.

### An item's date is a day earlier or later

Things stores a plain day with no timezone. Task Hub reads it as a date in the
timezone from **Settings**. If your Task Hub timezone does not match where you
actually are, dates can land a day out; correct it there.

### I want to write back to Things

Not available, and it will not become available: Cultured Code publishes no way
to write to Things Cloud, and guessing at one against an unpublished endpoint is
not a risk worth taking with somebody's task list.

So tasks from Todoist, Google or anywhere else will not appear in Things as a
mirror of what Task Hub holds. What you can do instead are Cultured Code's own
routes, and one of them is genuinely useful.

## Things' Reminders Inbox — the route that works

Things can watch one Apple Reminders list and pull anything appearing in it into
its own Inbox. Point that at a Task Hub list and tasks captured anywhere else
arrive in Things for you to file.

1. Put Task Hub on the device as a CalDAV account, if it is not already —
   [third-party apps](third-party-apps.md) has the three fields.
2. **Mac:** Things → Settings → **Reminders Inbox**, tick **Show to-dos from**,
   and choose the Task Hub list.
   **iPhone or iPad:** Things → Settings → **Reminders**, and the same choice.

Lists become projects, reminders become to-dos, due dates become When dates, and
tags come across.

**Understand what this is.** It is a capture route, not a sync. An item crosses
once, becomes an ordinary Things to-do, and stops being connected to anything —
edit it in Todoist afterwards and Things will not hear about it, and completing
it in Things will not tick it off anywhere else. That is the honest shape of it,
and it is still worth having if what you want is everything landing in one inbox
each morning.

One limit of Apple's: lists inside a **group** in Reminders are invisible to
Things. Drag the list out of its group if Things cannot see it.

## The other two

- **Mail to Things.** Every Things Cloud account can be given its own email
  address, and anything sent there becomes a to-do in the Inbox. Cultured Code
  supports it, and it is the only remote write route they offer.

  **Task Hub deliberately does not use it,** even though it can now send mail
  for the [daily summary](email.md). Mail is a one-way, fire-and-forget channel:
  nothing comes back to say the message arrived, an item that crossed cannot be
  found again to update or delete, and a retry after a failure makes a duplicate
  rather than correcting the first attempt. Driving a sync through it would mean
  a Things Inbox that fills with near-copies whenever anything goes wrong, with
  no way for Task Hub to notice or clean up. Reminders Inbox above does the same
  job visibly and reversibly, so that is the route documented here.

  Nothing stops you doing it by hand: your Things email address is under
  Things → Settings → Things Cloud, and anything you send to it lands in the
  Inbox.
- **The Things URL scheme.** `things:///add?title=…` adds a to-do from a
  shortcut or a link. It only works on a device with Things installed, so a
  server cannot drive it.

---

# Disconnecting

**Disconnect** in Task Hub deletes the saved email and password. To be thorough,
change your Things Cloud password at
**https://culturedcode.com/things/cloud/** as well. Nothing in Things is
altered either way — this connector has never written to it.
