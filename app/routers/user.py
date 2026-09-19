import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlmodel import Session

import storage
from auth import (
    check_lockout,
    hash_secret,
    record_failed_login,
    record_successful_login,
    require_user,
    user_by_name,
    verify_secret,
)
from db import get_session
from jobs import (
    JobActionError,
    corrected_duration_estimate_s,
    delete_own_job,
    format_duration,
    jobs_for_user,
    log_event,
    printing_eta,
    queue_position,
    queue_wait_seconds,
    slice_and_update,
    start_reslice,
    submit_draft,
)
from mesh import convert_obj_to_stl
from models import DRAFT_STATUSES, Job, JobStatus, User
from storage import MAX_UPLOAD_BYTES, scratch_stl_path
from templates_env import templates
from themes import DEFAULT_MODE, DEFAULT_THEME, MODES, THEMES, is_valid_mode, is_valid_theme

# OrcaSlicer's own support_style values, each confirmed (by directly
# comparing sliced gcode output, not just guessed) to actually produce
# distinct results from the others - see slicing/slice.py's
# SUPPORT_STYLE_TYPE for the full story of what each one needs to take
# effect. "default" lets Orca choose on its own; "organic" is PrusaSlicer's
# name for the plain tree-support algorithm (distinct from the
# hybrid/slim tree variants, not from "default" here specifically, since
# our profile's own baseline already is tree-based - see slice.py).
SUPPORT_STYLES = {
    "default": "Automatic",
    "grid": "Grid",
    "snug": "Snug",
    "organic": "Organic",
    "tree_hybrid": "Tree - hybrid",
    "tree_slim": "Tree - slim",
}

router = APIRouter()


@router.get("/signup")
def signup_form(request: Request):
    return templates.TemplateResponse(request, "user_signup.html", {})


@router.post("/signup")
def signup(
    request: Request,
    name: str = Form(...),
    pin: str = Form(...),
    session: Session = Depends(get_session),
):
    name = name.strip()
    error = None
    if not name:
        error = "Enter your name."
    elif len(pin) < 4:
        error = "PIN must be at least 4 digits."
    elif user_by_name(session, name):
        error = "That name is already taken - if it's you, log in instead."

    if error:
        return templates.TemplateResponse(
            request, "user_signup.html", {"error": error, "name": name}
        )

    user = User(name=name, pin_hash=hash_secret(pin))
    session.add(user)
    session.commit()
    session.refresh(user)

    log_event(session, None, f"user:{user.name}", "user_registered")
    session.commit()

    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "user_login.html", {})


@router.post("/login")
def login(
    request: Request,
    name: str = Form(...),
    pin: str = Form(...),
    session: Session = Depends(get_session),
):
    name = name.strip()
    user = user_by_name(session, name)
    if user is not None:
        lockout_error = check_lockout(user)
        if lockout_error:
            return templates.TemplateResponse(
                request, "user_login.html", {"error": lockout_error, "name": name}
            )
    if user is None or not verify_secret(pin, user.pin_hash):
        if user is not None:
            record_failed_login(session, user)
        return templates.TemplateResponse(
            request,
            "user_login.html",
            {"error": "Name and PIN didn't match.", "name": name},
        )
    if user.disabled:
        return templates.TemplateResponse(
            request,
            "user_login.html",
            {"error": "This account has been disabled. Contact an admin.", "name": name},
        )

    record_successful_login(session, user)
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.pop("user_id", None)
    return RedirectResponse("/login", status_code=303)


def _user_settings_context(user: User, error: str | None = None, saved: bool = False):
    return {
        "user": user,
        "themes": THEMES,
        "modes": MODES,
        "selected_theme": user.theme or DEFAULT_THEME,
        "selected_mode": user.theme_mode or DEFAULT_MODE,
        "error": error,
        "saved": saved,
    }


@router.get("/settings")
def settings_page(
    request: Request,
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(request, "user_settings.html", _user_settings_context(user))


@router.post("/settings")
def update_settings(
    request: Request,
    theme: str = Form(...),
    mode: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    error = None
    if not is_valid_theme(theme):
        error = "Not a real theme choice."
    elif not is_valid_mode(mode):
        error = "Not a real mode choice."
    else:
        user.theme = theme
        user.theme_mode = mode
        session.add(user)
        session.commit()
    return templates.TemplateResponse(
        request, "user_settings.html", _user_settings_context(user, error, error is None)
    )


def _dashboard_context(session: Session, user: User, flash_error: str | None = None):
    jobs = jobs_for_user(session, user.id)
    rows = []
    for job in jobs:
        estimate_s = corrected_duration_estimate_s(session, job)
        wait_s = queue_wait_seconds(job)
        rows.append(
            {
                "job": job,
                "position": queue_position(session, job),
                "eta": printing_eta(session, job),
                "duration_estimate_s": estimate_s,
                "duration_display": format_duration(estimate_s) if estimate_s else None,
                "queue_wait_display": format_duration(wait_s) if wait_s is not None else None,
            }
        )
    return {
        "user": user,
        "rows": rows,
        "flash_error": flash_error,
        "support_styles": SUPPORT_STYLES,
    }


@router.get("/dashboard")
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    # Flashed via session by upload()/reslice()/submit() below rather than
    # returned directly from that POST, so each can always redirect (a
    # plain <form>, with no JS at all, still gets a normal
    # post-redirect-get instead of a "confirm resubmission" page on
    # refresh) and the client-side upload progress bar (see the script in
    # user_dashboard.html) can always just navigate to /dashboard when the
    # transfer finishes, success or not, without needing to inspect or
    # splice in the response body itself.
    flash_error = request.session.pop("flash_error", None)
    return templates.TemplateResponse(
        request, "user_dashboard.html", _dashboard_context(session, user, flash_error)
    )


@router.get("/dashboard/jobs-table")
def dashboard_jobs_table(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Just the submissions table, for the htmx polling in
    templates/_jobs_table.html to re-fetch while a job is still slicing -
    see that template for why polling stops on its own once none are."""
    context = _dashboard_context(session, user)
    return templates.TemplateResponse(request, "_jobs_table.html", context)


def _stl_bytes_from_upload(filename: str, data: bytes) -> bytes:
    """Validates one uploaded model file and returns real STL bytes ready
    to write to scratch/ - converting from OBJ first if that's what this
    is (see mesh.py's own docstring for why that conversion happens here,
    immediately, rather than teaching anything downstream a second
    format). Raises ValueError with a user-facing message for anything
    that shouldn't become a job at all: empty, oversized, or (for OBJ) not
    actually parseable. Shared by a plain upload and each file pulled out
    of an uploaded zip - both need the exact same validation+conversion,
    just applied once vs. in a loop."""
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)")
    ext = Path(filename).suffix.lower()
    if ext != ".obj":
        return data
    with tempfile.TemporaryDirectory(prefix="queue3d-objconvert-") as tmp:
        obj_path = Path(tmp) / "in.obj"
        stl_path = Path(tmp) / "out.stl"
        obj_path.write_bytes(data)
        try:
            convert_obj_to_stl(obj_path, stl_path)
        except Exception as e:
            raise ValueError(f"couldn't read as an OBJ file ({e})")
        return stl_path.read_bytes()


def _create_job_from_model(
    session: Session,
    background_tasks: BackgroundTasks,
    user: User,
    filename: str,
    stl_bytes: bytes,
    enable_supports: bool,
    support_style: str | None,
) -> Job:
    """The actual job-creation body shared by a plain upload and each file
    extracted from a zip - `filename` is always what's shown as
    Job.original_filename (the *true* original name, e.g. "vase.obj",
    even though `stl_bytes` by this point is always real STL - see
    _stl_bytes_from_upload above)."""
    job = Job(
        user_id=user.id,
        original_filename=filename,
        status=JobStatus.submitted,
        supports_enabled=enable_supports,
        support_style=support_style if enable_supports else None,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    stl_path = scratch_stl_path(job.id)
    stl_path.write_bytes(stl_bytes)
    job.stl_path = str(stl_path)
    session.add(job)
    log_event(session, job.id, f"user:{user.name}", "submitted", detail=filename)
    session.commit()

    # Slicing (OrcaSlicer + mbotmake, both real subprocesses) can take
    # minutes for a large or support-dense model - returning now instead of
    # blocking on it is the whole point of this being a background task.
    # The job sits visibly in 'submitted' (see _jobs_table.html) until
    # slice_and_update finishes it one way or the other.
    background_tasks.add_task(
        slice_and_update,
        job.id,
        stl_path,
        enable_supports,
        support_style if enable_supports else None,
    )
    return job


@router.post("/upload")
def upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    enable_supports: bool = Form(False),
    support_style: str = Form("default"),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    filename = file.filename or "model.stl"
    ext = Path(filename).suffix.lower()
    if support_style not in SUPPORT_STYLES:
        support_style = "default"
    style = support_style if enable_supports else None

    def fail(message: str):
        request.session["flash_error"] = message
        return RedirectResponse("/dashboard", status_code=303)

    data = file.file.read()

    if ext == ".zip":
        # A Thingiverse-style download of several separate STLs - each
        # becomes its own job/draft, not a combined-plate print (see
        # storage.extract_model_files's own docstring for why, and
        # app/README.md's "Uploading zip/OBJ files" section). One bad
        # entry doesn't sink the whole zip - it's just skipped and named
        # in the flash message, same spirit as any partial success.
        try:
            entries = storage.extract_model_files(data)
        except ValueError as e:
            return fail(str(e))
        if not entries:
            return fail("No .stl or .obj files found in that zip.")
        created = 0
        skipped = []
        for entry_name, entry_data in entries:
            try:
                stl_bytes = _stl_bytes_from_upload(entry_name, entry_data)
            except ValueError as e:
                skipped.append(f"{entry_name} ({e})")
                continue
            _create_job_from_model(
                session, background_tasks, user, entry_name, stl_bytes, enable_supports, style
            )
            created += 1
        if created == 0:
            return fail("Couldn't use any files in that zip: " + "; ".join(skipped))
        if skipped:
            request.session["flash_error"] = (
                f"Uploaded {created} model(s) from the zip. Skipped: " + "; ".join(skipped)
            )
        return RedirectResponse("/dashboard", status_code=303)

    if ext not in (".stl", ".obj"):
        return fail("Only .stl, .obj, and .zip files are accepted.")

    try:
        stl_bytes = _stl_bytes_from_upload(filename, data)
    except ValueError as e:
        return fail(f"Couldn't use that file: {e}.")

    _create_job_from_model(session, background_tasks, user, filename, stl_bytes, enable_supports, style)
    return RedirectResponse("/dashboard", status_code=303)


def _owned_job(session: Session, user: User, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="No such job")
    return job


@router.get("/jobs/{job_id}/edit")
def edit_draft(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Pick up working on a draft - the model with its currently selected
    support settings, previewed exactly like the job-preview page (same
    supports overlay), plus the settings themselves as an editable form
    that re-slices in place. Not a thing once a job has actually been
    submitted - there's nothing left to edit at that point, so send
    anyone who lands here anyway (a stale link, or the row that put them
    here has since moved on) back to the dashboard rather than showing an
    edit form for a job it can no longer apply to."""
    job = _owned_job(session, user, job_id)
    if job.status not in DRAFT_STATUSES:
        return RedirectResponse("/dashboard", status_code=303)
    flash_error = request.session.pop("flash_error", None)
    return templates.TemplateResponse(
        request,
        "job_edit.html",
        {"job": job, "support_styles": SUPPORT_STYLES, "flash_error": flash_error},
    )


@router.post("/jobs/{job_id}/reslice")
def reslice(
    job_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    enable_supports: bool = Form(False),
    support_style: str = Form("default"),
    scale_percent: float = Form(100.0),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Re-slices a draft's already-uploaded file with new settings -
    reachable from its edit page (job_edit.html); no new file needed, the
    whole point of splitting slicing from submitting. Redirects back to
    that same edit page (not the dashboard) either way, so re-slicing
    repeatedly to try different settings stays a loop on one page, the
    same as it would with a real slicer's own settings panel.

    scale_percent, not a raw factor, in the form itself - matches what
    the edit page actually shows/lets someone type (see job_edit.html)."""
    job = _owned_job(session, user, job_id)
    if support_style not in SUPPORT_STYLES:
        support_style = "default"
    scale_factor = scale_percent / 100
    try:
        stl_path = start_reslice(
            session, job, enable_supports, support_style if enable_supports else None, scale_factor
        )
    except JobActionError as e:
        request.session["flash_error"] = str(e)
        return RedirectResponse(f"/jobs/{job_id}/edit", status_code=303)

    background_tasks.add_task(
        slice_and_update,
        job.id,
        stl_path,
        enable_supports,
        support_style if enable_supports else None,
        scale_factor,
    )
    return RedirectResponse(f"/jobs/{job_id}/edit", status_code=303)


@router.post("/jobs/{job_id}/submit")
def submit(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """The explicit "submit to queue" action - see jobs.submit_draft.
    Back to the dashboard either way: once submitted there's nothing left
    to edit, and a failure here means the job wasn't in a submittable
    state any more (e.g. a duplicate click), which the dashboard's own
    status column already explains."""
    job = _owned_job(session, user, job_id)
    try:
        submit_draft(session, job)
    except JobActionError as e:
        request.session["flash_error"] = str(e)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/jobs/{job_id}/delete")
def delete_job(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """A user removing their own still-undecided model - see
    jobs.delete_own_job. Genuinely deletes the job and its files, no
    undo - only reachable while still queued/approved; once released
    and printing, an admin is already acting on it, so this button
    doesn't show any more (see _jobs_table.html) and a request that
    somehow arrives anyway is rejected the same way any other
    already-moved-on action is."""
    job = _owned_job(session, user, job_id)
    try:
        delete_own_job(session, job, user)
    except JobActionError as e:
        request.session["flash_error"] = str(e)
    return RedirectResponse("/dashboard", status_code=303)
