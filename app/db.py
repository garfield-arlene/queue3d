"""Database engine/session setup. SQLite - this runs on one Pi next to one
printer; a separate DB server would be pure overhead, and a single file is
trivial to back up."""

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "queue3d.db"

# check_same_thread=False: FastAPI may use a different thread per request;
# we still only ever use one Session per request (see get_session), so this
# is safe.
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def init_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
