"""Database models.

Two separate account types by design (see project memory `queue3d-purpose`
for the deployment context that shaped this, without baking that context's
own terminology in here) - regular users self-serve with a name + PIN (low
friction for signing up on demand), admins get real accounts provisioned
either out-of-band for the very first one (create_admin.py, run directly
on the server - see that script) or by an already-signed-in admin
afterward (routers/admin.py's admins_page) - never by open self-signup
either way, since admin approval is the hard gate before anything reaches
the print queue. See Admin.unremovable for what keeps the CLI path's own
admin(s) permanent regardless of what happens to any admin created later.
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
    # Login rate-limiting (see auth.py's check_lockout/record_failed_login/
    # record_successful_login) - failed_login_attempts counts consecutive
    # failures since the last success or lockout; locked_until, once set,
    # blocks login regardless of correct credentials until that moment
    # passes. Per the user: PINs are short by design, which also makes
    # them easier to guess, and nothing previously slowed down repeated
    # attempts at all.
    failed_login_attempts: int = Field(default=0)
    locked_until: datetime | None = Field(default=None)


class Admin(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    # Same per-account preferences as User.theme/theme_mode above - admins
    # pick their own look independently of any user's.
    theme: str | None = Field(default=None)
    theme_mode: str | None = Field(default=None)
    # Same login rate-limiting as User above - an admin's password is a
    # higher-stakes target than any one user's PIN, so this applies here
    # too, not just the short-PIN case that motivated it.
    failed_login_attempts: int = Field(default=0)
    locked_until: datetime | None = Field(default=None)
    # Same meaning as User.disabled above (schema 6.5.0) - blocked from
    # logging in, and from an already-open session immediately (see
    # auth.get_current_admin), without deleting the account or its
    # history. Reversible, unlike delete - and unlike delete, this alone
    # doesn't need the unremovable check below to still make sense on its
    # own; routers/admin.py's disable_admin enforces that restriction at
    # the route level instead (disabling an unremovable admin would
    # otherwise be a functionally-identical way around the whole reason
    # unremovable exists - it doesn't delete the account, but it locks it
    # out just as completely).
    disabled: bool = Field(default=False)
    # True only for an admin created via create_admin.py (the CLI, run
    # directly on the server - see that script) - never settable through
    # the web UI at all, in either direction. Per the user: every admin
    # created this way is permanent, so routers/admin.py's delete_admin
    # can refuse to ever delete one - schema 6.4.0, added alongside the
    # web UI for admins creating *other* admins (see admins_page), which
    # is exactly what makes "can this ever be deleted" a real question
    # for the first time; every admin created *that* way is a normal,
    # deletable account (unremovable=False, the default) instead. Since
    # nothing anywhere can ever flip this once set, a deployment that
    # provisions at least one admin via the CLI (the only way to get the
    # very first admin at all, before any UI exists to log into) can
    # never end up with zero surviving admins, no matter what happens to
    # every other admin account created after it.
    unremovable: bool = Field(default=False)


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
    """A single-row table of admin-configurable settings that apply to
    the whole app at once, not per-account - see User/Admin.theme for
    the per-account equivalent. Not a generic key/value store; add
    columns here as more settings show up rather than reaching for that
    until there's actually more than a couple."""

    id: int = Field(default=1, primary_key=True)
    # How long a sliced-but-never-submitted draft sits before
    # cleanup_drafts.py expires it. Admin-configurable (see
    # routers/admin.py's /admin/settings) rather than a fixed constant,
    # per the user - the "harmless clutter vs. auto-expire" question the
    # draft feature raises doesn't have one right answer for every
    # deployment's traffic/storage.
    draft_expiry_days: int = Field(default=7)
    # An IANA zone name (e.g. "America/New_York") every timestamp in the
    # app is displayed in - see templates_env.local_time. Every
    # timestamp is still stored and compared internally as UTC
    # (unchanged, and it must stay that way - see that module's
    # docstring); this only affects what a viewer actually reads on the
    # page. Site-wide, not per-account: per the user, "should apply
    # everywhere at once, not per-page" - one admin-set value for the
    # whole deployment, not a per-viewer preference like theme/mode are.
    # Defaults to "UTC" so an untouched deployment shows exactly what it
    # always has.
    display_timezone: str = Field(default="UTC")
    # How many days a queued/approved job can sit waiting before it counts
    # as "old" - see jobs.is_old_job, /admin/jobs/old. Splits the normal
    # queue view from a separate backlog view, per the user, rather than
    # just showing everything together forever. Scoped to queued/approved
    # only, not printing - a print actively running is being acted on,
    # not sitting in backlog (same reasoning jobs.queue_wait_seconds
    # already uses). Defaults to 30, matching the to-do list's own
    # example value.
    old_job_threshold_days: int = Field(default=30)


class Color(SQLModel, table=True):
    """The admin-managed filament color list - per the user: admins add/
    remove colors and set how many rolls are on hand, enable/disable which
    ones users can currently pick from, and users pick exactly one (or
    "any available") at upload time. `rolls_available` is just an admin's
    own count of physical spools, shown for their own inventory tracking;
    `grams_available` (optional - a color can exist with rolls tracked but
    no gram total entered) is what actually powers the too-little-filament
    check in jobs.filament_status, compared against a job's own
    `Job.filament_grams` once it's been sliced.

    Deliberately **not** referenced by `Job` as a real foreign key - see
    `Job.color_name`'s own docstring for why a job's color is a plain
    string snapshot instead. This means deleting a color here never
    touches any existing job, by design.

    This whole feature is explicitly "best effort," per the user: the
    printer has no way to report how much filament is actually loaded or
    remaining, so `grams_available` is only ever as accurate as the last
    time an admin updated it by hand - see routers/admin.py's /admin/colors
    page, which says this in the UI itself, not just here."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    enabled: bool = Field(default=True)
    rolls_available: int = Field(default=0)
    grams_available: float | None = Field(default=None)


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
    # Indexed (schema 6.3.0) for the global activity log's own filters
    # (routers/admin.py's activity_log_page) - date range on `at`, actor
    # substring doesn't use an index (LIKE '%...%' can't), action is an
    # exact-match dropdown that benefits from one.
    at: datetime = Field(default_factory=utcnow, index=True)
    actor: str
    action: str = Field(index=True)  # short verb: "submitted", "sliced", "slice_failed", "reslice_started", "queued", "approved", "rejected", "released", "done", "failed", "expired"
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
    # A uniform scale factor (1.0 = original size) applied to the model
    # before centering/slicing - see slicing/stl_to_3mf.build_3mf and
    # routers/user.py's reslice(). Always uniform (never per-axis), per
    # the user: "resize the object while maintaining the aspect ratio" -
    # there's no control here that could distort it.
    scale_factor: float = Field(default=1.0)
    # Rotation in degrees, applied in this exact order (X, then Y, then
    # Z, each about the fixed world axis - matching Three.js's own
    # BufferGeometry.rotateX/Y/Z, called in that order, on the client
    # preview side) - see slicing/stl_to_3mf.rotate_vertices, whose
    # matrix math has to stay in lockstep with the browser's, the same
    # invariant center_vertices()/showModel() already maintain for
    # centering. Set either by hand (free rotation on any axis) or
    # computed by "snap to surface" (pick a face, reorient it as the new
    # bottom) - either way, the result always ends up expressed as these
    # three angles, never a separately-stored quaternion.
    rotate_x: float = Field(default=0.0)
    rotate_y: float = Field(default=0.0)
    rotate_z: float = Field(default=0.0)

    # When this row was created (upload time) - NOT when it joined the
    # queue, which may be much later or never (see queued_at below). Used
    # to order a user's own submissions list and to age out abandoned
    # drafts (cleanup_drafts.py), not for queue fairness. Indexed (schema
    # 6.3.0) for the user dashboard's own date-range filter (filters.py).
    created_at: datetime = Field(default_factory=utcnow, index=True)
    # When submit_draft() actually moved this job into the queue - None
    # until then. This, not created_at, is what queue_position() orders
    # by: a draft someone sat on for hours before submitting must not cut
    # ahead of everyone who submitted straight away in the meantime.
    # Indexed (schema 6.3.0) for the admin queue/old-jobs date filters.
    queued_at: datetime | None = Field(default=None, index=True)
    reviewed_at: datetime | None = Field(default=None)
    reviewed_by_admin_id: int | None = Field(default=None, foreign_key="admin.id")
    admin_note: str | None = Field(default=None)
    released_at: datetime | None = Field(default=None)
    # Indexed (schema 6.3.0) for the admin finished-jobs date filter.
    finished_at: datetime | None = Field(default=None, index=True)
    # Why a 'failed' job failed - shown to the submitter, not just an
    # admin (see jobs.mark_finished). Always set automatically when the
    # background poller detects the failure itself ("cancelled at the
    # printer", etc. - see check_and_finish_active_print); required from
    # an admin marking one failed by hand, same as admin_note is required
    # on a manual reject. None for a 'done' job - this is specifically
    # about explaining a failure, not a general outcome note.
    failure_reason: str | None = Field(default=None)
    # A photo of the build plate, taken via the printer's camera the
    # moment an admin marks this job done or failed (see jobs.mark_finished)
    # - regardless of outcome, so both the submitter and an admin have a
    # visual record of what actually happened, not just a status word.
    # None if capture failed (camera/printer unreachable, etc.) - a
    # missing photo never blocks recording the print's own outcome, see
    # that function's docstring.
    photo_path: str | None = Field(default=None)

    # The exact Color.name selected at upload/edit time - a plain string
    # snapshot, NOT a foreign key to Color. None means "Any available"
    # (per the user, so an admin doesn't have to change filament for
    # every job). A snapshot, not a relation, on purpose: an admin
    # removing or renaming a color later must never silently change what
    # an already-submitted job says it was printed in - this job's own
    # history is what was actually selected, at the time it was
    # selected, full stop. See jobs.filament_status for how this is
    # looked up against the *current* Color table anyway, best-effort,
    # purely for the low-filament warning - that's a live check, not
    # something this snapshot itself needs to stay valid for. Indexed
    # (schema 6.3.0) for every job table's own color filter (filters.py).
    color_name: str | None = Field(default=None, index=True)
    # Grams of filament this job actually used, read straight out of the
    # sliced .makerbot's own meta.json (mbotmake's real computed
    # extrusion mass - see storage.read_makerbot_filament_g) the moment
    # slicing succeeds, same timing as duration_estimate_s above. None
    # until sliced at least once.
    filament_grams: float | None = Field(default=None)
