"""The Overview page: connected services at a glance, plus manual sync."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import settings_store
from app.db.models import (
    Account,
    AccountStatus,
    CollectionKind,
    ItemStatus,
    ServiceKind,
    SyncRun,
)
from app.db.session import get_db
from app.services.caldav_client import CalDAVError
from app.web import deps
from app.web.radicale_admin import get_radicale_client
from app.web.services_view import CURRENT_PHASE, SERVICE_CATALOGUE

router = APIRouter()


def _has_account(db, service_key: str) -> bool:
    from sqlalchemy import select

    from app.db.models import Account, ServiceKind

    try:
        kind = ServiceKind(service_key)
    except ValueError:
        return False
    return db.execute(
        select(Account.id).where(Account.service == kind).limit(1)
    ).scalar_one_or_none() is not None


@router.get("/")

def overview(request: Request, db: Session = Depends(get_db)):
    accounts = db.execute(select(Account)).scalars().all()
    by_service: dict[str, list[Account]] = {}
    for account in accounts:
        by_service.setdefault(account.service.value, []).append(account)

    service_cards = []
    for definition in SERVICE_CATALOGUE:
        # A hidden service still appears once it has an account, so a connected
        # one can never become invisible and unmanageable.
        if definition.hidden and not _has_account(db, definition.key):
            continue
        connected = [
            a for a in by_service.get(definition.key, [])
            if a.status == AccountStatus.CONNECTED
        ]
        needs_attention = [
            a for a in by_service.get(definition.key, [])
            if a.status in (AccountStatus.NEEDS_AUTH, AccountStatus.ERROR)
        ]
        service_cards.append(
            {
                "definition": definition,
                "connected": len(connected),
                "needs_attention": len(needs_attention),
                "total": len(by_service.get(definition.key, [])),
            }
        )

    # -- Radicale health and content counts
    radicale_status = {"configured": False, "reachable": False, "error": None}
    task_totals = {"open": 0, "overdue": 0, "completed": 0}
    collection_counts = {"tasks": 0, "calendars": 0}

    client = get_radicale_client(db)
    if client is not None:
        radicale_status["configured"] = True
        tz_name = settings_store.get_timezone(db)
        try:
            collections = client.list_collections()
            radicale_status["reachable"] = True
            for info in collections:
                if info.kind == CollectionKind.TASKS:
                    collection_counts["tasks"] += 1
                    for record in client.list_records(
                        info.collection_id, CollectionKind.TASKS, include_completed=True
                    ):
                        if record.status == ItemStatus.COMPLETED:
                            task_totals["completed"] += 1
                        else:
                            task_totals["open"] += 1
                            if record.is_overdue(fallback_tz=tz_name):
                                task_totals["overdue"] += 1
                else:
                    collection_counts["calendars"] += 1
        except CalDAVError as exc:
            radicale_status["error"] = str(exc)

    last_run = db.execute(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()

    total_accounts = db.execute(select(func.count(Account.id))).scalar_one()

    return deps.render(
        request, db, "overview.html",
        service_cards=service_cards,
        radicale_status=radicale_status,
        task_totals=task_totals,
        collection_counts=collection_counts,
        last_run=last_run,
        total_accounts=total_accounts,
        current_phase=CURRENT_PHASE,
        sync_enabled=settings_store.get_bool(db, settings_store.SYNC_ENABLED),
        sync_interval=settings_store.get_sync_interval(db),
    )
