"""Builds an on-demand diagnostic bundle for offline bugfixing - per the
user: "After I setup the app/Pi, network, and printer in place, I want
to be able to show up and collect the support files." This deployment
has zero internet access (see project memory
queue3d-deployment-network), so there's no way to relay a live issue
back for help the normal way - an admin generates this .tar.gz on the
spot and hands it off physically instead.

What's actually useful for offline diagnosis, based on what every real
bug investigated this project has needed so far: the database (every
job's settings/status/history in one place - by far the single most
useful thing, and what every investigation this session started from),
the actual model files for anything that had trouble (without the real
geometry, a slicing failure can't be reproduced or diagnosed at all -
confirmed repeatedly: the fighter jet, Flexi_Seal, and the Christmas
tree investigations all depended on having the real file in hand), and
the full activity log (the sequence of what actually happened, not just
the current snapshot).

NOT included, and worth being explicit about why: a persistent
application log file. This app doesn't currently write one - its own
progress output goes straight to whatever terminal `uvicorn` happens to
be running in, not a file, so there's nothing on disk to collect here
yet. If/when the real deployment runs this under systemd, its journal
would be the natural next thing to add here - not attempted now since
that setup doesn't exist to test against.

Contains real user names and job filenames, and the database's stored
(hashed, not plaintext) PIN/password secrets - the same information an
admin already has full access to on the live server, not anything newly
exposed by bundling it. Worth knowing before handing the file to anyone
else, which is why this is documented plainly here rather than silently
assumed harmless.
"""

import os
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from backup import backup_database
from jobs import all_events
from models import Job
from version import APP_VERSION


def _manifest_text(job_count: int, event_count: int) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"queue3d support bundle\n"
        f"Generated: {generated_at}\n"
        f"App version: {APP_VERSION}\n"
        f"\n"
        f"Contents:\n"
        f"  database.db      - a safe, consistent copy of the live database\n"
        f"                     (every job, user, setting, and event)\n"
        f"  activity_log.txt - the full activity log, most recent first\n"
        f"                     ({event_count} events)\n"
        f"  models/          - the original upload for every job that ever\n"
        f"                     recorded a slice error, whether it ultimately\n"
        f"                     failed or an automatic rotation fixed it\n"
        f"                     ({job_count} file(s))\n"
        f"\n"
        f"Contains real user names, job filenames, and the database's\n"
        f"stored (hashed) PIN/password secrets - the same access an admin\n"
        f"already has on the live server, not anything newly exposed.\n"
    )


def _activity_log_text(events) -> str:
    lines = []
    for event, filename, _photo_path in events:
        when = event.at.strftime("%Y-%m-%d %H:%M:%S UTC")
        job_ref = f"job #{event.job_id} ({filename})" if event.job_id else "(no job)"
        detail = f" - {event.detail}" if event.detail else ""
        lines.append(f"{when}  {event.actor:<20}  {event.action:<20}  {job_ref}{detail}")
    return "\n".join(lines) + "\n" if lines else "(no events recorded)\n"


def build_support_bundle(session: Session) -> Path:
    """Writes the bundle to a fresh temp file and returns its path - the
    caller (routers/admin.py) is responsible for cleaning it up after the
    response is sent (a FastAPI BackgroundTask, the same "clean up after
    the response goes out" shape used elsewhere in this app, just at the
    HTTP-response layer instead of a subprocess temp dir)."""
    jobs_with_errors = session.exec(select(Job).where(Job.slice_error.is_not(None))).all()
    events = all_events(session, limit=5000)

    out_fd, out_path_str = tempfile.mkstemp(prefix="queue3d-support-", suffix=".tar.gz")
    os.close(out_fd)
    out_path = Path(out_path_str)

    with tempfile.TemporaryDirectory(prefix="queue3d-support-build-") as tmp:
        tmp = Path(tmp)
        db_path = backup_database(tmp / "db")  # safe online copy, not a raw file read

        manifest_path = tmp / "manifest.txt"
        manifest_path.write_text(_manifest_text(len(jobs_with_errors), len(events)))

        log_path = tmp / "activity_log.txt"
        log_path.write_text(_activity_log_text(events))

        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.txt")
            tar.add(db_path, arcname="database.db")
            tar.add(log_path, arcname="activity_log.txt")
            for job in jobs_with_errors:
                if job.stl_path and Path(job.stl_path).exists():
                    safe_name = Path(job.original_filename).name  # strip any path component
                    tar.add(job.stl_path, arcname=f"models/{job.id}_{safe_name}")

    return out_path
