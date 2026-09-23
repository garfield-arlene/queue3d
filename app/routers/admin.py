"""Admin login/dashboard/queue actions. Deliberately no self-service signup
here - admin accounts are provisioned out-of-band via create_admin.py, run
by whoever controls the server. Approving/releasing print jobs is a
position of trust over many users' shared printer time; open
admin signup would defeat the whole point of the review gate."""

import zoneinfo
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import Session, select

from auth import (
    admin_by_username,
    check_lockout,
    generate_pin,
    hash_secret,
    record_failed_login,
    record_successful_login,
    require_admin,
    verify_secret,
)
from backup import get_last_successful_backup, is_stale
from db import get_session
from jobs import (
    JobActionError,
    _admin_actor,
    active_jobs,
    all_events,
    approve,
    corrected_duration_estimate_s,
    delete_all_old_jobs,
    delete_old_job,
    filament_status,
    finished_jobs,
    format_duration,
    is_old_job,
    job_events,
    log_event,
    mark_finished,
    printing_eta,
    queue_wait_seconds,
    reject,
    release,
    requeue_job,
    user_has_active_jobs,
)
from models import Admin, Color, Job, Settings, User
from printer import PrinterError, connection_status, pairing_status, start_pairing, system_information
from support_bundle import build_support_bundle
from templates_env import is_valid_timezone, set_display_timezone, templates
from themes import DEFAULT_MODE, DEFAULT_THEME, MODES, THEMES, is_valid_mode, is_valid_theme

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
    username = username.strip()
    admin = admin_by_username(session, username)
    if admin is not None:
        lockout_error = check_lockout(admin)
        if lockout_error:
            return templates.TemplateResponse(
                request, "admin_login.html", {"error": lockout_error, "username": username}
            )
    if admin is None or not verify_secret(password, admin.password_hash):
        if admin is not None:
            record_failed_login(session, admin)
        return templates.TemplateResponse(
            request,
            "admin_login.html",
            {"error": "Username and password didn't match.", "username": username},
        )

    record_successful_login(session, admin)
    request.session["admin_id"] = admin.id
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.pop("admin_id", None)
    return RedirectResponse("/admin/login", status_code=303)


def _dashboard_context(session: Session, admin: Admin, action_error: str | None = None):
    last_backup = get_last_successful_backup(session)
    threshold_days = get_settings(session).old_job_threshold_days
    rows = []
    old_job_count = 0
    for job in active_jobs(session):
        # Split out, not just shown-alongside - an old, still-undecided
        # job moves to /admin/jobs/old entirely (see that route below),
        # per the user: "mutually exclusive, not shown in both."
        if is_old_job(job, threshold_days):
            old_job_count += 1
            continue
        user = session.get(User, job.user_id)
        estimate_s = corrected_duration_estimate_s(session, job)
        wait_s = queue_wait_seconds(job)
        rows.append(
            {
                "job": job,
                "user_name": user.name if user else "?",
                "eta": printing_eta(session, job),
                "duration_estimate_s": estimate_s,
                "duration_display": format_duration(estimate_s) if estimate_s else None,
                "queue_wait_display": format_duration(wait_s) if wait_s is not None else None,
                "filament": filament_status(session, job),
            }
        )
    return {
        "admin": admin,
        "last_backup": last_backup,
        "backup_stale": is_stale(last_backup),
        "rows": rows,
        "old_job_count": old_job_count,
        "old_job_threshold_days": threshold_days,
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


# Where a queue action (approve/reject/release/mark_done/mark_failed)
# redirects on success, and which template+context re-renders it inline
# on failure - keyed by an optional return_to form field each action
# route below now accepts. Exists because an "old" job (see
# jobs.is_old_job) is excluded from the normal /admin/dashboard queue
# entirely, not just flagged there too (per the user: "mutually
# exclusive, not shown in both") - so approve/reject/release still have
# to work *from* /admin/jobs/old for a job that literally cannot appear
# on the main dashboard any more. Defaults to the main dashboard so
# every pre-existing form (which never sends this field) keeps working
# unchanged.
_RETURN_TARGETS = {
    "/admin/dashboard": ("admin_dashboard.html", lambda session, admin, error: _dashboard_context(session, admin, error)),
    "/admin/jobs/old": ("admin_old_jobs.html", lambda session, admin, error: _old_jobs_context(session, admin, error)),
}


def _perform_action(
    request: Request,
    session: Session,
    admin: Admin,
    job_id: int,
    action_fn,
    *args,
    return_to: str = "/admin/dashboard",
):
    """Shared body for every queue action below: look up the job, run the
    requested transition, and either redirect (success) or re-render
    wherever the action was actually submitted from with the error
    inline (failure) - e.g. releasing a job that isn't approved, or
    rejecting without a note."""
    job = _get_job_or_404(session, job_id)
    template_name, context_fn = _RETURN_TARGETS.get(return_to, _RETURN_TARGETS["/admin/dashboard"])
    try:
        action_fn(session, job, *args)
    except JobActionError as e:
        return templates.TemplateResponse(request, template_name, context_fn(session, admin, str(e)))
    return RedirectResponse(return_to, status_code=303)


@router.post("/jobs/{job_id}/approve")
def approve_job(
    request: Request,
    job_id: int,
    return_to: str = Form("/admin/dashboard"),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, approve, admin, return_to=return_to)


@router.post("/jobs/{job_id}/reject")
def reject_job(
    request: Request,
    job_id: int,
    note: str = Form(...),
    return_to: str = Form("/admin/dashboard"),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, reject, admin, note, return_to=return_to)


@router.post("/jobs/{job_id}/release")
def release_job(
    request: Request,
    job_id: int,
    return_to: str = Form("/admin/dashboard"),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, release, admin, return_to=return_to)


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
    reason: str = Form(...),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _perform_action(request, session, admin, job_id, mark_finished, admin, False, reason)


def _old_jobs_context(session: Session, admin: Admin, action_error: str | None = None):
    threshold_days = get_settings(session).old_job_threshold_days
    rows = []
    for job in active_jobs(session):
        if not is_old_job(job, threshold_days):
            continue
        user = session.get(User, job.user_id)
        estimate_s = corrected_duration_estimate_s(session, job)
        wait_s = queue_wait_seconds(job)
        rows.append(
            {
                "job": job,
                "user_name": user.name if user else "?",
                "duration_display": format_duration(estimate_s) if estimate_s else None,
                "queue_wait_display": format_duration(wait_s) if wait_s is not None else None,
            }
        )
    return {
        "admin": admin,
        "rows": rows,
        "threshold_days": threshold_days,
        "action_error": action_error,
    }


@router.get("/jobs/old")
def old_jobs_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Queued/approved jobs that have been waiting at least
    Settings.old_job_threshold_days - split entirely out of the normal
    /admin/dashboard queue (see jobs.is_old_job / _dashboard_context
    above), not just flagged alongside everything else there."""
    return templates.TemplateResponse(request, "admin_old_jobs.html", _old_jobs_context(session, admin))


@router.post("/jobs/{job_id}/delete")
def delete_job_route(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Genuinely deletes a stale job - see jobs.delete_old_job for why
    this is a real, unrecoverable delete rather than another terminal
    status like reject(). Only reachable from the old-jobs page, and
    only meaningful for a job still queued/approved there - not a
    general "delete any job" action."""
    job = _get_job_or_404(session, job_id)
    try:
        delete_old_job(session, job, admin)
    except JobActionError as e:
        return templates.TemplateResponse(
            request, "admin_old_jobs.html", _old_jobs_context(session, admin, str(e))
        )
    return RedirectResponse("/admin/jobs/old", status_code=303)


@router.post("/jobs/{job_id}/requeue")
def requeue_job_route(
    request: Request,
    job_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Resets a stale job's queued_at to now instead of deleting or
    rejecting it - see jobs.requeue_job. Only reachable from the
    old-jobs page (always redirects back there, no return_to needed -
    unlike approve/reject/release, there's nowhere else this button
    exists), and by definition the job won't show up there any more
    once its wait clock has been reset."""
    job = _get_job_or_404(session, job_id)
    try:
        requeue_job(session, job, admin)
    except JobActionError as e:
        return templates.TemplateResponse(
            request, "admin_old_jobs.html", _old_jobs_context(session, admin, str(e))
        )
    return RedirectResponse("/admin/jobs/old", status_code=303)


@router.post("/jobs/old/delete_all")
def delete_all_old_jobs_route(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Clears the entire old-jobs backlog in one click - see
    jobs.delete_all_old_jobs. No JobActionError case to handle here
    unlike the single-job route above: every job this touches was just
    re-selected by is_old_job() itself, so there's nothing left that
    could fail _require_status's check inside delete_old_job()."""
    threshold_days = get_settings(session).old_job_threshold_days
    delete_all_old_jobs(session, admin, threshold_days)
    return RedirectResponse("/admin/jobs/old", status_code=303)


# ---- user account management ----
# Scoped to users only for now, not other admins - see README.md's To do
# list ("Accounts") for why admin-managing-admins is a separate item: it
# raises its own safety question (what stops the last admin account from
# being disabled/deleted, including by itself) that deserves its own
# design pass rather than reusing this code as-is.


def _users_context(
    session: Session,
    admin: Admin,
    action_error: str | None = None,
    flash_notice: str | None = None,
):
    users = session.exec(select(User).order_by(User.name)).all()
    return {
        "admin": admin,
        "users": users,
        "action_error": action_error,
        "flash_notice": flash_notice,
    }


@router.get("/users")
def users_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    # Popped, not just read - see reset_user_pin below: the new PIN is
    # shown here exactly once, right after the redirect that follows
    # resetting it, same flash-via-session pattern as user.py's
    # flash_error. A page refresh must not keep re-showing a secret that
    # was already relayed.
    flash_notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request, "admin_users.html", _users_context(session, admin, flash_notice=flash_notice)
    )


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


@router.post("/users/{user_id}/reset_pin")
def reset_user_pin(
    request: Request,
    user_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """The only recovery path for a forgotten PIN - there's no email to
    send a reset link to, and no security question, so an admin sets a
    new one directly and relays it in person. Generates it rather than
    taking one from a form: nothing for the admin to type or get wrong,
    and it's shown back exactly once (see users_page's flash_notice) for
    them to pass along right away. Never logged in plaintext - the
    activity log records that a reset happened and who did it, same as
    disable/enable/delete, not the credential itself."""
    user = _get_user_or_404(session, user_id)
    new_pin = generate_pin()
    user.pin_hash = hash_secret(new_pin)
    session.add(user)
    log_event(session, None, _admin_actor(admin), "pin_reset", detail=user.name)
    session.commit()
    request.session["flash_notice"] = f"New PIN for {user.name}: {new_pin} - give it to them now, it won't be shown again."
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


def _admin_settings_context(session: Session, admin: Admin, error: str | None = None, saved: bool = False):
    return {
        "admin": admin,
        "settings": get_settings(session),
        "themes": THEMES,
        "modes": MODES,
        "selected_theme": admin.theme or DEFAULT_THEME,
        "selected_mode": admin.theme_mode or DEFAULT_MODE,
        # Sorted once per render, not cached - this list only matters
        # while the settings page itself is open, nowhere near often
        # enough to be worth a module-level cache the way
        # templates_env's actual display timezone is.
        "timezones": sorted(zoneinfo.available_timezones()),
        "error": error,
        "saved": saved,
    }


@router.get("/settings")
def settings_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(request, "admin_settings.html", _admin_settings_context(session, admin))


@router.post("/settings")
def update_settings(
    request: Request,
    draft_expiry_days: int = Form(...),
    display_timezone: str = Form(...),
    old_job_threshold_days: int = Form(...),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    error = None
    if draft_expiry_days < 1:
        error = "Draft expiry must be at least 1 day."
    elif not is_valid_timezone(display_timezone):
        error = "Not a real timezone choice."
    elif old_job_threshold_days < 1:
        error = "Old-job threshold must be at least 1 day."
    else:
        settings = get_settings(session)
        settings.draft_expiry_days = draft_expiry_days
        settings.display_timezone = display_timezone
        settings.old_job_threshold_days = old_job_threshold_days
        session.add(settings)
        session.commit()
        # Takes effect immediately, for every viewer, not just after a
        # restart - see templates_env.set_display_timezone for why this
        # in-process cache exists at all rather than a DB read per
        # timestamp shown.
        set_display_timezone(display_timezone)
    return templates.TemplateResponse(
        request, "admin_settings.html", _admin_settings_context(session, admin, error, error is None)
    )


@router.post("/settings/theme")
def update_admin_theme(
    request: Request,
    theme: str = Form(...),
    mode: str = Form(...),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Separate from update_settings above on purpose - this is the
    signed-in admin's own personal preference (models.Admin.theme/
    theme_mode), not part of the shared, site-wide Settings row every
    admin edits together, so it gets its own form/endpoint rather than
    being bundled into the same submit."""
    error = None
    if not is_valid_theme(theme):
        error = "Not a real theme choice."
    elif not is_valid_mode(mode):
        error = "Not a real mode choice."
    else:
        admin.theme = theme
        admin.theme_mode = mode
        session.add(admin)
        session.commit()
    return templates.TemplateResponse(
        request, "admin_settings.html", _admin_settings_context(session, admin, error, error is None)
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


@router.get("/support-bundle")
def download_support_bundle(
    background_tasks: BackgroundTasks,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """On-demand diagnostic bundle for offline bugfixing with zero
    internet access - see support_bundle.py's own docstring for exactly
    what's in it and why. Written to a temp file (support_bundle.build_
    support_bundle already needs one internally for the safe DB copy, so
    this just reuses the same shape) and deleted via a BackgroundTask
    once the response has actually gone out - not before, or the
    download would be truncated."""
    bundle_path = build_support_bundle(session)
    background_tasks.add_task(bundle_path.unlink, missing_ok=True)
    filename = f"queue3d-support-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.tar.gz"
    return FileResponse(bundle_path, media_type="application/gzip", filename=filename)


# ---- colors ----
# The admin-managed filament color list (see models.Color) - add/remove
# colors, set rolls/grams on hand, enable/disable which ones users can
# currently pick from. Deliberately no "still in use, can't delete" guard
# like users/admins get above - a job's color is a plain string snapshot,
# never a live reference to this table (see Color's own docstring), so
# deleting one here can never leave any job in a broken state.


def _colors_context(session: Session, admin: Admin, error: str | None = None):
    colors = session.exec(select(Color).order_by(Color.name)).all()
    return {"admin": admin, "colors": colors, "action_error": error}


def _parse_grams(raw: str) -> tuple[float | None, str | None]:
    """Shared by add/update below - grams_available is optional (a color
    can exist with only its roll count tracked), so this has to
    distinguish "left blank" (fine, means None/unknown) from "typed
    something that isn't a real non-negative number" (a real error, not
    silently coerced to None) - returns (value, error)."""
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        grams = float(raw)
    except ValueError:
        return None, "Grams available must be a number."
    if grams < 0:
        return None, "Grams available can't be negative."
    return grams, None


@router.get("/colors")
def colors_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(request, "admin_colors.html", _colors_context(session, admin))


@router.post("/colors/add")
def add_color(
    request: Request,
    name: str = Form(...),
    rolls_available: int = Form(0),
    grams_available: str = Form(""),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    name = name.strip()
    grams, error = _parse_grams(grams_available)
    if not name:
        error = "Color name can't be empty."
    elif session.exec(select(Color).where(Color.name == name)).first() is not None:
        error = f'"{name}" already exists.'
    elif rolls_available < 0:
        error = "Rolls on hand can't be negative."
    if error:
        return templates.TemplateResponse(request, "admin_colors.html", _colors_context(session, admin, error))
    color = Color(name=name, rolls_available=rolls_available, grams_available=grams)
    session.add(color)
    log_event(session, None, _admin_actor(admin), "color_added", detail=name)
    session.commit()
    return RedirectResponse("/admin/colors", status_code=303)


def _get_color_or_404(session: Session, color_id: int) -> Color:
    color = session.get(Color, color_id)
    if color is None:
        raise HTTPException(status_code=404, detail="No such color")
    return color


@router.post("/colors/{color_id}/update")
def update_color(
    request: Request,
    color_id: int,
    enabled: bool = Form(False),
    rolls_available: int = Form(0),
    grams_available: str = Form(""),
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    color = _get_color_or_404(session, color_id)
    grams, error = _parse_grams(grams_available)
    if error is None and rolls_available < 0:
        error = "Rolls on hand can't be negative."
    if error:
        return templates.TemplateResponse(request, "admin_colors.html", _colors_context(session, admin, error))
    color.enabled = enabled
    color.rolls_available = rolls_available
    color.grams_available = grams
    session.add(color)
    log_event(
        session, None, _admin_actor(admin), "color_updated",
        detail=f"{color.name} (enabled={enabled}, rolls={rolls_available}, grams={grams})",
    )
    session.commit()
    return RedirectResponse("/admin/colors", status_code=303)


@router.post("/colors/{color_id}/delete")
def delete_color(
    request: Request,
    color_id: int,
    admin: Admin = Depends(require_admin),
    session: Session = Depends(get_session),
):
    color = _get_color_or_404(session, color_id)
    log_event(session, None, _admin_actor(admin), "color_deleted", detail=color.name)
    session.delete(color)
    session.commit()
    return RedirectResponse("/admin/colors", status_code=303)
