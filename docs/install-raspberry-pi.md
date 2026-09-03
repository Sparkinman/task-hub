# Installing on a Raspberry Pi

A Raspberry Pi is a good home for Task Hub. It is quiet, it uses about as much
electricity as a phone charger, and it can sit in a cupboard syncing your tasks
for years. This guide goes from a Pi still in its box to Task Hub running in
your browser.

It assumes nothing. Every command is one line you can copy and paste, and after
each one there is a description of what you should see.

**Already have a Pi running something?** Skip to
[Step 3](#step-3--see-what-is-already-installed), which checks what is on it
already and tells you only what is missing.

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

## Step 3 — See what is already installed

This is the step to start at if the Pi has been in use for a while. It checks
the machine and changes nothing:

```
curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/check-system.sh | sh
```

It prints something like this:

```
This computer
  OK    Processor: aarch64 (ARM, 64-bit — a ready-built image exists for this)
  OK    System: Debian GNU/Linux 12 (bookworm)
  OK    Model: Raspberry Pi 4 Model B Rev 1.5

Room to run
  OK    Memory: 3794 MB (Task Hub peaks at about 230 MB)
  OK    Free disk: 27100 MB (the image is about 650 MB)

Docker
  TODO  Not installed. On Linux and Raspberry Pi OS:
          curl -fsSL https://get.docker.com | sh
          sudo usermod -aG docker pi
        then log out and back in.
```

Read the lines marked **TODO** — those are things to fix. Lines marked **NOTE**
are worth knowing but do not stop you. On a brand-new Lite install the only
TODO will be Docker, which is the next step.

Two of its checks are worth understanding, because both cause failures that
appear to be about something else entirely:

- **Port 8080 already in use.** Something else on the Pi answers on the number
  Task Hub wants. Step 4 shows how to move Task Hub to another number.
- **Clock not synchronised.** Google and Microsoft reject sign-ins from a
  machine whose clock is wrong, with a message that blames your credentials.
  `sudo timedatectl set-ntp true` fixes it.

---

## Step 4 — Install Docker

Task Hub runs inside Docker, which packages an application together with
everything it needs. That is what lets one download work identically on a Pi, a
NAS, a Mac and a Windows machine.

```
curl -fsSL https://get.docker.com | sh
```

That is Docker's own installer, from Docker's own website. It takes a few
minutes and prints a lot; that is normal.

Then allow your account to use Docker without `sudo` in front of every command:

```
sudo usermod -aG docker $USER
```

**This does not take effect until you log out and back in**, so do that now:

```
exit
```

and connect again:

```
ssh pi@taskhub.local
```

Check it worked:

```
docker run --rm hello-world
```

You should see *"Hello from Docker!"*. If instead you get *"permission
denied"*, the log out and back in did not happen — do it again.

---

## Step 5 — Install Task Hub

Make a folder for it and go into it:

```
mkdir -p ~/taskhub && cd ~/taskhub
```

Download the one configuration file it needs:

```
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/Sparkinman/task-hub/main/docker-compose.yml
```

**Nothing in that file needs editing.** Task Hub works out its own address from
however you open it, so the same file is correct whether you reach it by the
Pi's address on your network, over Tailscale, through a Cloudflare tunnel or
behind your own reverse proxy.

Optionally, tell it the Pi's timezone so that "due today" means today from the
very first minute rather than after the setup wizard:

```
echo "TZ=$(cat /etc/timezone)" > .env
```

#### If the check in Step 3 said port 8080 was in use

Pick another number and record it:

```
echo "TASKHUB_HTTP_PORT=9090" >> .env
```

Then use that number everywhere below instead of 8080.

Now start it:

```
docker compose up -d
```

The first time, this downloads about 650 MB, which takes a few minutes on a
normal connection. It prints download progress, then `Container taskhub
Started`.

Check on it:

```
docker compose ps
```

Wait for the STATUS column to say **healthy** — up to half a minute. If it says
`unhealthy` or `restarting`, jump to [Troubleshooting](#troubleshooting).

---

## Step 6 — Open it

Find the Pi's address on your network:

```
hostname -I
```

That prints something like `192.168.1.50`. On your normal computer, open a
browser and go to:

```
http://192.168.1.50:8080
```

`http://taskhub.local:8080` usually works too and is easier to remember.

Task Hub greets you with its setup wizard. It asks you to create a login, pick
a timezone, and choose a CalDAV password for the phones and apps you will
connect later. Follow it to the end and you have a working Task Hub.

**Write the CalDAV password down.** It is shown once and is what every phone
and calendar app will need.

---

## Step 7 — Decide how you will reach it

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

```
cd ~/taskhub
docker compose pull
docker compose up -d
```

Your data is untouched by this. To reclaim the space the old version used:

```
docker image prune -f
```

### Backing up

Everything Task Hub owns — the database, your saved logins, every collection —
is in one Docker volume. This copies it into a file in the current folder:

```
cd ~/taskhub
docker compose stop
docker run --rm --volumes-from taskhub -v "$PWD":/backup alpine \
    tar czf /backup/taskhub-backup.tar.gz -C /data .
docker compose start
```

Copy that file somewhere else — that is the whole point of it. To put it back
on this or any other machine:

```
docker compose stop
docker run --rm --volumes-from taskhub -v "$PWD":/backup alpine \
    tar xzf /backup/taskhub-backup.tar.gz -C /data
docker compose start
```

> **That file can decrypt every service login you have saved.** Treat it the
> way you would treat a list of passwords, because that is what it is.

Note `--volumes-from taskhub` rather than a volume name. Docker Compose puts
the folder's name in front of volume names, and naming one that does not exist
does not fail — Docker quietly creates an empty volume and uses that instead.
A restore that appears to have worked, into nothing, is the failure to avoid.

### Restarting, stopping, logs

```
docker compose restart          # restart it
docker compose stop             # stop it, keep everything
docker compose start            # start it again
docker compose logs -f          # watch what it is doing; Ctrl-C to stop watching
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
