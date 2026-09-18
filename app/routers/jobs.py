"""Serves a job's model/supports for the 3D preview, and the preview page
itself - usable by either the job's owning user or any admin, unlike the
role-specific routers. The pre-submission preview parses a local File
directly in the browser; viewing an already-sliced job needs to fetch it
from somewhere, and both "a user checking their own job" and "an admin
reviewing it" need that same access.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlmodel import Session

from auth import AuthRedirect
from db import get_session
from jobs import print_progress
from models import Job
from templates_env import templates

router = APIRouter(prefix="/jobs")


def _job_with_access(job_id: int, request: Request, session: Session) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    admin_id = request.session.get("admin_id")
    user_id = request.session.get("user_id")
    if admin_id is not None or (user_id is not None and user_id == job.user_id):
        return job
    raise AuthRedirect("/login")


@router.get("/{job_id}/preview")
def preview_page(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = _job_with_access(job_id, request, session)
    return templates.TemplateResponse(request, "job_preview.html", {"job": job})


@router.get("/{job_id}/model.stl")
def model_file(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = _job_with_access(job_id, request, session)
    if not job.stl_path or not Path(job.stl_path).exists():
        raise HTTPException(status_code=404, detail="No model file for this job")
    return FileResponse(job.stl_path, media_type="model/stl")


@router.get("/{job_id}/supports.json")
def supports_file(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = _job_with_access(job_id, request, session)
    if job.supports_path and Path(job.supports_path).exists():
        return FileResponse(job.supports_path, media_type="application/json")
    return JSONResponse([])


@router.get("/{job_id}/progress")
def progress_fragment(job_id: int, request: Request, session: Session = Depends(get_session)):
    """Just the live-progress span, for the htmx polling in
    _print_progress.html to re-fetch while this job is still printing -
    see that template for why polling stops on its own once it isn't.
    Same owner-or-admin access as everything else here - a read-only,
    best-effort printer query (see jobs.print_progress), never something
    that can fail the request outright."""
    job = _job_with_access(job_id, request, session)
    return templates.TemplateResponse(
        request, "_print_progress.html", {"job": job, "progress": print_progress(job)}
    )


@router.get("/{job_id}/photo.jpg")
def photo_file(job_id: int, request: Request, session: Session = Depends(get_session)):
    """The build-plate photo captured when this job was marked done/failed
    (see jobs.mark_finished) - same access rule as everything else here,
    the job's own owner or any admin. 404 if none was ever captured
    (camera unreachable at the time, or the job hasn't finished yet)."""
    job = _job_with_access(job_id, request, session)
    if not job.photo_path or not Path(job.photo_path).exists():
        raise HTTPException(status_code=404, detail="No photo for this job")
    return FileResponse(job.photo_path, media_type="image/jpeg")
