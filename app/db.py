"""Database engine/session setup. SQLite - this runs on one Pi next to one
printer; a separate DB server would be pure overhead, and a single file is
trivial to back up."""

from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "queue3d.db"

# check_same_thread=False: FastAPI may use a different thread per request;
# we still only ever use one Session per request (see get_session), so this
# is safe.
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def _migrate_0_to_1(conn):
    """Job.submitted_at -> created_at, plus a new queued_at - the
    slice/submit-split schema change (see app/README.md's "Drafts and
    expiry"). Real incident, not a hypothetical: `create_all()` only
    creates tables that don't exist yet, so a database from before this
    change kept its old `submitted_at` column and had no `created_at`/
    `queued_at` at all - every query referencing either crashed the app
    right after login, on a database nobody had touched by hand."""
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


# Keyed by the version being upgraded FROM, so this reads as "how to get
# out of version N" - MIGRATIONS[0] takes a version-0 (or pre-versioning,
# which is treated the same - see init_db) database to version 1.
# CURRENT_SCHEMA_VERSION must always be len(MIGRATIONS): every migration
# added here bumps it by exactly one, in order, with nothing skipped.
MIGRATIONS = {
    0: _migrate_0_to_1,
}
CURRENT_SCHEMA_VERSION = len(MIGRATIONS)


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
    just once by hand, so upgrading to a new schema is "restart the app"
    rather than "remember to run the right script." A database that
    predates schemaversion entirely (anything from before this mechanism
    existed) is treated as version 0, same as an explicit version-0 row
    would be - there's no other database this could be, in practice, the
    one time that distinction would have mattered."""
    with engine.begin() as conn:
        version = 0
        if _table_exists(conn, "schemaversion"):
            row = conn.execute(text("SELECT version FROM schemaversion WHERE id=1")).first()
            if row is not None:
                version = row[0]
        # No schemaversion row means one of two things: a brand new
        # database (nothing to migrate - create_all() below builds every
        # table fresh from the current model definitions) or one old
        # enough to predate this mechanism entirely. Tell them apart by
        # whether `job` already exists: a new database doesn't have it
        # yet, an old one does. Only run migrations for the latter.
        if _table_exists(conn, "job"):
            while version < CURRENT_SCHEMA_VERSION:
                MIGRATIONS[version](conn)
                version += 1

    SQLModel.metadata.create_all(engine)  # creates schemaversion (and anything else new) if missing

    with engine.begin() as conn:
        # Upsert rather than a plain insert: a fresh database has no row
        # yet (inserts clean), an old one just migrated needs its version
        # recorded for the first time, and an already-current database's
        # update here is a harmless no-op.
        conn.execute(
            text(
                "INSERT INTO schemaversion (id, version) VALUES (1, :v) "
                "ON CONFLICT(id) DO UPDATE SET version=:v"
            ),
            {"v": CURRENT_SCHEMA_VERSION},
        )


def get_session():
    with Session(engine) as session:
        yield session
