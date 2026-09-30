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
not just Basic.

Mode (light/dark) is a separate axis from theme, not folded into it -
also per the user, after starting out one way (this file originally had
no MODES at all, before "let's add light mode and dark mode... as a
separate toggle" superseded that): picking "Ocean" and "Dark"
independently, say, should work the same as any other combination.
`current_theme()`/`current_mode()` in templates_env.py turn a viewer's
two separate stored choices into the two separate `data-theme`/
`data-mode` attributes CSS actually keys off of."""

from datetime import date

DEFAULT_THEME = "bmms"
DEFAULT_MODE = "light"

# The id every logged-out page (the two login pages, signup) always
# renders as, regardless of DEFAULT_THEME above - a deliberately separate
# constant, not the same thing. DEFAULT_THEME is what a *signed-in*
# account with no saved preference falls back to (see current_theme() in
# templates_env.py) - per the user, "select the BMMS theme as default for
# all users and admins." A logged-out request has no account to have a
# preference at all, and those pages were hand-built and verified
# around this one specific plain layout (see admin_login.html/
# user_login.html/user_signup.html's own .login-page) - a sidebar
# theme's structural CSS (a wrapped-in sidebar, a second .bmms-logo from
# the header on top of that page's own large letterhead one) would
# visually collide with it. Before
# DEFAULT_THEME became "bmms" this distinction was invisible (both
# constants happened to be "default"), which is exactly why it's worth
# spelling out explicitly now rather than letting the two quietly drift
# in different directions again next time either one changes.
LOGGED_OUT_THEME = "default"

THEMES = {
    "default": "Basic",
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
    # facing the viewer (his whole near-side body visible, not just his
    # head), and hanging from the top of the window by ONE FIST touching
    # the top edge directly - not by his neck (an earlier version's real
    # mistake, caught by the user as reading like a noose - wrong for a
    # school app) and not by a thread down to that fist either (the very
    # next fix attempt's own mistake, caught by the user as reading like
    # an obscene gesture instead) - while swinging a miniature spool
    # below him like a yoyo with the other. See theme-fil-hang.svg's own
    # top comment before changing anything near the top edge again.
    "fil": "Fil",
    # Same shared sidebar/full-width/bordered-section layout again, this
    # time built for one specific real deployment site rather than a
    # generic option: Black Mountain Middle School's own Raiders colors
    # (maroon and gold, per the user) and school logo
    # (app/static/bmms-logo.png - the school's own public logo, the same
    # image found via a plain web search, not anything sensitive).
    # Unlike Console/Savanna/Fil, this theme also reaches onto the
    # logged-out pages (admin_login.html, user_login.html, and
    # user_signup.html - the last one only added once the user noticed
    # it had been missed) - those render before any account (and
    # therefore any account's saved theme) exists for the request, so
    # current_theme() always resolves to LOGGED_OUT_THEME there
    # regardless of this theme's own existence (see that constant's own
    # comment above); those templates show the school logo
    # unconditionally instead of through the theme mechanism, per the
    # user wanting it there regardless. Also this app's actual
    # DEFAULT_THEME (above) - per the user, ahead of deployment: "select
    # the BMMS theme as default for all users and admins."
    "bmms": "BMMS",
    # Same shared sidebar/full-width/bordered-section layout, this time
    # seasonal rather than a permanent option - a haunted-mansion night
    # scene (app/static/theme-halloween-manor.svg: a full moon, bats, a
    # picket fence, tombstones, and a row of glowing jack-o'-lanterns)
    # behind the sidebar, and a spider hanging from the top of the
    # window on its own strand of web (app/static/theme-halloween-
    # spider.svg), in the same spot Fil hangs from under that theme.
    # Friendly, not frightening: no weapons, no blood, nothing sharper
    # than a jack-o'-lantern's carved smile - every character (the
    # ghosts, the spider, the pumpkins) has a plain round smiling face,
    # matching the same "school-appropriate" bar the signup page's own
    # acceptable-use rule sets for anything a user submits. See
    # SEASONAL_THEMES below for when it's actually offered, and
    # app/README.md's "The 'Halloween' theme" section for the full
    # writeup.
    "halloween": "Halloween",
    # Same shared sidebar/full-width/bordered-section layout again,
    # seasonal like Halloween - a cozy harvest-dusk scene (app/static/
    # theme-thanksgiving-harvest.svg: a warm harvest sun, a barn, corn
    # shocks, pumpkins and gourds, a rail fence, drifting leaves) behind
    # the sidebar, and a turkey standing at the bottom of the window
    # (app/static/theme-thanksgiving-turkey.svg) - not attached to the
    # top edge the way Fil/the Halloween spider are: a fist gripping the
    # edge or a strand of web both read fine for a filament creature or
    # a spider, but a turkey doesn't have an equivalent (and specifically
    # dangling by anything read too close to actual Thanksgiving-dinner
    # imagery to belong in a school app besides) - standing normally at
    # the bottom, feet on the ground, sidesteps that entirely. Deliberately
    # just an autumn harvest theme, not depicting Pilgrims, the First
    # Thanksgiving, or any Native American imagery - the same
    # "school-appropriate," nothing-that-could-offend bar every other
    # seasonal theme in this app follows. See SEASONAL_THEMES below for
    # when it's actually offered, and app/README.md's "The
    # 'Thanksgiving' theme" section for the full writeup.
    "thanksgiving": "Thanksgiving",
}

MODES = {
    "light": "Light",
    "dark": "Dark",
}

# Themes that only make sense for part of the year - id -> ((start_month,
# start_day), (end_month, end_day)), both ends inclusive, checked against
# today's date. Halloween's window is deliberately wider than just the
# few days around October 31st (mid-September through mid-November) so
# it's not gone again the moment the holiday itself passes. Any theme id
# not listed here (every non-seasonal one) is available year-round -
# see is_theme_selectable() below for the actual gating, and its own
# always-available-on-127.0.0.1 exception.
SEASONAL_THEMES = {
    "halloween": ((9, 15), (11, 15)),
    "thanksgiving": ((10, 15), (11, 30)),
}


def is_valid_theme(theme_id: str | None) -> bool:
    return theme_id in THEMES


def is_valid_mode(mode_id: str | None) -> bool:
    return mode_id in MODES


def _is_dev_loopback(request) -> bool:
    """Whether this request is hitting the app directly at 127.0.0.1/
    localhost, as a developer would running `uvicorn` on their own
    machine - deliberately `request.url.hostname` (built from the `Host`
    header), not `request.client.host` (the actual TCP peer). The real
    deployment (see app/README.md's "Deployment: zero internet access"
    section) always puts nginx in front, proxying to `uvicorn` over
    127.0.0.1 (`deploy/queue3d.service`/`deploy/nginx-queue3d.conf`) -
    from uvicorn's own perspective, `request.client.host` is *always*
    127.0.0.1 there too, for every real visitor on the deployment LAN,
    since that's nginx's own loopback connection making the request, not
    theirs. Checked directly against a running server: a request hitting
    uvicorn straight (as a raw dev session does) reports
    `url.hostname == "127.0.0.1"`; the exact same request with a
    `Host: q3d.home.mygarfield.us` header (what nginx's own
    `proxy_set_header Host $host` forwards, carrying the real visitor's
    requested host, not nginx's) reports the real hostname instead -
    `client.host` was "127.0.0.1" in both cases. Using `client.host`
    here would have made every seasonal theme permanently available in
    production, defeating the whole feature."""
    return request.url.hostname in ("127.0.0.1", "localhost", "::1")


def _in_season(window: tuple[tuple[int, int], tuple[int, int]]) -> bool:
    (start_month, start_day), (end_month, end_day) = window
    today = date.today()
    start = date(today.year, start_month, start_day)
    end = date(today.year, end_month, end_day)
    return start <= today <= end


def is_theme_selectable(theme_id: str, request, current: str | None = None) -> bool:
    """Whether theme_id can be picked right now - every non-seasonal
    theme always can; a seasonal one (SEASONAL_THEMES above) only during
    its own date window, or unconditionally for a developer hitting the
    app directly at 127.0.0.1/localhost (see _is_dev_loopback's own
    docstring). `current` is the account's *already-saved* theme, always
    treated as selectable regardless of season/host when passed - without
    it, a settings form resubmitted after Halloween's window closes
    (without anyone touching that dropdown) would reject its own
    currently-shown value as if it were a fresh, disallowed pick.

    Called without `current` (the default), this is also exactly "is the
    account's own existing pick still active right now" -
    effective_theme() below uses it that way, deliberately with no
    exemption for the account's own value: unlike the dropdown-listing
    case above, keeping an expired seasonal theme selected *is* the
    thing meant to end once its window (or the 127.0.0.1 exception)
    closes, not something to protect from rejection."""
    if theme_id == current:
        return True
    if theme_id not in SEASONAL_THEMES:
        return True
    return _is_dev_loopback(request) or _in_season(SEASONAL_THEMES[theme_id])


def theme_choices(request, current: str | None = None) -> dict[str, str]:
    """THEMES filtered to what a settings page's theme <select> should
    actually offer - see is_theme_selectable() above for the rule."""
    return {tid: name for tid, name in THEMES.items() if is_theme_selectable(tid, request, current)}


def effective_theme(theme: str | None, request) -> str:
    """What an account with this stored theme choice should actually
    render as right now: the stored value itself, unless it's unset or a
    seasonal theme that's since fallen out of its own window (and this
    isn't a 127.0.0.1/localhost dev request - is_theme_selectable's own
    exception) - DEFAULT_THEME in either of those cases. A seasonal theme
    doesn't just stop being offered in the settings dropdown once its
    season ends (theme_choices() already handles that) - it stops being
    the account's *active* theme at all, reverting to DEFAULT_THEME
    rather than silently continuing to render something no longer "in
    season" until someone manually changes it back. Used both by
    current_theme() (templates_env.py, which also persists this back to
    the account's own stored value - see _signed_in_account()) and by
    the settings pages' own selected_theme (routers/user.py,
    routers/admin.py), so a settings page's dropdown reflects the same
    reset on the very same render that triggers it, not one request
    later."""
    if theme and is_theme_selectable(theme, request):
        return theme
    return DEFAULT_THEME
