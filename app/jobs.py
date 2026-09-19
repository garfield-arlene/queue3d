"""Job queries and state transitions. Kept out of routers/ so the actual
business logic (what's allowed, what moves where) is testable on its own
and routers stay thin HTTP glue.
"""

import shutil
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from db import engine
from models import Admin, DRAFT_STATUSES, Job, JobEvent, JobStatus, QUEUE_STATUSES, TERMINAL_STATUSES, User
from pipeline import run_slice
from printer import PrinterError, capture_photo, send_print_job, system_information
from storage import (
    archive_photo_path,
    delete_job_files,
    move_job_to_archive,
    queue_paths,
    read_makerbot_duration_s,
    scratch_paths,
)


class JobActionError(Exception):
    """Raised when a requested transition isn't valid from a job's current
    state - e.g. releasing a job that isn't approved, or releasing a second
    job while one is already printing (only one job can be on the printer
    at a time)."""


def log_event(session: Session, job_id: int | None, actor: str, action: str, detail: str = "") -> None:
    """Appends one row to the activity log (models.JobEvent) - see that
    model's docstring for why this exists. Called from every function
    below that changes a job's status (and from upload()/cleanup_drafts.py
    for the two that don't live in this module), right alongside the
    session.add(job)/commit() for that same change, so the log and the
    job's own current state can never end up telling two different
    stories about the same action. `job_id=None` for an account
    lifecycle action (see routers/user.py's signup, routers/admin.py's
    disable/enable/delete) that isn't tied to any one job."""
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


def all_events(session: Session, limit: int = 500) -> list[tuple[JobEvent, str, str | None]]:
    """Every event across every job, most-recent first - the global admin
    activity log ("what's been happening, at a glance"), per the user:
    a single table of everything, not just reachable one job at a time.
    Distinct from job_events() above, which that per-job log still uses.

    Returns (event, original_filename, photo_path) triples from one
    joined query rather than N+1 separate lookups - `actor` is already a
    plain human-readable label stored directly on JobEvent (see that
    model's docstring), so the job's filename and (for a done/failed
    event) its photo are the only other things the log table needs.
    An *outer* join, not an inner one - an account lifecycle event
    (job_id=None, see that model's docstring) has no job to join at all,
    and should still show up here rather than silently vanishing from the
    query; original_filename/photo_path both come back None for those.

    Capped at `limit` for now, not paginated - full filtering is a
    separate, later to-do (per the user: "I will ask for log filters
    later"), so this is deliberately just "show recent activity," not a
    complete unbounded history browser yet."""
    return session.exec(
        select(JobEvent, Job.original_filename, Job.photo_path)
        .join(Job, JobEvent.job_id == Job.id, isouter=True)
        .order_by(JobEvent.at.desc())
        .limit(limit)
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


def queue_wait_seconds(job: Job) -> float | None:
    """How long ago a job actually joined the queue (job.queued_at), or
    None if it hasn't yet (still a draft) - same reasoning
    queue_position() above already uses for ordering: created_at (upload
    time) isn't queue time, since sitting on a draft for a while before
    submitting isn't time spent waiting in line. Same naive/aware
    handling as auth.check_lockout() (see that function's docstring for
    the full explanation) - queued_at is always written as UTC but comes
    back tzinfo-naive once round-tripped through SQLite."""
    if job.queued_at is None:
        return None
    queued_at = job.queued_at.replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0.0, (now - queued_at).total_seconds())


def is_old_job(job: Job, threshold_days: int) -> bool:
    """True once a still-undecided job has been waiting at least
    threshold_days (models.Settings.old_job_threshold_days) - the split
    point between the normal admin queue view and /admin/jobs/old.

    Deliberately queued/approved only, never printing - per the user,
    confirmed directly rather than guessed (README.md's to-do list
    explicitly flagged this as an open question): a print that's
    actively running is being acted on, not sitting in an undecided
    backlog, and already has its own live progress/ETA display (see
    "Live print progress") - a separate "this is old" signal on top of
    that would just be a second, different kind of staleness mixed into
    one view. Same scoping queue_wait_seconds() above already uses for
    exactly that reason."""
    if job.status not in (JobStatus.queued, JobStatus.approved):
        return False
    wait_s = queue_wait_seconds(job)
    return wait_s is not None and wait_s >= threshold_days * 86400


def _duration_correction_factor(session: Session) -> float:
    """Median ratio of actual-to-estimated duration across past
    *successful* prints, applied to future estimates in printing_eta()
    below - per the user, after noticing the slicer's own estimate run
    consistently short in real use. Only `done` jobs count, never
    `failed` ones: a failed print's duration says nothing about how long
    a full print actually takes - it could have been cut short at any
    point, 5% or 95% of the way through, by a cancellation or a real
    fault alike, and averaging that in would corrupt the correction
    rather than improve it. `max(1.0, ...)` - only ever corrects
    *upward* - since underestimating is the specific, observed problem;
    there's no evidence yet that a future estimate running long needs
    correcting the other way, and assuming so could make things worse.
    Median rather than mean so one unusually slow print doesn't skew
    every future estimate as more data accumulates. Returns 1.0 (no
    correction) with no `done` jobs yet to learn from.

    Necessarily includes whatever time elapsed between a print actually
    finishing and an admin noticing and clicking "Mark done" -
    `finished_at` is when that click happened, not confirmed to be the
    exact moment the printer itself actually stopped (`released_at` is
    accurate the other direction - see jobs.release). Rough by nature
    this way, but still meaningfully better than trusting the raw,
    uncorrected slicer estimate outright. Precisely fixing this would
    mean capturing the printer's own `current_process.elapsed_time` (see
    jobs.print_progress) at the moment of that click instead - not done
    here, since that reading has often been unavailable exactly when
    needed (the connection dying is the common case that motivated
    building the printer status banner in the first place) - see
    README.md's Printer to-do list."""
    done_jobs = session.exec(
        select(Job).where(
            Job.status == JobStatus.done,
            Job.released_at.is_not(None),
            Job.finished_at.is_not(None),
            Job.duration_estimate_s.is_not(None),
            Job.duration_estimate_s > 0,
        )
    ).all()
    ratios = [
        (job.finished_at - job.released_at).total_seconds() / job.duration_estimate_s
        for job in done_jobs
    ]
    if not ratios:
        return 1.0
    return max(1.0, statistics.median(ratios))


def corrected_duration_estimate_s(session: Session, job: Job) -> float | None:
    """job.duration_estimate_s scaled by _duration_correction_factor()
    above, or None if the slicer never produced an estimate for this job
    at all. The single place every displayed duration/ETA should go
    through - per the user, after noticing a job's displayed estimate
    changed (20 min shown at queue time, corrected to ~28 once released)
    depending on *when* it was looked at rather than showing the same,
    best-available number everywhere consistently. Cheap enough to call
    per job/request - one small indexed query - that no caching is worth
    the complexity yet."""
    if job.duration_estimate_s is None:
        return None
    return job.duration_estimate_s * _duration_correction_factor(session)


def format_duration(seconds: float) -> str:
    """"1d 2h 15m"-style formatting for any duration this app shows - a
    print's estimated length, previously always rendered as raw total
    minutes ("1500 min" for a genuinely multi-day print, per README.md's
    to-do list). Rounds to the nearest whole minute first (matching what
    was already shown - this never claimed second-level precision), then
    decomposes that into days/hours/minutes rather than rounding each
    unit separately, which would risk e.g. 59.6 minutes independently
    rounding to "1h 0m" out of an input that only rounds to "1h" as a
    whole. Drops leading AND trailing zero-value units ("2h" not
    "0d 2h 0m") but always shows at least "0m" rather than an empty
    string, for a (unrealistic in practice, but not impossible) estimate
    under 30 seconds."""
    total_minutes = round(seconds / 60)
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def printing_eta(session: Session, job: Job) -> datetime | None:
    """Estimated completion time for a job that's actively printing, or
    None if it isn't printing or there's nothing to estimate from
    (released_at/duration_estimate_s both need to be set - a job released
    before duration estimation existed, or one the slicer couldn't
    estimate for, has neither). `released_at + ` the history-corrected
    estimate (see corrected_duration_estimate_s above) - a fallback for
    whenever a live read isn't available (see print_progress below for
    the real thing), so per the user, this is a clearly-labeled countdown
    from that estimate, not a claim of real progress. Rendered
    client-side (see static/countdown.js) rather than recomputed "minutes
    remaining" server-side, so it keeps ticking between page loads/htmx
    polls without needing a matching request each time."""
    if job.status != JobStatus.printing or job.released_at is None or job.duration_estimate_s is None:
        return None
    return job.released_at + timedelta(seconds=corrected_duration_estimate_s(session, job))


def print_progress(job: Job) -> dict | None:
    """Live progress for a job that's printing right now, read directly
    from the printer (printer.system_information()) rather than derived
    from the original time estimate - see printing_eta() above for that
    estimate-only fallback, still needed for whenever this isn't
    available. Confirmed live against the real printer:
    `current_process.progress` tracks genuine print progress (its ratio
    to elapsed time visibly grows rather than staying constant, and it
    matched what the printer's own screen showed at the same moment) -
    but only once `current_process.step == "printing"`. Earlier steps
    (seen: "final_heating") reset `progress` to their own, unrelated
    0-100 scale (heating-to-temperature progress, not print progress), so
    showing it as print-percent then would be actively misleading -
    other, undocumented step values presumably exist too, and get the
    same conservative treatment: a percentage is only ever returned for
    the one step confirmed to mean "percent of the print done."

    Returns None - "no live reading available," not "0% done" - if the
    job isn't printing, the read fails (printer unreachable; this is
    read-only/best-effort and must never block anything else), or
    current_process doesn't actually name this job's file (a stale reply,
    or genuinely a different job - matched by filename since
    current_process has no job id of its own to compare against)."""
    if job.status != JobStatus.printing or not job.makerbot_path:
        return None
    try:
        info = system_information()
    except PrinterError:
        return None
    current = info.get("current_process")
    if not current:
        return None
    if Path(current.get("filename") or "").name != Path(job.makerbot_path).name:
        return None
    step = current.get("step")
    return {
        "step": step,
        "percent": current.get("progress") if step == "printing" else None,
        "time_remaining_s": current.get("time_remaining"),
    }


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


def _delete_job_genuinely(session: Session, job: Job, actor: str, detail: str) -> None:
    """The actual delete both delete_old_job() and delete_own_job() below
    share - a genuine, unrecoverable delete, not another terminal status
    like reject() (which deliberately keeps the job and archives its
    files as a permanent record). Only the actor label and detail
    wording differ between "an admin clearing a stale job" and "a user
    removing their own" - the mechanics are identical.

    The job's own prior event history (submit, slice, queue-submission,
    etc.) is deleted along with it rather than left behind as orphaned
    rows a global log join can no longer resolve to a filename - once
    the job itself is gone, that per-job history has nothing left to
    attach meaningfully to. What actually persists is one new,
    job_id=None event recording the deletion itself - the exact same
    pattern user_deleted already uses for a User that's gone by the
    time anyone reads that log entry back."""
    _require_status(job, JobStatus.queued, JobStatus.approved)
    for event in session.exec(select(JobEvent).where(JobEvent.job_id == job.id)).all():
        session.delete(event)
    delete_job_files(job)
    log_event(session, None, actor, "job_deleted", detail=detail)
    session.delete(job)
    session.commit()


def delete_old_job(session: Session, job: Job, admin: Admin) -> None:
    """Genuinely deletes a stale, still-undecided job - reachable only
    from /admin/jobs/old (routers/admin.py), never a general "delete any
    queued job" action. Per the user, this specific action "removes the
    job... and deletes the model files, with no undo" - see
    _delete_job_genuinely() above for the shared mechanics with
    delete_own_job() below, this branch's admin-side equivalent for a
    job that's gone stale rather than one its own submitter no longer
    wants."""
    user = session.get(User, job.user_id)
    submitter = user.name if user else "?"
    _delete_job_genuinely(
        session, job, _admin_actor(admin), f"{job.original_filename} (submitted by {submitter})"
    )


def delete_own_job(session: Session, job: Job, user: User) -> None:
    """A user deleting their own still-undecided job - per README.md's
    to-do list, "they may no longer want it," with the same genuine,
    no-undo delete semantics as delete_old_job() above (see
    _delete_job_genuinely() for the shared mechanics). Deliberately the
    same queued/approved-only restriction, not just "any job the user
    owns" - once released and printing, the job is being acted on by an
    admin already; deleting out from under that would be a different,
    much riskier action this to-do item never asked for. No "submitted
    by" clause in the log detail unlike delete_old_job()'s - the actor
    label (user:<name>) already says who, since here the actor and the
    submitter are always the same person."""
    _delete_job_genuinely(session, job, f"user:{user.name}", job.original_filename)


def requeue_job(session: Session, job: Job, admin: Admin) -> Job:
    """The "still relevant, just give it another chance" option
    alongside delete_old_job() above, on /admin/jobs/old - per the user,
    a way to move a stale job back to the normal queue rather than only
    being able to delete or reject it. Status is untouched (still
    queued or approved); only queued_at resets to now, which is also
    what queue_position() orders by - so this genuinely sends it to the
    back of the line again, the same as if it had just been submitted,
    not just a display change that leaves it cutting ahead of jobs that
    have been waiting less time."""
    _require_status(job, JobStatus.queued, JobStatus.approved)
    job.queued_at = datetime.now(timezone.utc)
    session.add(job)
    log_event(session, job.id, _admin_actor(admin), "requeued")
    session.commit()
    session.refresh(job)
    return job


def delete_all_old_jobs(session: Session, admin: Admin, threshold_days: int) -> int:
    """Bulk version of delete_old_job() above, for clearing an entire
    backlog in one click rather than one job at a time - per the user.
    Re-checks is_old_job() itself against the current threshold rather
    than trusting whatever a caller's own page happened to render a
    moment earlier, so this can't delete something that's since been
    requeued or is otherwise no longer actually old. Each job is deleted
    exactly the way delete_old_job() deletes one (its own event history
    removed, files deleted, one job_id=None "job_deleted" event logged) -
    just looped, not a separate bulk-specific code path; one commit per
    job rather than a single batched commit, same as calling the
    single-job delete route N times by hand would do - simple over
    optimal for what's expected to be a handful of jobs at once, not
    thousands. Returns how many were actually deleted."""
    to_delete = [job for job in active_jobs(session) if is_old_job(job, threshold_days)]
    for job in to_delete:
        delete_old_job(session, job, admin)
    return len(to_delete)


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
    scale_factor: float = 1.0,
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
                scale_factor=scale_factor,
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


MIN_SCALE_FACTOR = 0.01  # 1% - below this, a model isn't meaningfully printable any more
MAX_SCALE_FACTOR = 10.0  # 1000% - generous, but not unbounded


def start_reslice(
    session: Session,
    job: Job,
    enable_supports: bool,
    support_style: str | None,
    scale_factor: float = 1.0,
) -> Path:
    """Resets a draft to re-slice the same already-uploaded file with new
    settings - the whole point of splitting slicing from submitting: a
    user can freely iterate on support settings (or, now, scale - see
    models.Job.scale_factor) before ever deciding to submit. Returns the
    STL path to hand to slice_and_update (via a BackgroundTask, same as
    the initial slice - see routers/user.py)."""
    _require_status(job, JobStatus.sliced, JobStatus.slice_failed)
    if not (MIN_SCALE_FACTOR <= scale_factor <= MAX_SCALE_FACTOR):
        raise JobActionError(
            f"Scale must be between {MIN_SCALE_FACTOR * 100:.0f}% and {MAX_SCALE_FACTOR * 100:.0f}%."
        )
    job.supports_enabled = enable_supports
    job.support_style = support_style
    job.scale_factor = scale_factor
    job.status = JobStatus.submitted
    job.slice_error = None
    session.add(job)
    style_detail = (
        f"supports={enable_supports}"
        + (f" style={support_style}" if support_style else "")
        + (f" scale={scale_factor:.2f}" if scale_factor != 1.0 else "")
    )
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


def mark_finished(session: Session, job: Job, admin: Admin | None, success: bool, reason: str = "") -> Job:
    """Records a print's outcome - a manual admin action (`admin` set), or
    an automatic one (`admin=None`, actor logged as `"system"` - same
    convention `cleanup_drafts.py` already uses for automated draft
    expiry) from check_and_finish_active_print() below, now that live
    printer status reporting exists (see release() above and
    print_progress()) to actually notice a print ending on its own.

    Also captures a photo of the build plate via the printer's camera,
    regardless of outcome - success, failure, or (today, since there's no
    separate "stopped manually" state yet) whatever this was marked as -
    so both the submitting user and an admin have a visual record of what
    actually happened, not just a status word. Per the user: this also
    lets an admin visually confirm which physical print belongs to which
    submitter's claim. A failed capture (camera unreachable, printer
    already powered back off, etc.) never blocks recording the print's
    own outcome - it's a best-effort extra, not a precondition, and the
    reason for a missing photo is still recorded in the log entry either
    way. `reason` is prepended to that photo-outcome log note either way
    (e.g. why an automatic detection decided this was a failure, or that
    it detected completion), and - on a failure specifically - is also
    stored on Job.failure_reason so the submitter sees *why*, not just
    that it failed (see routers/admin.py's mark_failed_job, which
    requires one from a manual "Mark failed" the same way reject()
    requires admin_note). Ignored on success: a 'done' job has nothing to
    explain."""
    _require_status(job, JobStatus.printing)
    job.status = JobStatus.done if success else JobStatus.failed
    job.finished_at = datetime.now(timezone.utc)
    if not success and reason:
        job.failure_reason = reason
    move_job_to_archive(job)

    try:
        jpeg_data = capture_photo()
        photo_path = archive_photo_path(job.id)
        photo_path.write_bytes(jpeg_data)
        job.photo_path = str(photo_path)
        photo_detail = "photo captured"
    except PrinterError as e:
        photo_detail = f"photo capture failed: {e}"
    if reason:
        photo_detail = f"{reason}; {photo_detail}"

    session.add(job)
    actor = _admin_actor(admin) if admin is not None else "system"
    log_event(session, job.id, actor, job.status.value, detail=photo_detail)
    session.commit()
    session.refresh(job)
    return job


def check_and_finish_active_print(session: Session) -> Job | None:
    """Polled from a background thread (see main.py's startup handler),
    not from any request - the actual point of "automatic," per the user
    after noticing every real photo-capture failure so far traced back to
    the same root cause: the connection dying in the gap between a print
    actually finishing and an admin *noticing* and clicking "Mark done."
    Closing that gap is the whole reason this exists, not just saving a
    click - the connection this uses is whichever one was already live
    from the most recent progress poll, not one that's had time to go
    idle or get killed by a cancel in the meantime.

    Deliberately conservative: only acts on an *explicit* positive signal
    from the printer (`current_process.complete`/`cancelled`/`error`),
    never on absence or ambiguity. If current_process has already gone
    missing or stopped matching this job's file by the time this polls -
    which can genuinely happen if the printer clears it before an
    in-between poll catches the transition, or the connection simply
    isn't reachable this round - this does nothing and leaves the job
    `printing`, same as if this feature didn't exist, rather than guess
    at an outcome it can't actually confirm. The manual Mark done/Mark
    failed buttons stay exactly as they were - a safety net for whatever
    this misses, not something this replaces.

    Returns the job if it just took action, else None (nothing printing,
    printer unreachable, or nothing conclusive to act on yet)."""
    job = session.exec(select(Job).where(Job.status == JobStatus.printing)).first()
    if job is None or not job.makerbot_path:
        return None
    try:
        info = system_information()
    except PrinterError:
        return None
    current = info.get("current_process")
    if not current or Path(current.get("filename") or "").name != Path(job.makerbot_path).name:
        return None

    error = current.get("error")
    cancelled = bool(current.get("cancelled"))
    complete = bool(current.get("complete"))
    if cancelled or error:
        why = "cancelled at the printer" if cancelled else f"printer reported an error: {error}"
        return mark_finished(session, job, admin=None, success=False, reason=f"detected automatically - {why}")
    if complete:
        return mark_finished(session, job, admin=None, success=True, reason="detected automatically - print complete")
    return None


_AUTO_FINISH_POLL_INTERVAL_S = 15


def _auto_finish_poll_loop():
    while True:
        try:
            with Session(engine) as session:
                check_and_finish_active_print(session)
        except Exception:
            # Best-effort background loop - never let one bad tick (a
            # transient DB hiccup, an unexpected reply shape) kill the
            # whole poller. The manual Mark done/Mark failed buttons are
            # always there regardless of whether this is working.
            pass
        time.sleep(_AUTO_FINISH_POLL_INTERVAL_S)


def start_auto_finish_poller() -> None:
    """Starts the loop above on a daemon thread - called once, from
    main.py's startup handler. See check_and_finish_active_print() for
    what it actually checks each tick, and why it's deliberately
    conservative about when it acts."""
    threading.Thread(target=_auto_finish_poll_loop, daemon=True).start()
