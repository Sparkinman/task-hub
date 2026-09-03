"""Login and logout for the web interface."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import verify_password
from app.db.models import User
from app.db.session import get_db
from app.web import deps

router = APIRouter()


def _safe_next(target: str | None) -> str:
    """Only allow redirects to paths on this site.

    A ``next`` parameter is attacker-controllable, so anything that is not a
    plain local path is discarded rather than followed off-site.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if request.session.get(deps.SESSION_USER_KEY):
        return deps.redirect(_safe_next(next))
    return deps.render(request, db, "login.html", next=_safe_next(next))


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    user = db.execute(
        select(User).where(User.username == username.strip())
    ).scalar_one_or_none()

    if user is None or not verify_password(password, user.password_hash):
        # One message for both cases: naming which half was wrong tells an
        # attacker which usernames exist.
        deps.flash(request, "Incorrect username or password.", "error")
        return deps.render(
            request, db, "login.html", next=_safe_next(next), username=username
        )

    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    deps.login_user(request, user)
    return deps.redirect(_safe_next(next))


@router.get("/logout")
@router.post("/logout")
def logout(request: Request):
    deps.logout_user(request)
    return deps.redirect("/login")
