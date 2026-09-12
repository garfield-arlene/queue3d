"""Admin login/dashboard/queue actions. Deliberately no self-service signup
here - admin accounts are provisioned out-of-band via create_admin.py, run
by whoever controls the server. Approving/releasing print jobs is a
position of trust over many users' shared printer time; open
admin signup would defeat the whole point of the review gate."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from auth import admin_by_username, require_admin, verify_secret
from backup import get_last_successful_backup, is_stale
from db import get_session
from jobs import JobActionError, active_jobs, approve, mark_finished, reject, release
from models import Admin, Job, User

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="templates")


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", {})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    admin = admin_by_username(session, username.strip())
    if admin is None or not verify_secret(password, admin.password_hash):
        return templates.TemplateResponse(
            request,
            "admin_login.html",
            {"error": "Username and password didn't match.", "username": username},
        )

    request.session["admin_id"] = admin.id
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.pop("admin_id", None)
    return RedirectResponse("/admin/login", status_code=303)


def _dashboard_context(session: Session, admin: Admin, action_error: str | None = None):
    last_backup = get_last_successful_backup(session)
    rows = []
    for job in active_jobs(session):
        user = session.get(User, job.user_id)
        rows.append({"job": job, "user_name": user.name if user else "?"})
    return {
        "admin": admin,
        "last_backup": last_backup,
        "backup_stale": is_stale(last_backup),
        "rows": rows,
        "action_error": action_error,
    }


@router.get("/dashboard")
def dashboard(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "admin_dashboard.html", _dashboard_context(session, admin)
    )


def _get_job_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    return job


def _perform_action(request: Request, session: Session, admin: Admin, job_id: int, action_fn, *args):
    """Shared body for every queue action below: look up the job, run the
    requested transition, and either redirect (success) or re-render the
    dashboard with the error inline (failure) - e.g. releasing a job that
    isn't approved, or rejecting without a note."""
    job = _get_job_or_404(session, job_id)
    try:
        action_fn(session, job, *args)
    except JobActionError as e:
        return templates.TemplateResponse(
            request, "admin_dashboard.html", _dashboard_context(session, admin, str(e))
        )
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.post("/jobs/{job_id}/approve")
def approve_job(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, approve, admin)


@router.post("/jobs/{job_id}/reject")
def reject_job(
    request: Request,
    job_id: int,
    note: str = Form(...),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, reject, admin, note)


@router.post("/jobs/{job_id}/release")
def release_job(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, release)


@router.post("/jobs/{job_id}/mark_done")
def mark_done_job(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, mark_finished, True)


@router.post("/jobs/{job_id}/mark_failed")
def mark_failed_job(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, mark_finished, False)
