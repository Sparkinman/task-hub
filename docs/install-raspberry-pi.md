# Installing on a Raspberry Pi

A Raspberry Pi is a good home for Task Hub. It is quiet, it uses about as much
electricity as a phone charger, and it can sit in a cupboard syncing your tasks
for years. This guide goes from a Pi still in its box to Task Hub running in
your browser.

It assumes nothing, and it needs **one** command — a single line you copy and
paste once. Everything after that happens in your browser.

**Already have a Pi running something?** Skip to
[Step 3](#step-3--one-command-installs-everything). The installer checks what is
already there and only does what is missing.

---

## What you need

| | |
| --- | --- |
| A Raspberry Pi | 4, 5, 400 or Zero 2 W. 2 GB of memory is enough; 4 GB is comfortable. |
| Storage | A 16 GB or larger SD card. A USB SSD is better and the reason is below. |
| Power supply | The official one. Most odd Pi behaviour is a power supply that cannot keep up. |
| Network | Ethernet or Wi-Fi, either is fine. |
| Another computer | To write the SD card and to use Task Hub from. Mac, Windows or Linux. |

You do **not** need a screen, keyboard or mouse for the Pi. Everything is done
from your other computer.

### About SD cards

Task Hub writes to its database every time it syncs. SD cards wear out under
repeated writing, and when one fails it usually fails all at once. A USB SSD
costs a little more and will outlast the Pi. If you use an SD card, choose one
sold for continuous recording — "high endurance" — and keep the backups
described at the end of this guide.

---

## Step 1 — Write the operating system

Use **Raspberry Pi OS Lite (64-bit)**. Two words in that name matter:

- **64-bit.** Task Hub is published ready-built for 64-bit only. On 32-bit,
  several parts have to be compiled on the Pi itself, which takes hours and
  sometimes fails. Every Pi from the 3 onwards is 64-bit hardware; it is only
  the operating system that comes in both.
- **Lite.** No desktop. You will never look at a screen attached to the Pi, so
  a desktop would spend memory and card space on something nobody sees. Lite
  leaves all of it for Task Hub.

> Raspberry Pi's 64-bit builds are for the ARM processor and are labelled
> `arm64` or `aarch64`. That is the right one. There is also a "Raspberry Pi
> Desktop" image for old PCs, which is a different thing and not what you want.

1. Install **Raspberry Pi Imager** from
   [raspberrypi.com/software](https://www.raspberrypi.com/software/) on your
   normal computer, and open it.
2. **Choose Device** — pick your model.
3. **Choose OS** — scroll to **Raspberry Pi OS (other)**, then choose
   **Raspberry Pi OS Lite (64-bit)**.
4. **Choose Storage** — your SD card or USB SSD. Check this twice; whatever you
   pick is erased.
5. Click **Next**. When it asks *"Would you like to apply OS customisation
   settings?"*, choose **Edit Settings**. This part matters — without it the Pi
   boots with no way to reach it.

   In **General**:

   - **Set hostname:** `taskhub`  — the Pi's name on your network.
   - **Set username and password:** pick both and write them down. You will
     type them in a moment and there is no way to recover them later.
   - **Configure wireless LAN:** your Wi-Fi name and password, if you are not
     using a network cable. Set the **Wireless LAN country** as well, or the
     Wi-Fi will not come up.
   - **Set locale settings:** your timezone and keyboard.

   In **Services**:

   - Tick **Enable SSH**, and leave it on **Use password authentication**.

6. **Save**, then **Yes** to apply, then **Yes** to erase and write. This takes
   a few minutes.

---

## Step 2 — Start the Pi and connect to it

Put the card in the Pi, connect the network cable if you are using one, and
plug in the power. Give it two minutes on its first boot; it is resizing its
storage and restarting once by itself.

On your normal computer open a terminal:

- **Mac:** Terminal, in Applications → Utilities.
- **Windows:** Windows Terminal, or PowerShell. Both are already installed on
  Windows 10 and 11.
- **Linux:** whichever you use.

Then connect, replacing `pi` with the username you chose:

```
ssh pi@taskhub.local
```

The first time, it asks whether you trust this machine. Type `yes` and press
Enter. Then enter the password you chose. Nothing appears as you type the
password — that is deliberate, not a stuck keyboard.

You should end up at a prompt like `pi@taskhub:~ $`. **You are now typing on
the Pi.** Everything from here happens there.

#### If `taskhub.local` is not found

That name is resolved by a system called mDNS, which occasionally is not
available. Find the Pi's numeric address instead: open your router's admin page
and look at the list of connected devices for `taskhub`. Then use the number:

```
ssh pi@192.168.1.50
```

---

## Step 3 — One command installs everything

This is the step to start at if the Pi has already been in use.

Copy this line, paste it into the terminal window, and press Enter. It is the
only command in this guide.

```
curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/install.sh | sh
```

**To paste into a terminal:** Ctrl+Shift+V on Windows and Linux, Cmd+V on a Mac.
A plain Ctrl+V does nothing in most terminal windows, which catches everybody
out once.

That one line does the whole job:

- checks the Pi has enough memory and disk, and that its clock is right
- installs Docker if Docker is not already there
- moves Task Hub to a free port if something else is already using 8080
- downloads Task Hub and starts it
- waits until it reports itself healthy
- prints the address to open

It takes about five minutes on a new Pi, most of that downloading. It prints a
great deal while Docker installs; that is normal and none of it needs reading.

**It is safe to run again.** Run it a second time and it updates Task Hub to the
current version and leaves everything you have set up alone — your accounts,
your settings and your tasks live in a Docker volume that the installer never
touches.

### What you should see at the end

```
Done. Open Task Hub in a browser

  On another device:   http://192.168.1.50:8080
  Or by name:          http://taskhub.local:8080
  On this machine:     http://localhost:8080
```

Write down the address it prints. That is your Task Hub.

If it stops with a red **Stopped:** line instead, it says what went wrong and
what to do about it. The most common causes are in
[Troubleshooting](#troubleshooting) at the end of this guide.

### If you would rather not paste a command at all

You do not have to use a Raspberry Pi. Task Hub is the same program everywhere,
and on **Windows, a Mac, or a Synology or QNAP NAS** it installs entirely
through windows and buttons with no terminal at any point:

- [Windows](install-windows.md) — [macOS](install-macos.md) — [NAS](install-nas.md)

A Pi is the one place a single pasted line is unavoidable, because a Pi has no
Docker Desktop and no app store to install it from.

---

## Step 4 — Open it

The installer printed the address at the end. On your normal computer, open a
browser and go to it — something like:

```
http://192.168.1.50:8080
```

`http://taskhub.local:8080` usually works too and is easier to remember.

Task Hub greets you with its setup wizard. It asks you to create a login, pick
a timezone, and choose a CalDAV password for the phones and apps you will
connect later. Follow it to the end and you have a working Task Hub.

**Write the CalDAV password down.** It is shown once and is what every phone
and calendar app will need.

**You are finished with the terminal.** Everything from here — connecting
Google, Todoist, TickTick and Obsidian, choosing what syncs, backups, restores,
even restarting Task Hub — happens in that browser window. You can close the
terminal and never open it again.

---

## Step 5 — Decide how you will reach it

This is the one decision worth taking a minute over, because it determines
which task services you can connect.

The address you use in your browser is the address Task Hub hands to Google,
Microsoft, Todoist and TickTick when you connect them. Those services are fussy
about what they accept, and each is fussy in a different way.

| How you reach it | Address | Google | Microsoft | Todoist, TickTick | Works away from home |
| --- | --- | --- | --- | --- | --- |
| Your home network | `http://192.168.1.50:8080` | ✗ | ✗ | ✓ | ✗ |
| A tunnel to your own computer | `http://localhost:8080` | ✓ | ✓ | ✓ | ✗ |
| Tailscale | `https://taskhub.tailnet.ts.net` | ✓ | ✓ | ✓ | ✓ |
| Cloudflare tunnel | `https://tasks.example.com` | ✓ | ✓ | ✓ | ✓ |
| Your own reverse proxy | `https://tasks.example.com` | ✓ | ✓ | ✓ | depends |

You do not have to choose one for ever. Task Hub follows whichever address you
use at the time, and a service stays connected once connected — the address
only matters at the moment you sign in to it.

### Your home network — start here

Nothing to set up; you are already using it. Todoist, TickTick, Obsidian, Apple
and your phones all work this way. Only Google and Microsoft refuse, because
they insist on either HTTPS or the word `localhost`, and a home network address
is neither.

#### A trick that gets you a name instead of a number

Some services reject an address that is a bare number but accept a name.
`sslip.io` turns any address into a name at no cost and with no sign-up:
`http://192-168-1-50.sslip.io:8080` sends you to `192.168.1.50`.

That is enough for TickTick. It is **not** enough for Google, which also
requires HTTPS — and `sslip.io` cannot provide that on a private network,
because a certificate authority has no way to verify an address only your house
can reach.

### Connecting Google without setting anything up

Google makes one exception to its HTTPS rule: it always accepts `localhost`. So
you can borrow your own computer's `localhost` for as long as it takes to
connect. On your normal computer, in a terminal:

```
ssh -L 8080:localhost:8080 pi@taskhub.local
```

Leave that window open, and in your browser go to **`http://localhost:8080`**.
This is the same Task Hub — the connection is being carried over to the Pi —
but Google now sees an address it is happy with. Connect Google, then close the
terminal window.

Google keeps working from then on, from any address, because refreshing a
connection does not involve your address at all.

### Tailscale — the good permanent answer

Tailscale puts your devices on a private network of their own, and gives each a
real HTTPS address that every service accepts. It is free for personal use,
opens no ports on your router, and works when you are away from home.

On the Pi:

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

It prints a link. Open it on any computer and sign in — that is what joins the
Pi to your network. Then:

```
sudo tailscale serve --bg 8080
```

It prints your address, something like `https://taskhub.tailnet-name.ts.net`.
Install Tailscale on your phone and laptop too, sign in with the same account,
and that address works from anywhere with real HTTPS and no further setup.

Task Hub needs no configuration for this. It notices the new address by itself.

### Cloudflare tunnel — a public address on your own domain

Built into Task Hub already: turn it on under **Settings → Remote access**. It
needs a domain name you have added to Cloudflare. This publishes Task Hub on
the open internet, so your login is the only thing standing between the world
and your tasks — use a long password.

### Your own reverse proxy

If you already run nginx, Caddy or Nginx Proxy Manager, point it at the Pi on
port 8080. There is a ready-made nginx configuration at
[`deploy/nginx-taskhub.conf`](https://github.com/Sparkinman/task-hub/blob/main/deploy/nginx-taskhub.conf).

Two rules:

- **Forward the headers.** Task Hub builds its addresses from the request it
  receives, so it needs `Host`, `X-Forwarded-Proto` and `X-Forwarded-Host`. The
  example configuration sets all three.
- **Give it a name of its own.** `tasks.example.com` works;
  `example.com/tasks` does not. Task Hub's pages link from the root, so a
  sub-path sends every link to the wrong place.

---

## Keeping it running

### Updating

The same line that installed it also updates it. Connect to the Pi as in Step 2
and paste:

```
curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/install.sh | sh
```

It notices Docker is already there, downloads the current version, and restarts
Task Hub on it. **Your data is untouched** — accounts, settings and tasks live
in a Docker volume the installer never writes to.

This is the one job that cannot be done from the web interface, for the plain
reason that a program cannot replace itself while it is running.

To reclaim the space the old version used, afterwards:

```
docker image prune -f
```

### Backing up

**Settings → Backup and restore → Download backup.** That is the whole
procedure: your browser saves one file containing everything Task Hub owns —
the database, your saved service logins, the key that decrypts them, every
calendar and task, and your settings. Keep it somewhere other than the Pi,
because a backup that only exists on the machine it is backing up is not a
backup.

> **That file can unlock every service you have connected.** It holds the key
> that decrypts your saved logins, so anyone with the file has those logins.
> Keep it where you would keep passwords, not in a shared folder.

To put it back — on this Pi or on a completely different machine — go to the
same page, choose the file under **Restore**, type RESTORE to confirm, and Task
Hub replaces everything and restarts itself. The archive is checked before
anything is touched, so choosing the wrong file costs you an error message
rather than your data, and the data being replaced is set aside rather than
deleted in case the restore turns out to be the wrong one.

After a restore you sign in with the password from the backup, not the one you
were using beforehand.

Nothing here needs a terminal. If you would rather script it, the data lives in
the Docker volume `taskhub_taskhub-data` and can be archived with
`docker run --rm --volumes-from taskhub …` — but note `--volumes-from taskhub`
rather than a volume name, because naming a volume that does not exist does not
fail: Docker quietly creates an empty one and uses that instead, and a restore
that appears to have worked into nothing is the failure to avoid.

### Restarting, stopping, logs

**Settings → Backup and restore → Restart now** restarts Task Hub from the web
interface, which is all that is needed for the occasional stuck-looking moment.
Nothing is lost: your data is on disk, and a sync that was in progress simply
runs again.

Sync activity is on the **History** page rather than in a log file, and it is
more readable than container logs — it says what each service did and why.

The rest is terminal-only, because a container cannot stop or replace itself:

```
docker compose stop             # stop it, keep everything
docker compose start            # start it again
docker compose logs -f          # container logs; Ctrl-C to stop watching
docker compose down             # stop and remove the container, keep the data
```

Task Hub starts by itself when the Pi reboots. Nothing to set up for that.

---

## Troubleshooting

**The page will not load.**
Check it is running: `docker compose ps`. If STATUS is not `healthy`, read
`docker compose logs --tail=50`. If it is healthy, the Pi is fine and the
problem is between you and it — check the address, and that you included
`:8080`.

**`docker compose ps` says `unhealthy` or it keeps restarting.**
`docker compose logs --tail=50` gives the reason. The usual cause on a first
install is not enough disk space.

**"permission denied while trying to connect to the Docker daemon".**
The log out and back in after `usermod` did not happen. `exit`, connect again,
and retry.

**"port is already allocated".**
Something else on the Pi uses 8080. See the note in Step 5 about
`TASKHUB_HTTP_PORT`.

**Google says `redirect_uri_mismatch`.**
The address in your browser is not the one registered with Google. Task Hub
shows the exact address to register on its Google page — copy it from there
rather than typing it, and make sure you are using the same address when you
click connect.

**Everything is slow, or the Pi keeps rebooting.**
Almost always the power supply. Use the official one. `vcgencmd get_throttled`
returning anything other than `throttled=0x0` confirms it.

---

## Removing it completely

```
cd ~/taskhub
docker compose down -v
```

The `-v` deletes the data volume as well, which is everything Task Hub stored.
There is no undo, so take a backup first if there is any doubt.
