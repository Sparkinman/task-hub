# Installing on a Linux server or desktop

For Ubuntu, Debian, Fedora, Rocky, Alma, openSUSE, Arch and their relatives —
anything that is not a Raspberry Pi or a NAS, both of which have guides of
their own ([Raspberry Pi](install-raspberry-pi.md), [NAS](install-nas.md)).

Every command can be copied and pasted exactly as written.

There are two paths. Take **[A — a machine with nothing installed](#a--a-machine-with-nothing-installed)**
if this is a fresh server, or **[B — a machine that already has Docker](#b--a-machine-that-already-has-docker)**
if Docker is already here. The check below tells you which.

---

## The short way — one command

On Linux there is no Docker Desktop to click through, so the quickest route is a
single line. Paste this into a terminal:

```
curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/install.sh | sh
```

It checks the machine, installs Docker if Docker is missing, picks a free port
if something already uses 8080, downloads Task Hub, starts it, waits for it to
report itself healthy, and prints the address to open. About five minutes on a
clean machine, most of it downloading.

It is safe to run again: a second run updates Task Hub and leaves your accounts,
settings and tasks alone, because those live in a Docker volume it never writes
to.

When it finishes it prints something like:

```
Done. Open Task Hub in a browser

  On another device:   http://192.168.1.50:8080
  On this machine:     http://localhost:8080
```

Open that address and the setup wizard takes over. **Everything after this point
happens in the browser** — connecting services, choosing what syncs, backups,
restores, restarting. The only job that ever needs a terminal again is updating,
which is the same line as above.

If you would rather do each step yourself, or the installer stopped with an
error you want to work around, the rest of this guide does the same job by hand.

---

## First: what is already here

```
curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/check-system.sh | sh
```

It changes nothing — it looks, and reports. You will get something like:

```
This computer
  OK    Processor: x86_64 (Intel or AMD, 64-bit)
  OK    System: Ubuntu 24.04.1 LTS

Room to run
  OK    Memory: 3906 MB (Task Hub peaks at about 230 MB)
  OK    Free disk: 41210 MB (Task Hub needs about 700 MB)

Docker
  TODO  Not installed. On Linux and Raspberry Pi OS:
          curl -fsSL https://get.docker.com | sh
```

**If `curl: command not found`**, install it first:

```
sudo apt update && sudo apt install -y curl      # Ubuntu, Debian
sudo dnf install -y curl                          # Fedora, Rocky, Alma
sudo zypper install -y curl                       # openSUSE
sudo pacman -S --noconfirm curl                   # Arch
```

Then read the Docker section of the output:

- **`TODO  Not installed`** → [path A](#a--a-machine-with-nothing-installed).
- **Anything else** → [path B](#b--a-machine-that-already-has-docker).

Two of its other checks are worth understanding, because both cause failures
that look like something else entirely:

- **Port 8080 already in use.** Something else answers there. Fixed at
  [step 3](#step-3--choose-a-different-port-only-if-you-need-to).
- **Clock not synchronised.** Google and Microsoft reject sign-ins from a
  machine whose clock is wrong, blaming your credentials rather than the time.
  `sudo timedatectl set-ntp true` fixes it.

### What you need

| | |
| --- | --- |
| Processor | 64-bit: `x86_64` or `aarch64`. 32-bit is not supported. |
| Memory | 1 GB works; 2 GB is comfortable. Task Hub peaks around 230 MB. |
| Free disk | 1 GB. |
| Kernel | Anything current. Docker needs 3.10 or later; every supported distribution is far past that. |

---

## A — a machine with nothing installed

### A1. Install Docker

```
curl -fsSL https://get.docker.com | sh
```

That is Docker's own installer, from Docker's own site. It detects your
distribution, adds Docker's package repository and installs Docker Engine, the
CLI and the Compose plugin. It prints a lot and takes a few minutes.

It works on Ubuntu, Debian, Fedora, Rocky, Alma, CentOS Stream, openSUSE and
Raspberry Pi OS. **On Arch** it does not; use the distribution's own package:

```
sudo pacman -S --noconfirm docker docker-compose
sudo systemctl enable --now docker
```

### A2. Let your account use Docker without `sudo`

```
sudo usermod -aG docker $USER
```

**This does not take effect until you log out and back in.** If you are on SSH,
`exit` and reconnect. If you are at the machine, log out of your session.

Skip this if you are working as `root` — root already has access.

### A3. Make sure it starts on boot

`get.docker.com` normally does this. Confirming costs nothing:

```
sudo systemctl enable --now docker
```

### A4. Check it works

```
docker run --rm hello-world
```

**"Hello from Docker!"** means you are ready. **"permission denied while trying
to connect to the Docker daemon"** means the log out and back in did not
happen — do it and try again.

Now continue at [Install Task Hub](#install-task-hub).

---

## B — a machine that already has Docker

Three things to confirm.

### B1. Can your account use it?

```
docker info > /dev/null && echo "yes" || echo "no"
```

If `no`, either you need to log out and back in after being added to the
`docker` group, or you are not in it yet:

```
sudo usermod -aG docker $USER
```

### B2. Is Compose the modern one?

Task Hub's instructions use `docker compose` — two words, a space, no hyphen.

```
docker compose version
```

If that errors but `docker-compose --version` works, you have only the old
standalone tool. Add the plugin:

```
sudo apt install -y docker-compose-plugin      # Ubuntu, Debian
sudo dnf install -y docker-compose-plugin      # Fedora, Rocky, Alma
```

Or re-run `curl -fsSL https://get.docker.com | sh`, which adds it.

### B3. Is port 8080 free?

The check told you. To see what holds it:

```
sudo ss -tlnp | grep :8080
```

If something does, note it for
[step 3](#step-3--choose-a-different-port-only-if-you-need-to).

---

## Install Task Hub

Identical on both paths.

### Step 1 — Make a folder and fetch one file

```
mkdir -p ~/taskhub && cd ~/taskhub
curl -fsSL -O https://raw.githubusercontent.com/Sparkinman/task-hub/main/docker-compose.yml
```

Nothing in that file needs editing. Task Hub works out its own address from
however you reach it, so the same file is correct on a home network, behind a
reverse proxy, over Tailscale or through a tunnel.

### Step 2 — Tell it your timezone

```
echo "TZ=$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value)" > .env
```

Optional — the setup wizard asks anyway.

### Step 3 — Choose a different port, only if you need to

Skip unless 8080 was taken.

```
echo "TASKHUB_HTTP_PORT=9090" >> .env
```

Then use `9090` instead of `8080` everywhere below.

### Step 4 — Start it

```
docker compose up -d
```

The first run downloads about 150 MB, unpacking to about 650 MB.

### Step 5 — Check it started properly

```
docker compose ps
```

Wait for STATUS to say **healthy** — up to half a minute. If it says
`unhealthy` or `restarting`, `docker compose logs --tail=50` gives the reason.

### Step 6 — Open it

```
hostname -I
```

That prints this machine's address. In a browser on any device on the same
network:

```
http://192.168.1.50:8080
```

Or `http://localhost:8080` if you are sitting at the machine itself.

The wizard asks for a login, a timezone and a CalDAV password. **Write the
CalDAV password down** — it is shown once and every phone needs it.

---

## Getting HTTPS, and why you may want it

On a plain address, Google and Microsoft will refuse to connect: both insist on
HTTPS for anything that is not `localhost`.

If you are sitting at the machine, `http://localhost:8080` is enough and they
will connect happily. If you are on SSH, borrow your own computer's localhost
for two minutes — from your laptop:

```
ssh -L 8080:localhost:8080 you@your-server
```

Then browse to `http://localhost:8080` and connect Google there. It keeps
working from any address afterwards.

For a permanent answer, [How Task Hub finds its own address](addresses.md)
covers Tailscale, Cloudflare tunnels and reverse proxies. There is a ready-made
nginx configuration in the project at `deploy/nginx-taskhub.conf`.

> Do not simply forward port 8080 on your router without HTTPS. CalDAV clients
> authenticate with HTTP Basic, which is not encryption: your password would
> cross the internet readable, from every device, on every sync.

---

## Keeping it running

Task Hub starts automatically when the machine reboots — `restart:
unless-stopped` in the compose file handles it, and nothing else is needed.

**Backing up, restoring and restarting** are inside Task Hub, under
**Settings → Backup and restore**. No terminal.

**Updating** has to come from outside, because a container cannot replace
itself:

```
cd ~/taskhub
docker compose pull
docker compose up -d
docker image prune -f      # reclaim the old version's space
```

**Stopping and starting:**

```
cd ~/taskhub
docker compose stop
docker compose start
docker compose logs -f      # watch it; Ctrl-C to stop watching
```

### Running it as a system service instead

If you would rather systemd owned it than Docker's restart policy:

```
sudo tee /etc/systemd/system/taskhub.service > /dev/null <<EOF
[Unit]
Description=Task Hub
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$HOME/taskhub
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now taskhub
```

This is optional, and duplicates what the restart policy already does. It is
here because on a managed server it is often the expected shape.

---

## Removing it completely

```
cd ~/taskhub
docker compose down -v
```

The `-v` deletes the data volume too — everything Task Hub stored. Take a
backup from Settings first if there is any doubt.
