"""Keeps the downloaded Obsidian vault current, by supervising ``ob sync``.

Without this, the vault on disk is whatever a one-off ``ob sync`` last left
there. Task Hub would then sync a snapshot that quietly ages: a task ticked off
in Obsidian this morning would keep reappearing, and one added today would never
arrive at all. The fix is a long-lived ``ob sync --continuous`` alongside the
application, which is what Obsidian's own client is designed for.

One child process per linked vault. Obsidian signs in once and can sync several
vaults from that one session -- its client keeps state per vault, in
``sync/<vault id>/`` beneath a shared login -- so each vault is started, watched
and restarted independently of the others.

Modelled on :mod:`app.services.tunnel`, and for the same reasons: a child
process that must never take the application down with it, must come back on its
own after a network drop, and must not retry a hopeless failure in a tight loop.

Two things are specific to this one.

**A vault must never be half-read.** ``ob`` writes files as it downloads them,
so a sync pass in flight is a directory that does not yet correspond to any real
state of the vault. A pull taken mid-pass could see a task's file missing and
conclude it was deleted. :meth:`ObsidianSyncManager.settled` is what the
connector asks before trusting what it reads.

**Read-only is enforced by the client, not by us.** The vault is linked in
``mirror-remote`` mode, so the child reverts local changes rather than uploading
them. Supervising a process that could write to the real vault would be a much
more serious thing to get wrong, and this deliberately is not that.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from app.services import obsidian_cli as cli

logger = logging.getLogger(__name__)

#: Kept for the setup page. Small on purpose: this is a diagnostic, not a log
#: file, and it is held in memory for the life of the process.
LOG_LINES = 40

#: Rising delays between restarts. A vault that cannot sync because the
#: subscription lapsed must not be retried every two seconds forever.
BACKOFF = [5, 15, 30, 60, 120, 300]

#: A pass that has produced no output for this long is treated as finished, so
#: the connector is not blocked indefinitely by a child that stays quiet.
QUIET_SETTLE_SECONDS = 20.0

#: How long after start-up the first pass is given before reads are allowed
#: through anyway. A first sync of a large vault genuinely takes minutes, but
#: blocking every pull until it finishes would stall the whole application.
FIRST_PASS_GRACE = 900.0

#: Phrases that mean retrying will not help. Matched case-insensitively.
_FATAL = (
    "not logged in",
    "unauthorized",
    "subscription",
    "no such vault",
    "vault not found",
    "invalid credentials",
)

#: Phrases that mean a pass has finished and the directory is consistent.
_SETTLED = ("up to date", "sync complete", "fully synced", "no changes")

#: Phrases that mean a pass has started and the directory may be inconsistent.
_WORKING = ("downloading", "syncing", "fetching", "applying")


@dataclass
class SyncStatus:
    enabled: bool = False
    running: bool = False
    vault: str | None = None
    settled: bool = False
    last_error: str | None = None
    last_activity: float | None = None
    started_at: float | None = None
    log: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.running and self.last_error is None


class VaultSync:
    """Runs and supervises one ``ob sync --continuous`` process, for one vault.

    One per linked vault. Obsidian's client keeps its sync state per vault --
    ``sync/<vault id>/`` under a single shared login -- so several can run side
    by side without interfering, and each has to be started, watched and
    restarted independently of the others.
    """

    def __init__(self, vault: str | None = None) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._vault: str | None = vault
        self._enabled = False
        self._stopping = False
        self._settled = False
        self._last_error: str | None = None
        self._last_activity: float | None = None
        self._started_at: float | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._failures = 0
        self._fatal = False

    # -- Public API ------------------------------------------------------------

    def available(self) -> bool:
        return cli.available()

    def apply(self, enabled: bool, vault: str | None = None) -> None:
        """Bring this vault's continuous sync into the requested state.

        Safe to call repeatedly with the same arguments -- the setup page and
        start-up both call it, and neither should restart a healthy child.
        """
        with self._lock:
            vault = vault or self._vault
            if enabled and self._is_alive() and vault == self._vault:
                return
            self._stop_locked()
            self._enabled = bool(enabled and vault)
            self._vault = vault
            self._failures = 0
            self._fatal = False
            self._last_error = None
            if self._enabled:
                self._start_locked()

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
            self._stop_locked()

    def settled(self) -> bool:
        """Whether the vault directory is safe to read right now.

        True when no pass is in flight, and also when continuous sync is not
        running at all -- in that case the directory is simply whatever the last
        manual sync left, which is stale but internally consistent. It is only
        an in-flight pass that produces a directory matching no real state.
        """
        with self._lock:
            if not self._enabled or not self._is_alive():
                return True
            if self._settled:
                return True
            now = time.time()
            if self._last_activity and now - self._last_activity > QUIET_SETTLE_SECONDS:
                return True
            # A long first pass must not block reads forever.
            return bool(self._started_at and now - self._started_at > FIRST_PASS_GRACE)

    def status(self) -> SyncStatus:
        with self._lock:
            return SyncStatus(
                enabled=self._enabled,
                running=self._is_alive(),
                vault=self._vault,
                settled=self.settled(),
                last_error=self._last_error,
                last_activity=self._last_activity,
                started_at=self._started_at,
                log=list(self._log),
            )

    # -- Internals -------------------------------------------------------------

    def _is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start_locked(self) -> None:
        if not self.available():
            self._last_error = (
                "The Obsidian client is missing from this build. Rebuild with "
                "'docker compose up -d --build'."
            )
            logger.error(self._last_error)
            return

        # vault_path always returns a path -- it sanitises the name rather than
        # failing -- so the question that matters is whether it is there yet.
        path = cli.vault_path(self._vault or "")
        if not path.exists():
            self._last_error = (
                f"The vault {self._vault!r} has not been downloaded yet. "
                "Set it up on the Obsidian page first."
            )
            return

        try:
            self._process = subprocess.Popen(
                [cli.OB, "sync", "--path", str(path), "--continuous"],
                env=cli._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._last_error = f"Could not start Obsidian sync: {exc}"
            logger.error(self._last_error)
            return

        now = time.time()
        self._started_at = now
        self._last_activity = now
        # A fresh child is assumed to be working until it says otherwise, so a
        # pull cannot slip in during the opening moments of a pass.
        self._settled = False
        self._stopping = False
        self._reader = threading.Thread(
            target=self._read_output, args=(self._process,),
            name="obsidian-sync-reader", daemon=True,
        )
        self._reader.start()
        logger.info("Obsidian continuous sync starting for %r", self._vault)

    def _stop_locked(self) -> None:
        process, self._process = self._process, None
        self._started_at = None
        self._settled = False
        if process is None or process.poll() is not None:
            return
        self._stopping = True
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        logger.info("Obsidian continuous sync stopped")

    def _read_output(self, process: subprocess.Popen) -> None:
        """Follow the child's output, then decide whether to restart it."""
        try:
            for raw in process.stdout or ():
                line = cli.redact(raw.rstrip())
                if not line:
                    continue
                lowered = line.lower()
                with self._lock:
                    self._log.append(line)
                    self._last_activity = time.time()
                    if any(phrase in lowered for phrase in _WORKING):
                        self._settled = False
                    elif any(phrase in lowered for phrase in _SETTLED):
                        self._settled = True
                        self._failures = 0
                        self._last_error = None
                    fatal = next((p for p in _FATAL if p in lowered), None)
                    if fatal:
                        self._fatal = True
                        self._last_error = (
                            f"Obsidian sync cannot continue: {line}. "
                            "Sign in again on the Obsidian page."
                        )
        except Exception:  # noqa: BLE001 - the reader must never take the app down
            logger.exception("Obsidian sync log reader stopped")

        code = process.poll()
        with self._lock:
            if self._process is not process:
                return  # Superseded by a newer process; nothing to do.
            self._settled = False
            if self._stopping or not self._enabled:
                return
            if self._fatal:
                # Retrying would fail identically and bury the real reason.
                logger.error("Obsidian sync stopped and will not be retried: %s",
                             self._last_error)
                return
            if self._last_error is None:
                self._last_error = f"Obsidian sync stopped unexpectedly (exit {code})."
            delay = BACKOFF[min(self._failures, len(BACKOFF) - 1)]
            self._failures += 1
            logger.warning("Obsidian sync exited (%s); retrying in %ss", code, delay)

        # Restarted outside the lock so a long backoff cannot block the setup
        # page from reading status.
        time.sleep(delay)
        with self._lock:
            if self._enabled and not self._is_alive():
                self._start_locked()





class ObsidianSyncManager:
    """Keeps one :class:`VaultSync` per linked vault.

    Obsidian signs in once and can then sync any number of vaults, each with
    its own state directory and its own ``ob sync`` process. This is the thing
    that owns that set: it starts what should be running, stops what should
    not, and answers per-vault questions on behalf of whichever connector is
    asking.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._vaults: dict[str, VaultSync] = {}

    def available(self) -> bool:
        return cli.available()

    def apply(self, vaults: list[str]) -> None:
        """Run continuous sync for exactly these vaults, and no others."""
        wanted = {v for v in vaults if v}
        with self._lock:
            for name in list(self._vaults):
                if name not in wanted:
                    self._vaults.pop(name).stop()
            for name in wanted:
                child = self._vaults.get(name)
                if child is None:
                    child = self._vaults[name] = VaultSync(name)
                child.apply(True)

    def stop(self) -> None:
        with self._lock:
            children = list(self._vaults.values())
            self._vaults.clear()
        for child in children:
            child.stop()

    def settled(self, vault: str | None = None) -> bool:
        """Whether a vault is safe to read.

        An unknown vault reports settled: nothing of ours is writing to it, so
        it is stale rather than mid-download, which is the same situation as a
        vault whose continuous sync was never started.
        """
        with self._lock:
            if vault is None:
                return all(c.settled() for c in self._vaults.values())
            child = self._vaults.get(vault)
        return child.settled() if child else True

    def status(self, vault: str) -> SyncStatus:
        with self._lock:
            child = self._vaults.get(vault)
        return child.status() if child else SyncStatus(vault=vault, settled=True)

    def statuses(self) -> dict[str, SyncStatus]:
        with self._lock:
            children = dict(self._vaults)
        return {name: child.status() for name, child in children.items()}


manager = ObsidianSyncManager()


def apply_obsidian_sync_settings() -> None:
    """Start or stop continuous sync to match the stored account.

    Called at start-up and after the vault is linked, so a restart brings the
    vault back up to date without anyone having to ask it to.
    """
    from sqlalchemy import select

    from app.crypto import decrypt_json
    from app.db.models import Account, ServiceKind
    from app.db.session import session_scope

    try:
        with session_scope() as db:
            accounts = db.execute(
                select(Account).where(Account.service == ServiceKind.OBSIDIAN)
            ).scalars().all()
            vaults = []
            for account in accounts:
                if not account.enabled or not account.credentials:
                    continue
                name = (decrypt_json(account.credentials) or {}).get("name")
                if name:
                    vaults.append(name)
        manager.apply(vaults)
    except Exception:  # noqa: BLE001 - never block start-up on this
        logger.exception("Could not apply Obsidian sync settings")
