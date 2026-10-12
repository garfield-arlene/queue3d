#!/usr/bin/env python3
"""Back up the database (and, once it exists, the archive/ directory) to
one of two rotating backup targets, picked automatically by day parity.

Why day-parity rotation onto two always-mounted drives, rather than a
manually-swapped drive: users can submit anytime the location is open
(self-service, see project memory queue3d-purpose), so backups run daily,
unattended - a scheme requiring a physical drive swap couldn't reliably
keep pace with that. Both backup targets just stay plugged in; this
script decides which one to write to each run.

Each database backup is a dated snapshot (queue3d-YYYY-MM-DD.db), kept
for RETENTION_DAYS and pruned after that - not a single fixed filename
overwritten in place every run. That first version only ever gave two
total recoverable backups across both drives (today's and yesterday's),
which is nowhere near enough slack for a real deployment: per the
person running this one, directly, the site is visited roughly every
two weeks, and "something gets totally borked" needing to be caught on
the very next visit, or the one after, is a real scenario a 1-day-deep
backup can't help with at all. The archive/ mirror below stays a single
current copy, not dated snapshots of its own - archived job files are
effectively append-only (storage.py never edits a finished job's own
files in place), so there's nothing meaningful to roll back to there
the way there is for the database's own point-in-time state.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from db import DB_PATH, engine, init_db
from models import BackupRecord
from storage import ARCHIVE_DIR

# How stale the last successful backup can be before the dashboard flags it.
# Backups run daily; this gives slack for cron timing/a slow run without
# nagging over nothing.
STALE_AFTER_HOURS = 36

# How many days of dated database snapshots each drive keeps before
# prune_old_backups deletes them - three weeks, a week of margin past
# the real ~2-week on-site visit cadence this is actually sized for
# (see this module's own docstring), not an arbitrary round number.
RETENTION_DAYS = 21


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
    at the files it was supposed to produce.

    `db_snapshots` is every dated database backup currently retained at
    that target (see backup_database/prune_old_backups/RETENTION_DAYS),
    most recent first - this target's own real, current recovery depth,
    not a single overwritten file. The archive/ mirror has no history of
    its own (see this module's docstring for why), so it's still a
    single current snapshot.

    Same reasoning as sysmetrics.disk_mounts(): a target that doesn't
    exist yet (nothing's ever backed up there, or - on the real
    deployment - that drive is unplugged right now) reports an empty
    db_snapshots list rather than raising."""
    result = []
    for label, target_dir in backup_targets().items():
        entry = {
            "label": label,
            "path": str(target_dir),
            "db_snapshots": [],
            "db_total_size": 0,
            "archive_exists": False,
            "archive_file_count": None,
            "archive_size": None,
            "archive_mtime": None,
        }
        try:
            snapshot_paths = sorted(target_dir.glob("queue3d-*.db"), reverse=True)
        except OSError:
            snapshot_paths = []
        for path in snapshot_paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            # The date embedded in the filename, not file mtime - same
            # reasoning as prune_old_backups: mtime is what a cloned/
            # restored backup drive would reset, not what actually
            # determines how old a given snapshot really is. A name that
            # doesn't parse (shouldn't happen - this app is the only
            # thing that ever writes here) is skipped rather than shown
            # with a made-up date.
            try:
                snapshot_date = date.fromisoformat(path.stem.removeprefix("queue3d-"))
            except ValueError:
                continue
            entry["db_snapshots"].append({
                "name": path.name,
                "size": size,
                "date": snapshot_date,
            })
            entry["db_total_size"] += size

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


def _db_snapshot_name(d: date) -> str:
    return f"queue3d-{d.isoformat()}.db"


def backup_database(dest_dir: Path, today: date | None = None) -> Path:
    """Safe, consistent copy of the live SQLite DB using its own online
    backup API - NOT a raw file copy, which could grab a half-written page
    if the app happens to be mid-write.

    Writes a dated snapshot (queue3d-YYYY-MM-DD.db), never overwriting a
    previous day's - see prune_old_backups for how old ones eventually
    get cleaned up, and this module's own docstring for why a single
    fixed filename per drive isn't enough. `today` is a parameter (like
    pick_target's own), not always date.today() internally, so a caller
    can pin it for a deterministic, reproducible snapshot name in tests."""
    today = today or date.today()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / _db_snapshot_name(today)
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


def prune_old_backups(dest_dir: Path, today: date | None = None, retention_days: int = RETENTION_DAYS) -> None:
    """Deletes dated database snapshots in dest_dir older than
    retention_days, keyed off the date embedded in each snapshot's own
    filename - not file mtime, which copying a drive's contents
    elsewhere (e.g. for the support bundle, or just cloning a USB stick)
    could reset without actually changing how old the backup itself is.
    A filename that doesn't parse as one of ours is left alone rather
    than risking deleting something that isn't actually a backup
    snapshot at all."""
    today = today or date.today()
    cutoff = today - timedelta(days=retention_days)
    for path in dest_dir.glob("queue3d-*.db"):
        try:
            snapshot_date = date.fromisoformat(path.stem.removeprefix("queue3d-"))
        except ValueError:
            continue
        if snapshot_date < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


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
    today = date.today()
    label, target_dir = pick_target(today)
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
        db_dest = backup_database(target_dir, today)
        prune_old_backups(target_dir, today)
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
