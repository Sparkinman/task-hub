#!/bin/sh
# Task Hub — what is on this machine, and what is missing?
#
# Run this before installing, on any Linux machine or a Mac. It changes
# nothing: it only looks, and tells you what it found.
#
#   sh check-system.sh
#
# or, without downloading it first:
#
#   curl -fsSL https://raw.githubusercontent.com/Sparkinman/task-hub/main/check-system.sh | sh
#
# Written for /bin/sh rather than bash so it also runs on a NAS, where bash is
# often not installed.

RED=''; GREEN=''; YELLOW=''; BOLD=''; OFF=''
if [ -t 1 ]; then
  RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m')
  BOLD=$(printf '\033[1m'); OFF=$(printf '\033[0m')
fi

problems=0
warnings=0

ok()   { printf '  %sOK%s    %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '  %sNOTE%s  %s\n' "$YELLOW" "$OFF" "$1"; warnings=$((warnings + 1)); }
bad()  { printf '  %sTODO%s  %s\n' "$RED" "$OFF" "$1"; problems=$((problems + 1)); }
head_() { printf '\n%s%s%s\n' "$BOLD" "$1" "$OFF"; }

printf '\n%sTask Hub — checking this machine%s\n' "$BOLD" "$OFF"

# --- The machine itself -------------------------------------------------------

head_ "This computer"

os_name=$(uname -s)
arch=$(uname -m)

case "$arch" in
  x86_64|amd64)
    ok "Processor: $arch (Intel or AMD, 64-bit)" ;;
  aarch64|arm64)
    ok "Processor: $arch (ARM, 64-bit — a ready-built image exists for this)" ;;
  armv7l|armv6l)
    bad "Processor: $arch — this is a 32-bit system. Task Hub is published for
        64-bit only. On a Raspberry Pi 3, 4, 5 or Zero 2 W the hardware is
        64-bit and only the operating system is not: reinstall with the 64-bit
        version of Raspberry Pi OS and this becomes 'aarch64'." ;;
  *)
    warn "Processor: $arch — unusual. There may be no ready-built image, in
        which case Docker will have to build one, which takes a long time." ;;
esac

if [ -f /etc/os-release ]; then
  . /etc/os-release 2>/dev/null
  ok "System: ${PRETTY_NAME:-$os_name}"
elif [ "$os_name" = "Darwin" ]; then
  ok "System: macOS $(sw_vers -productVersion 2>/dev/null)"
else
  ok "System: $os_name"
fi

if [ -f /proc/device-tree/model ]; then
  model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)
  [ -n "$model" ] && ok "Model: $model"
fi

# --- Memory and disk ----------------------------------------------------------

head_ "Room to run"

if [ -r /proc/meminfo ]; then
  kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
  mb=$((kb / 1024))
  if [ "$mb" -ge 1800 ]; then
    ok "Memory: ${mb} MB (Task Hub peaks at about 230 MB)"
  elif [ "$mb" -ge 900 ]; then
    warn "Memory: ${mb} MB. Enough, but not much spare. Task Hub peaks at about
        230 MB during a sync."
  else
    bad "Memory: ${mb} MB. Below what Task Hub needs to sync comfortably."
  fi
fi

free_mb=$(df -Pm / 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$free_mb" ]; then
  if [ "$free_mb" -ge 3000 ]; then
    ok "Free disk: ${free_mb} MB (Task Hub needs about 700 MB)"
  elif [ "$free_mb" -ge 1500 ]; then
    warn "Free disk: ${free_mb} MB. The image alone unpacks to about 650 MB."
  else
    bad "Free disk: ${free_mb} MB. Not enough room for the image."
  fi
fi

# --- Docker -------------------------------------------------------------------

head_ "Docker"

if command -v docker >/dev/null 2>&1; then
  version=$(docker --version 2>/dev/null | sed 's/,.*//')
  ok "Installed: $version"

  if docker info >/dev/null 2>&1; then
    ok "Running, and this account may use it"
  elif [ "$(id -u)" -ne 0 ] && sudo -n docker info >/dev/null 2>&1; then
    warn "Running, but this account needs 'sudo' for every Docker command. Fix
        it once with:  sudo usermod -aG docker $(id -un)
        then log out and back in."
  else
    bad "Installed but not responding. Try:  sudo systemctl start docker"
  fi

  if docker compose version >/dev/null 2>&1; then
    ok "Compose: $(docker compose version --short 2>/dev/null)"
  elif command -v docker-compose >/dev/null 2>&1; then
    warn "Only the old separate 'docker-compose' is installed. Task Hub's
        instructions use the newer built-in 'docker compose' (a space, not a
        hyphen). Reinstalling Docker from get.docker.com adds it."
  else
    bad "Compose is missing. Reinstalling Docker from get.docker.com adds it."
  fi
else
  bad "Not installed. On Linux and Raspberry Pi OS:
        curl -fsSL https://get.docker.com | sh
        sudo usermod -aG docker $(id -un)
      then log out and back in."
fi

# --- The port -----------------------------------------------------------------

head_ "Port 8080"

port_user=""
if command -v ss >/dev/null 2>&1; then
  port_user=$(ss -tlnH 2>/dev/null | awk '{print $4}' | grep -E '(:|\])8080$' | head -1)
elif command -v netstat >/dev/null 2>&1; then
  port_user=$(netstat -an 2>/dev/null | grep -E '[.:]8080 .*LISTEN' | head -1)
fi

if [ -n "$port_user" ]; then
  if docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -q '8080->'; then
    warn "In use by a Docker container — quite possibly Task Hub already:
        $(docker ps --format '{{.Names}}' --filter publish=8080 | tr '\n' ' ')"
  else
    warn "Already in use by something else. Put TASKHUB_HTTP_PORT=9090 in a
        file called .env beside docker-compose.yml and use that number instead."
  fi
else
  ok "Free"
fi

# --- Clock --------------------------------------------------------------------

head_ "Clock"

if command -v timedatectl >/dev/null 2>&1; then
  if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
    ok "Synchronised with the internet ($(timedatectl show -p Timezone --value 2>/dev/null))"
  else
    warn "Not synchronised. A clock that is wrong by more than a few minutes
        makes Google and Microsoft reject sign-in with errors that blame
        something else. Fix with:  sudo timedatectl set-ntp true"
  fi
else
  ok "Time is $(date)"
fi

# --- Reaching it --------------------------------------------------------------

head_ "How you will reach it"

# Docker's own bridge networks have addresses too, and they are no use to a
# browser. Asking per interface and skipping Docker's lets the list be short
# enough to simply try.
addresses=""
if command -v ip >/dev/null 2>&1; then
  addresses=$(ip -4 -o addr show scope global 2>/dev/null \
    | awk '$2 !~ /^(docker|br-|veth|virbr)/ {print $4}' | cut -d/ -f1 | tr '\n' ' ')
fi
if [ -z "$addresses" ] && command -v hostname >/dev/null 2>&1; then
  addresses=$(hostname -I 2>/dev/null)
fi

port=8080
[ -f .env ] && port=$(awk -F= '/^TASKHUB_HTTP_PORT=/ {print $2}' .env | tr -d ' ' | tail -1)
[ -z "$port" ] && port=8080

if [ -n "$addresses" ]; then
  for a in $addresses; do
    printf '        http://%s:%s\n' "$a" "$port"
  done
else
  warn "No network address found. Is this machine connected?"
fi

host=$(hostname 2>/dev/null)
[ -n "$host" ] && printf '        http://%s.local:%s   (from a Mac, or Windows 10 and later)\n' "$host" "$port"

if command -v tailscale >/dev/null 2>&1; then
  ts=$(tailscale status --json 2>/dev/null | grep -o '"DNSName":"[^"]*"' | head -1 | cut -d'"' -f4 | sed 's/\.$//')
  if [ -n "$ts" ]; then
    printf '        https://%s   (Tailscale — works from anywhere, and Google accepts it)\n' "$ts"
  else
    ok "Tailscale is installed but this machine is not signed in to a network."
  fi
fi

# --- Verdict ------------------------------------------------------------------

printf '\n'
if [ "$problems" -gt 0 ]; then
  printf '%s%d thing(s) to sort out first%s — see TODO above.\n\n' "$RED" "$problems" "$OFF"
  exit 1
fi
if [ "$warnings" -gt 0 ]; then
  printf '%sReady, with %d note(s) worth reading above.%s\n\n' "$YELLOW" "$warnings" "$OFF"
  exit 0
fi
printf '%sEverything this machine needs is already here.%s\n\n' "$GREEN" "$OFF"
