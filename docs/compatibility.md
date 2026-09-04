# What works with Task Hub

Everything Task Hub can connect to, in one place, with an honest label on each
row. There are two different questions here and they are kept apart:

- **Services** Task Hub signs in to and syncs *with* — Google, Todoist, a
  Nextcloud server.
- **Apps** that connect *to* Task Hub's own CalDAV server — the app on your
  phone, your desktop calendar.

## What the labels mean

| Label | Meaning |
| --- | --- |
| **Verified** | Run against a real account or a real device, and seen working. Where a stress run was done, it is named. |
| **Should work** | Speaks a protocol Task Hub already proves elsewhere, or has a documented API — but this exact pairing has not been tried. Expect it to work; say so if it does not. |
| **Won't work** | Cannot work, with the reason. Not a to-do list. |

Nothing is listed as verified on the strength of its code being written. That
distinction has earned its keep: every connector that met a live account for the
first time was broken in some way no test had caught.

---

# Services Task Hub syncs with

## Verified

| Service | Tasks | Calendar | Notes |
| --- | :---: | :---: | --- |
| **Google** | ✓ | ✓ | Tasks and Calendar. Stress-run with 200 tasks and 200 events. Google Tasks stores no time of day — Task Hub keeps the time separately so Google cannot erase it |
| **Todoist** | ✓ | — | The quickest to connect: a personal API token, no app registration |
| **TickTick** | ✓ | — | Stress-run. The public API has no calendar and will not return tags |
| **Microsoft** | ✓ | ✓ | To Do and Outlook Calendar. Registering the app needs an Entra ID directory — [the options](microsoft.md) |
| **Apple iCloud** | ✓ | ✓ | 200 tasks and 200 events through a list and calendar it created and removed itself. [One caveat about upgraded Reminders](apple.md) |
| **Obsidian** | ✓ | — | Tasks in your vault, through Obsidian Sync. Read-only unless write-back is enabled |
| **Things 3** | ✓ | — | Import only. Its endpoint is unpublished and can change without warning — that warning never comes off |
| **CalDAV (generic)** | ✓ | ✓ | Verified against a live Radicale server: sign-in, discovery, and a to-do created, read back and deleted with due time, priority and notes intact |
| **Radicale (built in)** | ✓ | ✓ | The hub everything meets at |

## Should work — through the CalDAV connector

The [CalDAV connector](caldav.md) is not written per-provider: it asks the
server what the account owns and uses what it finds. Every server below answers
that conversation, so all of them should connect by entering the address, a
username and a password. None has been tried here yet.

| Server | Tasks | Calendar | Address to enter |
| --- | :---: | :---: | --- |
| **Nextcloud** | ✓ | ✓ | `https://cloud.example.com` — needs an app password from Settings → Security |
| **Fastmail** | ✓ | ✓ | `https://caldav.fastmail.com` — app password with the CalDAV permission |
| **Baïkal** | ✓ | ✓ | `https://dav.example.com` |
| **SOGo** | ✓ | ✓ | `https://sogo.example.com/SOGo/dav` |
| **Synology Calendar** | ✓ | ✓ | `https://diskstation.example.com:5001` |
| **mailbox.org** | ✓ | ✓ | `https://dav.mailbox.org` |
| **Posteo** | ✓ | ✓ | `https://posteo.de:8443` |
| **Zoho Calendar** | — | ✓ | `https://calendar.zoho.com` — calendar only |
| **Kolab** | ✓ | ✓ | your Kolab server's DAV address |
| **Another Radicale** | ✓ | ✓ | somebody else's, or a second one of your own |
| **Owncloud** | ✓ | ✓ | same shape as Nextcloud |

**Whether task lists appear is the server's decision, not Task Hub's.** A
collection has to accept `VTODO`. Nextcloud, Baïkal, Radicale and SOGo do; some
mail providers offer calendars only, and you will see calendars and no task
lists.

## Won't work

| Service | Why |
| --- | --- |
| **Notion** | No CalDAV. Its API models pages and databases, not tasks with due dates, so a connector would be a guess at what a task is |
| **Asana, ClickUp, Monday, Basecamp** | No CalDAV. Their APIs are project-management shaped — a task belongs to a workflow, not to a list — and none exports to iCalendar in a way that round-trips |
| **Trello** | No CalDAV. Its calendar feed is read-only iCalendar, so tasks could be read and never written |
| **Remember The Milk** | No CalDAV. It has been an open feature request on their own forum for over a decade |
| **Any.do** | No public API |
| **Apple Reminders on an "upgraded" Apple ID** | Apple moved those reminders to a private store no application can reach. Not a Task Hub limitation and not fixable by anyone — [what to do instead](apple.md) |
| **Vikunja** | It is a CalDAV *server*, like the one inside Task Hub, not a client. The two sit at the same layer and cannot be pointed at each other |

---

# Apps that connect to Task Hub

Task Hub runs a real CalDAV server, so these need no connector at all: they sign
in to Task Hub directly. [Setup for each one](third-party-apps.md).

## Verified

| App | Platform | Notes |
| --- | --- | --- |
| **Apple Calendar** | iPhone, iPad, Mac | Built in. Proven in both directions |
| **Apple Reminders** | iPhone, iPad, Mac | Built in. A reminder made on the phone reaches Todoist; a change anywhere reaches the phone. **Needs an HTTPS address** — [why](third-party-apps.md) |
| **Supernote** | E-ink tablet | Tasks created on the device arrive stamped as its own |
| **Thunderbird** | Windows, macOS, Linux | Calendars and tasks both, over plain HTTP as well as HTTPS |
| **DAVx⁵** | Android | The sync layer everything else on Android goes through |
| **Super Productivity** | Desktop | Reads tasks and syncs completion back. It **cannot create** tasks on the server — a good place to work through a list, a poor place to capture into |

## Should work

| App | Platform | Notes |
| --- | --- | --- |
| **Tasks.org** | Android | The Android task app to choose. Free on F-Droid; the Play build charges for sync. Pair with DAVx⁵ |
| **jtx Board** | Android | Tasks, notes and journals. Through DAVx⁵ |
| **OpenTasks** | Android | Older and simpler. Through DAVx⁵ |
| **Home Assistant** | Anywhere | Task lists become `todo.*` entities automations can read *and write*. Polls about every 15 minutes |
| **GNOME Calendar / GNOME To Do** | Linux | Settings → Online Accounts → Nextcloud — despite the name it is a plain CalDAV client |
| **KOrganizer** | Linux (KDE) | System Settings → DAV groupware resource |
| **Evolution** | Linux | Add a CalDAV calendar and task list |
| **eM Client** | Windows, macOS | Commercial, with a free tier |
| **BusyCal / BusyContacts** | macOS | Commercial |
| **Fantastical** | macOS, iOS | Commercial. Reads any CalDAV account the system knows about |
| **Calendar (Samsung, Xiaomi, etc.)** | Android | Whatever calendar app you already use, fed by DAVx⁵ |
| **Chiri** | Windows, macOS, Linux | A dedicated CalDAV task client |
| **Abeluna** | Linux | A small CalDAV to-do manager |
| **Nextcloud Tasks** | Browser | Only as a *server* Task Hub syncs with — it cannot subscribe to Task Hub. See the services table above |

## Won't work

| App | Why |
| --- | --- |
| **Microsoft Outlook** | No CalDAV support, in any version — desktop, web or new Outlook for Windows. Microsoft removed what existed and has no plans to restore it. **Connect Task Hub to Microsoft instead** and reach Outlook through Microsoft's own servers — [microsoft.md](microsoft.md) |
| **Google Calendar (the Android app)** | It shows any calendar the Android system knows about, so DAVx⁵ feeds it — but it cannot add a CalDAV account itself |
| **Google Tasks (the app)** | No CalDAV. Connect the **Google service** instead |
| **Todoist, TickTick, Things (the apps)** | None speaks CalDAV. Connect them as *services*, which Task Hub already does |
| **A calendar "subscription"** | On any platform, subscribing to a calendar URL gives you a **read-only** copy. If tasks appear but cannot be ticked off, this is what happened — remove it and add it as a CalDAV *account* |

---

# The rule of thumb

Connect the **app** when you want to see and edit your tasks on a device:
nothing is lost, it is immediate, and nothing passes through anyone else's
servers.

Connect the **service** when the tasks must genuinely live there — shared with
family in Google, on a work Outlook calendar, reachable by Siri.

**Never both for the same list.** Two routes to the same place produce
duplicates and edits that fight each other. If your iPhone talks directly to
Task Hub, do not also connect Task Hub to Apple for those same lists.

---

# Something missing?

If an app speaks CalDAV, it should already work — the three things it needs are
on Task Hub's **Radicale** page. If a service is not listed, the question is
whether it has an API that can represent a task with a due date and let one be
written back; that is the whole bar, and most of the "won't work" list fails it
by not having an API at all rather than by being difficult.
