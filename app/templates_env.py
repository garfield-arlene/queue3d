"""One shared Jinja2Templates instance, used by every router.

Previously each router made its own `Jinja2Templates(directory="templates")`
- harmless while nothing needed to reach every template, but the version
footer does: `base.html` (which every page extends) needs `APP_VERSION` in
scope on every render, and Jinja2 globals are set per-Environment, so three
separate instances would mean three places to keep in sync. One shared
instance, one place to set it.
"""

from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from db import engine
from models import Admin, User
from themes import DEFAULT_THEME
from version import APP_VERSION

templates = Jinja2Templates(directory="templates")
templates.env.globals["APP_VERSION"] = APP_VERSION


def current_theme(request) -> str:
    """The signed-in viewer's own theme choice (see themes.py,
    models.User.theme/Admin.theme), or themes.DEFAULT_THEME for a logged-
    out page or an account that's never set one. Registered as a Jinja
    global rather than something every route has to thread through its
    own context - `base.html` (which every page extends) needs this on
    every single render to set `<html data-theme="...">`, and `request`
    is already available in every template regardless of what its own
    route passed in (Starlette's Jinja2Templates adds it automatically),
    so this only needs `request` itself, not a bigger context-passing
    change touching every router.

    A short-lived Session of its own, not the request's - by the time
    base.html renders, most routes' own Session is already doing (or
    has done) other things, and this is cheap enough (one indexed
    primary-key lookup) that sharing one properly isn't worth the
    plumbing."""
    admin_id = request.session.get("admin_id")
    user_id = request.session.get("user_id")
    if admin_id is None and user_id is None:
        return DEFAULT_THEME
    with Session(engine) as session:
        if admin_id is not None:
            account = session.get(Admin, admin_id)
        else:
            account = session.get(User, user_id)
    return account.theme if account and account.theme else DEFAULT_THEME


templates.env.globals["current_theme"] = current_theme
