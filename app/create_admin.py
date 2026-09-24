#!/usr/bin/env python3
"""Create an admin account. Run this on the server directly - there's no
*open* self-service admin signup by design (see routers/admin.py); once
at least one admin exists, further ones can be added from the web UI
(routers/admin.py's admins_page) by an admin who's already signed in,
without needing server access again.

Every admin this script creates is permanently unremovable
(models.Admin.unremovable) - nothing, including the web UI above, can
ever delete or un-mark it. That's deliberate: this is the only way to
create the very first admin at all (before any UI exists to log into),
so at least one admin created this way needs to survive no matter what
happens to any admin created later, or a deployment could end up with
zero admins and no way back in short of server access again."""

import getpass
import sys

from sqlmodel import Session

from auth import admin_by_username, hash_secret
from db import engine, init_db
from models import Admin


def main():
    init_db()
    username = input("Admin username: ").strip()
    if not username:
        print("Username can't be empty.", file=sys.stderr)
        sys.exit(1)

    with Session(engine) as session:
        if admin_by_username(session, username):
            print(f"An admin named '{username}' already exists.", file=sys.stderr)
            sys.exit(1)

        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords didn't match.", file=sys.stderr)
            sys.exit(1)
        if len(password) < 8:
            print("Use at least 8 characters.", file=sys.stderr)
            sys.exit(1)

        admin = Admin(username=username, password_hash=hash_secret(password), unremovable=True)
        session.add(admin)
        session.commit()

    print(f"Admin '{username}' created (permanent - see this script's own docstring).")


if __name__ == "__main__":
    main()
