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
- no spaces), add it here with its display name, and add the
`[data-theme="<id>"] { ... }` override block in base.html's <style>."""

DEFAULT_THEME = "default"

THEMES = {
    "default": "Default",
}


def is_valid_theme(theme_id: str | None) -> bool:
    return theme_id in THEMES
