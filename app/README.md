# app

The queue3d web app: FastAPI + server-rendered Jinja2 templates + htmx for
the dynamic bits, SQLite for storage. See project memory
(`queue3d-purpose`) for the deployment context that shaped this - a shared
print queue for one admin-curated printer with many users.

**Status: the whole pipeline works, end to end, against real hardware.**
Auth (two account types), backups, and the whole job lifecycle - upload,
real slicing via `../slicing/`, admin review/approve/reject-with-note,
release, and now the actual printer upload/start - are all built and
verified. "Release" genuinely sends the file to the printer and only marks
a job `printing` once that upload succeeds; confirmed by watching a real
job go from submission through to the printer actually heating up and
printing. See "What release does" below for how.

## Why two account types

- **Users**: self-serve signup with just a name + PIN. Zero setup
  friction for users coming and going - no email,
  no password reset flow to build or support.
- **Admins**: real accounts, but deliberately no self-service signup -
  provisioned via `create_admin.py`, run directly on the server by whoever
  controls it. Approving/releasing print jobs is a position of trust over
  many users' shared printer time; open admin signup would defeat the
  review gate's whole purpose.

Sessions are signed cookies (Starlette's `SessionMiddleware`), independent
per role (`user_id` / `admin_id` keys) - logging in as one doesn't grant
the other, verified by test.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 create_admin.py        # one-time, creates the first admin account
python3 pair_printer.py        # one-time, pairs with the printer (needs someone at its dial)
uvicorn main:app --reload      # http://127.0.0.1:8000
```

`data/queue3d.db` (SQLite), `data/session_secret_key`, and
`data/printer_auth.json` are created on first run/pairing and gitignored -
the session key and printer token both persist across restarts so logins
and printer access survive a server restart without repeating either
one-time step.

## Deployment: zero internet access, by design

This runs on an isolated "island" LAN (Pi + printer wired to a router,
client devices join over wifi, nothing on that network ever reaches the
internet - see project memory `queue3d-deployment-network`). Consequences:

- Run with `--host 0.0.0.0` (not the `127.0.0.1` used above for local dev)
  so client devices on the LAN can actually reach it, e.g.:
  `uvicorn main:app --host 0.0.0.0 --port 8000`.
- `/docs` and `/redoc` are disabled (`docs_url=None` in `main.py`) - FastAPI's
  built-in interactive docs load their JS/CSS from `cdn.jsdelivr.net`,
  which is a dead link here and isn't needed for this app anyway.
- htmx is vendored in `static/htmx.min.js`, never CDN-loaded.
- Before adding any new frontend library/font/icon set/API integration,
  check whether it fetches anything from the internet at runtime and
  vendor it if so - there's no partial connectivity to fall back on here.
- Not yet set up: users need a way to find the Pi's address without
  DNS - a fixed IP or an mDNS hostname (`raspberrypi.local` via avahi) on
  the Pi would save typing/remembering a raw IP.

## Backups

`backup.py` copies the live SQLite DB (via SQLite's own online-backup API -
safe even while the app is running, unlike a raw file copy) plus the
archive/ directory (once it exists) to one of two rotating targets, chosen
automatically by day parity. See the module docstring for the full
reasoning - short version: submissions can happen anytime the location is
open, not just when an admin is physically there, so backups run
unattended on a daily cron rather than being tied to a visit.

Point it at the two backup USB mounts on the real Pi (both should stay
permanently plugged in - the rotation is decided in software, not by
swapping drives):

```bash
QUEUE3D_BACKUP_DIR_A=/mnt/backup-a QUEUE3D_BACKUP_DIR_B=/mnt/backup-b python3 backup.py
```

Wire it into cron for real deployment, e.g. daily at 3am:

```
0 3 * * * cd /path/to/app && QUEUE3D_BACKUP_DIR_A=/mnt/backup-a QUEUE3D_BACKUP_DIR_B=/mnt/backup-b .venv/bin/python3 backup.py
```

Without those env vars it defaults to `data/backups/{a,b}` under this app
directory - fine for local testing, not what you want on the real Pi.

The admin dashboard shows the most recent successful backup's timestamp,
flagged if it's more than 36 hours old (`backup.STALE_AFTER_HOURS`) - since
there's no internet for an alert email, this is the glance-and-verify
signal for whoever checks in.

## The job queue

State machine (confirmed with the user, see project memory
`queue3d-purpose` - don't drift from this without re-checking there):

```
submitted -> (sliced) -> queued
    -> approved (admin greenlit it, waiting its turn)
        -> printing (admin explicitly released it) -> done | failed
    -> rejected (admin declined, with a note - terminal)
submitted -> slice_failed (terminal - the submitter can resubmit)
```

Users submit directly into the one queue - there's no separate
pre-review gate before something counts as "in the queue." Only one job
can be `printing` at a time (enforced in `jobs.release`); `approve` and
`release` are separate actions since an admin may want to greenlit several
jobs while only one at a time can actually be on the printer.

Files move between three directories on one filesystem as a job's status
changes (see `storage.py` for why one filesystem, not physical drives per
stage): `data/scratch/` while a fresh upload is being sliced,
`data/queue/{job_id}.stl`+`.makerbot` for anything still active, moved to
`data/archive/` the moment a job goes `done`/`failed`/`rejected`. Files are
named by job id, never the submitter's original filename, to sidestep
collisions and path-traversal entirely.

### What release does

`jobs.release()` enforces the one-job-at-a-time rule, then calls
`printer.send_print_job()` (see `printer.py` - vendored from the validated
`../test-print/` proof-of-concept, since that directory isn't an
importable package) to actually upload the `.makerbot` and start the
print. The job is only marked `printing` in the database if that upload
genuinely succeeds - a failure (printer unreachable, not paired, rejected
mid-upload) leaves the job `approved` and shows the admin a clear error
instead, rather than claiming a print started that may not have. Verified
against the real printer: releasing an approved job actually made it heat
up and start printing.

Pairing is a one-time step (`pair_printer.py`, needs someone physically at
the printer's dial) that saves a long-lived access token to
`data/printer_auth.json`. If that token stops working (observed once
during testing after dismissing a printer error via its dial - unclear if
that specifically invalidates it or if it was coincidental), re-run
`pair_printer.py`; `QUEUE3D_PRINTER_HOST`/`QUEUE3D_PRINTER_PORT` env vars
override the printer's address if it's not at the default.

Not yet built: live print progress/status polling (the printer's own
`get_system_information` exists in the protocol but isn't wired up - see
`printer.py`'s `_dispatch` for where unsolicited status notifications
would need to be consumed) and detecting completion automatically -
`mark_done`/`mark_failed` are still a manual admin action for now.

## Layout

- `main.py` - app setup: session middleware, static files, the
  `AuthRedirect` -> real HTTP redirect exception handler, router mounting.
- `models.py` - `User`, `Admin`, `Job` (with `JobStatus`), `BackupRecord`
  tables (SQLModel).
- `db.py` - SQLite engine/session. One file, no separate DB server - this
  runs on one Pi next to one printer.
- `storage.py` - the scratch/queue/archive directory layout and file-moving
  helpers, plus reading a `.makerbot`'s slice-time duration estimate back
  out of its `meta.json`.
- `pipeline.py` - calls `../slicing/slice.py` as a subprocess (deliberately
  not imported - see the module docstring for why) to turn an uploaded STL
  into a `.makerbot`.
- `jobs.py` - queries (`jobs_for_user`, `active_jobs`, `queue_position`) and
  the actual state-transition logic (`approve`/`reject`/`release`/
  `mark_finished`), kept out of the routers so it's independently testable.
- `printer.py` - the printer's network protocol client (vendored from
  `../test-print/`, see its docstring for why not imported) and
  `send_print_job()`, the real hardware call behind `jobs.release()`.
- `pair_printer.py` - CLI for the one-time printer pairing step.
- `auth.py` - hashing (bcrypt, called directly - see note below) and the
  `require_user`/`require_admin` FastAPI dependencies that redirect to
  the right login page when not authenticated.
- `routers/user.py` - signup/login/logout/dashboard/upload.
- `routers/admin.py` - login/logout/dashboard (the queue view) and the
  approve/reject/release/mark_done/mark_failed actions.
- `create_admin.py` - CLI to provision an admin account.
- `templates/`, `static/htmx.min.js` - htmx is vendored locally rather than
  loaded from a CDN, since this may run on a network that restricts
  external domains and the whole point is it works reliably on the LAN.

## Note: bcrypt directly, not passlib

`passlib` (unmaintained since 2020) ships a self-test for an old bcrypt
wraparound bug that hashes a >72-byte string to detect it - current
`bcrypt` releases (>=4.1) now raise `ValueError` on inputs over 72 bytes
instead of silently truncating, which crashes that self-test on first use.
Calling `bcrypt.hashpw`/`bcrypt.checkpw` directly (see `auth.py`) sidesteps
the dead wrapper entirely - our secrets (PINs, passwords) are always well
under 72 bytes anyway, so there's no capability lost.
