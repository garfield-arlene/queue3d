#!/usr/bin/env python3
"""Restore the database (and the archive/ mirror) from one of the two
backup drives - the disaster-recovery counterpart to backup.py, for when
the live data is gone or corrupted. This app never runs this on its own
(unlike backup.py, there's no systemd timer for it) - it's a manual,
break-glass action a person runs deliberately. See deploy/README.md's
"Disaster recovery: restoring from backup" section for when and how.

Run with no arguments for the guided, interactive flow - lists what each
drive actually has (see backup.manifest), asks which drive and which
date, shows exactly what's about to happen, and requires typing
"restore" to proceed. Needs this app's own venv (sqlmodel etc.), same as
every other script here - not the system python3:
    /path/to/.venv/bin/python3 restore_backup.py

Or non-interactively, for scripting:
    /path/to/.venv/bin/python3 restore_backup.py --drive a --date 2026-10-07 --yes
    /path/to/.venv/bin/python3 restore_backup.py --drive a --latest --yes

Stop the app first (`sudo systemctl stop queue3d`) - this overwrites the
live database and archive/ directory, which should not be in use while
that happens. The current database (even if it's the corrupted one this
restore exists to fix) is copied aside first, to <DB_PATH>.before-restore,
in case the restore itself turns out to be a mistake - that safety copy
is overwritten by name on every run, not kept as its own history, so
it only ever protects against undoing the single most recent restore.

Restoring to a given snapshot loses anything submitted or changed after
that snapshot's own date - the same tradeoff deploy/README.md's upgrade
rollback section already makes for its own pre-upgrade backups, just on
the scale of up to ~2 days (this module's RETENTION_DAYS/day-parity
rotation) instead of "since the last upgrade."
"""

import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from backup import backup_targets, manifest
from db import DB_PATH
from storage import ARCHIVE_DIR


def _service_is_active() -> bool | None:
    """True/False from `systemctl is-active queue3d`, or None if
    systemctl itself isn't available at all (local dev, not a real
    systemd deployment) - None means "can't tell," handled as a softer
    warning rather than a hard block, unlike a confirmed True."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "queue3d"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError):
        return None
    return result.stdout.strip() == "active"


def _resolve_snapshot(drive: str, snapshot_date: date | None) -> Path:
    target_dir = backup_targets()[drive]
    if snapshot_date is None:
        candidates = sorted(target_dir.glob("queue3d-*.db"), reverse=True)
        if not candidates:
            raise FileNotFoundError(
                f"No database snapshots found on drive {drive.upper()} ({target_dir})."
            )
        return candidates[0]
    snapshot_path = target_dir / f"queue3d-{snapshot_date.isoformat()}.db"
    if not snapshot_path.exists():
        raise FileNotFoundError(
            f"No snapshot for {snapshot_date.isoformat()} on drive "
            f"{drive.upper()} ({snapshot_path})."
        )
    return snapshot_path


def restore(drive: str, snapshot_path: Path) -> None:
    """Does the actual, irreversible-without-the-safety-copy work -
    separated from main() so a script/test can call it directly without
    going through argument parsing or the confirmation prompt."""
    target_dir = backup_targets()[drive]
    archive_source = target_dir / "archive"

    if DB_PATH.exists():
        before_path = DB_PATH.with_name(DB_PATH.name + ".before-restore")
        shutil.copy2(DB_PATH, before_path)
        print(f"Current database saved aside to {before_path} first, in case this restore is itself a mistake.")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snapshot_path, DB_PATH)
    print(f"Database restored from {snapshot_path}.")

    if archive_source.is_dir():
        if ARCHIVE_DIR.exists():
            shutil.rmtree(ARCHIVE_DIR)
        shutil.copytree(archive_source, ARCHIVE_DIR)
        print(f"Archive restored from {archive_source}.")
    else:
        print("No archive mirror found on this drive - archive/ left untouched.")


def _print_available() -> None:
    print("Available snapshots:")
    for entry in manifest():
        print(f"  Drive {entry['label'].upper()} ({entry['path']}):")
        if not entry["db_snapshots"]:
            print("    (none)")
        for snap in entry["db_snapshots"]:
            print(f"    {snap['date'].isoformat()} - {snap['name']} ({snap['size']:,} bytes)")


def _interactive() -> tuple[str, date | None]:
    _print_available()
    drive = input("\nRestore from drive (a/b): ").strip().lower()
    if drive not in ("a", "b"):
        print("Not a or b.", file=sys.stderr)
        sys.exit(1)
    raw_date = input("Date to restore (YYYY-MM-DD, or blank for the newest on that drive): ").strip()
    try:
        snapshot_date = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        print(f"'{raw_date}' isn't a YYYY-MM-DD date.", file=sys.stderr)
        sys.exit(1)
    return drive, snapshot_date


def _parse_args(args: list[str]):
    yes = "--yes" in args
    args = [a for a in args if a != "--yes"]
    drive = None
    snapshot_date = None
    latest = False
    i = 0
    while i < len(args):
        if args[i] == "--drive" and i + 1 < len(args):
            drive = args[i + 1].lower()
            i += 2
        elif args[i] == "--date" and i + 1 < len(args):
            try:
                snapshot_date = date.fromisoformat(args[i + 1])
            except ValueError:
                print(f"'{args[i + 1]}' isn't a YYYY-MM-DD date.", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif args[i] == "--latest":
            latest = True
            i += 1
        else:
            print(f"Unrecognized argument: {args[i]}", file=sys.stderr)
            sys.exit(1)
    return drive, snapshot_date, latest, yes


def main():
    drive, snapshot_date, latest, yes = _parse_args(sys.argv[1:])

    if drive is None:
        drive, snapshot_date = _interactive()
    elif drive not in ("a", "b"):
        print("--drive must be a or b.", file=sys.stderr)
        sys.exit(1)
    elif not latest and snapshot_date is None:
        print("Pass --date YYYY-MM-DD or --latest.", file=sys.stderr)
        sys.exit(1)

    try:
        snapshot_path = _resolve_snapshot(drive, snapshot_date)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    active = _service_is_active()
    if active:
        print(
            "WARNING: the queue3d service is currently running. Stop it first\n"
            "(sudo systemctl stop queue3d) - restoring under a live app risks\n"
            "the app overwriting this restore the moment it next writes.",
            file=sys.stderr,
        )
        if not yes:
            sys.exit(1)

    print(
        f"\nAbout to restore drive {drive.upper()}'s {snapshot_path.name} over "
        f"the live database ({DB_PATH}), plus that drive's archive mirror over "
        f"the live archive/ directory ({ARCHIVE_DIR})."
    )
    print("Anything submitted or changed after that snapshot's date will be lost.")

    if not yes:
        confirm = input("Type 'restore' to continue: ").strip()
        if confirm != "restore":
            print("Not confirmed - nothing was changed.")
            sys.exit(1)

    restore(drive, snapshot_path)
    print("\nDone. Restart the app (sudo systemctl start queue3d) and verify it comes up correctly.")


if __name__ == "__main__":
    main()
