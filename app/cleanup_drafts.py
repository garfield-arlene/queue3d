#!/usr/bin/env python3
"""Expire sliced-but-never-submitted drafts once they've sat around longer
than the admin-configured threshold (models.Settings.draft_expiry_days, set
at /admin/settings).

Why this exists: splitting slicing from submitting to the queue (see
models.JobStatus's docstring) means a draft can now sit indefinitely
instead of resolving itself within seconds - the user, not a cron job,
decides when (or whether) to submit it. Left unchecked, abandoned drafts
would just accumulate forever, holding files in scratch/ with nothing
ever cleaning them up. The threshold is admin-configurable rather than a
fixed constant because there's no one right answer for every deployment's
traffic and storage - see the user's own call on this.

Run manually to test, or wire into cron for real deployment, e.g. daily
at 4am (after the 3am backup - see backup.py):
    0 4 * * * /path/to/.venv/bin/python3 /path/to/app/cleanup_drafts.py
"""

import sys
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from db import engine, init_db
from models import DRAFT_STATUSES, Job, JobStatus, Settings
from storage import move_draft_to_archive


def get_draft_expiry_days(session: Session) -> int:
    settings = session.get(Settings, 1)
    return settings.draft_expiry_days if settings else Settings().draft_expiry_days


def run_cleanup() -> list[dict]:
    """Expires every draft (models.DRAFT_STATUSES - includes a stuck
    'submitted' job too, as a safety net for a crashed slicing attempt,
    not just 'sliced'/'slice_failed') whose created_at is older than the
    configured threshold. Files move to archive/ (same as any other
    terminal job - see storage.move_draft_to_archive) rather than being
    deleted outright, consistent with how a rejected/done/failed job is
    handled: nothing this app finishes with ever just disappears.

    Returns plain dicts, not the Job rows themselves - those are detached
    the moment this function's Session closes, and touching an unloaded
    attribute on a detached SQLModel instance afterward (e.g. to print it)
    raises DetachedInstanceError rather than just working."""
    with Session(engine) as session:
        cutoff = datetime.now(timezone.utc) - timedelta(days=get_draft_expiry_days(session))
        expired = session.exec(
            select(Job)
            .where(Job.status.in_(list(DRAFT_STATUSES)))
            .where(Job.created_at < cutoff)
        ).all()
        info = [
            {"id": job.id, "original_filename": job.original_filename, "user_id": job.user_id}
            for job in expired
        ]
        for job in expired:
            move_draft_to_archive(job)
            job.status = JobStatus.expired
            job.finished_at = datetime.now(timezone.utc)
            session.add(job)
        session.commit()
        return info


def main():
    init_db()
    expired = run_cleanup()
    print(f"Expired {len(expired)} abandoned draft(s).")
    for job in expired:
        print(f"  #{job['id']} {job['original_filename']} (user_id={job['user_id']})")
    sys.exit(0)


if __name__ == "__main__":
    main()
