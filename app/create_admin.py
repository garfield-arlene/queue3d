#!/usr/bin/env python3
"""Create an admin account. Run this on the server directly - there's no
self-service admin signup by design (see routers/admin.py)."""

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

        admin = Admin(username=username, password_hash=hash_secret(password))
        session.add(admin)
        session.commit()

    print(f"Admin '{username}' created.")


if __name__ == "__main__":
    main()
