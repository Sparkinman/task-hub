# Getting started

This guide covers installing Task Hub, keeping your data safe, and connecting
your phone and laptop to the built-in CalDAV server. Connecting Google, Todoist,
TickTick, Microsoft, Apple and Things 3 each have their own guide.

You do not need to know how to program to follow this. There is exactly one
command in the whole document.

---

## What Task Hub is

Task Hub runs as a small web application on a computer you control. It does two
jobs:

1. It runs a **Radicale CalDAV server**, which is where your tasks and calendars
   actually live. This is a real, standard server — your iPhone, your Mac, your
   Android phone and Thunderbird can all sync with it directly.
2. It **synchronises** that server with your accounts on Google, Todoist,
   TickTick, Microsoft, Apple and Things 3, so that a task created in any one of
   them shows up in all the others.

Everything is configured through the web page. There is never a configuration
file to edit or a command to run.

---

## Installing

### What you need

- A computer that will stay switched on — a home server, an always-on desktop, a
  Raspberry Pi, or a virtual machine.
- **Docker Desktop** (macOS and Windows) or **Docker Engine** (Linux).

Task Hub runs identically on all three. The only difference is how you install
Docker.

### Step 1 — Install Docker

**macOS.** Download Docker Desktop from `docker.com/products/docker-desktop`,
open the `.dmg`, and drag Docker to Applications. Launch it and wait for the
whale icon in the menu bar to stop animating.

**Windows.** Download Docker Desktop from the same address and run the
installer. It will ask to enable WSL 2; say yes. Restart when prompted, then
launch Docker Desktop and wait for it to report that the engine is running.

**Linux (Ubuntu, Debian).** Follow the official instructions at
`docs.docker.com/engine/install`. Choose your distribution and copy the commands
shown there.

### Step 2 — Start Task Hub

Put the Task Hub folder somewhere sensible — your home directory is fine. Open a
terminal in that folder and run:

```
docker compose up -d
```

This is the only command you will ever need. It downloads what it needs, builds
the image and starts the application in the background.

The first run takes a few minutes. Subsequent starts take a couple of seconds.

### Step 3 — Open it

Go to **http://localhost:8080** in your browser.

The setup wizard appears. Work through it: it asks for a username and password
for the web page, your timezone, a separate username and password for the CalDAV
server, the names of your first task list and calendar, and how often to sync.

That is the whole installation.

---

## Everyday commands

You will rarely need these, but they are worth knowing.

| What you want | Command |
| --- | --- |
| Start Task Hub | `docker compose up -d` |
| Stop it | `docker compose down` |
| Restart it | `docker compose restart` |
| See what it is doing | `docker compose logs -f` |
| Update after changing the code | `docker compose up -d --build` |

> **Never run `docker compose down -v`.** The `-v` deletes the data volume,
> which erases your tasks, your settings and your saved connections. Plain
> `docker compose down` is always safe.

---

## Reaching Task Hub from other devices

`localhost` only works on the machine running Docker. To reach Task Hub from
your phone or another computer, you need the server's address on your network.

**Find the address.** On the machine running Task Hub:

- **macOS**: System Settings → Network → your connection → Details → IP address.
- **Windows**: Settings → Network & Internet → Wi-Fi or Ethernet → IP address.
- **Linux**: run `hostname -I` and take the first address.

It will look like `192.168.1.42`. Other devices on the same network can then
reach Task Hub at `http://192.168.1.42:8080`.

**Tell Task Hub its own address.** Go to **Settings → Public address** and enter
that same address, for example `http://192.168.1.42:8080`. This matters for two
reasons: it is the address shown to you for CalDAV clients, and it is what gets
registered as the OAuth redirect address with Google and Microsoft. If it is
wrong, those connections will fail with a redirect mismatch error.

### Reaching it from outside your home — the built-in tunnel

The simplest safe route is a Cloudflare tunnel, and Task Hub ships one.

It runs **beside Task Hub**, not on your router or your NAS. That matters if
Task Hub lives in a virtual machine: a VM usually cannot reach the address of
the very host it runs on, because its traffic leaves to the network switch and
is never sent back. A tunnel running on that host therefore cannot see the VM,
which shows up as a **502 Bad Gateway** no matter how the tunnel is configured.
Running the tunnel alongside Task Hub avoids the problem entirely — it reaches
the application over the container network and only ever makes outbound
connections.

1. In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create a
   tunnel**. Give it a name and copy the **token**.
2. Create a file called `.env` next to `docker-compose.yml` containing:

   ```
   CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi...your token...
   ```

3. Back in Cloudflare, add a **public hostname** for the tunnel — say
   `tasks.yourdomain.com` — and set its service to:

   ```
   http://taskhub:8080
   ```

   That is the container's name on Task Hub's own network, so no IP addresses
   are involved and nothing breaks when they change.
4. Start it:

   ```
   docker compose --profile tunnel up -d
   ```

Everything then arrives on that one hostname over HTTPS:

| Address | What it is |
| --- | --- |
| `https://tasks.yourdomain.com/` | the web interface |
| `https://tasks.yourdomain.com/radicale/<user>/` | CalDAV for Thunderbird, DAVx5, tasks.org |

No inbound port is opened anywhere, and your home IP address is never exposed.

Two settings to change afterwards:

- **Settings → Public address** → `https://tasks.yourdomain.com`. Until you do,
  every address Task Hub shows says `localhost`, which on your phone means your
  phone.
- Add `https://tasks.yourdomain.com/oauth/google/callback` to your Google Cloud
  OAuth client, alongside the localhost one.

> The token in `.env` is enough for anyone to route traffic through your tunnel.
> It is excluded from version control, and it should not be shared.

### Tailscale — the option that needs no domain name

A Cloudflare tunnel needs a domain name you have added to Cloudflare. If you do
not have one, Tailscale gives you the same result for free and needs nothing but
an account.

It requires **no changes to Task Hub at all**. Tailscale is installed on the
machine Task Hub runs on, not inside it:

1. On that machine, run the installer from `tailscale.com/download`, then
   `sudo tailscale up` and sign in through the link it prints.
2. Install Tailscale on your laptop or phone and sign in with the same account.
3. Task Hub is now reachable at `http://<machine-name>:8080` from any of your
   devices, wherever they are.
4. Put that address into **Settings → Public address**.

Everything travels directly between your own devices, encrypted end to end, and
no port is opened on your router.

**The catch:** every device that needs access must be able to run Tailscale.
Laptops, phones and tablets can. Many e-readers, e-ink tablets and older devices
cannot, and for those a Cloudflare tunnel is the only option — its address is an
ordinary web address that anything can reach.

The two work happily side by side if you need both.

### Other ways to reach it from outside

The tunnel above is the recommended route. Two alternatives work as well:

- **A VPN back to your home network** (Tailscale is the easiest by a wide
  margin, and needs no router configuration). Your devices then behave as if
  they were at home. Like the tunnel, it opens no inbound port.
- **A reverse proxy with a real domain name and HTTPS** (Caddy or Nginx Proxy
  Manager). Note that Task Hub needs no proxy to combine its two halves — the
  CalDAV server is already inside the application on the same port — so a proxy
  is only worth adding if you are terminating TLS or hosting several services on
  one address.

Whichever you choose, put the resulting address into **Settings → Public
address**.

> Do not simply forward port 8080 on your router without HTTPS. CalDAV clients
> authenticate with HTTP Basic, which is not encryption: your password would
> cross the internet readable, from every device, on every sync. So would your
> Task Hub login and every task you own.

---

## Running on a Raspberry Pi or other small computer

Task Hub is comfortable on modest hardware. Measured on a real installation
holding roughly 2,000 tasks and calendar events, with the tunnel running:

| | |
| --- | --- |
| Memory, idle | about 140 MB |
| Memory, peak during a full sync | about 230 MB |
| Disk used by your data | about 20 MB |
| Processor, between syncs | close to nothing |

On a Raspberry Pi 4B with 4 GB of memory that peak is under 6% of what the
machine has, leaving the rest for the operating system and anything else you
run. A 2 GB Pi would also be fine.

**Use the 64-bit operating system.** Raspberry Pi OS comes in 32-bit and 64-bit
versions, and this matters more than it sounds. On 64-bit, every component Task
Hub depends on installs as a ready-built package — nothing is compiled, and
setup takes a few minutes. On 32-bit, several of them have no ready-built
version and must be compiled on the Pi itself, which can take hours and
sometimes fails outright. Check with:

```
uname -m
```

`aarch64` is the 64-bit version and is what you want. `armv7l` is 32-bit.

**Put the data somewhere durable.** Task Hub writes to its database on every
sync. On a Pi that means writing to the SD card every few minutes, and SD cards
wear out under repeated writes. A USB SSD, or an SD card rated for continuous
recording, will last far longer. Lengthening the sync interval also helps.

**Give the first start a few minutes.** Building the image downloads a few
hundred megabytes. Later starts take seconds.

---

## Connecting your phone and laptop

Task Hub's Radicale tab shows your server address and the exact steps for each
platform. In summary:

**iPhone and iPad.** Settings → Apps → Calendar → Calendar Accounts → Add
Account → Other → Add CalDAV Account. Enter the server address from the Radicale
tab, your CalDAV username, and your CalDAV password. Tasks appear in the
Reminders app, events in Calendar.

**macOS.** Calendar → Settings → Accounts → **+** → Other CalDAV Account. Set
Account Type to **Manual** and enter the same three details.

**Android.** Install **DAVx⁵** from the Play Store or F-Droid. Add an account
using "Login with URL and user name". Tasks need a compatible app such as
Tasks.org or OpenTasks installed alongside it.

**Thunderbird.** Calendar → New Calendar → On the Network → CalDAV, then paste
the address.

Use the **CalDAV** username and password from setup here, not your web sign-in.
They are deliberately different: the CalDAV password gets typed into many
devices, so being able to change it without touching the login that guards your
connected accounts is worth the small extra effort.

---

## Disconnecting a service, and cleaning up after it

Disconnecting an account deletes its saved login and stops it syncing. It never
deletes anything from the service itself — disconnecting Google removes nothing
from Google.

By default it also leaves your collections exactly as they are. That is the safe
default, but it means the tasks and events that account brought in stay behind
with nothing left to keep them up to date. Try a few services and drop them
again and those orphans accumulate.

So the disconnect box offers to remove them, and tells you how many before you
decide. It is careful about what counts:

- **An item another connected service still holds is kept.** A task in Google,
  Todoist and your collection is not orphaned by Google leaving — Todoist still
  has it.
- **An item you created in Task Hub is kept.** A task you wrote here and pushed
  out to Google is still your task after Google goes.
- **Everything else that came from that account is removed** — from your
  collection and from Task Hub's database — because nothing upstream owns it any
  more.

The box shows all three numbers, so you can see what would go and what would
stay before agreeing to it. Leave the tick box alone and nothing is removed.

If your CalDAV server cannot be reached at that moment, nothing is removed at
all and the account stays connected, rather than clearing the database while
leaving the entries in your collection — which would be a worse mess than the
one being cleaned up.

---

## Backing up

Everything Task Hub stores lives in one Docker volume named `taskhub-data`: the
database, your settings, the encryption key for your saved credentials, and
every task and calendar in Radicale.

To back it up, run this in the Task Hub folder:

```
docker run --rm --volumes-from taskhub -v "$(pwd)":/backup alpine tar czf /backup/taskhub-backup.tar.gz -C /data .
```

That writes `taskhub-backup.tar.gz` into the folder. Copy it somewhere safe.

**Check the file size.** A real backup is megabytes; if you get a file of a
couple of hundred bytes, it backed up nothing. `--volumes-from taskhub` asks the
container itself where its data lives, which is why it is written that way —
naming the volume directly is fragile, because Docker Compose prefixes volume
names with the folder Task Hub sits in, and naming a volume that does not exist
creates a new empty one and archives that instead. Silently.

To restore it onto a fresh installation, stop Task Hub first:

```
docker compose stop
docker run --rm --volumes-from taskhub -v "$(pwd)":/backup alpine tar xzf /backup/taskhub-backup.tar.gz -C /data
docker compose start
```

On Windows, run these in PowerShell and replace `$(pwd)` with `${PWD}`.

> The backup contains the encryption key for every service credential you have
> saved. Treat the file as you would a password database.

---

## If something goes wrong

**The page will not load.** Check the container is running with
`docker compose ps`. If it shows as unhealthy or missing, `docker compose logs`
will say why.

**"Port is already allocated".** Something else on the machine is using port
8080. Edit `docker-compose.yml`, change `"8080:8080"` to `"9090:8080"`, and use
`http://localhost:9090` instead.

**You forgot the web password.** There is no reset — Task Hub has no email
server to send one from. The only way back in is to delete the data volume and
set up again, which erases everything. This is why the setup wizard asks you to
save the password properly.

**A service stopped syncing.** Open its page under Services. An expired login
shows as "Needs auth" with a reconnect button. This is normal and occasional —
services expire tokens for security.

---

## What to do next

Open **Services** and connect your first account. Each service has its own guide
written to the same level of detail as this one, including exactly which buttons
to press in the Google Cloud Console and the Azure portal.

Start with the service where most of your tasks already live. Task Hub will pull
them in, and everything you connect afterwards converges on the same set.
