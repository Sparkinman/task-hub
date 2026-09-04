# Installing on a Mac

Every command below can be copied and pasted exactly as written. Nothing needs
editing.

There are two paths. Take **[A — a Mac with nothing installed](#a--a-mac-with-nothing-installed)**
if this is a fresh start, or **[B — a Mac that already has Docker](#b--a-mac-that-already-has-docker)**
if you have used Docker here before. The check below tells you which you are.

**Should Task Hub live here?** Only if this Mac is awake when you want your
tasks to sync — a sleeping laptop is not syncing. A Mac is an excellent place
to *try* it, and moving to a Raspberry Pi or a NAS afterwards is two clicks;
there is a section at the end.

---

## First: what is already here

Open **Terminal**: press ⌘ Space, type `terminal`, press Enter. Paste this in
one piece and press Enter.

```
echo; echo "macOS:      $(sw_vers -productVersion)"
echo "Processor:  $(uname -m)"
echo "Memory:     $(( $(sysctl -n hw.memsize) / 1073741824 )) GB"
echo "Free disk:  $(df -h / | awk 'NR==2 {print $4}')"
if command -v docker >/dev/null 2>&1; then
  echo "Docker:     $(docker --version)"
  docker compose version --short >/dev/null 2>&1 && echo "Compose:    $(docker compose version --short)" || echo "Compose:    MISSING"
  docker info >/dev/null 2>&1 && echo "Running:    yes" || echo "Running:    NO - open Docker Desktop"
else
  echo "Docker:     NOT INSTALLED"
fi
lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1 && echo "Port 8080:  IN USE" || echo "Port 8080:  free"
echo; echo "This Mac's address on your network:"
ipconfig getifaddr en0 2>/dev/null | sed 's|^|  http://|;s|$|:8080|'
ipconfig getifaddr en1 2>/dev/null | sed 's|^|  http://|;s|$|:8080|'
```

It changes nothing. Read the Docker line:

- **`Docker: NOT INSTALLED`** → [path A](#a--a-mac-with-nothing-installed).
- **Anything else** → [path B](#b--a-mac-that-already-has-docker).

### What the answers need to be

| | |
| --- | --- |
| macOS | 13 (Ventura) or later for current Docker Desktop. |
| Processor | `arm64` is an Apple-chip Mac (M1 and later); `x86_64` is an Intel one. Both work — Task Hub is published for both. |
| Memory | 8 GB is comfortable; 4 GB works. Docker Desktop itself uses a slice of it. |
| Free disk | 3 GB or more. |

---

## A — a Mac with nothing installed

### A1. Install Docker Desktop

**If you have Homebrew** (most developers do — `brew --version` says so):

```
brew install --cask docker
```

**If you do not**, download it from
[docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/).
The page offers two versions and choosing wrongly wastes a download:

- **Apple Chip** — for M1, M2, M3, M4. This is what `arm64` above means.
- **Intel Chip** — for older Macs. This is what `x86_64` means.

Open the downloaded `.dmg` and drag Docker into Applications.

### A2. Start Docker Desktop once

Open it from Applications. Accept the licence terms, and give it your password
when it asks — it installs a small helper the first time. Wait until the whale
in the menu bar stops animating.

Leave it running. It starts with your Mac from now on.

### A3. Check it works

```
docker run --rm hello-world
```

You should see **"Hello from Docker!"**.

Now continue at [Install Task Hub](#install-task-hub).

---

## B — a Mac that already has Docker

Three things to confirm.

### B1. Is it running?

The check told you. If it said **`Running: NO`**, open Docker Desktop from
Applications and wait for the whale to settle.

### B2. Is Compose the modern one?

Task Hub uses `docker compose` — two words, a space, no hyphen.

```
docker compose version
```

If that errors but `docker-compose --version` works, you have only the old
standalone version. Update Docker Desktop:

```
brew upgrade --cask docker
```

or use **Check for Updates** in Docker Desktop's menu.

### B3. Is port 8080 free?

If the check said **`Port 8080: IN USE`**, note it and use
[step 3](#step-3--choose-a-different-port-only-if-you-need-to) below. To see
what is holding it:

```
lsof -nP -iTCP:8080 -sTCP:LISTEN
```

---

## Install Task Hub

Identical on both paths.

### Step 1 — Make a folder and fetch one file

```
mkdir -p ~/taskhub && cd ~/taskhub
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/Sparkinman/task-hub/main/docker-compose.yml
```

Nothing in that file needs editing.

### Step 2 — Tell it your timezone

```
echo "TZ=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')" > .env
```

Optional — the wizard asks anyway.

### Step 3 — Choose a different port, only if you need to

Skip unless the check said 8080 was in use.

```
echo "TASKHUB_HTTP_PORT=9090" >> .env
```

Then use `9090` instead of `8080` everywhere below.

### Step 4 — Start it

```
docker compose up -d
```

The first run downloads about 150 MB, unpacking to about 650 MB. It ends with
`Container taskhub Started`.

### Step 5 — Check it started properly

```
docker compose ps
```

Wait for STATUS to say **healthy** — up to half a minute.

### Step 6 — Open it

Go to **http://localhost:8080**.

The wizard asks for a login, a timezone, and a CalDAV password for the devices
you connect later. **Write the CalDAV password down** — it is shown once.

---

## Connecting your services

`localhost` has one large advantage: **Google and Microsoft both accept it**,
and almost nothing else without real HTTPS. So this is the easiest place there
is to connect them. Todoist, TickTick and Obsidian work here too, and those
connections survive moving Task Hub elsewhere later. See
[How Task Hub finds its own address](addresses.md).

---

## Syncing your Mac's own Calendar and Reminders

Task Hub is a CalDAV server, so macOS can subscribe to it directly:

**Calendar → Settings → Accounts → + → Other CalDAV Account.** Set Account Type
to **Manual**, then enter the server address from Task Hub's Radicale page,
your CalDAV username, and the CalDAV password from setup.

Tasks appear in Reminders, events in Calendar.

If you are syncing from another device rather than this Mac, use this Mac's
network address rather than `localhost` — the check at the top printed it.

---

## Keeping it running

**Backing up, restoring and restarting** are inside Task Hub, under
**Settings → Backup and restore**. No commands.

**Updating** must come from outside, because a container cannot replace itself.
Click the update arrow beside Task Hub in Docker Desktop, or:

```
cd ~/taskhub
docker compose pull
docker compose up -d
```

**Stopping and starting**: Docker Desktop's buttons, or:

```
cd ~/taskhub
docker compose stop
docker compose start
```

---

## Moving to a Raspberry Pi or NAS later

1. Here: **Settings → Backup and restore → Download backup**.
2. Install Task Hub there, following [its guide](install-raspberry-pi.md).
3. There: **Settings → Backup and restore**, choose the file, type RESTORE.

Everything moves across. You sign in on the new machine with the password from
the backup.

---

## If something is wrong

**"command not found: docker".**
Terminal was open before Docker was installed. Close it, open a new one.

**The page will not load.**
Is Docker Desktop running? Then `docker compose ps`; if STATUS is not
`healthy`, `docker compose logs --tail=50` says why.

**"port is already allocated".**
Something else uses 8080 — see [step 3](#step-3--choose-a-different-port-only-if-you-need-to).

**It is slow the first time.**
Docker Desktop runs a small Linux system underneath and is slow to wake. Later
starts take seconds.

**Docker Desktop will not start after a macOS upgrade.**
Reinstalling over the top fixes it and keeps your data:
`brew reinstall --cask docker`, or download it again.
