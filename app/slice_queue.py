"""Runs slicing one model at a time, in upload order, on one background
thread of its own.

Two OrcaSlicer runs at once compete for the Pi's memory and CPU, and a
real field session had one of two near-simultaneous uploads of the same
large model never finish. Queuing them also keeps waiting slices off
the web server's shared thread pool: each waiting slice there would hold
a thread that page requests need.

Also holds what the dashboard shows for a job that's still 'submitted'
(see describe()). This is in memory only - a restart empties it, and
jobs.fail_interrupted_slices() marks whatever was left 'submitted' as
failed at the next startup.
"""

import threading
import traceback
from collections import deque
from typing import Callable

_lock = threading.Condition()
_waiting: deque[tuple[int, Callable[[], None]]] = deque()
_current: int | None = None
_notes: dict[int, str] = {}
_worker: threading.Thread | None = None


def submit(job_id: int, work: Callable[[], None]) -> None:
    """Queues `work` (the whole slice for job_id) and returns at once."""
    global _worker
    with _lock:
        _waiting.append((job_id, work))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="slice-queue", daemon=True)
            _worker.start()
        _lock.notify()


def set_note(job_id: int, note: str) -> None:
    """What the dashboard shows while this job is slicing, e.g. which
    rotation retry is running."""
    with _lock:
        _notes[job_id] = note


def describe(job_id: int) -> str | None:
    """A short status for a 'submitted' job, or None if this queue
    doesn't know it (shown as plain "slicing")."""
    with _lock:
        if job_id == _current:
            return _notes.get(job_id, "slicing")
        for position, (waiting_id, _work) in enumerate(_waiting):
            if waiting_id == job_id:
                ahead = position + (1 if _current is not None else 0)
                return f"waiting to slice - {ahead} model{'s' if ahead != 1 else ''} ahead of it"
    return None


def is_slicing(job_id: int) -> bool:
    """True while this job's slice is the one running."""
    with _lock:
        return job_id == _current


def withdraw(job_id: int) -> bool:
    """Takes a job that hasn't started slicing yet out of the line, for
    deleting it. False if it's slicing right now - that slice can't be
    stopped part way, so the job has to wait until it finishes."""
    with _lock:
        if job_id == _current:
            return False
        for item in list(_waiting):
            if item[0] == job_id:
                _waiting.remove(item)
        return True


def _run() -> None:
    global _current
    while True:
        with _lock:
            while not _waiting:
                _lock.wait()
            job_id, work = _waiting.popleft()
            _current = job_id
        try:
            work()
        except Exception:
            # work() records its own failures on the job; this only keeps
            # one unexpected error from stopping every later slice.
            traceback.print_exc()
        finally:
            with _lock:
                _current = None
                _notes.pop(job_id, None)
