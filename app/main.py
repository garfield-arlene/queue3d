from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from auth import AuthRedirect, get_session_secret_key
from db import init_db
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


@app.get("/")
def root():
    return RedirectResponse("/login")


app.include_router(user.router)
app.include_router(admin.router)
app.include_router(jobs.router)
