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
the current snapshot). Also every user/admin-submitted feedback report
(see models.Feedback, feedback.py) - per the user: "Add the username,
feedback, date/time submitted, and any relevant files and logs to the
support bundle" - plus the model file for any job a report references,
even one that never itself recorded a slice error (a report can be
about anything, not just a failed slice).

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
from feedback import list_feedback
from jobs import all_events
from models import Job
from version import APP_VERSION


def _manifest_text(job_count: int, event_count: int, feedback_count: int) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"queue3d support bundle\n"
        f"Generated: {generated_at}\n"
        f"App version: {APP_VERSION}\n"
        f"\n"
        f"Contents:\n"
        f"  database.db      - a safe, consistent copy of the live database\n"
        f"                     (every job, user, setting, event, and\n"
        f"                     submitted feedback)\n"
        f"  activity_log.txt - the full activity log, most recent first\n"
        f"                     ({event_count} events)\n"
        f"  feedback.txt     - every user/admin-submitted feedback report,\n"
        f"                     most recent first ({feedback_count} report(s))\n"
        f"  models/          - the original upload for every job that ever\n"
        f"                     recorded a slice error (whether it ultimately\n"
        f"                     failed or an automatic rotation fixed it), or\n"
        f"                     that a feedback report above referenced\n"
        f"                     ({job_count} file(s))\n"
        f"\n"
        f"Contains real user names, job filenames, feedback text, and the\n"
        f"database's stored (hashed) PIN/password secrets - the same access\n"
        f"an admin already has on the live server, not anything newly\n"
        f"exposed.\n"
    )


def _activity_log_text(events) -> str:
    lines = []
    for event, filename, _photo_path in events:
        when = event.at.strftime("%Y-%m-%d %H:%M:%S UTC")
        job_ref = f"job #{event.job_id} ({filename})" if event.job_id else "(no job)"
        detail = f" - {event.detail}" if event.detail else ""
        lines.append(f"{when}  {event.actor:<20}  {event.action:<20}  {job_ref}{detail}")
    return "\n".join(lines) + "\n" if lines else "(no events recorded)\n"


def _feedback_text(reports) -> str:
    """One block per Feedback row, most recent first - a separate,
    dedicated file rather than folding these into activity_log.txt: a
    report's own description can run to many lines (a "decent size text
    field," per the user), which would read as noise squeezed into that
    log's one-line-per-event format. job_filename (a snapshot - see
    models.Feedback's own docstring) is shown directly rather than
    re-resolved through job_id, so this stays readable even for a report
    whose referenced job has since been archived/removed."""
    if not reports:
        return "(no feedback submitted)\n"
    blocks = []
    for fb in reports:
        submitted = fb.submitted_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        occurred = fb.occurred_at.strftime("%Y-%m-%d %H:%M UTC") if fb.occurred_at else "(not given)"
        job_ref = f"job #{fb.job_id} ({fb.job_filename})" if fb.job_id else "(not related to a specific job)"
        blocks.append(
            f"Submitted: {submitted}\n"
            f"By:        {fb.actor}\n"
            f"Model/job: {job_ref}\n"
            f"Occurred:  {occurred}\n"
            f"\n"
            f"{fb.description}\n"
        )
    return ("\n" + "-" * 60 + "\n\n").join(blocks)


def build_support_bundle(session: Session) -> Path:
    """Writes the bundle to a fresh temp file and returns its path - the
    caller (routers/admin.py) is responsible for cleaning it up after the
    response is sent (a FastAPI BackgroundTask, the same "clean up after
    the response goes out" shape used elsewhere in this app, just at the
    HTTP-response layer instead of a subprocess temp dir)."""
    jobs_with_errors = session.exec(select(Job).where(Job.slice_error.is_not(None))).all()
    reports = list_feedback(session)
    events = all_events(session, limit=5000)

    # Every job either flagged by its own slice error or referenced by a
    # feedback report - a union, not two separate model dumps, so a job
    # that's both (a real, common case: someone reports exactly the
    # failure the error column already recorded) only ever ends up in
    # the tarball once.
    jobs_for_models = {job.id: job for job in jobs_with_errors}
    for fb in reports:
        if fb.job_id is not None and fb.job_id not in jobs_for_models:
            job = session.get(Job, fb.job_id)
            if job is not None:
                jobs_for_models[job.id] = job

    out_fd, out_path_str = tempfile.mkstemp(prefix="queue3d-support-", suffix=".tar.gz")
    os.close(out_fd)
    out_path = Path(out_path_str)

    with tempfile.TemporaryDirectory(prefix="queue3d-support-build-") as tmp:
        tmp = Path(tmp)
        db_path = backup_database(tmp / "db")  # safe online copy, not a raw file read

        manifest_path = tmp / "manifest.txt"
        manifest_path.write_text(_manifest_text(len(jobs_for_models), len(events), len(reports)))

        log_path = tmp / "activity_log.txt"
        log_path.write_text(_activity_log_text(events))

        feedback_path = tmp / "feedback.txt"
        feedback_path.write_text(_feedback_text(reports))

        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.txt")
            tar.add(db_path, arcname="database.db")
            tar.add(log_path, arcname="activity_log.txt")
            tar.add(feedback_path, arcname="feedback.txt")
            for job in jobs_for_models.values():
                if job.stl_path and Path(job.stl_path).exists():
                    safe_name = Path(job.original_filename).name  # strip any path component
                    tar.add(job.stl_path, arcname=f"models/{job.id}_{safe_name}")

    return out_path
