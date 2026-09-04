# Installing on Windows

Every command below can be copied and pasted exactly as written. Nothing needs
editing, and nothing asks you a question you have to work out the answer to.

There are two paths. Take **[A — a machine with nothing installed](#a--a-machine-with-nothing-installed)**
if this is a fresh start, or **[B — a machine that already has Docker](#b--a-machine-that-already-has-docker)**
if you have used Docker here before. If you are not sure, start with
[the check](#first-what-is-already-here) — it tells you which one you are.

**Should Task Hub live here?** Only if this computer is switched on when you
want your tasks to sync. A laptop that is asleep is not syncing. Windows is an
excellent place to *try* it, and moving to a Raspberry Pi or a NAS afterwards
takes two clicks — there is a section on that at the end.

---

## The way with no typing at all

Docker Desktop can download and start Task Hub from its own window. There is no
terminal, no file to create and nothing to edit. If you have never used Docker
before, this is the route to take.

### 1. Install Docker Desktop

Download it from **https://www.docker.com/products/docker-desktop/** and run the
installer, taking every default. It will ask to restart your computer at the end; let it.

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
| **Volumes → Host path** | `C:\\TaskHub\\data` — click the field, then **Browse**, make a new folder called `TaskHub` on your C: drive with a `data` folder inside it, and choose that |
| **Volumes → Container path** | `/data` |

Windows may pop up a **File Sharing** permission box the first time. Click **Share it**.

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

Open **PowerShell**: press the Start button, type `powershell`, press Enter.
Then paste this in one piece and press Enter.

```powershell
Write-Host "`nWindows:" (Get-CimInstance Win32_OperatingSystem).Caption, (Get-CimInstance Win32_OperatingSystem).Version
Write-Host "Processor:" $env:PROCESSOR_ARCHITECTURE
Write-Host "Memory:" ([math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)) "GB"
Write-Host "Free disk:" ([math]::Round((Get-PSDrive C).Free/1GB,1)) "GB"
if (Get-Command docker -ErrorAction SilentlyContinue) {
  Write-Host "Docker:" (docker --version)
  try { Write-Host "Compose:" (docker compose version --short) } catch { Write-Host "Compose: MISSING" }
  try { docker info *> $null; Write-Host "Docker is running: yes" } catch { Write-Host "Docker is running: NO - start Docker Desktop" }
} else { Write-Host "Docker: NOT INSTALLED" }
if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) {
  Write-Host "Port 8080: IN USE - you will need a different port"
} else { Write-Host "Port 8080: free" }
Write-Host "`nThis computer's address on your network:"
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | ForEach-Object { Write-Host "  http://$($_.IPAddress):8080" }
```

It changes nothing. Read the Docker line:

- **`Docker: NOT INSTALLED`** → go to [path A](#a--a-machine-with-nothing-installed).
- **Anything else** → go to [path B](#b--a-machine-that-already-has-docker).

### What the answers need to be

| | |
| --- | --- |
| Windows | 10 version 22H2 or later, or Windows 11. Older versions cannot run current Docker Desktop. |
| Processor | `AMD64` or `ARM64`. Both are fine; Task Hub is published for both. |
| Memory | 4 GB minimum, because Docker Desktop itself uses some. Task Hub needs about 230 MB of it. |
| Free disk | 3 GB or more. |

---

## A — a machine with nothing installed

### A1. Install Docker Desktop

Paste this into PowerShell:

```powershell
winget install -e --id Docker.DockerDesktop
```

That is Microsoft's own installer service fetching Docker's own package. It
prints progress and finishes with `Successfully installed`.

<br>

**If `winget` is not recognised**, you are on an older Windows. Download the
installer by hand instead from
[docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/),
run it, and take every default.

<br>

**Restart the computer when it asks.** Docker Desktop turns on Windows features
— WSL 2, the Linux system built into Windows — and they are not active until
you do.

### A2. Start Docker Desktop once

Press Start, type `Docker Desktop`, press Enter. Accept the licence terms when
they appear. Wait until the whale icon in the bottom-left corner stops
animating and the window says **Engine running**.

Leave it running. It starts with Windows from now on, so this is the only time
you do this by hand.

### A3. Check it works

```powershell
docker run --rm hello-world
```

You should see **"Hello from Docker!"**. If not, Docker Desktop has not
finished starting — wait a minute and try again.

Now continue at [Install Task Hub](#install-task-hub).

---

## B — a machine that already has Docker

Everything you need may already be here. Three things to confirm.

### B1. Is Docker running?

The check above told you. If it said **`Docker is running: NO`**, start Docker
Desktop from the Start menu and wait for **Engine running**.

### B2. Is Compose the modern one?

Task Hub's instructions use `docker compose` — two words, a space, no hyphen.

```powershell
docker compose version
```

If that errors but `docker-compose --version` works, you have the old
standalone version. Update Docker Desktop and the modern one arrives with it:

```powershell
winget upgrade -e --id Docker.DockerDesktop
```

### B3. Is port 8080 free?

If the check said **`Port 8080: IN USE`**, something else on this machine
answers on that number. That is fine — Task Hub can use another. Note it now
and use it at [step 3](#step-3--choose-a-different-port-only-if-you-need-to)
below.

To see what is using it:

```powershell
Get-Process -Id (Get-NetTCPConnection -LocalPort 8080 -State Listen).OwningProcess
```

Now continue.

---

## Install Task Hub

Three steps, identical on both paths.

### Step 1 — Make a folder and fetch one file

```powershell
mkdir "$HOME\taskhub" -Force
cd "$HOME\taskhub"
curl.exe -fsSL -o docker-compose.yml https://raw.githubusercontent.com/Sparkinman/task-hub/main/docker-compose.yml
```

**`curl.exe`, not `curl`.** In PowerShell, plain `curl` is a different command
that will not do what you want. The `.exe` matters.

Nothing in that file needs editing.

### Step 2 — Tell it your timezone

```powershell
"TZ=$((Get-TimeZone).Id)" | Out-File -Encoding ascii .env
```

Optional. The setup wizard asks you anyway; this just makes "due today" correct
from the very first minute.

### Step 3 — Choose a different port, only if you need to

Skip this unless the check said port 8080 was in use.

```powershell
"TASKHUB_HTTP_PORT=9090" | Out-File -Encoding ascii -Append .env
```

Then use `9090` instead of `8080` everywhere below.

### Step 4 — Start it

```powershell
docker compose up -d
```

The first run downloads about 150 MB, which unpacks to about 650 MB. It takes a
few minutes and finishes with `Container taskhub Started`.

### Step 5 — Check it started properly

```powershell
docker compose ps
```

Wait until the STATUS column says **healthy** — up to half a minute. Task Hub
also appears in Docker Desktop's Containers list, where you can start and stop
it by clicking if you prefer that to typing.

### Step 6 — Open it

Go to **http://localhost:8080** in your browser.

The setup wizard asks you to create a login, choose a timezone, and pick a
CalDAV password for the phones you connect later. **Write the CalDAV password
down** — it is shown once, and every phone will need it.

---

## Connecting your services

Working from `localhost` has one large advantage: **Google and Microsoft both
accept it**, and they accept almost nothing else without a real HTTPS address.
So this is the easiest place there is to connect them. Todoist, TickTick and
Obsidian work here too.

Those connections keep working if you later move Task Hub elsewhere — renewing
one does not involve your address. See
[How Task Hub finds its own address](addresses.md) for the full picture.

---

## Reaching it from your phone

`localhost` means "this device" to whatever device it is typed into, so a phone
cannot use it. Use this computer's network address instead — the check at the
top printed it, or:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" } | Select-Object IPAddress
```

Then your phone uses `http://<that number>:8080`, and Task Hub's Radicale page
gives the exact steps for each kind of phone.

**Windows Firewall will ask the first time.** When a prompt appears about
Docker Desktop, allow it on **Private networks**. If you dismissed it and your
phone times out, this re-allows it:

```powershell
New-NetFirewallRule -DisplayName "Task Hub" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow -Profile Private
```

Run PowerShell as Administrator for that one: right-click the Start button and
choose **Terminal (Admin)**.

---

## Keeping it running

**Backing up, restoring and restarting** are all in Task Hub itself, under
**Settings → Backup and restore**. No commands needed.

**Updating** is the one thing that has to come from outside, because a
container cannot replace itself. Either click the update arrow beside Task Hub
in Docker Desktop's Containers list, or:

```powershell
cd "$HOME\taskhub"
docker compose pull
docker compose up -d
```

**Stopping and starting**: use the buttons in Docker Desktop, or:

```powershell
cd "$HOME\taskhub"
docker compose stop
docker compose start
```

---

## Moving to a Raspberry Pi or NAS later

1. Here: **Settings → Backup and restore → Download backup**.
2. Install Task Hub on the new machine, following
   [its guide](install-raspberry-pi.md).
3. There: **Settings → Backup and restore**, choose the file, type RESTORE.

Everything moves: your tasks, your connected services, your settings. You sign
in on the new machine with the password from the backup.

---

## If something is wrong

**"docker: The term 'docker' is not recognized."**
PowerShell was open before Docker was installed. Close it, open a new one, try
again.

**The page will not load.**
Is Docker Desktop running? It must be. Then `docker compose ps` — if STATUS is
not `healthy`, run `docker compose logs --tail=50` and read the reason.

**"port is already allocated".**
Something else uses 8080. See [step 3](#step-3--choose-a-different-port-only-if-you-need-to).

**"WSL 2 installation is incomplete."**
Run `wsl --install` in an Administrator PowerShell, restart, and start Docker
Desktop again.

**Docker Desktop says virtualization is disabled.**
It has to be turned on in your computer's BIOS. Check first — this often
reports wrongly:

```powershell
Get-CimInstance Win32_Processor | Select-Object VirtualizationFirmwareEnabled, SecondLevelAddressTranslationExtensions
```

**It is slow the first time.**
Docker Desktop runs a small Linux system underneath and is slow to wake. Later
starts take seconds.
