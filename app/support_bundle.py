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

Also the last two weeks of the service's systemd journal (journal.txt):
the app's own output, warnings and tracebacks, which the database
doesn't record. A job stuck 'submitted' in a real field bundle left no
trace anywhere else. Reading it needs the queue3d user in the
systemd-journal group, which deploy/remote_install.sh sets up; without
it, or off the Pi, journal.txt says why it's empty instead.

No real names leave the site in it: every account appears as a code
and every model filename as another (see pseudonyms.py), names are
scrubbed from free text, and PIN/password hashes are blanked in the
database copy (_scrub_database). The codes are stable across bundles and
can be looked up only on the Pi itself.
"""

import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from backup import backup_database
from feedback import list_feedback
from jobs import all_events
from models import Job
from pseudonyms import Scrubber
from version import APP_VERSION


def _manifest_text(job_count: int, event_count: int, feedback_count: int) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"queue3d support bundle\n"
        f"Generated: {generated_at}\n"
        f"App version: {APP_VERSION}\n"
        f"\n"
        f"Contents:\n"
        f"  database.db      - a copy of the live database (every job,\n"
        f"                     setting, event, and submitted feedback)\n"
        f"  activity_log.txt - the full activity log, most recent first\n"
        f"                     ({event_count} events)\n"
        f"  feedback.txt     - every user/admin-submitted feedback report,\n"
        f"                     most recent first ({feedback_count} report(s))\n"
        f"  journal.txt      - the service's systemd journal for the last\n"
        f"                     {JOURNAL_DAYS} days (the app's own output and errors)\n"
        f"  models/          - the model the slicer was given (an STL; .obj\n"
        f"                     and .3mf uploads are stored converted) for\n"
        f"                     every job that ever recorded a slice error\n"
        f"                     (whether it ultimately failed or an automatic\n"
        f"                     rotation fixed it), or that a feedback report\n"
        f"                     above referenced, named by job number\n"
        f"                     ({job_count} file(s))\n"
        f"\n"
        f"No real names. Every user and admin appears as a code like\n"
        f"user-3f9a2c, and every model filename as one like file-81bc04.stl,\n"
        f"the same in every bundle from this site. Names are also removed\n"
        f"from free text (feedback, notes, log details, the journal) - best\n"
        f"effort, since a name that isn't an account can't be recognised.\n"
        f"PINs and passwords (even hashed) are removed from the database\n"
        f"copy. An admin on site can look codes up under Admin > Support\n"
        f"bundle codes.\n"
    )


def _scrub_database(db_path: Path, scrubber: Scrubber) -> None:
    """Replaces every name, filename and secret in the bundle's copy of
    the database (never the live one), then VACUUMs it - an UPDATE leaves
    the old values in free pages of the file until then."""
    conn = sqlite3.connect(db_path)
    try:
        conn.create_function("scrub_text", 1, scrubber.text)
        conn.create_function("scrub_actor", 1, scrubber.actor)
        conn.create_function("scrub_file", 1, scrubber.filename)
        conn.create_function("user_code", 1, lambda name: scrubber.person("user", name))
        conn.create_function("admin_code", 1, lambda name: scrubber.person("admin", name))
        conn.executescript(
            """
            UPDATE user SET name = user_code(name), pin_hash = '';
            UPDATE admin SET username = admin_code(username), password_hash = '';
            UPDATE job SET
                original_filename = scrub_file(original_filename),
                reviewed_by_name = CASE WHEN reviewed_by_name IS NULL THEN NULL ELSE admin_code(reviewed_by_name) END,
                admin_note = scrub_text(admin_note),
                failure_reason = scrub_text(failure_reason),
                slice_error = scrub_text(slice_error);
            UPDATE jobevent SET actor = scrub_actor(actor), detail = scrub_text(detail);
            UPDATE feedback SET
                actor = scrub_actor(actor),
                description = scrub_text(description),
                job_filename = scrub_file(job_filename);
            """
        )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


JOURNAL_DAYS = 14
_JOURNAL_UNITS = ("queue3d", "queue3d-backup", "queue3d-cleanup")


def _journal_text() -> str:
    journalctl = shutil.which("journalctl")
    if journalctl is None:
        return "(journalctl isn't available here, so there's no service journal to include)\n"
    cmd = [journalctl, "--no-pager", "-o", "short-iso", "--since", f"-{JOURNAL_DAYS} days"]
    for unit in _JOURNAL_UNITS:
        cmd += ["-u", unit]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(couldn't read the service journal: {e})\n"
    text = result.stdout
    if result.stderr.strip():
        text += f"\n(journalctl exited {result.returncode}: {result.stderr.strip()})\n"
    return text or "(the service journal is empty)\n"


def _activity_log_text(events, scrubber: Scrubber) -> str:
    lines = []
    for event, filename, _photo_path in events:
        when = event.at.strftime("%Y-%m-%d %H:%M:%S UTC")
        job_ref = f"job #{event.job_id} ({scrubber.filename(filename)})" if event.job_id else "(no job)"
        detail = f" - {event.detail}" if event.detail else ""
        lines.append(f"{when}  {scrubber.actor(event.actor):<20}  {event.action:<20}  {job_ref}{detail}")
    return "\n".join(lines) + "\n" if lines else "(no events recorded)\n"


def _feedback_text(reports, scrubber: Scrubber) -> str:
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
        job_ref = f"job #{fb.job_id} ({scrubber.filename(fb.job_filename)})" if fb.job_id else "(not related to a specific job)"
        blocks.append(
            f"Submitted: {submitted}\n"
            f"By:        {scrubber.actor(fb.actor)}\n"
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

        scrubber = Scrubber(session)
        _scrub_database(db_path, scrubber)

        manifest_path = tmp / "manifest.txt"
        manifest_path.write_text(_manifest_text(len(jobs_for_models), len(events), len(reports)))

        # Built from the live rows, so scrubbed as whole text: actors
        # first (exact), then every remaining name and filename.
        log_path = tmp / "activity_log.txt"
        log_path.write_text(scrubber.text(_activity_log_text(events, scrubber)))

        feedback_path = tmp / "feedback.txt"
        feedback_path.write_text(scrubber.text(_feedback_text(reports, scrubber)))

        journal_path = tmp / "journal.txt"
        journal_path.write_text(scrubber.text(_journal_text()))

        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.txt")
            tar.add(db_path, arcname="database.db")
            tar.add(log_path, arcname="activity_log.txt")
            tar.add(feedback_path, arcname="feedback.txt")
            tar.add(journal_path, arcname="journal.txt")
            for job in jobs_for_models.values():
                if job.stl_path and Path(job.stl_path).exists():
                    # Named by job number, not filename (which often has
                    # a student's name in it), and for the stored file's
                    # real format: an uploaded .3mf or .obj is kept
                    # converted to STL.
                    tar.add(job.stl_path, arcname=f"models/job-{job.id}{Path(job.stl_path).suffix}")

    return out_path
