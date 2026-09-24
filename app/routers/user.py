import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

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
from filters import filtered_redirect, job_filter_params, job_filters_from_query_params
from jobs import (
    JobActionError,
    corrected_duration_estimate_s,
    delete_own_job,
    distinct_job_colors,
    filament_status,
    format_duration,
    jobs_for_user,
    log_event,
    printing_eta,
    queue_position,
    queue_wait_seconds,
    reprint_job,
    restore_job,
    slice_and_update,
    start_reslice,
    submit_draft,
)
from mesh import convert_obj_to_stl
from models import DRAFT_STATUSES, Color, Job, JobStatus, User
from storage import MAX_UPLOAD_BYTES, MAX_ZIP_MODEL_FILES, scratch_stl_path
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


@router.post("/settings/change_pin")
def update_pin(
    request: Request,
    current_pin: str = Form(...),
    new_pin: str = Form(...),
    confirm_pin: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Self-service - per the user: "all users (admins included) should
    be able to reset their own password [PIN, for a User account]."
    Before this, the only way a user's PIN ever changed was an admin
    resetting it for them (routers/admin.py's reset_user_pin, which
    generates a random replacement and needs no current PIN at all,
    since a different, already-authenticated admin's own session is the
    trust boundary there). This route is reachable by anyone with an
    open, unattended session on this account, not just its real owner in
    person, so requiring the current PIN first is the actual thing
    standing between that and a silent takeover - same reasoning
    routers/admin.py's update_admin_password applies for an admin
    account."""
    error = None
    if not verify_secret(current_pin, user.pin_hash):
        error = "Current PIN didn't match."
    elif new_pin != confirm_pin:
        error = "New PINs didn't match."
    elif len(new_pin) < 4:
        error = "PIN must be at least 4 digits."
    else:
        user.pin_hash = hash_secret(new_pin)
        session.add(user)
        # No detail beyond who did it - same reasoning as every other
        # credential-change event in this app: the log records that it
        # happened, never the PIN itself, old or new.
        log_event(session, None, f"user:{user.name}", "pin_changed")
        session.commit()
    return templates.TemplateResponse(
        request, "user_settings.html", _user_settings_context(user, error, error is None)
    )


def _enabled_colors(session: Session) -> list[Color]:
    """What the color dropdown offers, both at upload and wherever a job's
    color can be changed later - see models.Color's own docstring for the
    inventory this draws from. An admin disabling a color only affects
    what's offered going forward; it never touches a job that already
    selected it (see _colors_for_job below for how that job's own current
    color still shows up wherever it's editable, even once it's no
    longer in this list)."""
    return session.exec(select(Color).where(Color.enabled == True).order_by(Color.name)).all()  # noqa: E712


def _colors_for_job(session: Session, job: Job) -> list[Color]:
    """Enabled colors, plus this one job's own currently-selected color
    even if it's since been disabled or removed entirely - dropping it
    from the list the instant an admin changes something elsewhere would
    silently change what's selected before the user themselves ever
    touched anything. Used by the shared /jobs/{id}/edit page - the one
    place a job's color is ever changed from, whether it's still a draft
    or already queued/approved (see COLOR_EDITABLE_STATUSES below)."""
    colors = _enabled_colors(session)
    if job.color_name and job.color_name not in {c.name for c in colors}:
        colors = colors + [Color(name=job.color_name, enabled=False)]
    return colors


# Color has zero effect on the actual sliced file (see models.Job.color_name),
# so - unlike every other job setting - there's no reason changing it should
# stop being possible just because a job has already been queued/approved,
# the way re-slicing genuinely would need to. Stops at 'printing': the
# physical filament actually loaded is fixed by then, and changing the
# recorded color at that point would misrepresent what actually happened,
# not just update a preference.
COLOR_EDITABLE_STATUSES = DRAFT_STATUSES | {JobStatus.queued, JobStatus.approved}


def _dashboard_context(session: Session, user: User, flash_error: str | None = None, filters: dict | None = None):
    filters = filters or {}
    # "user" (a submitter-name filter) is part of the shared
    # filters.job_filter_params dependency for every admin job-listing
    # route, but meaningless here - this view is already scoped to one
    # user, with no submitter column to filter on at all (see
    # filter_show_user below) - jobs_for_user itself has no such
    # parameter, so it's dropped before the call rather than passed
    # through unused.
    job_filters = {k: v for k, v in filters.items() if k != "user"}
    jobs = jobs_for_user(session, user.id, **job_filters)
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
                "filament": filament_status(session, job),
            }
        )
    return {
        "user": user,
        "rows": rows,
        "flash_error": flash_error,
        "support_styles": SUPPORT_STYLES,
        "max_zip_models": MAX_ZIP_MODEL_FILES,
        "colors": _enabled_colors(session),
        # Filter form state - see templates/_job_filters.html. Every job
        # this user has ever had can be in any status at all (unlike the
        # admin queue/finished views, each scoped to one status subset),
        # so the status dropdown offers every JobStatus value with no
        # narrowing; "Uploaded" (created_at) is the closest thing this
        # view has to one single "date" column, since a draft that's
        # never been queued has no queued_at/finished_at yet at all.
        "filter_action": "/dashboard",
        "filter_colors": distinct_job_colors(session),
        "filter_statuses": [s.value for s in JobStatus],
        "filter_date_label": "Uploaded",
        "filter_show_user": False,
        **{f"filter_{k}": v for k, v in filters.items()},
    }


@router.get("/dashboard")
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    filters: dict = Depends(job_filter_params),
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
        request, "user_dashboard.html", _dashboard_context(session, user, flash_error, filters)
    )


@router.get("/dashboard/jobs-table")
def dashboard_jobs_table(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    filters: dict = Depends(job_filter_params),
):
    """Just the submissions table, for the htmx polling in
    templates/_jobs_table.html to re-fetch while a job is still slicing -
    see that template for why polling stops on its own once none are.
    Takes the same filter query params as /dashboard (see
    _job_filters.html's hx-get, which forwards the page's own current
    query string) so a filtered view doesn't silently revert to
    unfiltered every 2 seconds while something is still slicing."""
    context = _dashboard_context(session, user, filters=filters)
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
    color_name: str | None,
) -> Job:
    """The actual job-creation body shared by a plain upload and each file
    extracted from a zip - `filename` is always what's shown as
    Job.original_filename (the *true* original name, e.g. "vase.obj",
    even though `stl_bytes` by this point is always real STL - see
    _stl_bytes_from_upload above).

    color_name doesn't flow into slice_and_update below at all, unlike
    enable_supports/support_style - it has zero effect on the actual
    slice (see models.Job.color_name), so it's just set directly here,
    not threaded through the background slicing task."""
    job = Job(
        user_id=user.id,
        original_filename=filename,
        status=JobStatus.submitted,
        supports_enabled=enable_supports,
        support_style=support_style if enable_supports else None,
        color_name=color_name,
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
    color: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    filename = file.filename or "model.stl"
    ext = Path(filename).suffix.lower()
    if support_style not in SUPPORT_STYLES:
        support_style = "default"
    style = support_style if enable_supports else None
    # "" (the dropdown's own "Any available" option, per the user) means
    # None - no specific color recorded at all, not a color literally
    # named "Any available". Falls back to None rather than erroring out
    # if this doesn't match a currently-enabled color at all (e.g. it was
    # disabled/removed in the moment between loading this form and
    # submitting it) - color is a best-effort convenience, not something
    # worth blocking a real upload over.
    color_name = color.strip() or None
    if color_name is not None and color_name not in {c.name for c in _enabled_colors(session)}:
        color_name = None

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
                session, background_tasks, user, entry_name, stl_bytes, enable_supports, style, color_name
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

    _create_job_from_model(session, background_tasks, user, filename, stl_bytes, enable_supports, style, color_name)
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
    that re-slices in place. Also reachable for a queued/approved job
    (see COLOR_EDITABLE_STATUSES) - per the user, after landing here
    once already for a color change and expecting the same "Edit" link
    slice_failed jobs already have, rather than a different, inline
    control elsewhere - job_edit.html itself only shows the color form
    for one of those (no re-slice settings, no 3D preview/gizmo editor;
    see that template's own `is_draft` branching), since nothing else on
    this page is safe or meaningful to change once a job's already
    queued. Anything past COLOR_EDITABLE_STATUSES entirely (printing,
    done, failed, ...) has nothing left to edit at all, so send anyone
    who lands here anyway (a stale link, or the row that put them here
    has since moved on) back to the dashboard."""
    job = _owned_job(session, user, job_id)
    if job.status not in COLOR_EDITABLE_STATUSES:
        return RedirectResponse("/dashboard", status_code=303)
    flash_error = request.session.pop("flash_error", None)
    return templates.TemplateResponse(
        request,
        "job_edit.html",
        {
            "job": job,
            "support_styles": SUPPORT_STYLES,
            "flash_error": flash_error,
            "colors": _colors_for_job(session, job),
            "filament": filament_status(session, job),
        },
    )


@router.post("/jobs/{job_id}/color")
def update_job_color(
    job_id: int,
    request: Request,
    color: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Deliberately separate from /reslice below, not one more field on
    that same form - color has zero effect on the actual sliced file (see
    models.Job.color_name), so changing it has no business paying for a
    full re-slice (real CPU/memory cost on a Pi - see README.md's to-do
    list) the way a genuine settings change does. Editable through
    COLOR_EDITABLE_STATUSES - a draft or a queued/approved job, both from
    the same /jobs/{id}/edit page (see edit_draft above) - not just while
    still a draft, per the user, after noticing a queued job's color
    couldn't be changed at all despite there being no real reason it
    shouldn't be. Redirects back to that same edit page either way, same
    as /reslice does - there's now one single place a job's color is
    ever changed from, not a second, different control living somewhere
    else for a queued job specifically."""
    job = _owned_job(session, user, job_id)
    if job.status not in COLOR_EDITABLE_STATUSES:
        return RedirectResponse("/dashboard", status_code=303)
    color_name = color.strip() or None
    valid_names = {c.name for c in _enabled_colors(session)}
    if color_name is not None and color_name != job.color_name and color_name not in valid_names:
        color_name = None
    job.color_name = color_name
    session.add(job)
    session.commit()
    return RedirectResponse(f"/jobs/{job_id}/edit", status_code=303)


@router.post("/jobs/{job_id}/reslice")
def reslice(
    job_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    enable_supports: bool = Form(False),
    support_style: str = Form("default"),
    scale_percent: float = Form(100.0),
    rotate_x: float = Form(0.0),
    rotate_y: float = Form(0.0),
    rotate_z: float = Form(0.0),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Re-slices a draft's already-uploaded file with new settings -
    reachable from its edit page (job_edit.html); no new file needed, the
    whole point of splitting slicing from submitting. Also reachable for
    an already-queued/approved job now (see jobs.start_reslice/
    COLOR_EDITABLE_STATUSES), per the user - not just color, which is
    all this page was scoped to right after it first got reused for
    queued jobs. Redirects back to that same edit page (not the
    dashboard) either way, so re-slicing repeatedly to try different
    settings stays a loop on one page, the same as it would with a real
    slicer's own settings panel - including for the queued case, which
    naturally shows the same "slicing..." auto-reloading view a brand
    new upload does while this runs (see job_edit.html), since
    start_reslice puts it in the identical 'submitted' status either way.

    scale_percent, not a raw factor, in the form itself - matches what
    the edit page actually shows/lets someone type (see job_edit.html).
    rotate_x/y/z are already in degrees, applied in that order - see
    models.Job.rotate_x's own docstring for why the order matters."""
    job = _owned_job(session, user, job_id)
    if support_style not in SUPPORT_STYLES:
        support_style = "default"
    scale_factor = scale_percent / 100
    try:
        stl_path, resubmit_to_queue = start_reslice(
            session,
            job,
            enable_supports,
            support_style if enable_supports else None,
            scale_factor,
            rotate_x,
            rotate_y,
            rotate_z,
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
        rotate_x,
        rotate_y,
        rotate_z,
        resubmit_to_queue,
    )
    return RedirectResponse(f"/jobs/{job_id}/edit", status_code=303)


def _still_has_rows(session: Session, user: User, filters: dict) -> bool:
    """Whether the given filter would still show at least one of this
    user's own jobs right now - see filters.filtered_redirect. Reuses
    _dashboard_context wholesale (including its own "user"-key stripping
    for jobs_for_user) rather than re-implementing the exact same filter
    application a second time here."""
    return bool(_dashboard_context(session, user, filters=filters)["rows"])


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
    status column already explains. Carries the current filter query
    string back (see filters.filtered_redirect) - dropped only if it
    would now show nothing (e.g. this was the last job matching
    status=sliced, and submitting just moved it to queued)."""
    job = _owned_job(session, user, job_id)
    filters = job_filters_from_query_params(request.query_params)
    try:
        submit_draft(session, job)
    except JobActionError as e:
        request.session["flash_error"] = str(e)
    return filtered_redirect("/dashboard", request, _still_has_rows(session, user, filters))


@router.post("/jobs/{job_id}/delete")
def delete_job(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """A user removing their own model - see jobs.delete_own_job for the
    exact allowed statuses (queued/approved, a slice_failed draft with
    nowhere else to go, or a rejected one the user doesn't want to keep
    around) and why. Genuinely deletes the job and its files, no undo;
    once released and printing (or any other status past what
    delete_own_job allows), an admin is already acting on it or it's
    settled history, so this button doesn't show any more (see
    _jobs_table.html) and a request that somehow arrives anyway is
    rejected the same way any other already-moved-on action is. Carries
    the current filter query string back (see filters.filtered_redirect) -
    dropped only if it would now show nothing (e.g. this was the last
    job matching the current filter)."""
    job = _owned_job(session, user, job_id)
    filters = job_filters_from_query_params(request.query_params)
    try:
        delete_own_job(session, job, user)
    except JobActionError as e:
        request.session["flash_error"] = str(e)
    return filtered_redirect("/dashboard", request, _still_has_rows(session, user, filters))


@router.post("/jobs/{job_id}/restore")
def restore(
    job_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Copies an archived (rejected/done/failed/expired) job's model into
    a brand-new draft to modify and resubmit - see jobs.restore_job for
    why a slice_failed draft is deliberately not included (it already has
    a full edit path of its own). Schedules the same background
    slice_and_update() any fresh upload triggers, seeded with the
    archived job's own settings rather than plain defaults - lands
    straight on the new draft's own edit page (not the dashboard, unlike
    upload()) since there's always exactly one resulting job, never a
    zip's worth of several. On failure only, back to the dashboard - see
    filters.filtered_redirect for why the current filter query string
    carries through there too."""
    job = _owned_job(session, user, job_id)
    try:
        new_job = restore_job(session, job, user)
    except JobActionError as e:
        request.session["flash_error"] = str(e)
        filters = job_filters_from_query_params(request.query_params)
        return filtered_redirect("/dashboard", request, _still_has_rows(session, user, filters))

    background_tasks.add_task(
        slice_and_update,
        new_job.id,
        Path(new_job.stl_path),
        new_job.supports_enabled,
        new_job.support_style,
        scale_factor=new_job.scale_factor,
        rotate_x=new_job.rotate_x,
        rotate_y=new_job.rotate_y,
        rotate_z=new_job.rotate_z,
    )
    return RedirectResponse(f"/jobs/{new_job.id}/edit", status_code=303)


@router.post("/jobs/{job_id}/reprint")
def reprint(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """One-click "print another exactly as it was queued" for a job that
    already finished successfully - see jobs.reprint_job for why this is
    scoped to `done` only and skips slicing entirely (reusing the exact
    archived .makerbot). Lands back on the dashboard, same as a normal
    upload/submit - no background task to schedule here, unlike restore
    above, since nothing needs slicing. Carries the current filter query
    string back (see filters.filtered_redirect)."""
    job = _owned_job(session, user, job_id)
    filters = job_filters_from_query_params(request.query_params)
    try:
        reprint_job(session, job, user)
    except JobActionError as e:
        request.session["flash_error"] = str(e)
    return filtered_redirect("/dashboard", request, _still_has_rows(session, user, filters))
