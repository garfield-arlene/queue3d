from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from auth import (
    hash_secret,
    require_user,
    user_by_name,
    verify_secret,
)
from db import get_session
from jobs import jobs_for_user, queue_position, slice_and_update
from models import Job, JobStatus, User
from storage import MAX_UPLOAD_BYTES, scratch_stl_path
from templates_env import templates

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
    user = user_by_name(session, name.strip())
    if user is None or not verify_secret(pin, user.pin_hash):
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

    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.pop("user_id", None)
    return RedirectResponse("/login", status_code=303)


def _dashboard_context(session: Session, user: User, upload_error: str | None = None):
    jobs = jobs_for_user(session, user.id)
    rows = [{"job": job, "position": queue_position(session, job)} for job in jobs]
    return {
        "user": user,
        "rows": rows,
        "upload_error": upload_error,
        "support_styles": SUPPORT_STYLES,
    }


@router.get("/dashboard")
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    # Flashed via session by upload() below rather than returned directly
    # from that POST, so /upload can always redirect (a plain <form>, with
    # no JS at all, still gets a normal post-redirect-get instead of a
    # "confirm resubmission" page on refresh) and the client-side upload
    # progress bar (see the script in user_dashboard.html) can always just
    # navigate to /dashboard when the transfer finishes, success or not,
    # without needing to inspect or splice in the response body itself.
    upload_error = request.session.pop("upload_error", None)
    return templates.TemplateResponse(
        request, "user_dashboard.html", _dashboard_context(session, user, upload_error)
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
    if support_style not in SUPPORT_STYLES:
        support_style = "default"

    def fail(message: str):
        request.session["upload_error"] = message
        return RedirectResponse("/dashboard", status_code=303)

    if not filename.lower().endswith(".stl"):
        return fail("Only .stl files are accepted.")

    data = file.file.read()
    if not data:
        return fail("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        return fail(f"File is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB).")

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
    stl_path.write_bytes(data)
    job.stl_path = str(stl_path)
    session.add(job)
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

    return RedirectResponse("/dashboard", status_code=303)
