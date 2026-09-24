"""One shared Jinja2Templates instance, used by every router.

Previously each router made its own `Jinja2Templates(directory="templates")`
- harmless while nothing needed to reach every template, but the version
footer does: `base.html` (which every page extends) needs `APP_VERSION` in
scope on every render, and Jinja2 globals are set per-Environment, so three
separate instances would mean three places to keep in sync. One shared
instance, one place to set it.
"""

from datetime import datetime, timezone as _utc
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from db import engine
from models import Admin, User
from themes import DEFAULT_MODE, DEFAULT_THEME
from version import APP_VERSION

templates = Jinja2Templates(directory="templates")
templates.env.globals["APP_VERSION"] = APP_VERSION


def _signed_in_account(request):
    """The signed-in User or Admin behind this request, or None for a
    logged-out page - shared by current_theme()/current_mode() below,
    both of which need it on every single render (base.html, which every
    page extends, needs both to set <html data-theme=... data-mode=...>).

    A real bug, caught live: nothing about logging in as one role clears
    the other's session key - a deliberate choice, not an oversight, per
    the user, who keeps a user tab and an admin tab open side by side in
    the same browser on purpose. That means `admin_id` and `user_id` can
    both legitimately be present in the same session at once, and picking
    whichever one happened to be checked first (this used to always
    prefer `admin_id`) meant the *admin's* theme silently applied to
    every user's page too, in that browser - not a data bug (every
    account's own stored preference was always correct), a resolution
    bug in which account's preference this looked at. Fixed by asking
    which role's page this specific request is actually for, via the URL
    path (`/admin/...` vs. everything else) - matching exactly how
    require_admin/require_user are already scoped per-router - so each
    tab in a dual-session browser correctly resolves to its own role
    regardless of what the other tab is doing.

    A short-lived Session of its own, not the request's - by the time
    base.html renders, most routes' own Session is already doing (or has
    done) other things, and this is cheap enough (one indexed primary-key
    lookup) that sharing one properly isn't worth the plumbing."""
    admin_id = request.session.get("admin_id")
    user_id = request.session.get("user_id")
    is_admin_path = request.url.path.startswith("/admin")
    with Session(engine) as session:
        if is_admin_path and admin_id is not None:
            return session.get(Admin, admin_id)
        if not is_admin_path and user_id is not None:
            return session.get(User, user_id)
        # A shared, non-/admin page (e.g. /jobs/{id}/preview, reachable
        # by either role) or a role/path mismatch - fall back to
        # whichever id actually exists rather than assume neither does.
        if admin_id is not None:
            return session.get(Admin, admin_id)
        if user_id is not None:
            return session.get(User, user_id)
    return None


def current_theme(request) -> str:
    """The signed-in viewer's own theme choice (see themes.py,
    models.User.theme/Admin.theme), or themes.DEFAULT_THEME for a
    logged-out page or an account that's never set one. Registered as a
    Jinja global rather than something every route has to thread through
    its own context - see _signed_in_account() above for why."""
    account = _signed_in_account(request)
    return account.theme if account and account.theme else DEFAULT_THEME


def current_mode(request) -> str:
    """Same as current_theme() above, but for the separate light/dark
    axis (models.User.theme_mode/Admin.theme_mode) - per the user, mode
    is independent of which theme is selected, not folded into it."""
    account = _signed_in_account(request)
    return account.theme_mode if account and account.theme_mode else DEFAULT_MODE


templates.env.globals["current_theme"] = current_theme
templates.env.globals["current_mode"] = current_mode


# ---- display timezone (see models.Settings.display_timezone) ----
# Site-wide, not per-viewer like theme/mode above - one admin-set zone
# for the whole deployment. Cached in-process (module-level, not
# re-read from the DB on every call) because local_time() below runs
# once per *timestamp shown*, not once per page - an activity log page
# alone can render hundreds of rows, and this app is single-process (one
# Pi, one SQLite file - see db.py), so a plain global is both safe and
# correct: there's no other worker that could see a stale value. Primed
# once at startup (main.py) from the stored Settings row, and updated
# immediately whenever an admin actually saves a new one
# (routers/admin.py's update_settings) - never re-read from the DB on
# the hot path in between.
_display_timezone_name = "UTC"
_display_timezone = ZoneInfo("UTC")


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True


def get_display_timezone() -> ZoneInfo:
    """The admin-configured display timezone `local_time()` below already
    uses - a public accessor for `filters.py`'s date-range filtering,
    which needs the exact same zone to interpret a plain "YYYY-MM-DD"
    filter input as the *local* calendar day an admin actually meant,
    not literal UTC midnight. Reads `_display_timezone` fresh on every
    call rather than letting a caller bind it once - it's reassigned
    in place (not mutated) whenever an admin changes the setting (see
    set_display_timezone below), so a stale imported reference would
    silently keep using whatever zone was active at import time."""
    return _display_timezone


def set_display_timezone(name: str) -> None:
    """Updates the in-process cache local_time() reads. Falls back to
    UTC for a name that isn't a real IANA zone rather than raising -
    should never actually happen (update_settings validates with
    is_valid_timezone() above before saving), but a template filter is
    the wrong place to let a bad stored value take down every page that
    renders a timestamp."""
    global _display_timezone_name, _display_timezone
    if is_valid_timezone(name):
        _display_timezone_name = name
        _display_timezone = ZoneInfo(name)
    else:
        _display_timezone_name = "UTC"
        _display_timezone = ZoneInfo("UTC")


def local_time(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Jinja filter: `{{ some_utc_datetime | local_time }}` - converts to
    the admin-configured display timezone (default UTC) and formats with
    a real zone abbreviation (%Z - "EST"/"EDT"/"UTC"/etc.) rather than
    the hardcoded "UTC" label most timestamp displays used to have
    baked into their own format string regardless of what was actually
    shown. Every datetime this app stores is UTC but comes back
    tzinfo-naive once round-tripped through SQLite (the same recurring
    gotcha as auth.check_lockout() and elsewhere) - `.replace(tzinfo=None)`
    first normalizes an already-aware value (e.g. one just created
    in-process, not yet re-fetched) down to naive too, so this works
    identically either way before attaching the real UTC tzinfo and
    converting. `dt=None` (a timestamp field that hasn't happened yet -
    finished_at on a still-active job, etc.) returns "" so this can be
    used directly in a template without a separate `{% if %}` guard
    purely for that."""
    if dt is None:
        return ""
    aware_utc = dt.replace(tzinfo=None).replace(tzinfo=_utc.utc)
    return aware_utc.astimezone(_display_timezone).strftime(fmt)


templates.env.filters["local_time"] = local_time
