"""Task Hub application entry point.

Assembles the FastAPI application, mounts the embedded Radicale CalDAV server
and installs the middleware that keeps unauthenticated visitors out of
everything except the login page and the first-run wizard.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, Response

from app.config import (
    RADICALE_MOUNT_PATH,
    STATIC_DIR,
    ensure_directories,
    get_session_secret,
)
from app.db import settings_store
from app.db.session import init_db, session_scope
from app.radicale_embed import get_radicale_app
from app.web import deps
from app.web.deps import is_auth_exempt, is_public_path
from app.web.forwarded import ForwardedHeadersMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("taskhub")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepare storage and the database before the first request is served."""
    ensure_directories()
    init_db()
    with session_scope() as db:
        onboarded = settings_store.is_onboarded(db)

    if onboarded:
        # Only start scheduling once setup is done. Before that there are no
        # accounts to sync and no timezone to interpret dates in.
        from app.sync.scheduler import start_scheduler

        start_scheduler()

        # Bring remote access back up exactly as the user left it, so a reboot
        # does not silently take their phones and e-readers offline.
        from app.web.settings_view import apply_tunnel_settings

        apply_tunnel_settings()

        # And bring the Obsidian vault back up to date. Without this the vault
        # on disk stays frozen at whatever the last sync left, so tasks ticked
        # off in Obsidian would keep coming back.
        from app.services.obsidian_sync import apply_obsidian_sync_settings

        apply_obsidian_sync_settings()

    logger.info(
        "Task Hub started (%s)",
        "ready" if onboarded else "awaiting first-run setup",
    )
    yield

    from app.services.obsidian_sync import manager as obsidian_manager
    from app.services.tunnel import manager as tunnel_manager
    from app.sync.scheduler import shutdown_scheduler

    shutdown_scheduler()
    tunnel_manager.stop()
    obsidian_manager.stop()
    logger.info("Task Hub stopped")


app = FastAPI(
    title="Task Hub",
    description="Self-hosted task and calendar synchronisation hub",
    lifespan=lifespan,
    docs_url=None,      # The interactive API docs would sit behind the login
    redoc_url=None,     # gate anyway, and they only invite confusion here.
    openapi_url=None,
)


#: Methods that only ever come from a calendar client. A browser uses none
#: of them, so a request carrying one is a phone looking for the CalDAV
#: service rather than a person looking at a web page -- and it should be
#: pointed at the service rather than at a login form.
DAV_METHODS = frozenset({
    "PROPFIND", "PROPPATCH", "REPORT", "MKCOL", "MKCALENDAR",
    "COPY", "MOVE", "LOCK", "UNLOCK",
})


class AccessGateMiddleware(BaseHTTPMiddleware):
    """Routes every visitor to the right place before any handler runs.

    Doing this once in middleware rather than as a dependency on each route
    means a newly added page cannot accidentally ship without a login check --
    the failure mode is a redirect to the login page, not an open door.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Radicale's own raw editor is an advanced-mode feature. Hiding only the
        # link would leave the page reachable by anyone who had bookmarked it,
        # so the route itself goes away too -- "not available", not "hidden".
        # The CalDAV endpoints around it are untouched, so phones keep syncing.
        if path.startswith(f"{RADICALE_MOUNT_PATH}/.web"):
            with session_scope() as db:
                if not settings_store.is_advanced(db):
                    return PlainTextResponse("Not found", status_code=404)

        # Static files, the CalDAV endpoint and the health check are outside
        # the session system entirely.
        if is_public_path(path):
            return await call_next(request)

        # A calendar client probing for the service must never be handed the
        # login page. Given only a server address, iOS tries the root, then
        # /principals/, then a couple of vendor-specific paths, and each of
        # those was being answered with a 303 to /login and then a 405 -- an
        # HTML form where a DAV collection was expected. The account is saved
        # anyway, warns that it may not sync, and then never syncs.
        #
        # These methods are used by calendar clients and never by the web
        # interface, so answering them with a redirect to the CalDAV mount
        # cannot affect anyone using a browser.
        if request.method in DAV_METHODS:
            return RedirectResponse(f"{RADICALE_MOUNT_PATH}/", status_code=301)

        with session_scope() as db:
            onboarded = settings_store.is_onboarded(db)
            has_session = bool(request.session.get(deps.SESSION_USER_KEY))

        if not onboarded:
            # Until setup finishes, the wizard is the only thing that exists.
            if path.startswith("/setup"):
                return await call_next(request)
            return RedirectResponse("/setup", status_code=307)

        # Setup is finished; the wizard must not be re-runnable by a stranger.
        if path.startswith("/setup"):
            return RedirectResponse("/" if has_session else "/login", status_code=303)

        if not has_session and not is_auth_exempt(path):
            if request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse({"error": "authentication required"}, status_code=401)
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(f"/login?next={target}", status_code=303)

        return await call_next(request)


# Middleware is applied outermost-last, so SessionMiddleware is registered after
# the gate in order to wrap it -- the gate needs a decoded session to read.
class CacheHeaderMiddleware(BaseHTTPMiddleware):
    """Say plainly how long each kind of response may be reused.

    Neither Starlette's StaticFiles nor a template response sets Cache-Control,
    and a browser given only an ETag falls back to heuristic caching -- it may
    reuse a stylesheet for hours without ever asking whether it changed. That
    turns a shipped fix into a bug report, so the two cases are made explicit:
    pages are never stored, and static files are cached hard but only ever
    fetched through a URL stamped with their version.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path

        if path.startswith("/static/"):
            if request.query_params.get("v"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
            return response

        if path.startswith(RADICALE_MOUNT_PATH):
            return response  # Radicale sets its own; leave it alone.

        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(AccessGateMiddleware)
app.add_middleware(CacheHeaderMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie="taskhub_session",
    same_site="lax",
    https_only=False,  # Works over plain HTTP on a LAN; set true behind TLS.
    max_age=60 * 60 * 24 * 30,
)

# Outermost, so that every middleware, every route and the embedded Radicale
# server see the browser's own scheme and host rather than the reverse proxy's.
# This is what lets one image serve a LAN address, a tunnel, a Tailscale name
# and somebody's nginx without being told which it is.
app.add_middleware(ForwardedHeadersMiddleware)


# --- Mounts -------------------------------------------------------------------

@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """The service worker, served from the root so it can control every page.

    A worker's scope is the directory it was served from, so one delivered from
    /static/js/ could only ever manage /static/js/. Serving the same file here
    is what lets it handle navigations. It is deliberately never cached: a
    stale worker is the one bug in this area that users cannot clear themselves.
    """
    from fastapi.responses import FileResponse as _FileResponse

    return _FileResponse(
        STATIC_DIR / "js" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest():
    """What an installer reads to put Task Hub on a home screen."""
    from fastapi.responses import FileResponse as _FileResponse

    return _FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/offline", include_in_schema=False)
def offline_page():
    """Shown by the service worker when the network is gone.

    Deliberately a bare page with no data on it: it is cached on the device, so
    anything it contained would be a copy of somebody's tasks sitting outside
    the session that fetched them.
    """
    from fastapi.responses import HTMLResponse as _HTMLResponse

    return _HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Task Hub is not reachable</title>"
        "<link rel=stylesheet href=/static/css/app.css>"
        "</head><body style='padding:2rem;font-family:system-ui'>"
        "<h1>Task Hub is not reachable</h1>"
        "<p>This device cannot reach your Task Hub right now. Nothing is lost — "
        "everything is on the server, and this page will work again as soon as "
        "the connection does.</p>"
        "<p><a href='/'>Try again</a></p>"
        "</body></html>",
        headers={"Cache-Control": "public, max-age=86400"},
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# The embedded CalDAV server. External clients point here; so does Task Hub's
# own CalDAV client, over loopback.
app.mount(RADICALE_MOUNT_PATH, WSGIMiddleware(get_radicale_app()), name="radicale")


# --- Routers ------------------------------------------------------------------

from app.web import (  # noqa: E402 - imported after app creation by design
    auth,
    calendar_view,
    docs_view,
    google_setup,
    notes_view,
    push_view,
    oauth_setup,
    obsidian_setup,
    password_setup,
    overview,
    radicale_admin,
    services_view,
    settings_view,
    setup,
    supernote_setup,
    sync_view,
    tasks_view,
    wellknown,
)

app.include_router(wellknown.router)
app.include_router(setup.router)
app.include_router(auth.router)
app.include_router(overview.router)
app.include_router(tasks_view.router)
app.include_router(calendar_view.router)
app.include_router(radicale_admin.router)
# Google's list discovery sits at a more specific path than the generic one
# on services_view, and FastAPI matches in registration order rather than by
# specificity -- so it is registered first, or the generic route swallows it
# and a token refreshed during discovery would be thrown away.
app.include_router(google_setup.router)
app.include_router(services_view.router)
app.include_router(oauth_setup.router)
app.include_router(password_setup.router)
app.include_router(obsidian_setup.router)
app.include_router(supernote_setup.router)
app.include_router(notes_view.router)
app.include_router(push_view.router)
app.include_router(sync_view.router)
app.include_router(settings_view.router)
app.include_router(docs_view.router)


# --- Infrastructure endpoints -------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> PlainTextResponse:
    """Liveness probe used by the container healthcheck."""
    return PlainTextResponse("ok")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return RedirectResponse("/static/img/favicon.svg", status_code=301)
