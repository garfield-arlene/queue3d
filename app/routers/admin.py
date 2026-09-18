"""Admin login/dashboard/queue actions. Deliberately no self-service signup
here - admin accounts are provisioned out-of-band via create_admin.py, run
by whoever controls the server. Approving/releasing print jobs is a
position of trust over many users' shared printer time; open
admin signup would defeat the whole point of the review gate."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from auth import admin_by_username, require_admin, verify_secret
from backup import get_last_successful_backup, is_stale
from db import get_session
from jobs import (
    JobActionError,
    _admin_actor,
    active_jobs,
    all_events,
    approve,
    corrected_duration_estimate_s,
    finished_jobs,
    job_events,
    log_event,
    mark_finished,
    printing_eta,
    reject,
    release,
    user_has_active_jobs,
)
from models import Admin, Job, Settings, User
from printer import PrinterError, connection_status, pairing_status, start_pairing, system_information
from templates_env import templates

router = APIRouter(prefix="/admin")


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
        rows.append(
            {
                "job": job,
                "user_name": user.name if user else "?",
                "eta": printing_eta(session, job),
                "duration_estimate_s": corrected_duration_estimate_s(session, job),
            }
        )
    return {
        "admin": admin,
        "last_backup": last_backup,
        "backup_stale": is_stale(last_backup),
        "rows": rows,
        "action_error": action_error,
        "printer_status": connection_status(),
        "pairing": pairing_status(),
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


@router.get("/printer-status")
def printer_status_fragment(
    request: Request,
    admin: Admin = Depends(require_admin),
):
    """Just the printer-status banner, for the htmx polling in
    templates/_printer_status.html to re-fetch while pairing is in
    progress - see that template for why polling stops on its own once
    it's done."""
    return templates.TemplateResponse(
        request,
        "_printer_status.html",
        {"printer_status": connection_status(), "pairing": pairing_status()},
    )


@router.post("/printer/pair")
def printer_pair(
    request: Request,
    admin: Admin = Depends(require_admin),
):
    """Kicks off pairing in the background (see printer.start_pairing) -
    a no-op if one's already running - then sends the admin straight back
    to the dashboard, which shows the "waiting for the dial press" state
    (and starts polling for it) from _dashboard_context above."""
    start_pairing()
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.get("/printer/info")
def printer_info(
    request: Request,
    admin: Admin = Depends(require_admin),
):
    """Raw `get_system_information` reply from the printer - see
    printer.system_information(). Exists to find out what this actually
    contains (particularly `current_process` while a job is printing,
    towards a real progress indicator - see README.md's Printer to-do
    list) since MakerBot never documented this JSON-RPC method anywhere;
    not wired into anything else yet."""
    error = None
    info = None
    try:
        info = system_information()
    except PrinterError as e:
        error = str(e)
    return templates.TemplateResponse(
        request, "admin_printer_info.html", {"admin": admin, "info": info, "error": error}
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
    return _perform_action(request, session, admin, job_id, release, admin)


@router.post("/jobs/{job_id}/mark_done")
def mark_done_job(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, mark_finished, admin, True)


@router.post("/jobs/{job_id}/mark_failed")
def mark_failed_job(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, mark_finished, admin, False)


# ---- user account management ----
# Scoped to users only for now, not other admins - see README.md's To do
# list ("Accounts") for why admin-managing-admins is a separate item: it
# raises its own safety question (what stops the last admin account from
# being disabled/deleted, including by itself) that deserves its own
# design pass rather than reusing this code as-is.


def _users_context(session: Session, admin: Admin, action_error: str | None = None):
    users = session.exec(select(User).order_by(User.name)).all()
    return {"admin": admin, "users": users, "action_error": action_error}


@router.get("/users")
def users_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(request, "admin_users.html", _users_context(session, admin))


def _get_user_or_404(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    return user


@router.post("/users/{user_id}/disable")
def disable_user(
    request: Request,
    user_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    user = _get_user_or_404(session, user_id)
    user.disabled = True
    session.add(user)
    log_event(session, None, _admin_actor(admin), "user_disabled", detail=user.name)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/enable")
def enable_user(
    request: Request,
    user_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    user = _get_user_or_404(session, user_id)
    user.disabled = False
    session.add(user)
    log_event(session, None, _admin_actor(admin), "user_enabled", detail=user.name)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user(
    request: Request,
    user_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    user = _get_user_or_404(session, user_id)
    if user_has_active_jobs(session, user.id):
        error = f"Can't delete {user.name} - they still have a job in the queue or printing. Resolve it first."
        return templates.TemplateResponse(request, "admin_users.html", _users_context(session, admin, error))
    log_event(session, None, _admin_actor(admin), "user_deleted", detail=user.name)
    session.delete(user)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/delete_all")
def delete_all_users(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    users = session.exec(select(User)).all()
    blocked = [u.name for u in users if user_has_active_jobs(session, u.id)]
    if blocked:
        error = (
            "Didn't delete anyone - these users still have a job in the queue or "
            f"printing: {', '.join(blocked)}. Resolve those first."
        )
        return templates.TemplateResponse(request, "admin_users.html", _users_context(session, admin, error))
    for user in users:
        log_event(session, None, _admin_actor(admin), "user_deleted", detail=user.name)
        session.delete(user)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


# ---- settings ----
# A single-row table (models.Settings) rather than a generic key/value
# store - see that model's docstring for why. Currently just the one
# knob: how long a sliced-but-never-submitted draft sits before
# cleanup_drafts.py expires it (run from cron, same pattern as backup.py -
# see app/README.md).


def get_settings(session: Session) -> Settings:
    settings = session.get(Settings, 1)
    if settings is None:
        # First run: no row yet - create the default rather than making
        # every caller (this page, cleanup_drafts.py) handle a None case.
        settings = Settings(id=1)
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings


@router.get("/settings")
def settings_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "admin_settings.html", {"admin": admin, "settings": get_settings(session)}
    )


@router.post("/settings")
def update_settings(
    request: Request,
    draft_expiry_days: int = Form(...),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    error = None
    if draft_expiry_days < 1:
        error = "Draft expiry must be at least 1 day."
    else:
        settings = get_settings(session)
        settings.draft_expiry_days = draft_expiry_days
        session.add(settings)
        session.commit()
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {"admin": admin, "settings": get_settings(session), "error": error, "saved": error is None},
    )


# ---- finished jobs + audit log ----
# Two distinct views (see README.md's To do list, "Audit log" and
# "Job review & feedback" sections, for why they're kept separate rather
# than combined into one): browsing past jobs (this app's data/archive/
# contents, in effect) vs. one job's own full history of what happened to
# it and when. Built together after a real report - a rejection had
# actually recorded correctly (confirmed directly in the database) but an
# admin had no page anywhere to go see that, so it read as if nothing had
# happened at all.


@router.get("/jobs/finished")
def finished_jobs_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    rows = []
    for job in finished_jobs(session):
        user = session.get(User, job.user_id)
        rows.append({"job": job, "user_name": user.name if user else "?"})
    return templates.TemplateResponse(
        request, "admin_finished_jobs.html", {"admin": admin, "rows": rows}
    )


@router.get("/jobs/{job_id}/log")
def job_log_page(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    job = _get_job_or_404(session, job_id)
    user = session.get(User, job.user_id)
    return templates.TemplateResponse(
        request,
        "admin_job_log.html",
        {
            "admin": admin,
            "job": job,
            "user_name": user.name if user else "?",
            "events": job_events(session, job_id),
        },
    )


# Global activity log - every event across every job, one table, most
# recent first, so an admin can see what's been happening at a glance
# without opening one job at a time. Per the user: this is the actual
# "admin log view," distinct from (and more central than) the per-job
# log above, which stays as a way to focus on one job's own history.
ACTIVITY_LOG_LIMIT = 500


@router.get("/log")
def activity_log_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    rows = [
        {"event": event, "filename": filename, "photo_path": photo_path}
        for event, filename, photo_path in all_events(session, limit=ACTIVITY_LOG_LIMIT)
    ]
    return templates.TemplateResponse(
        request,
        "admin_log.html",
        {"admin": admin, "rows": rows, "limit": ACTIVITY_LOG_LIMIT},
    )
