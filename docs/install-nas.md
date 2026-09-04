# Installing on a NAS

A NAS is an excellent home for Task Hub: it is already on all the time, it
already has reliable storage, and it is already backed up. If you own a Synology,
QNAP, Unraid, TrueNAS or Asustor, this guide gets Task Hub running on it.

**One thing to check first.** Your NAS must be a 64-bit Intel, AMD or ARM
machine — which is nearly all of them made since about 2015, and all the popular
models. A handful of older ARM-based budget models are 32-bit and cannot run
Task Hub. [Step 1](#step-1--check-your-nas) checks this in a few seconds.

---

## Step 1 — Check your NAS

If you can open a terminal on the NAS (over SSH, or through its own interface),
this reports everything at once and changes nothing:

```
curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/check-system.sh | sh
```

If you would rather not use a terminal at all, check these three things in your
NAS's own interface instead:

| | What you need |
| --- | --- |
| Processor | 64-bit — shown as `x86_64`, `amd64` or `aarch64`. `armv7l` will not work. |
| Memory | 2 GB or more. Task Hub peaks around 230 MB, but the NAS needs its own. |
| Docker | Available in your NAS's package centre. It may be called Container Manager, Container Station, or just Docker. |

---

## Step 2 — Install Docker

Every make calls it something different, and all of them install it the same way
— from the built-in app store.

| NAS | Where to find it |
| --- | --- |
| **Synology** (DSM 7.2+) | Package Center → search **Container Manager** → Install |
| **Synology** (DSM 7.0–7.1) | Package Center → search **Docker** → Install |
| **QNAP** | App Center → search **Container Station** → Install |
| **Unraid** | Already included. Settings → Docker → Enable, if it is not already on. |
| **TrueNAS SCALE** | Already included, under Apps. |
| **Asustor** | App Central → **Docker Engine** and **Portainer** |

---

## Step 3 — Make a folder for the data

Task Hub keeps everything it owns in one folder — the database, your saved
logins, every task and event. Putting it somewhere you can see means your NAS's
own backup software can protect it.

Create a folder using your NAS's File Station or equivalent:

| NAS | Suggested folder |
| --- | --- |
| Synology | `/volume1/docker/taskhub` |
| QNAP | `/share/Container/taskhub` |
| Unraid | `/mnt/user/appdata/taskhub` |
| TrueNAS | a dataset such as `/mnt/tank/apps/taskhub` |

Create it yourself rather than letting Docker create it. Docker would create it
owned by a user that Task Hub cannot write as, and the container then fails to
start with a permission error that does not mention the folder.

---

## Step 4 — Set it up

There are two ways. Both end up in the same place, so use whichever suits you.

### With a compose file — recommended, and quicker

Modern Synology, QNAP and Unraid all accept a compose file, which describes the
whole thing at once and means no clicking through form fields.

Save this as `docker-compose.yml` inside the folder you made, changing only the
line marked:

```yaml
services:
  taskhub:
    image: sparkinman/task-hub:latest
    container_name: taskhub
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /volume1/docker/taskhub:/data     # <-- your folder from Step 3
    environment:
      TZ: "Europe/London"                 # <-- your timezone
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Then:

- **Synology Container Manager:** Project → Create → set the path to your folder
  → it finds the `docker-compose.yml` → Next → Done.
- **QNAP Container Station:** Applications → Create → paste the file in → Create.
- **Unraid:** use the Compose Manager plugin, or the manual method below.
- **Portainer** (any NAS): Stacks → Add stack → paste the file in → Deploy.

### By filling in the form

If your NAS only offers the point-and-click route:

1. Search the registry for **`sparkinman/task-hub`** and download the `latest`
   tag. Docker picks the right version for your processor automatically.
2. Create a container from it, named `taskhub`.
3. **Enable auto-restart** — called "Enable auto-restart" on Synology, "restart
   policy: unless-stopped" elsewhere. Without it, Task Hub does not come back
   after a reboot.
4. **Port settings:** local port `8080`, container port `8080`. If 8080 is taken
   — and on QNAP it is, by QNAP itself — use `9090` on the left and remember it.
5. **Volume settings:** mount your folder from Step 3 to the path `/data`.
   Read-write, not read-only.
6. **Environment:** add `TZ` set to your timezone, e.g. `Europe/London`.
7. Apply and start it.

---

## Step 5 — Open it

Your NAS's address, plus the port:

```
http://192.168.1.10:8080
```

Same address you use for the NAS's own interface, with `:8080` instead of
whatever port that uses. The setup wizard takes it from there: a login, a
timezone, and a CalDAV password for the phones you connect later. **Write the
CalDAV password down** — it is shown once.

---

## Step 6 — Connecting your task services

Todoist, Obsidian, Apple and every phone and calendar app work over your NAS's
plain address straight away.

Google, Microsoft and TickTick will not, because they insist on HTTPS or on a
name rather than a number. Three ways round it, in increasing order of effort:

- **Borrow `localhost` for two minutes.** From your own computer:
  `ssh -L 8080:localhost:8080 admin@192.168.1.10`, then browse to
  `http://localhost:8080` and connect Google there. Once connected it stays
  connected from any address.
- **Use your NAS's own certificate.** Synology and QNAP can both get a free
  Let's Encrypt certificate and put their reverse proxy in front of Task Hub.
  Synology: Control Panel → Login Portal → Advanced → Reverse Proxy. Point a
  hostname at `localhost:8080`. This is the tidiest permanent answer if you own
  a domain name.
- **Tailscale**, which most NAS makers offer as a package. It gives your NAS a
  real HTTPS address that every service accepts, with no domain name and no
  ports opened.

[How Task Hub finds its own address](addresses.md) explains why each service
behaves as it does. Task Hub itself needs no configuration for any of these — it
follows whatever address you arrive on.

---

## Keeping it running

**Updating.** With a compose file: Container Manager → your project → Stop →
Build, or `docker compose pull && docker compose up -d` in a terminal. With the
form method: download the `latest` tag again, then stop, clear and restart the
container.

**Backing up.** Settings → Backup and restore → **Download backup** gives you
one file holding everything, and needs no terminal. Because Step 3 put the data
in a folder you can see, you can also simply point your NAS's own backup task
— Hyper Backup, Hybrid Backup Sync, whatever you already use — at that folder
and let it run on a schedule. Doing both is not excessive.

> That folder, and the backup file, both contain the key that decrypts every
> service login you have saved. Back them up somewhere you would be
> comfortable keeping passwords.

**Restoring.** Settings → Backup and restore, choose the file, type RESTORE.
Task Hub replaces everything and restarts itself.

**Moving it.** Copy the folder to the new machine, point a new container at it,
and Task Hub picks up exactly where it left off.

---

## If something is wrong

**The container starts and immediately stops.** Almost always the data folder's
permissions. Check it exists and is writable, and that it is mounted to `/data`
and not something else.

**The page will not load.** Check the container is running in your NAS's Docker
interface, and that you used the right port — the left-hand number from Step 4,
not necessarily 8080.

**"port is already allocated" on QNAP.** QNAP's own interface uses 8080. Use
9090 on the left instead, and open `http://your-nas:9090`.

**Your phone cannot reach it.** Some NAS firewalls only allow their own ports
out of the box. Allow the port you chose, or check the NAS's firewall rules.
