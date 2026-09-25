"""The set of selectable UI themes - one shared source of truth for the
settings pages' dropdowns (routers/user.py, routers/admin.py) and for
validating a submitted choice, so a bad/stale value can never get saved
to `User.theme`/`Admin.theme` and silently fail to match anything in
base.html's CSS.

Deliberately just an id -> display name mapping, nothing about a theme's
actual look lives here - that's CSS custom properties in base.html,
keyed by the same id via `[data-theme="<id>"]` (see current_theme() in
templates_env.py for how a viewer's stored choice becomes that
attribute). Everything about a theme (fonts, wallpaper images if any,
future additions) must stay fully self-hosted - see project memory
queue3d-deployment-network: this app runs with zero internet access, so
nothing here can ever reach for a CDN font or an external image URL.

Adding a theme: pick an id (lowercase, matches a CSS attribute selector
- no spaces), add it here with its display name, and add both a
`[data-theme="<id>"] { ... }` block (its light palette) and a
`[data-theme="<id>"][data-mode="dark"] { ... }` block (its dark
palette) in base.html's <style> - per the user, every theme gets both,
not just Default.

Mode (light/dark) is a separate axis from theme, not folded into it -
also per the user, after starting out one way (this file originally had
no MODES at all, before "let's add light mode and dark mode... as a
separate toggle" superseded that): picking "Ocean" and "Dark"
independently, say, should work the same as any other combination.
`current_theme()`/`current_mode()` in templates_env.py turn a viewer's
two separate stored choices into the two separate `data-theme`/
`data-mode` attributes CSS actually keys off of."""

DEFAULT_THEME = "default"
DEFAULT_MODE = "light"

THEMES = {
    "default": "Default",
    # Left-sidebar nav (the same {% block nav %} links every page already
    # has, just laid out as a vertical column instead of a top row),
    # bordered/titled sections, and a full browser-width layout instead
    # of the default's fixed 720px column - per the user. See base.html's
    # own [data-theme="console"] blocks for the actual styling, and its
    # inline script for the one thing pure CSS genuinely can't do here:
    # grouping each <h3> and the content after it (up to the next <h3>)
    # into one bordered box, since CSS has no "select these siblings up
    # to a stopping point" selector.
    "console": "Console",
}

MODES = {
    "light": "Light",
    "dark": "Dark",
}


def is_valid_theme(theme_id: str | None) -> bool:
    return theme_id in THEMES


def is_valid_mode(mode_id: str | None) -> bool:
    return mode_id in MODES
