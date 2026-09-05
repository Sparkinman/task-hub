"""Backing up Supernote notebooks as PDFs, and staying polite while doing it.

Every rule tested here exists because of something learned against a live
account, and most of them are about not being a nuisance on somebody else's
server. Ratta publish no API for any of this and owe nobody anything; the
fastest way to lose the access is to hammer a converter that costs them money
to run. So the interval floor, the md5 check and the per-pass cap are all
treated as correctness, not as tuning.

The other half is honesty on the page. A big planner comes back as "this file
is being converted" -- queued, not broken -- and showing that as a failure
would have somebody investigating a notebook that was about to arrive.
"""

from __future__ import annotations

import sys

from app.db import settings_store
from app.db.models import SupernoteNote
from app.db.session import init_db, session_scope
from app.services.supernote_files import Entry
from app.sync import note_backup

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


init_db()

print("\nA folder listing row is understood")
note = Entry(id="1", name="Meeting notes.note", is_folder=False, parent_id="9",
             size=100, md5="abc", updated_at=1788548731000)
folder = Entry(id="2", name="Work", is_folder=True, parent_id="0")
check("a .note is recognised", note.is_note is True)
check("a folder is not a note", folder.is_note is False)
check("the .note suffix is dropped for display", note.stem == "Meeting notes", note.stem)
check("a PDF is not mistaken for a note",
      Entry(id="3", name="Planner.pdf", is_folder=False, parent_id="0").is_note is False)
check("case does not matter",
      Entry(id="4", name="NOTES.NOTE", is_folder=False, parent_id="0").is_note is True)

print("\nThe interval can never be set fast enough to be rude")
floor = settings_store.MIN_NOTE_BACKUP_INTERVAL_MINUTES
with session_scope() as session:
    for asked, expected in [(1, floor), (5, floor), (29, floor), (30, 30),
                            (360, 360), (1440, 1440)]:
        settings_store.set_value(
            session, settings_store.SUPERNOTE_BACKUP_INTERVAL_MINUTES, str(asked)
        )
        session.flush()
        got = note_backup.backup_interval(session)
        check(f"asking for {asked} minutes gives {expected}", got == expected, str(got))

    # Clamped where it is read, not only where it is written, so a value put
    # straight into the database cannot schedule something too frequent.
    for rubbish in ("", "soon", None, "-5"):
        settings_store.set_value(
            session, settings_store.SUPERNOTE_BACKUP_INTERVAL_MINUTES, rubbish
        )
        session.flush()
        check(f"{rubbish!r} falls back to something sane",
              note_backup.backup_interval(session) >= floor,
              str(note_backup.backup_interval(session)))
    session.rollback()

print("\nFolder choices survive a round trip")
with session_scope() as session:
    note_backup.set_selected_folders(session, ["  17 ", "4", "", "17"])
    session.flush()
    # Deduplicated and trimmed: the same folder ticked twice must not mean the
    # tree is walked twice.
    check("duplicates and blanks are dropped",
          note_backup.selected_folders(session) == ["17", "4"],
          str(note_backup.selected_folders(session)))
    note_backup.set_selected_folders(session, [])
    session.flush()
    check("clearing them means nothing is backed up",
          note_backup.selected_folders(session) == [])
    session.rollback()

print("\nA queued notebook is not a broken one")
# What a large planner really returned: Supernote accepted it and is rendering
# it in the background. Reported as a failure, this would have somebody
# investigating a notebook that was about to arrive on its own.
queued = SupernoteNote(account_id=1, note_id="x", name="2026 Planner",
                       error="This file is being converted")
broken = SupernoteNote(account_id=1, note_id="y", name="Odd one",
                       error="The QT program failed to parse the file!")
fine = SupernoteNote(account_id=1, note_id="z", name="Recipes", pdf_name="z.pdf")
check("a queued notebook reads as pending", note_backup.is_pending(queued) is True)
check("a genuine failure does not", note_backup.is_pending(broken) is False)
check("nor does a healthy one", note_backup.is_pending(fine) is False)

print("\nThe PDF is named from the id, never from the notebook's title")
# A notebook can be called anything at all, including things that are not safe
# as a file name and things that change. Neither may reach the filesystem.
nasty = SupernoteNote(account_id=1, note_id="1234", name="../../etc/passwd",
                      pdf_name="1234.pdf")
path = note_backup.pdf_path(nasty)
check("the title does not appear in the path", "passwd" not in str(path), str(path))
check("and it stays inside the notes directory",
      path.parent == note_backup.NOTES_DIR, str(path.parent))
missing = SupernoteNote(account_id=1, note_id="5678", name="No pdf yet")
check("a note with no PDF still resolves somewhere safe",
      note_backup.pdf_path(missing).parent == note_backup.NOTES_DIR)

print("\nThe backup does nothing at all until it is asked to")
with session_scope() as session:
    settings_store.set_bool(session, settings_store.SUPERNOTE_BACKUP_ENABLED, False)
    note_backup.set_selected_folders(session, [])
    session.commit()
# No folders chosen and the switch off: this must make no network call at all,
# which it demonstrates by returning instantly with nothing done.
result = note_backup.run_backup()
check("a disabled backup converts nothing", result.converted == 0)
check("and reports no errors either", result.errors == [], str(result.errors))

print("\nThe cap and the pause are set to values that are actually gentle")
check("no more than a couple of dozen conversions per pass",
      1 <= note_backup.MAX_PER_RUN <= 50, str(note_backup.MAX_PER_RUN))
check("and it waits between them",
      note_backup.PAUSE_BETWEEN_CONVERSIONS >= 1.0,
      str(note_backup.PAUSE_BETWEEN_CONVERSIONS))
check("the floor is at least half an hour", floor >= 30, str(floor))
check("and the default is measured in hours",
      settings_store.DEFAULT_NOTE_BACKUP_INTERVAL_MINUTES >= 60,
      str(settings_store.DEFAULT_NOTE_BACKUP_INTERVAL_MINUTES))

print("\nRemoving a copy has to stick, or the button does not work")
# The notebook is still sitting in a folder marked for backup, so it is seen on
# every pass. Deleting the row rather than marking it would mean the very next
# pass downloaded it again -- a delete button that visibly undoes itself.
with session_scope() as session:
    row = SupernoteNote(account_id=1, note_id="keepme", name="Recipes",
                        root_folder_id="9", pdf_name="keepme.pdf",
                        thumb_name="keepme.thumb.png", pdf_size=1234,
                        source_md5="abc")

    note_backup.remove_copy(session, row)
    check("it is marked as removed", row.excluded is True)
    check("the PDF is forgotten", row.pdf_name is None, str(row.pdf_name))
    check("and so is the preview", row.thumb_name is None, str(row.thumb_name))
    check("and the size is cleared", row.pdf_size == 0, str(row.pdf_size))
    # Cleared so that restoring converts again rather than believing a file it
    # no longer has is still current.
    check("the checksum is cleared so a restore refetches",
          row.source_md5 == "", repr(row.source_md5))

    note_backup.restore_copy(session, row)
    check("restoring un-marks it", row.excluded is False)
    check("and still has no checksum, so the next pass fetches it",
          row.source_md5 == "", repr(row.source_md5))
    session.rollback()

print("\nRemoving a copy never touches the tablet")
# Worth stating as a test because "delete" beside a notebook reasonably reads
# as deleting the notebook. There is no call in this module that could.
import inspect  # noqa: E402
source = inspect.getsource(note_backup)
for forbidden in ("delete_note", "/file/delete", "note_delete"):
    check(f"nothing here calls {forbidden!r}", forbidden not in source)
check("remove_copy only edits the row and the local file",
      "files." not in inspect.getsource(note_backup.remove_copy),
      inspect.getsource(note_backup.remove_copy)[:80])

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll note backup tests passed.")
