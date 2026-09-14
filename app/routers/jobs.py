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
