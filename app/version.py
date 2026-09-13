"""Single source of truth for the running app's version - a plain text
file (`VERSION`), not a hardcoded constant, so it can be bumped without
touching code and doesn't depend on git being present on the deployed Pi.
Read once at import time, not per-request/per-migration-check: this only
changes on a deploy/restart, never while the process is running.

Used for two things that both need the exact same value: the footer on
every page (see templates_env.py), and db.py's migration system, which
records this version alongside the schema each time it changes - per the
user, a schema change should always come with a version bump, so the
version itself doubles as the schema version rather than tracking a
separate number that could drift out of sync with it.
"""

from pathlib import Path

_VERSION_FILE = Path(__file__).parent / "VERSION"
try:
    APP_VERSION = _VERSION_FILE.read_text().strip()
except FileNotFoundError:
    APP_VERSION = "unknown"
