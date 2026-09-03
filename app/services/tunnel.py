"""Cloudflare tunnel, managed from the Settings page.

Remote access is the one thing that previously needed a terminal: create a file
holding a token, then start a second container with a profile flag. That is
exactly the kind of step this project exists to avoid, so cloudflared is bundled
into the image and supervised from here. Tick a box, paste a token, done.

The token is treated as a credential throughout. It is encrypted at rest with
the same key as every OAuth token, passed to the child process through the
environment rather than the command line -- so it never appears in a process
listing -- and redacted from anything written to the log.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CLOUDFLARED = "/usr/local/bin/cloudflared"

#: Lines kept for the status panel. Enough to show what went wrong, small
#: enough to hold in memory forever.
LOG_LINES = 40

#: Restart backoff after the child exits, in seconds. A bad token fails
#: instantly and would otherwise spin.
BACKOFF = [2, 5, 15, 30, 60, 120]

_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{20,}={0,2}")


def redact(text: str) -> str:
    """Remove anything that looks like a tunnel token from a log line."""
    return _TOKEN_RE.sub("<token redacted>", text)


def looks_like_token(token: str) -> tuple[bool, str]:
    """Check a token is the shape Cloudflare issues, before trying to use it.

    Catches the two mistakes people actually make -- pasting the whole install
    command, or pasting the tunnel's UUID instead of its token -- and says so
    plainly rather than leaving cloudflared to fail a minute later with
    something cryptic.
    """
    token = (token or "").strip()
    if not token:
        return False, "Paste the tunnel token."
    if token.startswith("docker ") or " " in token:
        return False, (
            "That looks like the whole install command. Paste only the long "
            "string that comes after --token."
        )
    if re.fullmatch(r"[0-9a-fA-F-]{36}", token):
        return False, (
            "That is the tunnel's ID, not its token. The token is much longer "
            "and starts with 'eyJ'."
        )
    if not token.startswith("eyJ"):
        return False, "A tunnel token starts with 'eyJ'. Check what you copied."

    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False, "That token is not readable. Copy it again from Cloudflare."

    if not {"a", "t", "s"} <= set(payload):
        return False, "That token is missing information Cloudflare normally includes."
    return True, ""


@dataclass
class TunnelStatus:
    enabled: bool = False
    running: bool = False
    connections: int = 0
    last_error: str | None = None
    started_at: float | None = None
    log: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.running and self.connections > 0


class TunnelManager:
    """Runs and supervises one cloudflared process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._token: str | None = None
        self._enabled = False
        self._stopping = False
        self._connections = 0
        self._last_error: str | None = None
        self._started_at: float | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._failures = 0

    # -- Public API ------------------------------------------------------------

    def available(self) -> bool:
        """Whether the bundled binary is present in this image."""
        return os.path.exists(CLOUDFLARED)

    def apply(self, enabled: bool, token: str | None) -> None:
        """Bring the tunnel into the requested state. Safe to call repeatedly."""
        with self._lock:
            same_token = token == self._token
            if enabled and self._is_alive() and same_token:
                return  # Already running with these settings.
            self._stop_locked()
            self._enabled = enabled
            self._token = token
            self._failures = 0
            self._last_error = None
            if enabled and token:
                self._start_locked()

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
            self._stop_locked()

    def status(self) -> TunnelStatus:
        with self._lock:
            return TunnelStatus(
                enabled=self._enabled,
                running=self._is_alive(),
                connections=self._connections,
                last_error=self._last_error,
                started_at=self._started_at,
                log=list(self._log),
            )

    # -- Internals -------------------------------------------------------------

    def _is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start_locked(self) -> None:
        if not self.available():
            self._last_error = (
                "The cloudflared program is missing from this build. Rebuild "
                "with 'docker compose up -d --build'."
            )
            logger.error(self._last_error)
            return

        env = dict(os.environ)
        # Through the environment, never the command line: an argument would be
        # visible to anything that can list processes in this container.
        env["TUNNEL_TOKEN"] = self._token or ""
        env.setdefault("TUNNEL_METRICS", "127.0.0.1:0")

        try:
            self._process = subprocess.Popen(
                [CLOUDFLARED, "tunnel", "--no-autoupdate", "run"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._last_error = f"Could not start the tunnel: {exc}"
            logger.error(self._last_error)
            return

        self._connections = 0
        self._started_at = time.time()
        self._stopping = False
        self._reader = threading.Thread(
            target=self._read_output, args=(self._process,),
            name="cloudflared-reader", daemon=True,
        )
        self._reader.start()
        logger.info("Cloudflare tunnel starting")

    def _stop_locked(self) -> None:
        process, self._process = self._process, None
        self._connections = 0
        self._started_at = None
        if process is None or process.poll() is not None:
            return
        self._stopping = True
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        logger.info("Cloudflare tunnel stopped")

    def _read_output(self, process: subprocess.Popen) -> None:
        """Follow the child's output, then decide whether to restart it."""
        try:
            for raw in process.stdout or ():
                line = redact(raw.rstrip())
                if not line:
                    continue
                with self._lock:
                    self._log.append(line)
                    if "Registered tunnel connection" in line:
                        self._connections += 1
                        self._failures = 0
                        self._last_error = None
                    elif "Unauthorized" in line or "invalid tunnel secret" in line:
                        self._last_error = (
                            "Cloudflare rejected the token. Check it was copied "
                            "in full, and that the tunnel still exists."
                        )
                    elif "failed to connect" in line.lower():
                        self._last_error = "Could not reach Cloudflare. Check this machine's internet connection."
        except Exception:  # noqa: BLE001 - reader must never take the app down
            logger.exception("Tunnel log reader stopped")

        code = process.poll()
        with self._lock:
            if self._process is not process:
                return  # Superseded by a newer process; nothing to do.
            self._connections = 0
            if self._stopping or not self._enabled:
                return
            if self._last_error is None:
                self._last_error = f"The tunnel stopped unexpectedly (exit {code})."
            delay = BACKOFF[min(self._failures, len(BACKOFF) - 1)]
            self._failures += 1
            logger.warning("Tunnel exited (%s); retrying in %ss", code, delay)

        # Restart outside the lock so a long backoff cannot block the settings
        # page from reading status.
        time.sleep(delay)
        with self._lock:
            if self._enabled and not self._is_alive():
                self._start_locked()


manager = TunnelManager()
