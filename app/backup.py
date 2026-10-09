#!/usr/bin/env python3
"""Back up the database (and, once it exists, the archive/ directory) to
one of two rotating backup targets, picked automatically by day parity.

Why day-parity rotation onto two always-mounted drives, rather than a
manually-swapped drive: users can submit anytime the location is open
(self-service, see project memory queue3d-purpose), so backups run daily,
unattended - a scheme requiring a physical drive swap couldn't reliably
keep pace with that. Both backup targets just stay plugged in; this
script decides which one to write to each run.

Run manually to test, or see deploy/queue3d-backup.service/.timer for how
the real deployment actually runs this (a systemd timer, daily at 3am,
`Persistent=true` so a Pi that's off at that moment catches up on next
boot - not cron, despite this module predating that unit and still
showing a crontab-style example below for anyone wiring it in by hand
instead):
    0 3 * * * /path/to/.venv/bin/python3 /path/to/app/backup.py

Backup target paths come from env vars so the same script works in local
dev (pointed at throwaway directories) and on the real Pi (pointed at the
two USB backup mounts):
    QUEUE3D_BACKUP_DIR_A, QUEUE3D_BACKUP_DIR_B
Defaults to data/backups/{a,b} under this app directory if unset, which is
fine for local testing but NOT what you want on the real Pi - point these
at the two backup USB mounts there (/mnt/queue3d-backup-a,
/mnt/queue3d-backup-b - see deploy/README.md). A real incident, not a
hypothetical: deploy/queue3d.service (the main app, not this script's own
queue3d-backup.service) went without these for a while, which starved
only the System page's own live "Disk space" view of them (this script's
own scheduled runs were never affected, already having their own correct
copies) - see app/README.md's "Backups" section for the full account.
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


# How many past BackupRecord rows the admin-facing history page shows -
# at one row/day (see pick_target's own day-parity rotation), this is
# comfortably more than a school year, so in practice it's "all of
# them" rather than a real cap.
HISTORY_LIMIT = 400


def get_backup_history(session: Session, limit: int = HISTORY_LIMIT) -> list[BackupRecord]:
    """Every attempt (success or failure), most recent first - the full
    log behind get_last_successful_backup's single most-recent summary.
    See README.md's Backups & recovery to-do item this exists to close:
    an admin should be able to see the actual history, not just trust a
    single "last backup" timestamp on the dashboard."""
    return session.exec(
        select(BackupRecord).order_by(BackupRecord.started_at.desc()).limit(limit)
    ).all()

APP_DIR = Path(__file__).resolve().parent


def backup_targets():
    a = Path(os.environ.get("QUEUE3D_BACKUP_DIR_A", APP_DIR / "data" / "backups" / "a"))
    b = Path(os.environ.get("QUEUE3D_BACKUP_DIR_B", APP_DIR / "data" / "backups" / "b"))
    return {"a": a, "b": b}


def manifest() -> list[dict]:
    """What's actually sitting at each backup target right now, stat'd
    live off disk - not what the history log above *claims* happened.
    The other half of the same README to-do item: a log entry says a
    backup succeeded, but the only way to really confirm that is to look
    at the file it was supposed to produce.

    Each rotating target only ever holds the ONE most recent backup
    written there - backup_database/backup_archive overwrite in place
    every run, they don't accumulate - so this is a live snapshot of
    that one file/directory per target, not a history of its own.

    Same reasoning as sysmetrics.disk_mounts(): a target that doesn't
    exist yet (nothing's ever backed up there, or - on the real
    deployment - that drive is unplugged right now) reports
    db_exists=False rather than raising."""
    result = []
    for label, target_dir in backup_targets().items():
        entry = {
            "label": label,
            "path": str(target_dir),
            "db_exists": False,
            "db_size": None,
            "db_mtime": None,
            "archive_exists": False,
            "archive_file_count": None,
            "archive_size": None,
            "archive_mtime": None,
        }
        try:
            stat = (target_dir / DB_PATH.name).stat()
            entry["db_exists"] = True
            entry["db_size"] = stat.st_size
            entry["db_mtime"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        except OSError:
            pass

        archive_dir = target_dir / "archive"
        if archive_dir.is_dir():
            entry["archive_exists"] = True
            count = 0
            total_size = 0
            newest_mtime = 0.0
            for path in archive_dir.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                count += 1
                total_size += st.st_size
                newest_mtime = max(newest_mtime, st.st_mtime)
            entry["archive_file_count"] = count
            entry["archive_size"] = total_size
            entry["archive_mtime"] = (
                datetime.fromtimestamp(newest_mtime, tz=timezone.utc) if count else None
            )
        result.append(entry)
    return result


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
        # Only enforced when this specific target came from an explicit
        # QUEUE3D_BACKUP_DIR_A/B env var (the real deployment) - local dev's
        # default data/backups/{a,b} is a plain directory, never a mount,
        # and that's fine. On the real Pi, a target that ISN'T actually
        # mounted right now (drive unplugged, not yet remounted) is just an
        # ordinary empty folder on the SD card - writing there would
        # "succeed" and the dashboard would show a fresh green backup
        # timestamp, while silently defeating the entire point of the
        # two-drive rotation (the whole reason it exists is so one drive
        # being bad/missing never leaves you with zero real backups). Fail
        # this specific run loudly instead - the *other* drive still gets
        # its own real attempt on its own day, this only ever skips the
        # one target that's actually absent right now.
        env_var = f"QUEUE3D_BACKUP_DIR_{label.upper()}"
        if env_var in os.environ and not os.path.ismount(target_dir):
            raise RuntimeError(
                f"{target_dir} (backup target '{label}') is not actually a "
                "mounted filesystem right now - refusing to write a backup "
                "onto local disk instead of the real external drive."
            )
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
