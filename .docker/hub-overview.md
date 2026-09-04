# Task Hub

**One place where all your tasks and calendars agree.** Runs on your own
hardware. Your data never leaves it.

Task Hub syncs tasks and calendars between the services you already use —
Google, Todoist, TickTick, Microsoft, Apple iCloud, Obsidian, Things 3 and any
CalDAV server — and gives your phone and desktop apps a CalDAV server of their
own to talk to. Tick something off in one place and it is ticked off everywhere.

- **Self-hosted.** One container. No account with anybody, no cloud service in
  the middle, no telemetry.
- **Nothing is lost.** Due times, timezones, priorities, repeat rules, tags and
  notes survive the round trip wherever the far end can hold them — and where a
  service cannot (Google Tasks and Microsoft To Do both discard the time of
  day), Task Hub keeps the value separately so that service cannot erase it.
- **Set up in a browser.** No configuration files to edit and no terminal
  commands after the container is running.

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
