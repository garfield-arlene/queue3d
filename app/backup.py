#!/usr/bin/env python3
"""Back up the database (and, once it exists, the archive/ directory) to
one of two rotating backup targets, picked automatically by day parity.

Why day-parity rotation onto two always-mounted drives, rather than a
manually-swapped drive: users can submit anytime the location is open
(self-service, see project memory queue3d-purpose), so backups run on a
daily cron, unattended - a scheme requiring a physical drive swap couldn't
reliably keep pace with that. Both backup targets just stay plugged in;
this script decides which one to write to each run.

Run manually to test, or wire into cron for real deployment:
    0 3 * * * /path/to/.venv/bin/python3 /path/to/app/backup.py

Backup target paths come from env vars so the same script works in local
dev (pointed at throwaway directories) and on the real Pi (pointed at the
two USB backup mounts):
    QUEUE3D_BACKUP_DIR_A, QUEUE3D_BACKUP_DIR_B
Defaults to data/backups/{a,b} under this app directory if unset, which is
fine for local testing but NOT what you want on the real Pi - point these
at the two backup USB mounts there.
"""

import os
import shutil
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from db import DB_PATH, engine, init_db
from models import BackupRecord
from storage import ARCHIVE_DIR

# How stale the last successful backup can be before the dashboard flags it.
# Backups run daily; this gives slack for cron timing/a slow run without
# nagging over nothing.
STALE_AFTER_HOURS = 36


def get_last_successful_backup(session: Session) -> BackupRecord | None:
    return session.exec(
        select(BackupRecord)
        .where(BackupRecord.success == True)  # noqa: E712 (SQLAlchemy needs ==, not `is`)
        .order_by(BackupRecord.started_at.desc())
    ).first()


def is_stale(record: BackupRecord | None) -> bool:
    if record is None:
        return True
    age = datetime.now(timezone.utc) - record.started_at.replace(tzinfo=timezone.utc)
    return age.total_seconds() > STALE_AFTER_HOURS * 3600

APP_DIR = Path(__file__).resolve().parent


def backup_targets():
    a = Path(os.environ.get("QUEUE3D_BACKUP_DIR_A", APP_DIR / "data" / "backups" / "a"))
    b = Path(os.environ.get("QUEUE3D_BACKUP_DIR_B", APP_DIR / "data" / "backups" / "b"))
    return {"a": a, "b": b}


def pick_target(today: date | None = None) -> tuple[str, Path]:
    """Alternate by day-of-year parity so consecutive runs land on
    different drives - if one backup happens to run against a corrupted
    live DB, the previous day's backup on the other drive is untouched."""
    today = today or date.today()
    targets = backup_targets()
    label = "a" if today.toordinal() % 2 == 0 else "b"
    return label, targets[label]


def backup_database(dest_dir: Path):
    """Safe, consistent copy of the live SQLite DB using its own online
    backup API - NOT a raw file copy, which could grab a half-written page
    if the app happens to be mid-write."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / DB_PATH.name
    source = sqlite3.connect(str(DB_PATH))
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return dest_path


def backup_archive(dest_dir: Path):
    """Mirror the archive/ directory (finished jobs - done/failed/rejected,
    see storage.py). scratch/ is deliberately never backed up - it only
    ever holds transient in-progress work, not anything worth preserving."""
    if not ARCHIVE_DIR.exists() or not any(ARCHIVE_DIR.iterdir()):
        return None  # nothing archived yet - skip rather than copy an empty dir
    dest = dest_dir / "archive"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(ARCHIVE_DIR, dest)
    return dest


def run_backup() -> BackupRecord:
    init_db()
    label, target_dir = pick_target()
    started_at = datetime.now(timezone.utc)
    try:
        db_dest = backup_database(target_dir)
        archive_dest = backup_archive(target_dir)
        detail = f"db -> {db_dest}"
        if archive_dest:
            detail += f", archive -> {archive_dest}"
        record = BackupRecord(started_at=started_at, target_label=label, success=True, detail=detail)
    except Exception as e:
        record = BackupRecord(started_at=started_at, target_label=label, success=False, detail=str(e))

    with Session(engine) as session:
        session.add(record)
        session.commit()
        session.refresh(record)
    return record


def main():
    record = run_backup()
    status = "OK" if record.success else "FAILED"
    print(f"[{status}] backup to '{record.target_label}': {record.detail}")
    sys.exit(0 if record.success else 1)


if __name__ == "__main__":
    main()
