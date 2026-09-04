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

## The way with no typing at all

Docker Desktop can download and start Task Hub from its own window. There is no
terminal, no file to create and nothing to edit. If you have never used Docker
before, this is the route to take.

### 1. Install Docker Desktop

Download it from **https://www.docker.com/products/docker-desktop/** and run the
installer, taking every default. Choose the version matching your Mac — **Apple Silicon** for M1 and later, **Intel** for older ones. If unsure, click the Apple menu → About This Mac.

### 2. Open Docker Desktop once

Accept the licence when it asks. Wait until the bottom-left corner says
**Engine running** with a green dot. The first start takes a minute or two.

You can ignore everything else in the window, and you never need a Docker
account — skip the sign-in prompts.

### 3. Download Task Hub

In the search box at the top of the Docker Desktop window, type:

```
sparkinman/task-hub
```

Press Enter, find **sparkinman/task-hub** in the results, and click **Pull**.
That downloads about 150 MB and takes a few minutes. When it finishes, close the
search results.

### 4. Start it

Go to the **Images** tab on the left. You will see `sparkinman/task-hub` listed.
Click the **▶ Run** button on its row.

A dialog appears. Click **Optional settings** to open it, and fill in three
things:

| Field | What to put |
|---|---|
| **Container name** | `taskhub` |
| **Host port** | `8080` |
| **Volumes → Host path** | a new folder called `TaskHub` in your Home folder — click **Browse**, press Cmd+Shift+N to make it, and choose it |
| **Volumes → Container path** | `/data` |

macOS may ask permission to access that folder the first time. Click **OK**.

Then click **Run**.

> **The volume is the important one.** It is where Task Hub keeps your accounts,
> settings and tasks. Without it, everything you set up disappears the next time
> the container is replaced — which is what an update does. Take the extra
> fifteen seconds.

### 5. Open it

Go to the **Containers** tab. `taskhub` should be listed as **Running**. Give it
half a minute to finish starting, then open a browser at:

```
http://localhost:8080
```

Task Hub greets you with its setup wizard: create a login, pick a timezone, and
choose a CalDAV password for the phones and apps you will connect later.

**Write the CalDAV password down.** It is shown once and is what every phone and
calendar app will need.

Everything from here — connecting your services, choosing what syncs, backups,
restores, restarting — happens in that browser window.

### Updating later

In Docker Desktop: search for `sparkinman/task-hub` and **Pull** again, then on
the **Containers** tab stop and delete the old `taskhub` container and run the
new image exactly as in step 4, with the same port and the same volume. Your
data is in the volume, so it comes back with everything intact.

---

## The way with a compose file

Everything below is the alternative for people who would rather use a terminal,
or who want the configuration written down in a file they can keep. It produces
exactly the same result. **If you followed the section above, you are done and
can stop reading.**

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
