"""The shared, self-service help page - not gated behind login, since the
login and signup pages themselves need to link to it before anyone has an
account at all (see help.html's own "Logging in and signing up" section).
Per the user: "This is a self-service app and the help/how to should
maintain that" - every user-facing page's own nav gets a "Help" link here
(deep-linked to the section actually relevant to it, e.g. /help#submitting
from the dashboard), rather than a separate how-to page per feature that
would need its own nav entry and its own upkeep.

Deliberately user-or-anonymous only - no admin content, and no attempt to
look at whether the visitor also happens to be signed in as an admin in
this same browser (see routers/admin.py's own admin-only `help` route for
that instead). A real incident caught this the hard way: an earlier
version of this route also took an `admin: Admin | None = Depends(...)`
and picked which nav to show by whichever of user/admin was truthy - but
Starlette's session cookie is one shared cookie for the whole browser, and
the user was deliberately running an admin login in one tab and a user
login in another tab of that same browser (two legitimate, independent
sessions in the app's own model, just sharing one cookie) - so a plain
user's tab could render the admin nav, and a stale/lingering admin_id
(from a previous tab, possibly long since "logged out" in every way that
mattered to that person) could resurface unpredictably. Per the user
afterward: "The auth should never simply look for the presence of a
cookie... This should NEVER happen again." The fix isn't more careful
session bookkeeping - it's not doing role detection here at all: this
route only ever depends on get_current_user, so its content and nav can
never reflect anything but "is *a* user signed in or not," the same
single-role contract every other non-admin page already relies on.
"""

from fastapi import APIRouter, Depends, Request

from auth import get_current_user
from models import User
from templates_env import templates

router = APIRouter()


@router.get("/help")
def help_page(
    request: Request,
    user: User | None = Depends(get_current_user),
):
    """user is None for a visitor who isn't signed in at all (the common
    case, arriving from the login or signup page) - get_current_user
    rather than require_user on purpose, since this page must never
    redirect anyone away to log in first. See this module's own docstring
    for why there is deliberately no admin parameter here."""
    return templates.TemplateResponse(request, "help.html", {"user": user})
