"""Database engine/session setup. SQLite - this runs on one Pi next to one
printer; a separate DB server would be pure overhead, and a single file is
trivial to back up."""

import os
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from version import APP_VERSION

# Defaults to a local dev directory; the real deployment points this at a
# dedicated external drive instead (QUEUE3D_DATA_DIR, set in
# deploy/queue3d.service) - see project memory queue3d-app-progress for
# why: the microSD boots the OS, one flash drive holds the live data
# (this - scratch/queue/archive all live under DATA_DIR too, see
# storage.py), the other two rotate as backups (see backup.py).
DATA_DIR = Path(os.environ.get("QUEUE3D_DATA_DIR", Path(__file__).resolve().parent / "data"))

# Only enforced when QUEUE3D_DATA_DIR is explicitly set - a real deployment
# never wants this directory to silently exist as an ordinary (empty)
# folder on the SD card just because the external drive happened to be
# unplugged or not yet mounted at boot. Without this check, that failure
# mode wouldn't look like a failure at all: the app would just start up
# fine against a brand new, empty database, quietly discarding every real
# job/user/setting on the actual drive until someone noticed the queue
# looked wrong. Fail loudly at startup instead - see
# deploy/queue3d.service's RequiresMountsFor for the systemd-level version
# of this same guard.
if "QUEUE3D_DATA_DIR" in os.environ and not os.path.ismount(DATA_DIR):
    raise RuntimeError(
        f"QUEUE3D_DATA_DIR={DATA_DIR} is set but is not actually a mounted "
        "filesystem right now - refusing to start rather than silently "
        "creating a fresh, empty database on local disk instead of using "
        "the real external drive. Check it's plugged in and mounted "
        "(see deploy/README.md)."
    )

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "queue3d.db"

# check_same_thread=False: FastAPI may use a different thread per request;
# we still only ever use one Session per request (see get_session), so this
# is safe.
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def _migrate_to_2_1_0(conn):
    """Job.submitted_at -> created_at, plus a new queued_at - the
    slice/submit-split schema change (see app/README.md's "Drafts and
    expiry"). Real incident, not a hypothetical: `create_all()` only
    creates tables that don't exist yet, so a database from before this
    change kept its old `submitted_at` column and had no `created_at`/
    `queued_at` at all - every query referencing either crashed the app
    right after login, on a database nobody had touched by hand. (This
    shipped without a version bump at the time, which is the whole reason
    the version-keyed scheme below exists now - see MIGRATIONS.)"""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(job)")).fetchall()}
    if "created_at" not in cols and "submitted_at" in cols:
        conn.execute(text("ALTER TABLE job RENAME COLUMN submitted_at TO created_at"))
    if "queued_at" not in cols:
        conn.execute(text("ALTER TABLE job ADD COLUMN queued_at DATETIME"))
        # Backfill for jobs that predate this migration: under the old
        # model, a successful slice meant immediately queued - there was
        # no separate draft/submit step yet - so created_at is the
        # closest true record of when each of these was actually queued.
        # Anything never queued (an old-model failed/incomplete attempt)
        # is left NULL, matching what a real never-submitted draft looks
        # like today.
        conn.execute(
            text(
                "UPDATE job SET queued_at = created_at "
                "WHERE queued_at IS NULL "
                "AND status IN ('queued', 'approved', 'printing', 'done', 'failed', 'rejected')"
            )
        )


def _migrate_to_2_4_0(conn):
    """New Job.photo_path column - a photo of the build plate, captured
    via the printer's camera when a job is marked done/failed (see
    jobs.mark_finished, app/README.md's "Printer camera"). Purely
    additive (a new nullable column, nothing renamed or backfilled), but
    still needs a real ALTER TABLE - create_all() only creates tables
    that don't exist yet, it never alters an existing one, same as every
    other entry in this dict."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(job)")).fetchall()}
    if "photo_path" not in cols:
        conn.execute(text("ALTER TABLE job ADD COLUMN photo_path VARCHAR"))


def _migrate_to_2_5_0(conn):
    """JobEvent.job_id becomes nullable - the activity log now also
    records account lifecycle actions (signup, disable, re-enable,
    delete) that aren't tied to any one job (see that model's docstring).
    SQLite can't relax a column's NOT NULL constraint with a plain ALTER
    TABLE, so this rebuilds the table: a new one with the relaxed schema,
    every existing row copied across unchanged (a job_id that was never
    NULL before is trivially still valid once NULL is merely *allowed*),
    the old one dropped, the new one renamed into its place."""
    info = conn.execute(text("PRAGMA table_info(jobevent)")).fetchall()
    job_id_col = next((c for c in info if c[1] == "job_id"), None)
    if job_id_col is None or job_id_col[3] == 0:  # column gone, or already nullable
        return
    conn.execute(
        text(
            "CREATE TABLE jobevent_new ("
            "id INTEGER NOT NULL, "
            "job_id INTEGER, "
            "at DATETIME NOT NULL, "
            "actor VARCHAR NOT NULL, "
            "action VARCHAR NOT NULL, "
            "detail VARCHAR NOT NULL, "
            "PRIMARY KEY (id), "
            "FOREIGN KEY(job_id) REFERENCES job (id))"
        )
    )
    conn.execute(
        text("INSERT INTO jobevent_new (id, job_id, at, actor, action, detail) "
             "SELECT id, job_id, at, actor, action, detail FROM jobevent")
    )
    conn.execute(text("DROP TABLE jobevent"))
    conn.execute(text("ALTER TABLE jobevent_new RENAME TO jobevent"))
    conn.execute(text("CREATE INDEX ix_jobevent_job_id ON jobevent (job_id)"))


def _migrate_to_3_1_0(conn):
    """New User.theme/Admin.theme columns - a per-account UI theme
    preference (see themes.py, templates_env.current_theme()) that
    follows an account across logins/devices, per the user ("I want a
    settings page for the users to select their own theme that will
    persist across logins"). Purely additive (both nullable, `None`
    meaning "no preference set, use the default"), but still needs a real
    ALTER TABLE on each table - same as every other purely-additive entry
    here."""
    for table in ("user", "admin"):
        cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
        if "theme" not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN theme VARCHAR"))


def _migrate_to_3_2_0(conn):
    """New User.theme_mode/Admin.theme_mode columns - light/dark, a
    separate axis from theme (see themes.py), added the same way and for
    the same reason as theme itself in 3.1.0 just above. Purely additive
    (both nullable, `None` meaning "no preference set, use the
    default")."""
    for table in ("user", "admin"):
        cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
        if "theme_mode" not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN theme_mode VARCHAR"))


def _migrate_to_3_3_0(conn):
    """New User.failed_login_attempts/locked_until and
    Admin.failed_login_attempts/locked_until columns - login
    rate-limiting (see auth.py). Purely additive; failed_login_attempts
    defaults to 0 (not NULL) so existing rows read as "no failed
    attempts on record" rather than needing a None-check everywhere this
    gets incremented."""
    for table in ("user", "admin"):
        cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
        if "failed_login_attempts" not in cols:
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0")
            )
        if "locked_until" not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN locked_until DATETIME"))


def _migrate_to_3_4_0(conn):
    """New Job.failure_reason column - failure reasons shown to the
    submitter, not just admins (see jobs.mark_finished). Purely
    additive/nullable; every existing 'failed' job simply reads as "no
    reason given" (see _jobs_table.html) rather than needing a backfill -
    there's no real reason to reconstruct for those, only to record one
    going forward."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(job)")).fetchall()}
    if "failure_reason" not in cols:
        conn.execute(text("ALTER TABLE job ADD COLUMN failure_reason VARCHAR"))


def _migrate_to_4_4_0(conn):
    """New Settings.display_timezone column - an admin-configurable IANA
    zone name every timestamp in the app is shown in (see
    templates_env.local_time), rather than the unlabeled UTC every
    display was hardcoded to before. Defaults to 'UTC' - the exact
    values every existing timestamp is already stored as and was already
    (silently) displayed as, so this changes nothing for a deployment
    that never visits the new settings field."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(settings)")).fetchall()}
    if "display_timezone" not in cols:
        conn.execute(
            text("ALTER TABLE settings ADD COLUMN display_timezone VARCHAR NOT NULL DEFAULT 'UTC'")
        )


def _migrate_to_4_5_0(conn):
    """New Settings.old_job_threshold_days column - the admin-configurable
    age threshold splitting the normal queue view from /admin/jobs/old
    (see jobs.is_old_job). Defaults to 30, matching the to-do list's own
    example value; purely additive."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(settings)")).fetchall()}
    if "old_job_threshold_days" not in cols:
        conn.execute(
            text("ALTER TABLE settings ADD COLUMN old_job_threshold_days INTEGER NOT NULL DEFAULT 30")
        )


def _migrate_to_5_5_0(conn):
    """New Job.scale_factor column - the first of the model
    orientation/sizing controls (resize, maintaining aspect ratio - see
    jobs.start_reslice). Defaults to 1.0 (unscaled), so an existing job
    that's never touched this reads as "print at its original size,"
    exactly what happened before this column existed. Purely additive."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(job)")).fetchall()}
    if "scale_factor" not in cols:
        conn.execute(text("ALTER TABLE job ADD COLUMN scale_factor FLOAT NOT NULL DEFAULT 1.0"))


def _migrate_to_5_6_0(conn):
    """New Job.rotate_x/rotate_y/rotate_z columns (degrees) - part two of
    the model orientation/sizing controls (free rotation + "snap to
    surface" - see jobs.start_reslice, slicing/stl_to_3mf.rotate_vertices).
    Defaults to 0.0 (no rotation), so an existing job reads as "print at
    its original orientation," exactly what happened before these
    columns existed. Purely additive."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(job)")).fetchall()}
    for col in ("rotate_x", "rotate_y", "rotate_z"):
        if col not in cols:
            conn.execute(text(f"ALTER TABLE job ADD COLUMN {col} FLOAT NOT NULL DEFAULT 0.0"))


def _migrate_to_6_2_0(conn):
    """New Job.color_name/filament_grams columns - the color-selection and
    best-effort filament-inventory feature (see models.Color,
    jobs.filament_status). The new `color` table itself needs no migration
    here at all - create_all() below already creates any missing table
    from scratch, it just can't ALTER an existing one, which is the only
    reason any of these functions exist. Both new columns purely additive/
    nullable: an existing job simply reads as "no color recorded" and
    "filament usage unknown" - exactly what was already implicitly true
    of it before these columns existed, nothing to backfill."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(job)")).fetchall()}
    if "color_name" not in cols:
        conn.execute(text("ALTER TABLE job ADD COLUMN color_name VARCHAR"))
    if "filament_grams" not in cols:
        conn.execute(text("ALTER TABLE job ADD COLUMN filament_grams FLOAT"))


def _migrate_to_6_3_0(conn):
    """New indexes for the job/log filtering feature (filters.py) - per
    the user, filters everywhere the underlying data exists, across
    several tables that can realistically grow over years of school use.
    Unlike every migration above, this doesn't add/rename a column -
    `CREATE INDEX IF NOT EXISTS` works directly against an existing
    SQLite table with no rebuild needed, so this is here only because
    create_all() below has the exact same limitation for indexes as it
    does for columns: it happily creates every index a *new* database
    needs from the current model definitions, but never retrofits one
    onto a table that already exists. Named to match SQLAlchemy's own
    default index-naming convention (ix_<table>_<column>) exactly, so a
    brand-new database's create_all()-generated indexes and an existing
    database's migration-created ones end up identical - nothing here is
    a "different index with the same job," just the same one arriving by
    two different paths depending on how old the database is."""
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_job_color_name ON job (color_name)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_job_created_at ON job (created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_job_queued_at ON job (queued_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_job_finished_at ON job (finished_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobevent_at ON jobevent (at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobevent_action ON jobevent (action)"))


def _migrate_to_6_4_0(conn):
    """New Admin.unremovable column - see that field's own docstring in
    models.py for why it exists (the web UI for one admin creating
    another - routers/admin.py's admins_page - is what first makes "can
    this admin ever be deleted" a real question). Backfilled to 1/True
    for every admin row that already exists at migration time, not left
    at the column's own False default the way a purely additive column
    normally would be here: every one of those rows was necessarily
    created via create_admin.py, since the web UI this migration ships
    alongside is the *only* other way an Admin row can ever come to
    exist - there was no such thing as a non-CLI-created admin before
    this exact migration runs. Per the user, directly: "mark the admin
    created from the cmd we just did as unremovable" - backfilling every
    pre-existing row this way satisfies that literally, with no need to
    know which username(s) to single out by hand, and stays consistent
    with the same rule create_admin.py itself now applies going forward.
    A brand new admin created *after* this migration, through the new
    web UI, still gets the column's real default (False) via create_all()
    - only rows that predate the UI's existence get backfilled here."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(admin)")).fetchall()}
    if "unremovable" not in cols:
        conn.execute(text("ALTER TABLE admin ADD COLUMN unremovable BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("UPDATE admin SET unremovable = 1"))


# Keyed by the app VERSION a schema change shipped in, not a separate
# incrementing number - per the user, a schema change should always come
# with a version bump, so there's exactly one number to keep track of,
# not two that can drift apart (which is exactly what happened: the fix
# above shipped without bumping VERSION at the time, so nothing recorded
# that this database needed it). Applied in ascending version order,
# regardless of dict insertion order - see _version_tuple.
#
# Adding a future migration: bump app/VERSION, write a new function next
# to _migrate_to_2_1_0, and add it here keyed by that same new version.
MIGRATIONS = {
    "2.1.0": _migrate_to_2_1_0,
    "2.4.0": _migrate_to_2_4_0,
    "2.5.0": _migrate_to_2_5_0,
    "3.1.0": _migrate_to_3_1_0,
    "3.2.0": _migrate_to_3_2_0,
    "3.3.0": _migrate_to_3_3_0,
    "3.4.0": _migrate_to_3_4_0,
    "4.4.0": _migrate_to_4_4_0,
    "4.5.0": _migrate_to_4_5_0,
    "5.5.0": _migrate_to_5_5_0,
    "5.6.0": _migrate_to_5_6_0,
    "6.2.0": _migrate_to_6_2_0,
    "6.3.0": _migrate_to_6_3_0,
    "6.4.0": _migrate_to_6_4_0,
}


def _version_tuple(v: str) -> tuple[int, ...]:
    """(2, 1, 0) from "2.1.0", ignoring a "-dev"-style suffix - just
    enough to order two of this project's own version strings against
    each other, not a general-purpose semver parser."""
    return tuple(int(part) for part in v.split("-", 1)[0].split("."))


def _table_exists(conn, name: str) -> bool:
    return (
        conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": name},
        ).first()
        is not None
    )


def init_db():
    """Creates any missing tables, then runs whichever migrations (above)
    this database hasn't had applied yet - checked on every startup, not
    just once by hand, so upgrading to a new schema is "bump VERSION,
    pull, restart" rather than "remember to run the right script.\""""
    with engine.begin() as conn:
        stored_version = None
        if _table_exists(conn, "schemaversion"):
            row = conn.execute(text("SELECT version FROM schemaversion WHERE id=1")).first()
            if row is not None:
                stored_version = row[0]
        # This table briefly stored a small integer (1) rather than a
        # version string, before the scheme above existed - translate the
        # one real value that shipped that way to the version it actually
        # corresponds to, so the comparison below works the same
        # regardless of which era of this table wrote it.
        if stored_version in (0, 1, "0", "1"):
            stored_version = "2.1.0" if stored_version in (1, "1") else None

        # No stored version at all means one of two things: a brand new
        # database (nothing to migrate - create_all() below builds every
        # table fresh from the current model definitions) or one old
        # enough to predate this mechanism entirely. Tell them apart by
        # whether `job` already exists: a new database doesn't have it
        # yet, an old one does. Only run migrations for the latter,
        # treating a missing version as older than anything in the list.
        if _table_exists(conn, "job"):
            pending = sorted(
                (
                    v
                    for v in MIGRATIONS
                    if stored_version is None or _version_tuple(v) > _version_tuple(stored_version)
                ),
                key=_version_tuple,
            )
            for v in pending:
                MIGRATIONS[v](conn)

    SQLModel.metadata.create_all(engine)  # creates schemaversion (and anything else new) if missing

    with engine.begin() as conn:
        # Upsert rather than a plain insert: a fresh database has no row
        # yet (inserts clean), an old one just migrated needs its version
        # recorded for the first time, and an already-current database's
        # update here is a harmless no-op. Always the *running app's*
        # version, not just the latest migration key - most version bumps
        # won't have a schema change at all, and this still needs to
        # reflect that the current code has looked at this database.
        conn.execute(
            text(
                "INSERT INTO schemaversion (id, version) VALUES (1, :v) "
                "ON CONFLICT(id) DO UPDATE SET version=:v"
            ),
            {"v": APP_VERSION},
        )


def get_session():
    with Session(engine) as session:
        yield session
