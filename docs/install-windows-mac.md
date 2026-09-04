# Installing on Windows or a Mac

Task Hub runs on any computer that can run Docker, which includes every Mac made
since 2012 and any Windows 10 or 11 machine. This is the quickest way to try it:
about ten minutes, most of which is a download.

**Should you run it here permanently?** Only if this computer is on when you
want your tasks to sync. Syncing happens on a timer, and a laptop that is asleep
is not syncing. For an always-on setup, a Raspberry Pi or a NAS is a better
home — see [Installing on a Raspberry Pi](install-raspberry-pi.md) or
[Installing on a NAS](install-nas.md). Moving later is easy: everything Task Hub
owns is one folder, and there are instructions for copying it at the end.

---

## Step 1 — Install Docker Desktop

Download it from
[docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
and run the installer.

**On a Mac**, the page offers two versions. Choose **Apple Chip** for any Mac
from 2020 onwards, and **Intel Chip** for older ones. If you are not sure: click
the Apple menu → About This Mac. "Apple M1", "M2", "M3" or "M4" means Apple
Chip.

**On Windows**, take the defaults. The installer may enable WSL 2 — a Linux
system built into Windows — and ask you to restart. Let it; Docker needs it and
nothing else changes.

Open Docker Desktop once after installing and leave it running. It has to be
running for Task Hub to run, and by default it starts with your computer.

---

## Step 2 — Make a folder and put one file in it

Task Hub needs exactly one configuration file, which tells Docker what to
download.

**On a Mac**, open Terminal (Applications → Utilities → Terminal) and paste:

```
mkdir -p ~/taskhub && cd ~/taskhub
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/Sparkinman/task-hub/main/docker-compose.yml
```

**On Windows**, open Windows Terminal or PowerShell (press Start and type
"terminal") and paste:

```
mkdir "$HOME\taskhub" -Force; cd "$HOME\taskhub"
curl.exe -fsSL -o docker-compose.yml https://raw.githubusercontent.com/Sparkinman/task-hub/main/docker-compose.yml
```

Nothing in that file needs editing.

---

## Step 3 — Start it

Both systems, same command:

```
docker compose up -d
```

The first run downloads about 150 MB, which unpacks to about 650 MB on disk,
and takes a few minutes. It finishes by
saying `Container taskhub Started`.

Check on it:

```
docker compose ps
```

Wait for the STATUS column to say **healthy**. You will also see Task Hub appear
in Docker Desktop's Containers list, where you can start and stop it by clicking
if you prefer that to typing.

---

## Step 4 — Open it

Go to **http://localhost:8080** in your browser.

The setup wizard asks you to create a login, choose a timezone, and pick a
CalDAV password for the phones and apps you connect later. **Write the CalDAV
password down** — it is shown once, and every phone will need it.

---

## Step 5 — Connect your services

Working from `localhost` has one large advantage: **Google and Microsoft both
accept it**, and they accept almost nothing else without a real HTTPS address.
So this is the easiest place to connect them. Todoist, TickTick and Obsidian
work here too.

If you later move Task Hub to a Pi or a NAS, those connections keep working —
renewing them does not involve your address. See
[How Task Hub finds its own address](addresses.md) for the whole picture.

---

## Reaching it from your phone

`localhost` means "this device" to whatever device it is typed into, so it is
not an address a phone can use. To sync a phone, give it this computer's address
on your network instead.

**On a Mac:** System Settings → Network → your connection → Details → TCP/IP.
The IP address is there.

**On Windows:** Settings → Network & Internet → Wi-Fi (or Ethernet) →
Properties, and look for "IPv4 address".

Then the address for your phone is `http://<that number>:8080`, and the Radicale
page in Task Hub gives the exact steps for each kind of phone.

**Windows only:** the first time something connects from another device,
Windows Firewall may block it. Allow Docker Desktop when prompted, or the phone
will simply time out with no explanation.

---

## Keeping it running

**Updating.** In the same folder:

```
docker compose pull
docker compose up -d
```

**Backing up.** Settings → Backup and restore → **Download backup**. One file,
saved by your browser, containing everything Task Hub owns: the database, your
saved service logins, the key that decrypts them, every calendar and task, and
your settings. Keep it somewhere other than this machine.

> That file can unlock every service you have connected. Treat it like a
> password list, because that is what it is.

**Restoring.** Same page: choose the file, type RESTORE to confirm, and Task
Hub replaces everything and restarts itself. The archive is checked before
anything is touched, and the data being replaced is set aside rather than
deleted. Afterwards you sign in with the password from the backup.

**Restarting.** Settings → Backup and restore → **Restart now**.

None of this needs a terminal.

**Moving to a Pi or a NAS later.** Install Task Hub there, download a backup
from this machine, and upload it under Restore on the new one. Everything moves
across: your tasks, your connected services and your settings.

---

## If something is wrong

**The page will not load.** Is Docker Desktop running? It has to be. Then
`docker compose ps` — if STATUS is not `healthy`, `docker compose logs
--tail=50` says why.

**"port is already allocated".** Something else on this computer uses 8080.
Create a file called `.env` in the same folder containing
`TASKHUB_HTTP_PORT=9090`, run `docker compose up -d` again, and use
`http://localhost:9090`.

**Windows: "docker: command not found".** Docker Desktop was installed but the
terminal was open before it. Close the terminal, open a new one, try again.

**Mac: it is very slow the first time.** Docker Desktop on macOS runs a small
Linux system underneath and is slow to wake. Later starts take seconds.
