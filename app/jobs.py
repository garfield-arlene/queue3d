"""Job queries and state transitions. Kept out of routers/ so the actual
business logic (what's allowed, what moves where) is testable on its own
and routers stay thin HTTP glue.
"""

import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from db import engine
from models import Admin, DRAFT_STATUSES, Job, JobEvent, JobStatus, QUEUE_STATUSES, TERMINAL_STATUSES, User
from pipeline import run_slice
from printer import PrinterError, send_print_job
from storage import move_job_to_archive, queue_paths, read_makerbot_duration_s, scratch_paths


class JobActionError(Exception):
    """Raised when a requested transition isn't valid from a job's current
    state - e.g. releasing a job that isn't approved, or releasing a second
    job while one is already printing (only one job can be on the printer
    at a time)."""


def log_event(session: Session, job_id: int, actor: str, action: str, detail: str = "") -> None:
    """Appends one row to the audit log (models.JobEvent) - see that
    model's docstring for why this exists. Called from every function
    below that changes a job's status (and from upload()/cleanup_drafts.py
    for the two that don't live in this module), right alongside the
    session.add(job)/commit() for that same change, so the log and the
    job's own current state can never end up telling two different
    stories about the same action."""
    session.add(JobEvent(job_id=job_id, actor=actor, action=action, detail=detail))


def _user_actor(session: Session, user_id: int) -> str:
    user = session.get(User, user_id)
    return f"user:{user.name}" if user else f"user:#{user_id}"


def _admin_actor(admin: Admin) -> str:
    return f"admin:{admin.username}"


def jobs_for_user(session: Session, user_id: int) -> list[Job]:
    return session.exec(
        select(Job).where(Job.user_id == user_id).order_by(Job.created_at.desc())
    ).all()


def active_jobs(session: Session) -> list[Job]:
    """Everything actually in the shared queue, oldest-queued first - this
    is what a reviewing admin looks at. An explicit allow-list
    (models.QUEUE_STATUSES), not just "not terminal" - a sliced-but-
    unsubmitted draft is also not terminal, but must never show up here;
    see models.py's comment on the three-way status partition this and
    user_has_active_jobs below both rely on."""
    return session.exec(
        select(Job)
        .where(Job.status.in_(list(QUEUE_STATUSES)))
        .order_by(Job.queued_at.asc())
    ).all()


def finished_jobs(session: Session) -> list[Job]:
    """Everything done with (models.TERMINAL_STATUSES - rejected/done/
    failed/expired), most-recently-finished first - the admin "browse
    finished jobs" view. Distinct from active_jobs() (the live queue) and
    from job_events() below (one job's full history, not a cross-job
    list)."""
    return session.exec(
        select(Job)
        .where(Job.status.in_(list(TERMINAL_STATUSES)))
        .order_by(Job.finished_at.desc())
    ).all()


def job_events(session: Session, job_id: int) -> list[JobEvent]:
    """A job's full audit trail, oldest first - see models.JobEvent."""
    return session.exec(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.at.asc())
    ).all()


def user_has_active_jobs(session: Session, user_id: int) -> bool:
    """Used to guard deleting a user - see routers/admin.py's user
    management actions. Deleting someone with a job still in flight would
    either orphan a queue entry mid-review or, worse, leave a printing job
    with no owner to attribute it to. Unlike active_jobs() above, this
    deliberately still counts drafts (not just queued+) as "active" - a
    draft has real files sitting in scratch/ and represents unfinished
    work, even though an admin never sees it; deleting a draft-only user
    is left to draft expiry cleaning things up first, same as anyone
    else's job."""
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
    oldest-queued first across all users - None if this job isn't in that
    waiting state at all. Ordered by queued_at (when submit_draft actually
    put it in the queue), not created_at (when it was first uploaded) -
    sitting on a sliced draft for a while before submitting must not let
    it cut in ahead of jobs submitted right away in the meantime."""
    if job.status not in (JobStatus.queued, JobStatus.approved):
        return None
    ahead = session.exec(
        select(Job)
        .where(Job.status.in_([JobStatus.queued, JobStatus.approved]))
        .where(Job.queued_at < job.queued_at)
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
    log_event(session, job.id, _admin_actor(admin), "approved")
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
    log_event(session, job.id, _admin_actor(admin), "rejected", detail=job.admin_note)
    session.commit()
    session.refresh(job)
    return job


def release(session: Session, job: Job, admin: Admin) -> Job:
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
    log_event(session, job.id, _admin_actor(admin), "released")
    session.commit()
    session.refresh(job)
    return job


def slice_and_update(
    job_id: int,
    stl_path: Path,
    enable_supports: bool,
    support_style: str | None,
) -> None:
    """Runs slicing for a draft and records the outcome as 'sliced' (ready
    to preview and, if the user wants, submit) or 'slice_failed' - never
    'queued' directly any more, now that slicing and submitting are split;
    see submit_draft below for the separate, explicit action that actually
    puts a job in the queue. Called as a FastAPI `BackgroundTask`, both
    from routers/user.py's upload handler (the first slice) and its
    reslice handler (any later one, same uploaded file, new settings), so
    neither request blocks on however long slicing takes. Opens its own
    Session - the request's is already closed by the time a background
    task runs (see db.py's per-request get_session).

    Output goes to scratch/, not queue/ - a draft's files stay in scratch/
    for as long as it's a draft, however many times it gets re-sliced;
    only submit_draft moves anything into queue/.

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

        _scratch_stl, scratch_makerbot, scratch_supports = scratch_paths(job.id)
        try:
            success, detail = run_slice(
                stl_path,
                scratch_makerbot,
                enable_supports=enable_supports,
                support_style=support_style,
                supports_json_path=scratch_supports if enable_supports else None,
            )
        except Exception as e:
            success, detail = False, f"Unexpected error while slicing: {e}"

        actor = _user_actor(session, job.user_id)
        if success:
            job.makerbot_path = str(scratch_makerbot)
            job.duration_estimate_s = read_makerbot_duration_s(scratch_makerbot)
            if enable_supports and scratch_supports.exists():
                job.supports_path = str(scratch_supports)
            else:
                job.supports_path = None  # clear a stale one from a previous re-slice attempt
            job.slice_error = None  # clear a stale one from a previous failed attempt
            job.status = JobStatus.sliced
            log_event(session, job.id, actor, "sliced")
        else:
            job.status = JobStatus.slice_failed
            job.slice_error = detail[-4000:]  # cap - slicer output can be long
            log_event(session, job.id, actor, "slice_failed", detail=job.slice_error[-1000:])

        session.add(job)
        session.commit()


def start_reslice(
    session: Session, job: Job, enable_supports: bool, support_style: str | None
) -> Path:
    """Resets a draft to re-slice the same already-uploaded file with new
    settings - the whole point of splitting slicing from submitting: a
    user can freely iterate on support settings before ever deciding to
    submit. Returns the STL path to hand to slice_and_update (via a
    BackgroundTask, same as the initial slice - see routers/user.py)."""
    _require_status(job, JobStatus.sliced, JobStatus.slice_failed)
    job.supports_enabled = enable_supports
    job.support_style = support_style
    job.status = JobStatus.submitted
    job.slice_error = None
    session.add(job)
    style_detail = f"supports={enable_supports}" + (f" style={support_style}" if support_style else "")
    log_event(session, job.id, _user_actor(session, job.user_id), "reslice_started", detail=style_detail)
    session.commit()
    return Path(job.stl_path)


def submit_draft(session: Session, job: Job) -> Job:
    """The explicit "submit to queue" action - moves a successfully-sliced
    draft's files from scratch/ to queue/ and actually puts it in the
    queue. Nothing before this point (uploading, slicing, re-slicing) is
    ever visible to an admin or counted in queue_position - see
    active_jobs() above."""
    _require_status(job, JobStatus.sliced)
    scratch_stl, scratch_makerbot, scratch_supports = scratch_paths(job.id)
    queue_stl, queue_makerbot, queue_supports = queue_paths(job.id)
    if scratch_stl.exists():
        shutil.move(str(scratch_stl), str(queue_stl))
        job.stl_path = str(queue_stl)
    if scratch_makerbot.exists():
        shutil.move(str(scratch_makerbot), str(queue_makerbot))
        job.makerbot_path = str(queue_makerbot)
    if scratch_supports.exists():
        shutil.move(str(scratch_supports), str(queue_supports))
        job.supports_path = str(queue_supports)
    job.status = JobStatus.queued
    job.queued_at = datetime.now(timezone.utc)
    session.add(job)
    log_event(session, job.id, _user_actor(session, job.user_id), "queued")
    session.commit()
    session.refresh(job)
    return job


def mark_finished(session: Session, job: Job, admin: Admin, success: bool) -> Job:
    """Manual admin override to record a print's outcome, until live
    printer status reporting exists (see release() above)."""
    _require_status(job, JobStatus.printing)
    job.status = JobStatus.done if success else JobStatus.failed
    job.finished_at = datetime.now(timezone.utc)
    move_job_to_archive(job)
    session.add(job)
    log_event(session, job.id, _admin_actor(admin), job.status.value)
    session.commit()
    session.refresh(job)
    return job
