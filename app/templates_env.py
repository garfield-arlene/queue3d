"""One shared Jinja2Templates instance, used by every router.

Previously each router made its own `Jinja2Templates(directory="templates")`
- harmless while nothing needed to reach every template, but the version
footer does: `base.html` (which every page extends) needs `APP_VERSION` in
scope on every render, and Jinja2 globals are set per-Environment, so three
separate instances would mean three places to keep in sync. One shared
instance, one place to set it.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")

# A plain text file, not a hardcoded constant - lets the deployed version on
# the printer's Pi be bumped (or read by a deploy script) without touching
# code. Read once at import time, not per-request: this only changes on a
# deploy/restart, never while the process is running.
_VERSION_FILE = Path(__file__).parent / "VERSION"
try:
    APP_VERSION = _VERSION_FILE.read_text().strip()
except FileNotFoundError:
    APP_VERSION = "unknown"

templates.env.globals["APP_VERSION"] = APP_VERSION
