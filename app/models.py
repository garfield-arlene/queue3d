"""Database models.

Two separate account types by design (see project memory `queue3d-purpose`
for the deployment context that shaped this, without baking that context's
own terminology in here) - regular users self-serve with a name + PIN (low
friction for signing up on demand), admins get real accounts provisioned
out-of-band (see create_admin.py), since admin approval is the hard gate
before anything reaches the print queue.
"""

import enum
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow():
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    pin_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class Admin(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class BackupRecord(SQLModel, table=True):
    """One row per backup attempt (success or failure), written by
    backup.py. The admin dashboard shows the most recent successful one so
    an admin can tell at a glance whether the automated backups are
    actually running - see project memory queue3d-deployment-network for
    why: submissions can happen anytime, so backups run on a daily cron
    rather than at any fixed, predictable time."""

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow)
    target_label: str  # which backup drive/directory this run targeted
    success: bool
    detail: str = ""  # error message on failure, brief summary on success


class JobStatus(str, enum.Enum):
    """Confirmed state machine (project memory queue3d-purpose - don't
    drift from this): users submit directly into the one queue, there's
    no separate pre-review gate before something counts as "in the queue."

        submitted -> (sliced) -> queued
            -> approved (admin greenlit it, waiting its turn)
                -> printing (admin explicitly released it) -> done | failed
            -> rejected (admin declined, with a note - terminal)
        submitted -> slice_failed (terminal - the submitter can resubmit)

    Only an explicit admin *release* sends an approved job to the printer -
    approval alone just marks it greenlit while it waits, since only one
    job can be on the printer at a time.
    """

    submitted = "submitted"
    slice_failed = "slice_failed"
    queued = "queued"
    approved = "approved"
    rejected = "rejected"
    printing = "printing"
    done = "done"
    failed = "failed"


# Terminal states whose files get moved to archive/ (see storage.py) -
# nothing more will ever happen to a job in one of these. "Active" is
# defined as simply not-terminal (see jobs.active_jobs) rather than a
# second overlapping allow-list, so the two can't drift out of sync.
TERMINAL_STATUSES = {JobStatus.rejected, JobStatus.done, JobStatus.failed}


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    original_filename: str
    status: JobStatus = Field(default=JobStatus.submitted, index=True)

    stl_path: str | None = Field(default=None)
    makerbot_path: str | None = Field(default=None)
    duration_estimate_s: float | None = Field(default=None)
    slice_error: str | None = Field(default=None)

    submitted_at: datetime = Field(default_factory=utcnow)
    reviewed_at: datetime | None = Field(default=None)
    reviewed_by_admin_id: int | None = Field(default=None, foreign_key="admin.id")
    admin_note: str | None = Field(default=None)
    released_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
