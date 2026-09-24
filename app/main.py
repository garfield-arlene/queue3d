from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.sessions import SessionMiddleware

from auth import AuthRedirect, get_session_secret_key
from db import engine, init_db
from jobs import start_auto_finish_poller
from models import Settings
from printer import close_connection
from routers import admin, jobs, user
from templates_env import set_display_timezone

# docs_url/redoc_url disabled: FastAPI's built-in interactive docs load
# their JS/CSS from cdn.jsdelivr.net by default, which is a dead link on a
# network with zero internet access (see project memory: this runs on an
# isolated "island" LAN, no exceptions). Not needed here anyway.
app = FastAPI(title="queue3d", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=get_session_secret_key())
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Force every /static/ request to revalidate with the server rather
    than a browser silently serving a stale cached copy with no request
    at all - a real incident, not a hypothetical: a genuine JS bugfix
    (see app/README.md's "3D preview" section, the OrbitControls
    devicePixelRatio fix) kept appearing broken purely because the
    browser was still running the pre-fix file from cache, with no way
    to tell short of knowing to hard-refresh - cost real back-and-forth
    time to even recognize what was actually going on. StaticFiles
    already sends a real content-based ETag, so this doesn't disable
    caching or force a full re-download each time - it just guarantees
    every load checks with the server first (a fast 304 if nothing
    changed), never silently serving old content once something has."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(AuthRedirect)
async def auth_redirect_handler(request: Request, exc: AuthRedirect):
    return RedirectResponse(exc.location, status_code=303)


@app.on_event("startup")
def on_startup():
    init_db()
    # Primes templates_env's in-process display-timezone cache from
    # whatever's actually stored - see that module's local_time() for
    # why this is a plain module-level global rather than a DB read per
    # timestamp shown, and routers/admin.py's update_settings for the
    # other place this gets called (whenever an admin actually changes
    # it, so a restart is never required to see a saved change take
    # effect). No row yet on a brand new database - Settings() with its
    # own defaults (including "UTC") stands in, same as get_settings()
    # in routers/admin.py.
    with Session(engine) as session:
        settings = session.get(Settings, 1) or Settings()
        set_display_timezone(settings.display_timezone)
    # Notices a print finishing/failing/being cancelled on its own and
    # records it automatically (see jobs.check_and_finish_active_print) -
    # closes the gap between a print actually ending and an admin
    # noticing and clicking "Mark done," which is where every real
    # photo-capture failure so far actually came from (the connection
    # dying in that gap, not the capture logic itself). The manual
    # Mark done/Mark failed buttons are unchanged - this is a safety net
    # on top of them, not a replacement.
    start_auto_finish_poller()


@app.on_event("shutdown")
def on_shutdown():
    # Not required for correctness - the OS reclaims the socket on process
    # exit either way - but closes the printer's persistent connection
    # (see printer.py) cleanly rather than leaving it lingering across a
    # restart.
    close_connection()


@app.get("/")
def root():
    return RedirectResponse("/login")


app.include_router(user.router)
app.include_router(admin.router)
app.include_router(jobs.router)
