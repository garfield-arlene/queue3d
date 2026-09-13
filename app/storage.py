"""Where job files live on disk, and how they move between lifecycle
stages. Three directories on one filesystem (see project memory
queue3d-app-progress for why not three physical drives): scratch/ for
anything not yet submitted to the queue (models.DRAFT_STATUSES - not just
briefly transient any more now that slicing and submitting are separate
actions; a draft can sit in scratch/ indefinitely until the user submits
it or it expires), queue/ for anything actually in the shared queue
(models.QUEUE_STATUSES), archive/ for anything finished
(models.TERMINAL_STATUSES, which now includes an expired, never-submitted
draft alongside done/failed/rejected). Moving a job between stages is an
atomic rename within one filesystem, not a cross-device copy+delete.

Files are named by job id, not the user's original filename - avoids
collisions and path-traversal footguns from user-supplied names entirely.
The original filename is only ever used for display (see Job.original_filename).
"""

import json
import zipfile
from pathlib import Path

from db import DATA_DIR

SCRATCH_DIR = DATA_DIR / "scratch"
QUEUE_DIR = DATA_DIR / "queue"
ARCHIVE_DIR = DATA_DIR / "archive"

for _d in (SCRATCH_DIR, QUEUE_DIR, ARCHIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB - generous for a desktop-printer-scale STL


def scratch_stl_path(job_id: int) -> Path:
    return SCRATCH_DIR / f"{job_id}.stl"


def scratch_paths(job_id: int) -> tuple[Path, Path, Path]:
    """Where a draft's files live - a job stays here for as long as it's
    sliced but not yet submitted (see models.DRAFT_STATUSES), not just
    during the brief window before slicing used to finish. Re-slicing a
    draft (jobs.start_reslice) overwrites the makerbot/supports files here
    in place; submit_draft moves all three into queue/ only once the user
    actually submits."""
    return (
        scratch_stl_path(job_id),
        SCRATCH_DIR / f"{job_id}.makerbot",
        SCRATCH_DIR / f"{job_id}.supports.json",
    )


def queue_paths(job_id: int) -> tuple[Path, Path, Path]:
    return (
        QUEUE_DIR / f"{job_id}.stl",
        QUEUE_DIR / f"{job_id}.makerbot",
        QUEUE_DIR / f"{job_id}.supports.json",
    )


def archive_paths(job_id: int) -> tuple[Path, Path, Path]:
    return (
        ARCHIVE_DIR / f"{job_id}.stl",
        ARCHIVE_DIR / f"{job_id}.makerbot",
        ARCHIVE_DIR / f"{job_id}.supports.json",
    )


def move_job_to_archive(job) -> None:
    """Move a job's files from queue/ to archive/ once it reaches a
    terminal state (done/failed/rejected). Tolerant of any file being
    absent (e.g. a rejected job might never have finished slicing, or may
    have no supports)."""
    src_stl, src_makerbot, src_supports = queue_paths(job.id)
    dest_stl, dest_makerbot, dest_supports = archive_paths(job.id)
    if src_stl.exists():
        src_stl.rename(dest_stl)
        job.stl_path = str(dest_stl)
    if src_makerbot.exists():
        src_makerbot.rename(dest_makerbot)
        job.makerbot_path = str(dest_makerbot)
    if src_supports.exists():
        src_supports.rename(dest_supports)
        job.supports_path = str(dest_supports)


def move_draft_to_archive(job) -> None:
    """Move an expired draft's files from scratch/ to archive/
    (cleanup_drafts.py) - a draft's files live in scratch/ the whole time
    it's a draft (see scratch_paths), never queue/, since it never reached
    the queue at all. Tolerant of any file being absent, same as
    move_job_to_archive - a slice_failed draft may never have produced a
    .makerbot or supports.json."""
    src_stl, src_makerbot, src_supports = scratch_paths(job.id)
    dest_stl, dest_makerbot, dest_supports = archive_paths(job.id)
    if src_stl.exists():
        src_stl.rename(dest_stl)
        job.stl_path = str(dest_stl)
    if src_makerbot.exists():
        src_makerbot.rename(dest_makerbot)
        job.makerbot_path = str(dest_makerbot)
    if src_supports.exists():
        src_supports.rename(dest_supports)
        job.supports_path = str(dest_supports)


def read_makerbot_duration_s(makerbot_path: Path) -> float | None:
    """Read the slicer's own duration estimate out of a .makerbot's
    meta.json - see slicing/README.md for the file format. Returns None on
    any problem reading it; a missing estimate isn't fatal to anything."""
    try:
        with zipfile.ZipFile(makerbot_path) as z, z.open("meta.json") as f:
            meta = json.load(f)
        return meta.get("duration_s")
    except Exception:
        return None
