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
    # A disabled user is blocked from logging in - and from an already-open
    # session, immediately (see auth.get_current_user) - without deleting
    # their account or job history. Reversible, unlike delete.
    disabled: bool = Field(default=False)
    # A per-account UI preference (see themes.py), not a per-device one -
    # per the user, this needs to follow them across logins/devices, which
    # is exactly what a browser-only preference (localStorage) can't do.
    # None means "no preference set" - templates_env.current_theme() falls
    # back to themes.DEFAULT_THEME, not this column directly, so a theme
    # ever getting removed from themes.THEMES can't strand an account on
    # a dead value.
    theme: str | None = Field(default=None)
    # Light/dark - a separate axis from theme, not folded into it (see
    # themes.py) - independently persisted the same way and for the same
    # reason.
    theme_mode: str | None = Field(default=None)


class Admin(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    # Same per-account preferences as User.theme/theme_mode above - admins
    # pick their own look independently of any user's.
    theme: str | None = Field(default=None)
    theme_mode: str | None = Field(default=None)


class SchemaVersion(SQLModel, table=True):
    """A single-row marker of the app VERSION (see version.py) that last
    touched this database's schema - not a separate incrementing number.
    Per the user: a schema change should always come with a version bump,
    so the app version doubles as the schema version rather than tracking
    a second number that could quietly drift out of sync with it (which
    is exactly what happened the first time - see db.py's migration list
    for the incident this whole mechanism exists because of).

    Not an ORM-schema version in the Alembic sense - this project has no
    migration framework, deliberately, for something this small (see
    db.py). `SQLModel.metadata.create_all()` only ever creates tables
    that don't exist yet; it never alters an existing one, so a running
    deployment's database can silently fall behind the code's model
    definitions after a `git pull`. Checked on every startup so that
    upgrading is "bump VERSION, pull, restart," not "remember to run a
    script and hope you remember which one."""

    id: int = Field(default=1, primary_key=True)
    version: str


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
    no separate pre-review gate before something counts as "in the queue" -
    but slicing and submitting *to* that queue are two distinct, explicit
    user actions, not one combined step. A user can re-slice a 'sliced' or
    'slice_failed' draft with different settings as many times as they
    want (reusing the same already-uploaded file - see jobs.start_reslice)
    before ever deciding to submit it, without spamming the shared,
    admin-visible queue with abandoned attempts just to preview a
    different support style.

        submitted -> sliced -> queued (an explicit "submit" action)
            -> approved (admin greenlit it, waiting its turn)
                -> printing (admin explicitly released it) -> done | failed
            -> rejected (admin declined, with a note - terminal)
        submitted -> slice_failed (can retry: re-slice in place, or let it expire)
        {submitted, sliced, slice_failed} -> expired (never submitted, past
            the admin-configured age threshold - see cleanup_drafts.py)

    Only an explicit admin *release* sends an approved job to the printer -
    approval alone just marks it greenlit while it waits, since only one
    job can be on the printer at a time.
    """

    submitted = "submitted"
    sliced = "sliced"
    slice_failed = "slice_failed"
    expired = "expired"
    queued = "queued"
    approved = "approved"
    rejected = "rejected"
    printing = "printing"
    done = "done"
    failed = "failed"


# Every status is in exactly one of these three sets - keep it that way, so
# "is this job admin-visible / does deleting its user need to be blocked /
# is anything more ever going to happen to it" all stay correct by
# construction instead of needing their own separate allow-lists that can
# quietly drift out of sync with a status added later.
#
# DRAFT: sliced (or slicing, or failed to) but never submitted - exists
# only for its own user; the admin queue view must never show these.
# QUEUE: actually in the shared queue, admin-visible and actionable.
# TERMINAL: nothing more will ever happen to it; files live in archive/,
# never queue/ or scratch/ (see storage.py).
DRAFT_STATUSES = {JobStatus.submitted, JobStatus.sliced, JobStatus.slice_failed}
QUEUE_STATUSES = {JobStatus.queued, JobStatus.approved, JobStatus.printing}
TERMINAL_STATUSES = {JobStatus.rejected, JobStatus.done, JobStatus.failed, JobStatus.expired}


class Settings(SQLModel, table=True):
    """A single-row table of admin-configurable settings - just one so far.
    A real key/value settings table would be overkill for one integer;
    add columns here as more settings show up rather than reaching for
    that until there's actually more than one."""

    id: int = Field(default=1, primary_key=True)
    # How long a sliced-but-never-submitted draft sits before
    # cleanup_drafts.py expires it. Admin-configurable (see
    # routers/admin.py's /admin/settings) rather than a fixed constant,
    # per the user - the "harmless clutter vs. auto-expire" question the
    # draft feature raises doesn't have one right answer for every
    # deployment's traffic/storage.
    draft_expiry_days: int = Field(default=7)


class JobEvent(SQLModel, table=True):
    """One row per audited action - the activity log. `Job` itself only
    ever holds the *current* snapshot (`reviewed_at`/`reviewed_by_admin_id`/
    `admin_note` capture just the latest review, nothing earlier), which
    was the actual gap that prompted this: a rejection was recorded
    correctly but there was nowhere to go *see* that it had been, so it
    read as if nothing had happened. This is the full history instead -
    every submit, slice attempt, re-slice, queue-submit, approve, reject,
    release, done/failed, and expiry, in order, for a given job - plus,
    per the user ("all actions should be captured"), account lifecycle
    actions that aren't tied to any one job at all: a user registering,
    and an admin disabling/re-enabling/deleting one. `job_id` is nullable
    for exactly that reason (schema 2.5.0) - None for an account action,
    with the affected user's name in `detail` instead of a job's filename.

    `actor` is a plain label (`"user:<name>"`, `"admin:<username>"`, or
    `"system"` for an automated action like draft expiry) rather than a
    real foreign key to either `User` or `Admin` - those are two separate
    tables, and a job's actor could be either one or nothing at all;
    a label is simpler than a polymorphic FK for something only ever
    displayed, never joined against."""

    id: int | None = Field(default=None, primary_key=True)
    job_id: int | None = Field(default=None, foreign_key="job.id", index=True)
    at: datetime = Field(default_factory=utcnow)
    actor: str
    action: str  # short verb: "submitted", "sliced", "slice_failed", "reslice_started", "queued", "approved", "rejected", "released", "done", "failed", "expired"
    detail: str = ""  # e.g. the rejection note, or a truncated slice error


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    original_filename: str
    status: JobStatus = Field(default=JobStatus.submitted, index=True)

    stl_path: str | None = Field(default=None)
    makerbot_path: str | None = Field(default=None)
    duration_estimate_s: float | None = Field(default=None)
    slice_error: str | None = Field(default=None)

    supports_enabled: bool = Field(default=False)
    # OrcaSlicer's own support_style setting used for this job (grid/snug/
    # organic/tree_hybrid/tree_slim/default) - see routers/user.py's
    # SUPPORT_STYLES. Only meaningful when supports_enabled.
    support_style: str | None = Field(default=None)
    # Path to a JSON file of simplified support-material line segments (see
    # app/supports.py) - present once slicing succeeds, even if empty
    # (supports enabled but the model didn't need any).
    supports_path: str | None = Field(default=None)

    # When this row was created (upload time) - NOT when it joined the
    # queue, which may be much later or never (see queued_at below). Used
    # to order a user's own submissions list and to age out abandoned
    # drafts (cleanup_drafts.py), not for queue fairness.
    created_at: datetime = Field(default_factory=utcnow)
    # When submit_draft() actually moved this job into the queue - None
    # until then. This, not created_at, is what queue_position() orders
    # by: a draft someone sat on for hours before submitting must not cut
    # ahead of everyone who submitted straight away in the meantime.
    queued_at: datetime | None = Field(default=None)
    reviewed_at: datetime | None = Field(default=None)
    reviewed_by_admin_id: int | None = Field(default=None, foreign_key="admin.id")
    admin_note: str | None = Field(default=None)
    released_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    # A photo of the build plate, taken via the printer's camera the
    # moment an admin marks this job done or failed (see jobs.mark_finished)
    # - regardless of outcome, so both the submitter and an admin have a
    # visual record of what actually happened, not just a status word.
    # None if capture failed (camera/printer unreachable, etc.) - a
    # missing photo never blocks recording the print's own outcome, see
    # that function's docstring.
    photo_path: str | None = Field(default=None)
