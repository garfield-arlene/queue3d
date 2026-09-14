"""Password/PIN hashing and session-based login for both account types.

Sessions are plain signed cookies (Starlette's SessionMiddleware) holding a
user_id and/or admin_id. That's enough for this scale of app on a LAN - no
need for token refresh flows, etc. The secret key is generated once and
persisted to disk so restarting the app doesn't log everyone out.
"""

import secrets
from pathlib import Path

import bcrypt
from fastapi import Depends, Request
from sqlmodel import Session, select

from db import get_session
from models import Admin, User

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
