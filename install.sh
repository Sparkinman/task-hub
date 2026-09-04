#!/bin/sh
# Task Hub — one-line installer for Linux and Raspberry Pi OS.
#
#   curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/install.sh | sh
#
# It does every part of the setup that a terminal is needed for, so that nothing
# else has to be typed: it checks the machine, installs Docker if Docker is
# missing, writes the one configuration file, picks a free port if 8080 is
# taken, starts Task Hub, waits for it to report itself healthy, and prints the
# address to open. Everything after that happens in the browser.
#
# Safe to run again. It never overwrites your data: the database lives in a
# Docker volume that survives updates, reinstalls and this script. Running it a
# second time updates Task Hub to the current published image and leaves
# everything you have set up alone.
#
# Windows, macOS and most NAS boxes do not need this at all -- they have a
# window with buttons for the same job. See the guides in docs/.

set -eu

REPO="https://raw.githubusercontent.com/Sparkinman/task-hub/main"
DIR="${TASKHUB_DIR:-$HOME/taskhub}"
PORT="${TASKHUB_HTTP_PORT:-8080}"

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
ok()   { printf '  OK %s\n' "$*"; }
die()  { printf '\n\033[31mStopped: %s\033[0m\n' "$*" >&2; exit 1; }

# --- Do we need sudo, and do we have it? --------------------------------------
#
# Running the whole script as root is fine and common on a fresh server. When it
# is not root, every privileged step is prefixed instead -- and the very first
# one prompts for the password, so the prompt appears at the start rather than
# halfway through an install that then sits waiting unattended.

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die \
        "This needs administrator rights, and 'sudo' is not installed. Log in as root and run it again."
    SUDO="sudo"
fi

say "Task Hub installer"
say "=================="

# --- Check the machine --------------------------------------------------------

step "Checking this computer"

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64)  ok "Processor: $ARCH (a ready-built image exists for this)" ;;
    aarch64|arm64) ok "Processor: $ARCH (a ready-built image exists for this)" ;;
    armv7l|armv6l) die "This is a 32-bit ARM system. Task Hub needs 64-bit. On a Raspberry Pi, write the 64-bit version of Raspberry Pi OS to the card and start again." ;;
    *)             warn "Unrecognised processor: $ARCH. Carrying on, but there may be no image for it." ;;
esac

if [ -r /etc/os-release ]; then
    . /etc/os-release
    ok "System: ${PRETTY_NAME:-unknown}"
fi

FREE_MB="$(df -Pm "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)"
if [ "${FREE_MB:-0}" -lt 1200 ] 2>/dev/null; then
    warn "Only ${FREE_MB} MB free. Task Hub needs about 700 MB; this may fail."
else
    ok "Free disk: ${FREE_MB} MB"
fi

# A wrong clock is rejected by Google and Microsoft with a message blaming the
# password, which sends people off fixing entirely the wrong thing.
if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q "^yes$"; then
        ok "Clock is synchronised"
    else
        warn "The clock is not synchronised. Google and Microsoft will refuse to"
        warn "sign in and will blame your password. Fixing it now:"
        $SUDO timedatectl set-ntp true 2>/dev/null || \
            warn "Could not turn on time sync automatically; set the clock yourself."
    fi
fi

# --- Docker -------------------------------------------------------------------

step "Checking Docker"

if command -v docker >/dev/null 2>&1 && $SUDO docker info >/dev/null 2>&1; then
    ok "Docker is already installed"
else
    if command -v docker >/dev/null 2>&1; then
        say "  Docker is installed but not running. Starting it."
        $SUDO systemctl enable --now docker 2>/dev/null || true
        sleep 3
    fi
    if ! $SUDO docker info >/dev/null 2>&1; then
        say "  Installing Docker from Docker's own installer. This takes a few"
        say "  minutes and prints a lot; that is normal."
        if ! command -v curl >/dev/null 2>&1; then
            $SUDO apt-get update -qq >/dev/null 2>&1 && \
            $SUDO apt-get install -y curl >/dev/null 2>&1 || \
                die "Could not install 'curl', which is needed to fetch Docker."
        fi
        curl -fsSL https://get.docker.com | $SUDO sh || \
            die "Docker would not install. The output above says why."
        $SUDO systemctl enable --now docker 2>/dev/null || true
        ok "Docker installed"
    fi
fi

# Let this account use Docker without sudo in future. It does not take effect
# until the next login, which is exactly why this script uses $SUDO for the
# commands below rather than asking anyone to log out and back in mid-install.
if [ -n "$SUDO" ]; then
    $SUDO usermod -aG docker "$(id -un)" 2>/dev/null || true
fi

# Compose ships as a plugin with current Docker; older systems have it as a
# separate command. Either is fine, so find whichever is present.
if $SUDO docker compose version >/dev/null 2>&1; then
    COMPOSE="$SUDO docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="$SUDO docker-compose"
else
    die "Docker installed but Docker Compose did not come with it. Install the 'docker-compose-plugin' package and run this again."
fi
ok "Docker Compose is available"

# --- Pick a port nothing else is using ----------------------------------------

step "Choosing a port"

port_in_use() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
    else
        return 1
    fi
}

# Only search when the user has not asked for a specific one. Someone who set
# TASKHUB_HTTP_PORT meant it, and silently moving them elsewhere would be worse
# than failing loudly.
if [ -z "${TASKHUB_HTTP_PORT:-}" ]; then
    while port_in_use "$PORT"; do
        say "  Port $PORT is already used by something else on this machine."
        PORT=$((PORT + 1))
    done
fi
ok "Task Hub will answer on port $PORT"

# --- Write the configuration --------------------------------------------------

step "Setting it up in $DIR"

mkdir -p "$DIR"
cd "$DIR"

if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o docker-compose.yml "$REPO/docker-compose.yml" || \
        die "Could not download the configuration file. Check the internet connection."
elif command -v wget >/dev/null 2>&1; then
    wget -qO docker-compose.yml "$REPO/docker-compose.yml" || \
        die "Could not download the configuration file. Check the internet connection."
else
    die "Neither 'curl' nor 'wget' is installed, so the configuration file cannot be downloaded."
fi
ok "Configuration downloaded (nothing in it needs editing)"

# The timezone makes "due today" mean today from the first minute, rather than
# only after the setup wizard asks. Written to .env so that an update never
# overwrites it.
TZ_NAME=""
[ -r /etc/timezone ] && TZ_NAME="$(cat /etc/timezone 2>/dev/null || true)"
if [ -z "$TZ_NAME" ] && command -v timedatectl >/dev/null 2>&1; then
    TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
fi
{
    [ -n "$TZ_NAME" ] && printf 'TZ=%s\n' "$TZ_NAME"
    printf 'TASKHUB_HTTP_PORT=%s\n' "$PORT"
} > .env
[ -n "$TZ_NAME" ] && ok "Timezone: $TZ_NAME"

# --- Start it -----------------------------------------------------------------

step "Downloading and starting Task Hub"
say "  The download is about 150 MB and takes a few minutes on a slow connection."

$COMPOSE pull  || die "Could not download Task Hub. The output above says why."
$COMPOSE up -d || die "Task Hub would not start. The output above says why."

printf '  Waiting for it to be ready'
i=0
while [ "$i" -lt 60 ]; do
    STATE="$($SUDO docker inspect --format '{{.State.Health.Status}}' taskhub 2>/dev/null || echo starting)"
    [ "$STATE" = "healthy" ] && break
    printf '.'
    sleep 2
    i=$((i + 1))
done
printf '\n'

[ "${STATE:-}" = "healthy" ] || die \
    "Task Hub started but has not reported itself healthy. Run: cd $DIR && $COMPOSE logs"

ok "Task Hub is running"

# --- Say where to find it -----------------------------------------------------

IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[ -z "$IP" ] && IP="$(hostname -i 2>/dev/null | awk '{print $1}' || true)"
NAME="$(hostname 2>/dev/null || true)"

step "Done. Open Task Hub in a browser"
say ""
[ -n "$IP" ]   && say "  On another device:   http://$IP:$PORT"
[ -n "$NAME" ] && say "  Or by name:          http://$NAME.local:$PORT"
say "  On this machine:     http://localhost:$PORT"
say ""
say "The first page asks you to choose a password. Everything after that --"
say "connecting Google, Todoist, TickTick and Obsidian, backups, updates --"
say "happens in that browser window. You should not need this terminal again."
say ""
