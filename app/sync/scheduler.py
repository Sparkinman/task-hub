"""Background scheduling of sync passes.

APScheduler runs in-process on a background thread. That is the right shape for
a single-container application: no broker to run, no worker to supervise, and
the schedule disappears cleanly when the container stops.
"""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.db import settings_store
from app.db.session import session_scope

logger = logging.getLogger(__name__)

JOB_ID = "taskhub-sync"

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


def shutdown_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is None:
            return
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


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
