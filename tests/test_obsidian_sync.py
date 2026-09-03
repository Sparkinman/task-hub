"""Tests for the supervisor that keeps the Obsidian vault current.

The part that matters here is not "does it start a process" -- it is the
question the connector asks it before every pull: *is the vault safe to read
right now?* Getting that wrong in the permissive direction means reading a
half-downloaded vault and concluding the user deleted several hundred tasks, so
these tests drive the state machine directly rather than launching a real child.
"""

from __future__ import annotations

import sys
import time

from app.services.obsidian_sync import (
    ObsidianSyncManager, QUIET_SETTLE_SECONDS, VaultSync,
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


class FakeProcess:
    """Stands in for a running child that never exits on its own."""

    def __init__(self):
        self.stdout = iter(())
        self._returncode = None

    def poll(self):
        return self._returncode

    def exit(self, code=1):
        self._returncode = code


def running_manager() -> VaultSync:
    """A manager that believes it has a live child, without starting one."""
    m = VaultSync("Vault")
    m._enabled = True
    m._vault = "Vault"
    m._process = FakeProcess()
    m._started_at = time.time()
    m._last_activity = time.time()
    m._settled = False
    return m


def feed(manager: VaultSync, line: str) -> None:
    """Push one output line through the same branch the reader thread uses."""
    lowered = line.lower()
    from app.services.obsidian_sync import _FATAL, _SETTLED, _WORKING

    with manager._lock:
        manager._log.append(line)
        manager._last_activity = time.time()
        if any(p in lowered for p in _WORKING):
            manager._settled = False
        elif any(p in lowered for p in _SETTLED):
            manager._settled = True
            manager._failures = 0
            manager._last_error = None
        fatal = next((p for p in _FATAL if p in lowered), None)
        if fatal:
            manager._fatal = True
            manager._last_error = f"Obsidian sync cannot continue: {line}."


print("Obsidian sync supervisor")

# --- The reading guard --------------------------------------------------------

idle = VaultSync("Vault")
check("a manager that was never started reports the vault readable",
      idle.settled(),
      "nothing is writing to the directory, so it is stale but consistent")

m = running_manager()
check("a freshly started child is NOT considered settled",
      not m.settled(),
      "a pass may be in flight; reading now could look like mass deletion")

feed(m, "Downloading .obsidian/plugins/x/data.json")
check("'Downloading' keeps it unsettled", not m.settled())

feed(m, "Fully synced")
check("'Fully synced' marks it settled", m.settled())

feed(m, "Downloading Notes/Another.md")
check("a new pass makes it unsettled again", not m.settled())

# A child that goes quiet must not block reads forever.
m._last_activity = time.time() - (QUIET_SETTLE_SECONDS + 5)
check("a child quiet for longer than the settle window is treated as settled",
      m.settled(),
      "otherwise a silent child would stall every pull indefinitely")

# --- Not retrying a hopeless failure -----------------------------------------

m2 = running_manager()
feed(m2, "Error: not logged in")
check("a fatal message is recognised", m2._fatal)
check("a fatal message records a readable reason",
      m2._last_error is not None and "not logged in" in m2._last_error)

m3 = running_manager()
feed(m3, "Downloading something.md")
check("an ordinary line is not treated as fatal", not m3._fatal)

# --- Idempotence --------------------------------------------------------------

m4 = VaultSync("Vault")
started = []
m4._start_locked = lambda: started.append(1)          # type: ignore[method-assign]
m4.apply(True)
check("apply starts the child once", len(started) == 1)

m4._process = FakeProcess()
m4.apply(True)
check("applying the same settings again does not restart it",
      len(started) == 1,
      "the setup page and start-up both call apply; neither should bounce a healthy child")

# --- Status reporting ---------------------------------------------------------

m5 = running_manager()
feed(m5, "Fully synced")
status = m5.status()
check("status reports the vault it is watching", status.vault == "Vault")
check("status reports running", status.running)
check("status reports healthy once there is no error", status.healthy)
check("status carries the recent output", "Fully synced" in status.log)

m6 = running_manager()
feed(m6, "Error: unauthorized")
check("an errored manager is not reported healthy", not m6.status().healthy)

# --- The registry: one child per vault ---------------------------------------

print()
print("The registry")


class FakeChild:
    made: list[str] = []

    def __init__(self, vault):
        self.vault = vault
        self.running = False
        self.stopped = False
        FakeChild.made.append(vault)

    def apply(self, enabled, vault=None):
        self.running = enabled

    def stop(self):
        self.stopped = True
        self.running = False

    def settled(self):
        return not self.running

    def status(self):
        from app.services.obsidian_sync import SyncStatus
        return SyncStatus(vault=self.vault, running=self.running)


import app.services.obsidian_sync as _mod

_real = _mod.VaultSync
_mod.VaultSync = FakeChild
try:
    reg = ObsidianSyncManager()
    reg.apply(["Work", "Personal"])
    check("a child is started for each vault", sorted(FakeChild.made) == ["Personal", "Work"],
          str(FakeChild.made))
    check("both report running", all(c.running for c in reg._vaults.values()))

    before = list(FakeChild.made)
    reg.apply(["Work", "Personal"])
    check("re-applying the same set starts nothing new",
          FakeChild.made == before, str(FakeChild.made))

    dropped = reg._vaults["Personal"]
    reg.apply(["Work"])
    check("a vault removed from the set is stopped", dropped.stopped)
    check("  ...and forgotten", "Personal" not in reg._vaults)
    check("the remaining vault is untouched", reg._vaults["Work"].running)

    check("an unknown vault reports settled, so a pull is not blocked by it",
          reg.settled("Nonexistent"))
    check("status for an unknown vault is empty rather than an error",
          reg.status("Nonexistent").vault == "Nonexistent")

    reg.stop()
    check("stop() stops every child", "Work" not in reg._vaults)
finally:
    _mod.VaultSync = _real


# --- Redaction must hide secrets without rewriting meaning -------------------

print()
print("Redaction")

from app.services.obsidian_cli import redact as _redact

cases_kept = [
    "Password not provided.",
    "Password required for this vault.",
    "No password was given.",
    "Token expired, sign in again.",
]
for text in cases_kept:
    check(f"leaves {text!r} intact", _redact(text) == text, _redact(text))

cases_hidden = [
    ("ob sync-setup --password hunter2 --json", "hunter2"),
    ("ob login --password=hunter2", "hunter2"),
    ("Password: hunter2", "hunter2"),
    ("token=abc123def", "abc123def"),
]
for text, secret in cases_hidden:
    out = _redact(text)
    check(f"hides the value in {text!r}", secret not in out and "<redacted>" in out, out)


print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("All Obsidian sync supervisor tests passed.")
