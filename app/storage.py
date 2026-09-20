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

import io
import json
import zipfile
from pathlib import Path

from db import DATA_DIR
from models import DRAFT_STATUSES

SCRATCH_DIR = DATA_DIR / "scratch"
QUEUE_DIR = DATA_DIR / "queue"
ARCHIVE_DIR = DATA_DIR / "archive"

for _d in (SCRATCH_DIR, QUEUE_DIR, ARCHIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB - generous for a desktop-printer-scale STL

# Model file extensions this app can actually turn into a job - see
# app/mesh.py for the OBJ half (converted to a real .stl immediately on
# upload, so nothing past that point needs to know OBJ ever existed).
MODEL_EXTENSIONS = {".stl", ".obj"}

# A Thingiverse-style download is often a zip of several separate STLs
# (variants, accessories, a multi-part model) rather than one file - per
# the user, each becomes its own job/draft, the same as uploading each
# separately, rather than attempting a combined-plate arrangement (this
# app's whole pipeline is built around one object per job - see
# slicing/stl_to_3mf.py's --arrange 0). Capped, not unbounded - but NOT
# to bound concurrent slicing load, which this was originally (wrongly)
# justified by: FastAPI's BackgroundTasks added within one request run
# strictly sequentially (confirmed directly - never more than one real
# OrcaSlicer/mbotmake process alive at a time, across many checks, for a
# real 15-file upload), so there's no concurrency risk to bound here at
# all. The original value of 10 was hit by a genuine real multi-part
# functional print (a 15-file differential gear assembly) - a real,
# legitimate use this was wrongly blocking. Raised with real headroom
# above that; the remaining reasons for any cap at all are bounding one
# upload's total *serial* slicing time and the memory
# extract_model_files() holds for every matched file's bytes at once,
# not concurrency.
MAX_ZIP_MODEL_FILES = 25


def extract_model_files(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    """Returns [(filename, data), ...] for every .stl/.obj entry in the
    zip - everything else (a README, a photo, a license file, a nested
    folder Thingiverse sometimes wraps everything in) is silently
    ignored, not an error. Raises ValueError with a user-facing message
    for anything that should stop the whole zip: not a real zip, more
    than MAX_ZIP_MODEL_FILES model files, or one individually over
    MAX_UPLOAD_BYTES (checked from the zip's own recorded uncompressed
    size, before actually decompressing it - a real defense against a
    small zip bomb expanding into gigabytes, not just a courtesy).

    Deliberately never calls extractall() or builds any filesystem path
    from an entry's own name (the classic "zip slip" path-traversal
    footgun, e.g. an entry literally named "../../etc/cron.d/x") - only
    `zf.read()` into memory, and only the basename of each entry's name
    is ever kept (for display as Job.original_filename), never used to
    construct a path anywhere."""
    results = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("Not a valid zip file.")
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if Path(name).suffix.lower() not in MODEL_EXTENSIONS:
                continue
            if info.file_size == 0:
                continue
            if info.file_size > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"{name} is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)."
                )
            if len(results) >= MAX_ZIP_MODEL_FILES:
                raise ValueError(f"Too many model files in this zip (max {MAX_ZIP_MODEL_FILES}).")
            results.append((name, zf.read(info)))
    return results


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


def archive_photo_path(job_id: int) -> Path:
    """Where a job's build-plate photo is saved (see jobs.mark_finished)
    - directly here in archive/, not scratch/ or queue/ first, since the
    photo is only ever captured at the moment a job reaches a terminal
    state, unlike the stl/makerbot/supports files which exist earlier and
    get moved."""
    return ARCHIVE_DIR / f"{job_id}.photo.jpg"


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


def delete_job_files(job) -> None:
    """Permanently removes a job's own files - used only by
    jobs.delete_old_job/delete_own_job, the one place this app actually
    deletes files outright rather than archiving them (contrast
    move_job_to_archive/move_draft_to_archive above, and "nothing this
    app finishes with just disappears" everywhere else) - per the user,
    this specific delete is meant to have "no undo," unlike every other
    terminal outcome. Tolerant of any file being absent, same as the
    archive functions - an approved-but-not-yet-sliced-again job could be
    missing its makerbot/supports files in some edge cases, and a
    slice_failed draft never had any to begin with.

    Which directory to look in depends on the job's own status, not a
    parameter the caller has to get right: DRAFT_STATUSES (originally
    just slice_failed, per the user - see jobs.py's own allowed-statuses
    comment) still live in scratch/, never having been submitted;
    everything else this is ever called for is QUEUE_STATUSES, already
    moved to queue/ by submit_draft."""
    stl_path, makerbot_path, supports_path = (
        scratch_paths(job.id) if job.status in DRAFT_STATUSES else queue_paths(job.id)
    )
    for path in (stl_path, makerbot_path, supports_path):
        if path.exists():
            path.unlink()


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
