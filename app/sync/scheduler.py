"""Background scheduling of sync passes.

APScheduler runs in-process on a background thread. That is the right shape for
a single-container application: no broker to run, no worker to supervise, and
the schedule disappears cleanly when the container stops.
"""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from sqlalchemy import select as _sqlselect

from app.db import settings_store
from app.db.session import session_scope

logger = logging.getLogger(__name__)

JOB_ID = "taskhub-sync"
#: The daily summary. A separate job from the sync because it runs once a day at
#: a chosen hour rather than on an interval, and because a mail server being down
#: must never interfere with syncing.
DIGEST_JOB_ID = "taskhub-digest"

#: The Supernote note backup, on its own far slower clock. Kept as a separate
#: job rather than folded into the sync pass because it talks to a different
#: server, on an unpublished API, doing work that costs its owner real money --
#: so it must never inherit the task sync's cadence, whatever that is set to.
NOTE_BACKUP_JOB_ID = "taskhub-note-backup"

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()
#: Guards against a scheduled pass starting while a manual one is still running.
#: Two passes at once would race on the same items and could double-create.
_running = threading.Lock()


def _run_job() -> None:
    if not _running.acquire(blocking=False):
        logger.info("Skipping scheduled sync: one is already running.")
        return
    try:
        from app.sync.engine import run_sync_now

        run = run_sync_now(trigger="scheduled", on_start=_note_active_run)
        logger.info(
            "Scheduled sync finished: %s (pulled %s, pushed %s, errors %s)",
            run.outcome.value, run.items_pulled, run.items_pushed, run.errors,
        )
        _notify_sync_outcome(run)
    except Exception:  # noqa: BLE001 - a failure must not kill the scheduler
        logger.exception("Scheduled sync failed")
    finally:
        _note_active_run(None)
        _running.release()


#: Id of the run currently in flight, so the interface can follow its progress.
_active_run_id: int | None = None


def _note_active_run(run_id: int | None) -> None:
    global _active_run_id
    _active_run_id = run_id


def active_run_id() -> int | None:
    return _active_run_id


def start_manual_sync() -> bool:
    """Begin a sync in the background and return at once.

    Deliberately does not wait for the result. A first sync of a large calendar
    takes minutes, and running it inside the HTTP request meant the browser sat
    on a spinning tab with no output -- indistinguishable from a hang. Starting
    a thread lets the page come straight back and report progress as it goes.

    Returns False if a sync is already running.
    """
    if not _running.acquire(blocking=False):
        return False

    def _work() -> None:
        try:
            from app.sync.engine import run_sync_now

            run = run_sync_now(trigger="manual", on_start=_note_active_run)
            logger.info(
                "Manual sync finished: %s (pulled %s, pushed %s, errors %s)",
                run.outcome.value, run.items_pulled, run.items_pushed, run.errors,
            )
        except Exception:  # noqa: BLE001 - never let a thread die silently
            logger.exception("Manual sync failed")
        finally:
            _note_active_run(None)
            _running.release()

    threading.Thread(target=_work, name="taskhub-manual-sync", daemon=True).start()
    return True


def is_running() -> bool:
    locked = _running.acquire(blocking=False)
    if locked:
        _running.release()
        return False
    return True


def start_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return
        _scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={
                # If the container was asleep, run once on waking rather than
                # firing every interval that was missed.
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 300,
            },
        )
        _scheduler.start()
        logger.info("Scheduler started")
    reschedule()
    reschedule_digest()
    reschedule_note_backup()


def shutdown_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is None:
            return
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


def _run_digest() -> None:
    """Send the daily summary, and never let a mail failure escape.

    A mail server that is down, rejecting, or simply misconfigured must not take
    the scheduler with it -- this job runs beside the one that keeps everybody's
    tasks in step.
    """
    try:
        from app.services.digest import send_digest

        with session_scope() as session:
            outcome = send_digest(session)
        logger.info("Daily summary: %s", outcome)
    except Exception:  # noqa: BLE001 - reported, never fatal
        logger.exception("Daily summary failed")

    _notify_tasks_due()


def _notify_tasks_due() -> None:
    """Tell subscribed devices what is due, once a day.

    Deliberately tied to the daily summary's schedule rather than given a clock
    of its own. A task reminder that fires per task, or on every sync, is the
    kind of notification people switch off within a week -- and switching it off
    switches off the sync failures and the expiring sign-ins with it.
    """
    try:
        from app.services.digest import collect
        from app.web.push_view import broadcast

        with session_scope() as session:
            digest = collect(session)
        if digest.empty:
            return  # Nothing due: say nothing.

        parts = []
        if digest.overdue:
            parts.append(f"{len(digest.overdue)} overdue")
        if digest.today:
            parts.append(f"{len(digest.today)} due today")

        broadcast(
            title="Task Hub",
            body=", ".join(parts).capitalize() + ".",
            url="/tasks",
            tag="taskhub-tasks-due",
            category="tasks",
        )
    except Exception:  # noqa: BLE001 - never fatal
        logger.debug("Could not send a task notification", exc_info=True)


def reschedule_digest() -> None:
    """Apply the daily summary's on/off switch, time and chosen days.

    Scheduled in the user's own timezone rather than the server's, so "seven in
    the morning" means theirs. Somebody who moves the instance between machines,
    or changes the timezone in settings, gets the hour they asked for either way.
    """
    global _scheduler
    if _scheduler is None:
        return

    with session_scope() as session:
        enabled = settings_store.get_bool(session, settings_store.DIGEST_ENABLED)
        when = (settings_store.get(session, settings_store.DIGEST_TIME) or "07:00").strip()
        zone = settings_store.get(session, settings_store.TIMEZONE) or "UTC"
        days = settings_store.digest_days(session)

    existing = _scheduler.get_job(DIGEST_JOB_ID)
    if not enabled:
        if existing:
            _scheduler.remove_job(DIGEST_JOB_ID)
            logger.info("Daily summary disabled")
        return

    try:
        hour, _, minute = when.partition(":")
        trigger = CronTrigger(
            day_of_week=",".join(days),
            hour=int(hour), minute=int(minute or 0), timezone=zone,
        )
    except (ValueError, TypeError):
        logger.warning("Daily summary time %r is not readable; not scheduling", when)
        return

    if existing:
        _scheduler.reschedule_job(DIGEST_JOB_ID, trigger=trigger)
    else:
        _scheduler.add_job(
            _run_digest, trigger=trigger, id=DIGEST_JOB_ID,
            name="Task Hub daily summary", replace_existing=True,
        )
    logger.info("Daily summary at %s %s on %s", when, zone, ",".join(days))


def _notify_sync_outcome(run) -> None:
    """Tell subscribed devices when a sync stops working.

    Only on the change from working to not: a service that has been down for a
    week should not send a notification every fifteen minutes. The previous
    outcome is what decides, so recovery is silent and the failure is announced
    once.
    """
    from app.db.models import SyncOutcome, SyncRun

    try:
        with session_scope() as session:
            previous = session.execute(
                _sqlselect(SyncRun)
                .where(SyncRun.id != run.id)
                .order_by(SyncRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        bad = (SyncOutcome.FAILED, SyncOutcome.PARTIAL)
        if run.outcome not in bad:
            return
        if previous is not None and previous.outcome in bad:
            return  # Already told them.

        from app.web.push_view import broadcast

        broadcast(
            title="Task Hub sync is failing",
            body="A sync did not complete. Open Task Hub to see which service.",
            url="/sync",
            tag="taskhub-sync-failure",
            category="sync",
        )
    except Exception:  # noqa: BLE001 - a notification must never break a sync
        logger.debug("Could not send a sync notification", exc_info=True)


def _run_note_backup() -> None:
    """Convert any changed Supernote notes, on the backup's own schedule."""
    from app.sync.note_backup import run_backup

    try:
        result = run_backup()
    except Exception:  # noqa: BLE001 - a scheduled job must never die silently
        logger.exception("Supernote note backup failed")
        return
    if result.converted or result.removed or result.errors:
        logger.info("Supernote note backup: %r", result)


def reschedule_note_backup() -> None:
    """Apply the note backup's own interval and switch.

    The interval is read through :func:`app.sync.note_backup.backup_interval`,
    which clamps it to the thirty-minute floor. Reading the raw setting here
    would let a value written straight into the database schedule something far
    too frequent against somebody else's server.
    """
    global _scheduler
    if _scheduler is None:
        return

    from app.sync.note_backup import backup_enabled, backup_interval

    with session_scope() as session:
        enabled = backup_enabled(session)
        minutes = backup_interval(session)

    existing = _scheduler.get_job(NOTE_BACKUP_JOB_ID)
    if not enabled:
        if existing:
            _scheduler.remove_job(NOTE_BACKUP_JOB_ID)
            logger.info("Supernote note backup disabled")
        return

    trigger = IntervalTrigger(minutes=minutes)
    if existing:
        _scheduler.reschedule_job(NOTE_BACKUP_JOB_ID, trigger=trigger)
    else:
        _scheduler.add_job(
            _run_note_backup, trigger=trigger, id=NOTE_BACKUP_JOB_ID,
            name="Supernote note backup", replace_existing=True,
        )
    logger.info("Supernote note backup every %s minutes", minutes)


def note_backup_next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job(NOTE_BACKUP_JOB_ID)
    return job.next_run_time if job else None


def digest_next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job(DIGEST_JOB_ID)
    return job.next_run_time if job else None


def reschedule() -> None:
    """Apply the interval and on/off switch from settings.

    Called at startup and whenever the user saves sync settings, so a change
    takes effect immediately rather than at the next container restart.
    """
    global _scheduler
    if _scheduler is None:
        return

    with session_scope() as session:
        enabled = settings_store.get_bool(session, settings_store.SYNC_ENABLED)
        minutes = settings_store.get_sync_interval(session)

    existing = _scheduler.get_job(JOB_ID)
    if not enabled:
        if existing:
            _scheduler.remove_job(JOB_ID)
            logger.info("Automatic sync disabled")
        return

    trigger = IntervalTrigger(minutes=minutes)
    if existing:
        _scheduler.reschedule_job(JOB_ID, trigger=trigger)
    else:
        _scheduler.add_job(
            _run_job, trigger=trigger, id=JOB_ID, name="Task Hub sync",
            replace_existing=True,
        )
    logger.info("Automatic sync every %s minutes", minutes)


def next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None
