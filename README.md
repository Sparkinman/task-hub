# Task Hub

[![Publish image](https://github.com/Sparkinman/task-hub/actions/workflows/publish.yml/badge.svg)](https://github.com/Sparkinman/task-hub/actions/workflows/publish.yml)

A self-hosted web application that keeps tasks and calendars synchronised across
**Google, Todoist, TickTick and Obsidian**, with a built-in Radicale CalDAV
server as the place everything converges — so your phone, your laptop's
calendar and every service you use are all looking at the same tasks.

Connectors for Apple, Microsoft and Things 3 are written but **not yet
finished**: see [what works today](#what-works-today) before you plan around
them.

Everything is configured through the web interface. There is no configuration
file to edit, and no command beyond starting the container.

---

## Install

One file, one command, no editing. On a Raspberry Pi, a NAS, a Mac, a Windows
machine or any Linux server:

```
mkdir -p ~/taskhub && cd ~/taskhub
curl -fsSL -O https://raw.githubusercontent.com/Sparkinman/task-hub/main/docker-compose.yml
docker compose up -d
```

Then open **http://localhost:8080** — or, from another device, this machine's
address on your network with `:8080` after it — and follow the setup wizard.

Step-by-step guides, each written for someone who has not done this before:

| | |
| --- | --- |
| [Raspberry Pi](docs/install-raspberry-pi.md) | From a blank SD card to a working install, including what to check on a Pi that is already in use |
| [NAS](docs/install-nas.md) | Synology, QNAP, Unraid, TrueNAS and Asustor |
| [Windows or Mac](docs/install-windows-mac.md) | The quickest way to try it |
| [How it finds its own address](docs/addresses.md) | What each service accepts, and why sign-in fails when it does |

Not sure whether your machine is up to it? This checks and changes nothing:

```
curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/check-system.sh | sh
```

### One image, any address

Most self-hosted software has to be told where it lives, and quietly breaks when
you reach it a different way. Task Hub works its own address out from each
request instead, so the same image is correct on a home network address, behind
a Cloudflare tunnel, over Tailscale, and behind your own nginx — with nothing to
configure and nothing to change when you move it.

Published for both Intel and ARM at
[`sparkinman/task-hub`](https://hub.docker.com/r/sparkinman/task-hub) and
`ghcr.io/sparkinman/task-hub`, built from this repository by GitHub Actions.

---

## What it does

**Two-way sync across services.** A task created, edited or completed anywhere
propagates everywhere else. Each service supports up to ten independent accounts,
and you choose per-list which ones to read from and which to write back to.

**Lossless round-tripping.** Services disagree about what a task is. Google Tasks
cannot store a time of day at all — it keeps the date and discards the time.
Todoist has priorities Google lacks. If a hub naively mirrored whatever each
service reported, editing a task's date in Google would destroy the 2:30pm you
set in Todoist, and the next sync would spread that loss everywhere.

Task Hub stores due dates as **separate date, time and timezone components**, and
each connector declares which fields it can faithfully represent. Fields a
service cannot express are treated as *absent* rather than empty, so they never
overwrite anything. A date-only edit arriving from Google is applied to the date
component alone, and the time survives.

**An echo is not an edit.** Services stamp one modification time on a whole
item, so changing a date in Google marks its note as freshly modified too --
even though the note is just the value Task Hub last sent. Task Hub remembers
what it pushed to each service, field by field, and ignores values a service is
merely echoing back. Without this, a stale copy would routinely overwrite newer
edits made elsewhere.

**A real CalDAV server.** Radicale is embedded in the same container and mounted
at `/radicale`. Your iPhone, Mac, Android phone or Thunderbird can sync with it
directly, and Task Hub itself is just another client of it.

---

## What works today

Task Hub is being built one service at a time, and a service only counts as
finished once it has been run against a real account — not when its code is
written. Three of them have not passed that point yet, and this table is the
honest state of each.

| Service | Tasks | Calendar | State |
| --- | --- | --- | --- |
| **Google** | ✓ | ✓ | **Working.** Tested against a live account. |
| **Todoist** | ✓ | — | **Working.** Tested against a live account. |
| **TickTick** | ✓ | — | **Working.** Tested against a live account. |
| **Obsidian** | ✓ | — | **Working.** Tested against live vaults. Read-only unless you turn write-back on. |
| **Radicale** (built in) | ✓ | ✓ | **Working.** This is where everything meets. |
| Apple | — | — | **Not finished.** Written, never run against a real iCloud account, no tests. |
| Microsoft | — | — | **Not finished.** Written, never run against a real account, no tests. |
| Things 3 | — | — | **Not finished.** Hidden in the interface until it can be tried. |

**Please do not set up Apple, Microsoft or Things 3 yet.** Each of them costs
you real effort before you find out whether it works — an Azure app
registration for Microsoft, a second Apple ID used purely as a task store for
Apple — and none of that effort has been repaid by a single successful sync so
far. All three are kept off the services list for that reason, and each says so
on its own page and at the top of its guide. Nothing is removed — you can still
reach them deliberately if you are willing to be the one who finds out — but
you will not be offered them by accident. Apple is next in line, and this table
changes the day it syncs.

Everything else in the project is finished: the sync engine, the field-level
merge, scheduling, sync groups and history, the embedded CalDAV server, the
task and calendar views, and orphan cleanup on disconnect. Backup and restore
from the web interface is the one planned feature still outstanding, so backups
are for now the Docker commands in the install guides — the last thing that
needs a terminal.

---

## How lists map to collections

Each list has one of two roles in a collection, and the difference decides what
lands in it.

**Full member** — tick a collection on that list's row. The list and the
collection then keep each other up to date in both directions, and the list holds
everything the collection holds, whatever service each task came from. This is
what almost every setup wants and it is a single tick.

**Aggregate** — choose the list under another list's *"Also write out to"*. It
then receives only the tasks that came from the list that named it, which is what
makes it useful for gathering several lists somewhere they can be seen and ticked
off together. Completing something there flows back to the original; anything
created there stays there.

Two rules are enforced rather than left as advice. A list may accept write-back
from only one collection, because two would each create their own copy of every
task and then undo one another. And a list synced with a collection through its
own row is never switched off by a save made on another service's page.

---

## Known service limitations

These are limits of the services themselves, verified against their current
APIs. They are surfaced in the interface next to each service rather than
hidden. The three unfinished connectors are included so that the reasons they
are hard are on the record — not as a suggestion to try them.

- **Google Tasks** cannot store a time of day, has no priorities, no tags and no
  location. Its OAuth app must be published to *Production* — in *Testing* mode
  Google expires the login every seven days.
- **TickTick** exposes only tasks and projects to third parties. Its calendar is
  not available at all, it has no webhooks, and its Inbox is unreachable. Its
  listings omit completed tasks entirely, so Task Hub asks about each task that
  disappears rather than reading absence as deletion. Its tokens cannot be
  refreshed, so an expired one needs reconnecting by hand.
- **Todoist** has no calendar, and its 1–4 priority scale runs opposite to
  iCalendar's. Its REST v2 and Sync v9 APIs were retired in early 2026; Task Hub
  uses the unified API v1. Connecting needs nothing more than the personal API
  token from Todoist's own settings — OAuth is offered but not required.
- **Apple Reminders** requires a *second* Apple ID used purely as a task store,
  added to your devices as a manual CalDAV account. Apple's Reminders app moves
  an "upgraded" primary account's lists somewhere CalDAV cannot reach. Apple
  Calendar syncs normally with an app-specific password.
- **Things 3** publishes no API of any kind. Its connector uses a
  community-documented Things Cloud endpoint, which is unofficial and can break
  without warning. Its failures never stall the other services.
- **Obsidian** needs an Obsidian Sync subscription: Sync has no public API, and
  signing in as another device is the only way a server can read a vault. It is
  read-only, enforced by Obsidian's own client rather than by convention. A task
  written on a line holds no time of day; a TaskNotes file does. A plain
  checkbox is never treated as a task.

---

## Tests

Every suite runs without credentials or a network connection, because the merge
rules must be right regardless of which services happen to be connected. They
also run against a throwaway database, so they are safe to run while Task Hub is
syncing real accounts:

```
./run-tests.sh
```

`test_merge` covers the field-level rules directly, and `test_engine` drives the
whole pull/merge/push loop against stub services — one of which mimics Google
Tasks by refusing to store a time of day. `test_forwarded` covers the address
detection above, one case per deployment shape, because a regression there
breaks sign-in for one whole class of user with an error that points elsewhere.

The same suites run on every push, and no image is published unless they pass.

## Architecture

Python 3.12, FastAPI, SQLite, Jinja2 templates, hand-written CSS and vanilla ES
modules. No Node build step and no CDN — every asset is vendored, so the
container works offline and behaves identically on macOS, Windows and Linux.

```
app/
  main.py            FastAPI app; mounts Radicale WSGI at /radicale
  config.py          Paths, runtime config, key bootstrapping
  crypto.py          Fernet encryption for credentials; bcrypt for passwords
  radicale_embed.py  Embedded CalDAV server and htpasswd management
  db/                SQLAlchemy models, session handling, settings store
  services/          iCalendar domain model and the CalDAV client
  connectors/        base.py (the interface + capability declarations),
                     google.py, radicale_local.py
  sync/              merge.py (the field merge), engine.py (orchestration),
                     scheduler.py, ratelimit.py
  web/               Routers: setup, auth, overview, tasks, calendar,
                     radicale admin, services, google setup, sync, settings, docs
  templates/         Jinja2 templates
  static/            CSS design tokens and client-side behaviour
docs/                Setup guides, rendered inside the application
```

**Why one container.** Radicale ships a WSGI application, so it is mounted inside
the FastAPI app rather than run separately. That means one image, one port and
one process to supervise. Task Hub still talks to it over loopback HTTP through
the standard `caldav` library, so Radicale stays swappable for an external server
without touching any sync code.

**Credentials at rest.** OAuth refresh tokens and app-specific passwords are
encrypted with a Fernet key stored in a separate file from the database. Copying
`taskhub.db` without `secret.key` yields nothing useful. Passwords are hashed
with bcrypt, and the same hash format serves both the web login and Radicale's
htpasswd file.

---

## Data and backup

Everything lives in the Docker volume `taskhub-data`: the database, the
encryption key, and all Radicale collections.

`docker compose down` is safe. **`docker compose down -v` erases everything.**

Backup and restore commands are in
[docs/getting-started.md](docs/getting-started.md#backing-up). The backup file
contains the key to every saved credential, so treat it like a password database.

---

## Security notes

- Session cookies are signed and marked `SameSite=Lax`.
- Login failures do not reveal whether the username exists.
- Setup becomes unreachable once completed.
- The `next` parameter on login only accepts local paths, so it cannot be used to
  redirect elsewhere.
- Deleting a collection requires typing its name.
- There is no password reset. Task Hub has no mail server, so a lost password
  means starting over. The wizard says so before you choose one.

Task Hub is designed to be internet-reachable, but put it behind HTTPS before
exposing it — over plain HTTP the login and every task travel in clear text. A
VPN such as Tailscale is the simplest safe option; a reverse proxy with a real
certificate is the other.
