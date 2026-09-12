import shutil

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from auth import (
    hash_secret,
    require_user,
    user_by_name,
    verify_secret,
)
from db import get_session
from jobs import jobs_for_user, queue_position
from models import Job, JobStatus, User
from pipeline import run_slice
from storage import MAX_UPLOAD_BYTES, queue_paths, read_makerbot_duration_s, scratch_stl_path

router = APIRouter()
templates = Jinja2Templates(directory="templates")


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

    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.pop("user_id", None)
    return RedirectResponse("/login", status_code=303)


def _dashboard_context(session: Session, user: User, upload_error: str | None = None):
    jobs = jobs_for_user(session, user.id)
    rows = [{"job": job, "position": queue_position(session, job)} for job in jobs]
    return {"user": user, "rows": rows, "upload_error": upload_error}


@router.get("/dashboard")
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "user_dashboard.html", _dashboard_context(session, user)
    )


@router.post("/upload")
def upload(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    filename = file.filename or "model.stl"

    def error_response(message: str):
        return templates.TemplateResponse(
            request, "user_dashboard.html", _dashboard_context(session, user, message)
        )

    if not filename.lower().endswith(".stl"):
        return error_response("Only .stl files are accepted.")

    data = file.file.read()
    if not data:
        return error_response("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        return error_response(f"File is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB).")

    job = Job(user_id=user.id, original_filename=filename, status=JobStatus.submitted)
    session.add(job)
    session.commit()
    session.refresh(job)

    stl_path = scratch_stl_path(job.id)
    stl_path.write_bytes(data)
    job.stl_path = str(stl_path)
    session.add(job)
    session.commit()

    queue_stl, queue_makerbot = queue_paths(job.id)
    success, detail = run_slice(stl_path, queue_makerbot)
    if success:
        shutil.move(str(stl_path), str(queue_stl))
        job.stl_path = str(queue_stl)
        job.makerbot_path = str(queue_makerbot)
        job.duration_estimate_s = read_makerbot_duration_s(queue_makerbot)
        job.status = JobStatus.queued
    else:
        job.status = JobStatus.slice_failed
        job.slice_error = detail[-4000:]  # cap - slicer output can be long

    session.add(job)
    session.commit()

    return RedirectResponse("/dashboard", status_code=303)
