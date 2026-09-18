"""Password/PIN hashing and session-based login for both account types.

Sessions are plain signed cookies (Starlette's SessionMiddleware) holding a
user_id and/or admin_id. That's enough for this scale of app on a LAN - no
need for token refresh flows, etc. The secret key is generated once and
persisted to disk so restarting the app doesn't log everyone out.
"""

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
from fastapi import Depends, Request
from sqlmodel import Session, select

from db import get_session
from models import Admin, User

# Per the user: PINs are short by design (low signup friction), which
# also makes them easier to guess - and nothing previously slowed down
# repeated attempts at all, for either account type (an admin's
# password is a higher-stakes target than any one user's PIN, so this
# applies to both, not just the short-PIN case that motivated it).
# Deliberately simple - a fixed threshold and a fixed lockout duration,
# not escalating durations or per-IP tracking - see check_lockout()/
# record_failed_login() below for why per-*account* is the right scope
# for the actual threat here (one person guessing a specific other
# person's credentials), not a general anti-abuse system.
LOGIN_LOCKOUT_THRESHOLD = 5
LOGIN_LOCKOUT_DURATION = timedelta(minutes=15)

# Using bcrypt directly rather than passlib: passlib (unmaintained since
# 2020) ships a bundled self-test for an old bcrypt wraparound bug that
# crashes outright against current bcrypt releases (>=4.1), which now raise
# instead of silently truncating on the >72-byte test secret it hashes.
# Calling bcrypt directly sidesteps that dead wrapper entirely.


class AuthRedirect(Exception):
    """Raised by require_user/require_admin when not logged in. Caught by
    an exception handler in main.py that turns it into a redirect to the
    right login page - plain HTTPException would render as a JSON error
    body instead of actually redirecting."""

    def __init__(self, location: str):
        self.location = location

_SECRET_KEY_PATH = Path(__file__).resolve().parent / "data" / "session_secret_key"


def get_session_secret_key() -> str:
    if _SECRET_KEY_PATH.exists():
        return _SECRET_KEY_PATH.read_text().strip()
    key = secrets.token_hex(32)
    _SECRET_KEY_PATH.write_text(key)
    return key


def hash_secret(secret: str) -> str:
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()


def verify_secret(secret: str, secret_hash: str) -> bool:
    return bcrypt.checkpw(secret.encode(), secret_hash.encode())


def get_current_user(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = session.get(User, user_id)
    if user is None or user.disabled:
        # A disabled user is logged out immediately, not just blocked from
        # a future login attempt - re-checked fresh from the DB on every
        # request, so an admin disabling someone takes effect right away
        # even if that user already has an open session.
        return None
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise AuthRedirect("/login")
    return user


def get_current_admin(
    request: Request, session: Session = Depends(get_session)
) -> Admin | None:
    admin_id = request.session.get("admin_id")
    if admin_id is None:
        return None
    return session.get(Admin, admin_id)


def require_admin(admin: Admin | None = Depends(get_current_admin)) -> Admin:
    if admin is None:
        raise AuthRedirect("/admin/login")
    return admin


def user_by_name(session: Session, name: str) -> User | None:
    return session.exec(select(User).where(User.name == name)).first()


def admin_by_username(session: Session, username: str) -> Admin | None:
    return session.exec(select(Admin).where(Admin.username == username)).first()


# ---- login rate-limiting (see LOGIN_LOCKOUT_THRESHOLD/DURATION above) ----
# Works identically for a User or an Admin - both have the same
# failed_login_attempts/locked_until columns (schema 3.3.0) - via plain
# duck typing rather than a shared base class, matching how little else
# in this codebase bothers abstracting over the two account types.


def check_lockout(account) -> str | None:
    """A user-facing error message if this account is currently locked
    out, else None. Read-only - call this *before* even checking the
    submitted password/PIN, both so a locked-out login doesn't do
    needless bcrypt work and so the response is the clear "try again in
    N minutes" message rather than a generic "didn't match" regardless
    of whether this attempt's credentials would actually have been
    right.

    `locked_until` is always written as UTC (see record_failed_login
    below) but - same gotcha this app has already hit for released_at/
    finished_at/event.at - comes back tzinfo-naive once round-tripped
    through SQLite, while a value just set moments ago on the same
    in-memory object (not yet re-fetched) is still tzinfo-aware. Handles
    both: strips tzinfo from whichever side has it rather than assuming
    one or the other, so this can't crash comparing aware to naive."""
    if account.locked_until is None:
        return None
    locked_until = account.locked_until.replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    remaining = locked_until - now
    if remaining <= timedelta(0):
        return None
    minutes = max(1, int(remaining.total_seconds() // 60) + 1)
    return f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."


def record_failed_login(session: Session, account) -> None:
    """Counts one more failed attempt, locking the account out for
    LOGIN_LOCKOUT_DURATION once it reaches LOGIN_LOCKOUT_THRESHOLD (and
    resetting the count for whatever comes after that lockout expires,
    rather than letting it climb forever). Commits immediately, not
    batched with anything else in the caller - a failed login attempt
    should never end up not-recorded because some *other* part of the
    same request didn't commit."""
    account.failed_login_attempts += 1
    if account.failed_login_attempts >= LOGIN_LOCKOUT_THRESHOLD:
        account.locked_until = datetime.now(timezone.utc) + LOGIN_LOCKOUT_DURATION
        account.failed_login_attempts = 0
    session.add(account)
    session.commit()


def record_successful_login(session: Session, account) -> None:
    """Clears any lockout bookkeeping on an actual successful login - a
    legitimate sign-in shouldn't stay throttled by attempts that came
    before it. A no-op write is skipped (nothing to clear) rather than
    committing on every single login regardless."""
    if account.failed_login_attempts or account.locked_until:
        account.failed_login_attempts = 0
        account.locked_until = None
        session.add(account)
        session.commit()
