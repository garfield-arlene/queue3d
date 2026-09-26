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
    # Same sidebar/full-width/bordered-section layout as Console (the
    # structural CSS rules in base.html are shared between the two,
    # keyed off both ids at once) with a desert-sunrise look instead -
    # per the user: warm oranges/golds, an acacia-silhouette background
    # photo behind the sidebar. That photo (app/static/theme-savanna-
    # sunset.jpg) is "The Savannah's Last Ember" by Wikimedia Commons
    # user Temptious, dedicated to the public domain (CC0 - no
    # attribution legally required, credited here anyway for
    # traceability): https://commons.wikimedia.org/wiki/File:The_Savannah%E2%80%99s_Last_Ember.jpg
    # - cropped (a soccer goalpost visible at the original photo's right
    # edge) and downscaled from 4000x3000/3.6MB to 1600x1363/~300KB for
    # a background image loaded on every page.
    "savanna": "Savanna",
    # Same shared sidebar/full-width/bordered-section layout again, this
    # time with a wholly original mascot instead of a photo - per the
    # user, after a Mickey Mouse theme was proposed and turned down: only
    # the specific 1928 Steamboat Willie design is actually public
    # domain, and even that is still a live Disney trademark regardless
    # of copyright status, which using it as a recurring UI mascot would
    # squarely risk. "Fil" (app/static/theme-fil-*.svg) is drawn from
    # scratch for this project instead, with no license or trademark
    # question at all - a stick figure made of bent filament wire (per
    # the user, Forky-from-Toy-Story vibes: googly eyes, a scribbled
    # marker mouth, bendy limbs), peeking around the sidebar's own edge
    # facing the viewer, and dangling from the top of the window while
    # swinging a miniature spool of filament below him like a yoyo.
    "fil": "Fil",
}

MODES = {
    "light": "Light",
    "dark": "Dark",
}


def is_valid_theme(theme_id: str | None) -> bool:
    return theme_id in THEMES


def is_valid_mode(mode_id: str | None) -> bool:
    return mode_id in MODES
