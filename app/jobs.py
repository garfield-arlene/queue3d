"""Job queries and state transitions. Kept out of routers/ so the actual
business logic (what's allowed, what moves where) is testable on its own
and routers stay thin HTTP glue.
"""

import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from db import engine
from models import Admin, Job, JobStatus, TERMINAL_STATUSES
from pipeline import run_slice
from printer import PrinterError, send_print_job
from storage import move_job_to_archive, queue_paths, read_makerbot_duration_s


class JobActionError(Exception):
    """Raised when a requested transition isn't valid from a job's current
    state - e.g. releasing a job that isn't approved, or releasing a second
    job while one is already printing (only one job can be on the printer
    at a time)."""


def jobs_for_user(session: Session, user_id: int) -> list[Job]:
    return session.exec(
        select(Job).where(Job.user_id == user_id).order_by(Job.submitted_at.desc())
    ).all()


def active_jobs(session: Session) -> list[Job]:
    """Everything not yet finished, oldest first - this is "the queue" a
    reviewing admin looks at. Defined as NOT-terminal rather than an
    explicit allow-list so it stays correct by construction if a status is
    ever added."""
    return session.exec(
        select(Job)
        .where(Job.status.not_in(list(TERMINAL_STATUSES)))
        .order_by(Job.submitted_at.asc())
    ).all()


def user_has_active_jobs(session: Session, user_id: int) -> bool:
    """Used to guard deleting a user - see routers/admin.py's user
    management actions. Deleting someone with a job still in flight would
    either orphan a queue entry mid-review or, worse, leave a printing job
    with no owner to attribute it to."""
    return (
        session.exec(
            select(Job)
            .where(Job.user_id == user_id)
            .where(Job.status.not_in(list(TERMINAL_STATUSES)))
        ).first()
        is not None
    )


def queue_position(session: Session, job: Job) -> int | None:
    """1-based position among jobs waiting their turn (queued/approved),
    oldest-first across all users - None if this job isn't in that
    waiting state at all."""
    if job.status not in (JobStatus.queued, JobStatus.approved):
        return None
    ahead = session.exec(
        select(Job)
        .where(Job.status.in_([JobStatus.queued, JobStatus.approved]))
        .where(Job.submitted_at < job.submitted_at)
    ).all()
    return len(ahead) + 1


def _require_status(job: Job, *allowed: JobStatus):
    if job.status not in allowed:
        allowed_names = ", ".join(s.value for s in allowed)
        raise JobActionError(f"Job is '{job.status.value}', expected one of: {allowed_names}")


def approve(session: Session, job: Job, admin: Admin) -> Job:
    _require_status(job, JobStatus.queued)
    job.status = JobStatus.approved
    job.reviewed_at = datetime.now(timezone.utc)
    job.reviewed_by_admin_id = admin.id
    job.admin_note = None
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def reject(session: Session, job: Job, admin: Admin, note: str) -> Job:
    _require_status(job, JobStatus.queued, JobStatus.approved)
    if not note.strip():
        raise JobActionError("A note is required when rejecting a job.")
    job.status = JobStatus.rejected
    job.reviewed_at = datetime.now(timezone.utc)
    job.reviewed_by_admin_id = admin.id
    job.admin_note = note.strip()
    job.finished_at = datetime.now(timezone.utc)
    move_job_to_archive(job)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def release(session: Session, job: Job) -> Job:
    """Sends an approved job to the printer and marks it printing. Actually
    talks to the hardware (see printer.py) - only flips the status once
    the upload genuinely succeeds, so a failed send leaves the job
    'approved' rather than claiming a print started that may not have."""
    _require_status(job, JobStatus.approved)
    already_printing = session.exec(
        select(Job).where(Job.status == JobStatus.printing)
    ).first()
    if already_printing is not None:
        raise JobActionError(
            f"Job #{already_printing.id} is already printing - mark it done/failed first."
        )
    if not job.makerbot_path:
        raise JobActionError("This job has no sliced file to send.")

    try:
        send_print_job(Path(job.makerbot_path))
    except PrinterError as e:
        raise JobActionError(str(e))

    job.status = JobStatus.printing
    job.released_at = datetime.now(timezone.utc)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def slice_and_update(
    job_id: int,
    stl_path: Path,
    enable_supports: bool,
    support_style: str | None,
) -> None:
    """Runs slicing for a just-submitted job and records the outcome -
    called as a FastAPI `BackgroundTask` from routers/user.py's upload
    handler, so that request can save the file and return right away
    instead of blocking on however long slicing takes (previously the
    whole point of the "upload progress" to-do item: the request used to
    block silently until slicing finished entirely, with no way to show
    the user anything was happening). Opens its own Session - the
    request's is already closed by the time a background task runs (see
    db.py's per-request get_session).

    Any unexpected exception here (not just an ordinary slicer failure,
    which run_slice already reports as (False, detail)) still has to leave
    the job in a real terminal-for-this-attempt state rather than stuck at
    'submitted' forever with no way for the user to tell it isn't still
    working - a background task's exceptions don't propagate anywhere a
    user would ever see them.
    """
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            return

        queue_stl, queue_makerbot, queue_supports = queue_paths(job.id)
        try:
            success, detail = run_slice(
                stl_path,
                queue_makerbot,
                enable_supports=enable_supports,
                support_style=support_style,
                supports_json_path=queue_supports if enable_supports else None,
            )
        except Exception as e:
            success, detail = False, f"Unexpected error while slicing: {e}"

        if success:
            shutil.move(str(stl_path), str(queue_stl))
            job.stl_path = str(queue_stl)
            job.makerbot_path = str(queue_makerbot)
            job.duration_estimate_s = read_makerbot_duration_s(queue_makerbot)
            if enable_supports and queue_supports.exists():
                job.supports_path = str(queue_supports)
            job.status = JobStatus.queued
        else:
            job.status = JobStatus.slice_failed
            job.slice_error = detail[-4000:]  # cap - slicer output can be long

        session.add(job)
        session.commit()


def mark_finished(session: Session, job: Job, success: bool) -> Job:
    """Manual admin override to record a print's outcome, until live
    printer status reporting exists (see release() above)."""
    _require_status(job, JobStatus.printing)
    job.status = JobStatus.done if success else JobStatus.failed
    job.finished_at = datetime.now(timezone.utc)
    move_job_to_archive(job)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job
