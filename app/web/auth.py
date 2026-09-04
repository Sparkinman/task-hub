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
    """Show the sign-in page, or skip it for someone already signed in.

    The ``next`` parameter carries where the visitor was heading before the
    gate stopped them, so that signing in returns them there rather than
    dumping them on the home page. It is sanitised on the way in and out --
    see :func:`_safe_next` -- because it arrives from the URL.
    """
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
    """Check a username and password, and start a session if they match.

    Deliberately slow to distinguish between a wrong username and a wrong
    password: both produce the same message, because saying which half was
    wrong tells someone guessing which usernames exist. There is no lockout,
    on the reasoning that Task Hub is normally reachable only from a home
    network and locking the owner out of their own tasks is the more likely
    harm.
    """
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
    """End the session and return to the sign-in page.

    Answers to GET as well as POST so that a plain link can log out, which
    matters because the only other route is a form, and a session that cannot
    be ended without JavaScript is a session that stays open.
    """
    deps.logout_user(request)
    return deps.redirect("/login")
