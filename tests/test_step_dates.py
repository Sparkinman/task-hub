"""Dates written on the end of a sub task line.

The parsing rule is shared with the Supernote plugin's ``parseSteps``: a step
typed on the tablet and a step typed in the browser must behave identically, or
the same text produces two different tasks depending on where it was written.
"""

import datetime as dt

from app.web.tasks_view import parse_step_line


def test_plain_line_has_no_date():
    assert parse_step_line("Draft the release notes") == ("Draft the release notes", None)


def test_bullets_are_stripped():
    assert parse_step_line("- Bump the version") == ("Bump the version", None)
    assert parse_step_line("  * Ship it  ") == ("Ship it", None)


def test_trailing_iso_date_becomes_the_due_date():
    assert parse_step_line("Draft the notes @2026-09-10") == (
        "Draft the notes",
        dt.date(2026, 9, 10),
    )


def test_an_at_that_is_not_a_date_stays_in_the_title():
    # "email @dave" means the words, not a date the parser failed to read.
    assert parse_step_line("email @dave") == ("email @dave", None)
    assert parse_step_line("review @2026-13-45") == ("review @2026-13-45", None)
    # Well-shaped but not a real day.
    assert parse_step_line("read @2026-02-30") == ("read @2026-02-30", None)


def test_a_date_alone_is_a_title_not_an_empty_step():
    assert parse_step_line("@2026-09-10") == ("@2026-09-10", None)


def test_blank_lines_produce_nothing():
    assert parse_step_line("") == ("", None)
    assert parse_step_line("   ") == ("", None)
    assert parse_step_line("-") == ("", None)
