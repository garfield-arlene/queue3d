from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from auth import AuthRedirect, get_session_secret_key
from db import init_db
from jobs import start_auto_finish_poller
from printer import close_connection
from routers import admin, jobs, user

# docs_url/redoc_url disabled: FastAPI's built-in interactive docs load
# their JS/CSS from cdn.jsdelivr.net by default, which is a dead link on a
# network with zero internet access (see project memory: this runs on an
# isolated "island" LAN, no exceptions). Not needed here anyway.
app = FastAPI(title="queue3d", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=get_session_secret_key())
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(AuthRedirect)
async def auth_redirect_handler(request: Request, exc: AuthRedirect):
    return RedirectResponse(exc.location, status_code=303)


@app.on_event("startup")
def on_startup():
    init_db()
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
