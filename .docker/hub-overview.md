# Task Hub

**One place where all your tasks and calendars agree.** Runs on your own
hardware. Your data never leaves it.

Task Hub syncs tasks and calendars between the services you already use —
Google, Todoist, TickTick, Microsoft, Apple iCloud, Obsidian, Things 3,
Supernote and any CalDAV server — and gives your phone and desktop apps a
CalDAV server of their own to talk to. Tick something off in one place and it
is ticked off everywhere.

- **Self-hosted.** One container. No account with anybody, no cloud service in
  the middle, no telemetry.
- **Nothing is lost.** Due times, timezones, priorities, repeat rules, tags and
  notes survive the round trip wherever the far end can hold them — and where a
  service cannot (Google Tasks and Microsoft To Do both discard the time of
  day), Task Hub keeps the value separately so that service cannot erase it.
- **Supernote's built-in To-Do app, both ways.** Task Hub is the only thing
  outside Supernote's own apps that reads it — not a CalDAV app installed on
  the tablet, but the real one. It also reads and writes **Digest** — the
  passages you highlight out of documents, with the notes you add to them in
  pen as well as typed — and backs your handwritten notebooks up as PDFs you
  can read on any device. A task written on a page of
  a notebook links straight to that page, when the notebook is one you have
  backed up. Ratta publish no API for this, so it may
  change at any time, and Task Hub says so plainly wherever it is shown.
- **It works alongside Supernote Cloud, not instead of it.** This is not a
  private cloud instance and nothing has to be migrated. Your tablet stays
  signed in to Ratta's own service and keeps the Partner app, the web viewer
  and syncing between your devices; Task Hub signs in as you would and reads
  the to-dos, backs the notebooks up as PDFs, shows the digests and carries
  your tasks out to everything else. Disconnecting it leaves the account
  exactly as it was.
- **Fixes the Supernote's one-calendar limit.** Ratta's own calendar app can
  subscribe to exactly one calendar. **Task Hub aggregates as many as you like
  — work, family, shared — into a single feed the tablet can use**, so the
  device shows everything instead of making you pick.
- **Installs on your phone.** Add it to a home screen and it opens like an app:
  own icon, full screen, no browser bar. Needs an HTTPS address, which the
  built-in Cloudflare tunnel or Tailscale provides. It can send notifications
  too — tasks due, sync failures and expiring sign-ins, each off until you ask
  for it, and encrypted before they leave your server.
- **Set up in a browser.** No configuration files to edit and no terminal
  commands after the container is running.

🖊️ **[Task Hub Supernote plugin](https://github.com/Sparkinman/task-hub-supernote-plugin)** — a companion that runs on
the tablet: handwriting captured straight into tasks, with on-device lists,
calendar views and a daily agenda.

📖 **[Full documentation and setup guides](https://github.com/Sparkinman/task-hub)**

---

## Quick start

```yaml
services:
  taskhub:
    image: sparkinman/task-hub:latest
    container_name: taskhub
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - taskhub-data:/data

volumes:
  taskhub-data:
```

```
docker compose up -d
```

Then open **http://localhost:8080** and follow the setup wizard, which creates
your sign-in, your CalDAV credentials and your first collections.

On a Raspberry Pi or any Linux box, one line does the whole thing including
installing Docker:

```
curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/install.sh | sh
```

---

## Tags

| Tag | What it is |
| --- | --- |
| `latest` | The current release. What the instructions above pull. |
| `edge`, `main` | Every commit on the main branch. |
| `1.2.3`, `1.2`, `1` | Pinned versions, if you would rather updates never surprise you. |

Built for **linux/amd64** and **linux/arm64**, from the same commit, on every
push — so a Raspberry Pi never has to compile anything.

## Configuration

| | |
| --- | --- |
| **Port** | `8080` inside the container. Map it wherever you like. |
| **Data** | `/data` — the database, the CalDAV collections and the encryption key. Back this volume up; everything else is replaceable. |
| **Timezone** | Set in the web interface, not in the environment. |

Nothing else is needed to start. Connected accounts, addresses, backups and the
daily summary email are all configured in the browser.

## What it connects to

**Services it syncs with:** Google Tasks and Calendar · Todoist · TickTick ·
Microsoft To Do and Outlook Calendar · Apple iCloud · Obsidian · Things 3
(import) · any CalDAV server such as Nextcloud, Fastmail or Baïkal.

**Apps that connect to it:** Apple Calendar and Reminders · DAVx⁵ and Tasks.org
on Android · Thunderbird · Super Productivity · GNOME and KDE calendars ·
Home Assistant · anything else that speaks CalDAV.

[The full list, with what is verified and what is merely expected to work](https://github.com/Sparkinman/task-hub/blob/main/docs/compatibility.md)

## Source and licence

[github.com/Sparkinman/task-hub](https://github.com/Sparkinman/task-hub) —
GPLv3. Also published to `ghcr.io/sparkinman/task-hub`.
