"""Backing up and restoring everything Task Hub owns, from the web interface.

Task Hub is meant to be operated entirely from its own pages. Until now the
only way to take a backup was a pair of ``docker run`` incantations in the
install guides, which is precisely the kind of thing that never gets done --
and an unbacked-up Task Hub is a lost set of service logins, not merely a lost
list of tasks.

Everything Task Hub owns lives under one directory, so a backup is that
directory and a restore is putting it back. The care in this module is almost
entirely about the two ways that can go wrong:

* **A backup taken while the database is being written to.** Copying a live
  SQLite file can capture it mid-transaction, producing an archive that looks
  fine and restores into a corrupt database. So the database is snapshotted
  through SQLite's own backup API rather than copied as a file.

* **A restore that fails halfway.** Overwriting the live directory in place
  means an interrupted restore leaves neither the old data nor the new. So the
  archive is unpacked and checked in full first, the existing data is moved
  aside rather than deleted, and only then is the new data put in place. If
  anything raises before that final step, nothing has changed.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from app.config import DATA_DIR, DB_PATH, SECRET_KEY_PATH

logger = logging.getLogger(__name__)

#: Files that must be present for an archive to be a Task Hub backup. The
#: encryption key is on the list deliberately: an archive without it restores a
#: database whose every saved credential is undecryptable, which would look
#: like a successful restore right up until the first sync fails.
REQUIRED_MEMBERS = ("taskhub.db", "secret.key")

#: SQLite's write-ahead log and shared-memory files. They are excluded because
#: the snapshot already contains everything they hold; including them would
#: restore a stale log alongside a newer database.
SQLITE_SIDECARS = ("taskhub.db-wal", "taskhub.db-shm", "taskhub.db-journal")

#: Where a restore moves the previous data before putting the new data in
#: place. Inside the data directory so the move is a rename rather than a copy
#: across filesystems, which matters on a Raspberry Pi writing to an SD card.
ROLLBACK_PREFIX = ".superseded-"


# --- Sizing -------------------------------------------------------------------


def directory_size(path: Path) -> int:
    """Total bytes under a directory, following no symlinks."""
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            # A file vanishing mid-scan is normal on a live system and is not
            # worth failing a size estimate over.
            continue
    return total


def size_breakdown() -> dict[str, int]:
    """What the backup will contain, by area, so the size is not a surprise.

    Shown before the download starts. An Obsidian vault can be far larger than
    everything else combined, and someone on a slow connection deserves to know
    that before clicking rather than after.
    """
    areas = {
        "Database": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "Calendars and tasks": directory_size(DATA_DIR / "radicale"),
        "Obsidian vaults": directory_size(DATA_DIR / "obsidian"),
    }
    areas["Total"] = directory_size(DATA_DIR)
    return areas


def human_size(count: int) -> str:
    """Bytes as something a person can read at a glance."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# --- Taking a backup ----------------------------------------------------------


def suggested_filename() -> str:
    """A name that sorts chronologically and says what it is."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H%M")
    return f"taskhub-backup-{stamp}.tar.gz"


def _snapshot_database(destination: Path) -> None:
    """Copy the database out consistently, even while it is in use.

    SQLite's own backup API takes a transactionally consistent copy of a live
    database, waiting for writers as needed. A plain file copy does not: it can
    catch the file mid-write and produce an archive that restores into
    something corrupt. The difference only shows up under exactly the
    circumstances a backup is most likely to be taken -- a running, syncing
    system -- so it is worth the extra few lines.
    """
    source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def write_backup(destination: Path) -> Path:
    """Write a complete backup archive to ``destination``.

    The archive holds everything under the data directory: the database, the
    encryption key that makes it readable, every Radicale collection, and the
    Obsidian working files. Restoring it onto a fresh machine reproduces this
    installation exactly.
    """
    with tempfile.TemporaryDirectory(prefix="taskhub-backup-") as workspace:
        snapshot = Path(workspace) / "taskhub.db"
        if DB_PATH.exists():
            _snapshot_database(snapshot)

        with tarfile.open(destination, "w:gz") as archive:
            if snapshot.exists():
                archive.add(snapshot, arcname="taskhub.db")

            for entry in sorted(DATA_DIR.iterdir()):
                # The database is already in, as the consistent snapshot; its
                # sidecar files would contradict it; and a rollback directory
                # from an earlier restore is not part of the current state.
                if entry.name == "taskhub.db" or entry.name in SQLITE_SIDECARS:
                    continue
                if entry.name.startswith(ROLLBACK_PREFIX):
                    continue
                archive.add(entry, arcname=entry.name)

    return destination


# --- Inspecting an uploaded archive -------------------------------------------


class BackupError(Exception):
    """An uploaded file is not a usable Task Hub backup."""


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Reject any archive entry that would write outside the target directory.

    A tar archive can name absolute paths and paths containing ``..``, and
    extracting one naively writes wherever it says. The file being extracted
    here arrives by upload, so it is untrusted by definition even when the
    person uploading it is not.
    """
    members = []
    for member in archive.getmembers():
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise BackupError(
                "This archive contains a file path that points outside the "
                "backup, which a genuine Task Hub backup never does. It has "
                "not been unpacked."
            )
        if member.islnk() or member.issym():
            raise BackupError(
                "This archive contains a shortcut to another file, which a "
                "genuine Task Hub backup never does. It has not been unpacked."
            )
        members.append(member)
    return members


def inspect_archive(path: Path) -> dict[str, object]:
    """Read an uploaded archive and decide whether it is a Task Hub backup.

    Returns a short description for the confirmation screen, so that somebody
    about to replace everything can see what they are replacing it with before
    they do. Raises :class:`BackupError` with a plain-English reason if the
    file is not usable.
    """
    if not tarfile.is_tarfile(path):
        raise BackupError(
            "That file is not a Task Hub backup. A backup is a .tar.gz file "
            "produced by the Download backup button on this page."
        )

    with tarfile.open(path, "r:*") as archive:
        members = _safe_members(archive)
        names = {m.name for m in members}
        missing = [m for m in REQUIRED_MEMBERS if m not in names]
        if missing:
            raise BackupError(
                "That archive is missing "
                + " and ".join(missing)
                + ", so it is not a complete Task Hub backup. Restoring it "
                "would leave this installation unable to read its own saved "
                "logins."
            )
        total = sum(m.size for m in members)

    return {
        "files": len(members),
        "uncompressed": total,
        "has_obsidian": any(n.startswith("obsidian") for n in names),
        "has_collections": any(n.startswith("radicale") for n in names),
    }


# --- Restoring ----------------------------------------------------------------


def restore_archive(path: Path) -> None:
    """Replace everything in the data directory with the contents of ``path``.

    Ordered so that a failure at any point leaves the existing installation
    intact:

    1. The archive is unpacked in full, somewhere else. A truncated or corrupt
       archive fails here, having touched nothing.
    2. The unpacked result is checked for the files that make it a backup.
    3. Only then is the current data moved aside -- moved, not deleted -- and
       the new data put in its place.
    4. If step 3 fails part-way, what was moved is moved back.

    The data directory itself is never removed, because it is a mount point;
    its contents are replaced instead.
    """
    inspect_archive(path)

    staging = Path(tempfile.mkdtemp(prefix="taskhub-restore-", dir="/tmp"))
    rollback = DATA_DIR / f"{ROLLBACK_PREFIX}{int(time.time())}"
    try:
        with tarfile.open(path, "r:*") as archive:
            members = _safe_members(archive)
            # filter="data" is Python's own hardening against hostile archives;
            # the explicit member list above is belt and braces.
            archive.extractall(staging, members=members, filter="data")

        for required in REQUIRED_MEMBERS:
            if not (staging / required).exists():
                raise BackupError(
                    f"The archive unpacked without {required}, so it cannot be "
                    "restored. Nothing has been changed."
                )

        rollback.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        try:
            for entry in list(DATA_DIR.iterdir()):
                if entry == rollback or entry.name.startswith(ROLLBACK_PREFIX):
                    continue
                target = rollback / entry.name
                shutil.move(str(entry), str(target))
                moved.append((entry, target))

            for entry in staging.iterdir():
                shutil.move(str(entry), str(DATA_DIR / entry.name))
        except Exception:
            # Put back whatever was moved, in reverse, so a half-finished
            # restore does not leave the installation in pieces.
            for original, stored in reversed(moved):
                if not original.exists() and stored.exists():
                    shutil.move(str(stored), str(original))
            raise

        logger.info("Restore complete. Previous data kept at %s", rollback)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def discard_rollbacks() -> int:
    """Delete data set aside by previous restores. Returns how many went.

    Kept until asked for, because the moment someone realises they restored the
    wrong archive is the moment they need the old data back, and that is
    usually a few minutes after the restore rather than during it.
    """
    removed = 0
    for entry in DATA_DIR.iterdir():
        if entry.is_dir() and entry.name.startswith(ROLLBACK_PREFIX):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def rollback_size() -> int:
    """Bytes currently held by set-aside data from previous restores."""
    return sum(
        directory_size(entry)
        for entry in DATA_DIR.iterdir()
        if entry.is_dir() and entry.name.startswith(ROLLBACK_PREFIX)
    )


# --- Restarting ---------------------------------------------------------------


def restart_supported() -> bool:
    """Whether stopping this process will bring it back up again.

    Task Hub restarts itself by exiting, and relies on Docker's restart policy
    to start it again -- a container cannot restart itself any other way. Run
    outside a container, exiting would simply stop it, so the button is not
    offered there.
    """
    return Path("/.dockerenv").exists() or os.environ.get("TASKHUB_IN_CONTAINER") == "1"


def request_restart(delay: float = 1.0) -> None:
    """Exit shortly, so Docker's restart policy starts a fresh process.

    Deferred by a second so the browser receives the response that says the
    restart is happening; exiting immediately would drop the connection and
    show a browser error instead, which looks like a crash rather than an
    intentional restart.

    ``os._exit`` rather than a graceful shutdown on purpose: this runs after a
    restore has already replaced the database on disk, and letting the ORM
    flush anything it still holds in memory would write stale rows over the
    data just restored.
    """

    def stop() -> None:
        time.sleep(delay)
        logger.info("Restarting on request from the web interface.")
        os._exit(0)

    threading.Thread(target=stop, daemon=True, name="taskhub-restart").start()


def close_database() -> None:
    """Release the database before its file is replaced underneath us.

    Without this the engine keeps file handles open on a database that is about
    to be moved aside, and on some filesystems the restore fails outright.
    """
    from app.db.session import get_engine

    get_engine().dispose()
