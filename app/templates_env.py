"""One shared Jinja2Templates instance, used by every router.

Previously each router made its own `Jinja2Templates(directory="templates")`
- harmless while nothing needed to reach every template, but the version
footer does: `base.html` (which every page extends) needs `APP_VERSION` in
scope on every render, and Jinja2 globals are set per-Environment, so three
separate instances would mean three places to keep in sync. One shared
instance, one place to set it.
"""

from fastapi.templating import Jinja2Templates

from version import APP_VERSION

templates = Jinja2Templates(directory="templates")
templates.env.globals["APP_VERSION"] = APP_VERSION
