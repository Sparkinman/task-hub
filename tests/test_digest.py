"""What the daily summary contains, and what it declines to send.

The rules here are small and all of them are about whether somebody keeps
reading the message. A summary that pads itself with completed work, or arrives
every morning to say nothing happened, gets filtered within a fortnight -- and
then the one that mattered is filtered too.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.db import settings_store
from app.db.models import (
    CollectionKind,
    Item,
    ItemStatus,
    ServiceKind,
    SyncGroup,
)
from app.db.session import init_db, session_scope
from app.services.digest import collect, render, source_of
from app.services.mailer import MailSettings, SECURITIES

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


init_db()
TODAY = dt.date(2026, 9, 10)

with session_scope() as session:
    group = SyncGroup(name="Digest test", kind=CollectionKind.TASKS, enabled=True)
    off = SyncGroup(name="Switched off", kind=CollectionKind.TASKS, enabled=False)
    session.add_all([group, off])
    session.commit()

    def add(title, due, status=ItemStatus.NEEDS_ACTION, at=None, in_group=None,
            origin=ServiceKind.LOCAL):
        session.add(Item(
            uid=f"uid-{title}", sync_group_id=(in_group or group).id,
            kind=CollectionKind.TASKS, title=title, status=status,
            due_date=due, due_time=at, origin_service=origin,
        ))

    add("Late by a week", TODAY - dt.timedelta(days=7), origin=ServiceKind.TODOIST)
    add("Late by a day", TODAY - dt.timedelta(days=1))
    add("Due today, morning", TODAY, at=dt.time(9, 0), origin=ServiceKind.TICKTICK)
    add("Due today, evening", TODAY, at=dt.time(18, 0))
    add("Due today, no time", TODAY)
    add("Tomorrow", TODAY + dt.timedelta(days=1), origin=ServiceKind.MICROSOFT)
    add("Six days out", TODAY + dt.timedelta(days=6))
    add("Exactly a week out", TODAY + dt.timedelta(days=7))
    add("Eight days out", TODAY + dt.timedelta(days=8))
    add("Already done", TODAY, status=ItemStatus.COMPLETED)
    add("Cancelled", TODAY, status=ItemStatus.CANCELLED)
    add("No due date at all", None)
    add("In a disabled collection", TODAY, in_group=off)
    session.commit()

    digest = collect(session, today=TODAY)

    print("\nWhat lands in each section")
    check("both late items are overdue", len(digest.overdue) == 2,
          [i.title for i in digest.overdue])
    check("three items are due today", len(digest.today) == 3,
          [i.title for i in digest.today])
    check("the coming week holds the three within seven days",
          len(digest.upcoming) == 3, [i.title for i in digest.upcoming])
    check("only tomorrow's item is tomorrow's", len(digest.tomorrow) == 1,
          [i.title for i in digest.tomorrow])
    check("the seventh day is inside the week",
          "Exactly a week out" in [i.title for i in digest.upcoming])
    check("the eighth day is not",
          "Eight days out" not in [i.title for i in digest.upcoming])

    print("\nWhat is deliberately left out")
    titles = {i.title for i in digest.overdue + digest.today + digest.upcoming}
    check("a completed task is not listed", "Already done" not in titles)
    check("a cancelled task is not listed", "Cancelled" not in titles)
    check("a task with no due date is not listed", "No due date at all" not in titles)
    check("a task in a switched-off collection is not listed",
          "In a disabled collection" not in titles)
    check("a fortnight away is not brought forward", "Eight days out" not in titles)

    print("\nOrdering, which is what makes it readable at a glance")
    check("the most overdue comes first",
          digest.overdue[0].title == "Late by a week",
          [i.title for i in digest.overdue])
    check("today runs in time order, untimed first",
          [i.title for i in digest.today] ==
          ["Due today, no time", "Due today, morning", "Due today, evening"],
          [i.title for i in digest.today])

    print("\nEvery line says where it came from")
    check("a Todoist item is named for Todoist",
          source_of(digest.overdue[0]) == "Todoist", source_of(digest.overdue[0]))
    check("Microsoft's list app gets its real name",
          source_of(digest.upcoming[0]) == "Microsoft To Do",
          source_of(digest.upcoming[0]))
    check("something made here is named Task Hub",
          source_of(digest.overdue[1]) == "Task Hub", source_of(digest.overdue[1]))

    print("\nThe message itself")
    subject, body = render(session, digest)
    check("the subject counts both kinds",
          "2 overdue" in subject and "3 due today" in subject, subject)
    check("the subject carries the date", TODAY.isoformat() in subject, subject)
    check("overdue is the first section", body.index("OVERDUE") < body.index("DUE TODAY"))
    check("the week ahead comes last",
          body.index("DUE TODAY") < body.index("THE NEXT 7 DAYS"))
    check("an overdue line shows which day it was due", "Thu 3 Sep" in body, body)
    check("a timed item shows its time", "09:00" in body)
    check("a week-ahead line carries its date", "Fri 11 Sep" in body, body)
    check("the source is on the line", "[Todoist · Digest test]" in body, body)
    check("the collection is named", "Digest test" in body)
    check("nothing completed leaked into the body", "Already done" not in body)

    print("\nA quiet day")
    # Deliberately *before* everything rather than after. A date far in the
    # future makes every item overdue, which is the opposite of quiet -- the
    # first version of this test made that mistake and passed nothing.
    quiet = collect(session, today=TODAY - dt.timedelta(days=400))
    check("nothing due means nothing to send", quiet.empty,
          f"overdue={len(quiet.overdue)} today={len(quiet.today)}")
    check("and it says so if asked to send anyway",
          "Nothing is due today" in render(session, quiet)[1])

    print("\nA day whose only work is tomorrow's is still a quiet day")
    # The day before the earliest item: tomorrow has something, today does not.
    # Tomorrow's list is context for today's, never a reason to write.
    eve = collect(session, today=TODAY - dt.timedelta(days=8))
    check("tomorrow is populated", len(eve.tomorrow) == 1,
          [i.title for i in eve.tomorrow])
    check("but the message is still not worth sending", eve.empty,
          f"overdue={len(eve.overdue)} today={len(eve.today)}")

print("\nWhich days the summary goes out")
with session_scope() as session:
    check("every day by default",
          settings_store.digest_days(session) == list(settings_store.DIGEST_DAY_CODES),
          settings_store.digest_days(session))

    settings_store.set_value(session, settings_store.DIGEST_DAYS, "fri,mon,wed")
    session.commit()
    check("chosen days come back in week order",
          settings_store.digest_days(session) == ["mon", "wed", "fri"],
          settings_store.digest_days(session))

    settings_store.set_value(session, settings_store.DIGEST_DAYS, "mon,funday,,SAT")
    session.commit()
    check("nonsense is dropped and case does not matter",
          settings_store.digest_days(session) == ["mon", "sat"],
          settings_store.digest_days(session))

    # Losing every day must not silently switch the summary off -- somebody who
    # wanted it off would have used the switch.
    settings_store.set_value(session, settings_store.DIGEST_DAYS, "nonsense")
    session.commit()
    check("a value with no real days falls back to every day",
          settings_store.digest_days(session) == list(settings_store.DIGEST_DAY_CODES),
          settings_store.digest_days(session))

print("\nAnd the cron field built from them")
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

trigger = CronTrigger(day_of_week="mon,wed,fri", hour=7, minute=0, timezone="UTC")
fired = str(trigger)
check("APScheduler accepts the day list Task Hub writes",
      "mon" in fired and "wed" in fired and "fri" in fired, fired)

print("\nMail settings know when they are unusable")
check("no host is not configured",
      not MailSettings("", 587, "starttls", "", "", "a@b.c").configured)
check("no from address is not configured",
      not MailSettings("h", 587, "starttls", "", "", "").configured)
check("host and sender is enough",
      MailSettings("h", 587, "starttls", "", "", "a@b.c").configured)
check("the sender is formatted with a name",
      "Task Hub" in MailSettings("h", 587, "starttls", "", "", "a@b.c").sender())
check("every offered security is a real choice",
      set(SECURITIES) == {"starttls", "ssl", "none"}, SECURITIES)

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
    sys.exit(1)
print("All digest tests passed.")
