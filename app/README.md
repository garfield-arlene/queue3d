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

Any admin can manage user accounts from `/admin/users`
(`routers/admin.py`'s user-management routes,
`templates/admin_users.html`): disable/re-enable or permanently delete,
individually or all at once. `User.disabled` is checked fresh from the DB
on every request (`auth.get_current_user`), not just at login - disabling
someone logs them out immediately even if they already have an open
session. Deleting is blocked (`jobs.user_has_active_jobs`) while that user
still has a job that isn't `done`/`failed`/`rejected` yet, naming who's
blocking it; "delete all" is all-or-nothing, refusing entirely rather than
partially deleting if anyone's blocked. This covers users only, not other
admins - see README.md's To do list for why that's a separate, harder
question (mainly: what stops an admin from locking everyone out by
disabling/deleting every admin account, including their own).

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

## 3D preview

Two different views, both in `static/preview.js` (Three.js, vendored
locally - see the deployment section above on why):

- **Pre-submission** (on the upload form): renders a chosen file entirely
  client-side - parsed straight from the browser's `File` object via
  `FileReader`, no server round-trip - on a grid sized to the printer's
  real build plate, with orbit/zoom. Flags a model that exceeds the build
  volume. Since nothing has been sliced yet, there's no supports or
  duration estimate to show here.
- **Job preview** (`/jobs/{id}/preview`, a "View 3D" link on both the
  user's and the admin's job list): the same viewer, but loading the
  already-sliced job's model from the server
  (`GET /jobs/{id}/model.stl`), plus generated support material overlaid
  as orange line segments if the job was sliced with supports enabled
  (`GET /jobs/{id}/supports.json`). Both endpoints check that the
  requester is either the job's owner or an admin (`routers/jobs.py`);
  anyone else is redirected to log in, whether or not the job exists.

Enabling supports is a checkbox on the upload form, threaded through
`pipeline.run_slice()` into an `enable_support` override on the OrcaSlicer
profile for that one job (`slicing/stl_to_3mf.build_3mf`'s `overrides`
param) - it doesn't touch the shared default profile. A support style
dropdown (`routers/user.py`'s `SUPPORT_STYLES` - Automatic/Grid/Snug/
Organic/Tree-hybrid/Tree-slim, matching OrcaSlicer's own `support_style`
values) goes along with it.

**Support style is pickier than it looks - read this before changing
either list.** OrcaSlicer silently ignores a `support_style` that isn't
compatible with the currently-active `support_type` (our profile defaults
to `tree(auto)`), rather than erroring - so picking a style has to co-set
the type, or the choice does nothing and nobody's told. `slicing/slice.py`'s
`SUPPORT_STYLE_TYPE` maps each style to the type it actually needs; every
entry in both that mapping and `SUPPORT_STYLES` was confirmed by slicing
`models/overhang_test.stl` and diffing the real gcode output, not guessed.
One entry is deliberately *not* offered: "organic" is PrusaSlicer's name
for the plain tree-support algorithm itself (not a further variant
alongside hybrid/slim), so under `tree(auto)` with no other style set it
correctly matches "Automatic" - that's the right answer once you know what
"organic" means here, not a sign it's broken.

**Where the support geometry actually comes from:** the final `.makerbot`
file has no concept of "this move was a support" - `mbotmake`'s conversion
collapses every move into an undifferentiated command (see
`slicing/mbotmake/mbotmake`). OrcaSlicer's *intermediate gcode* does mark
feature types with `;TYPE:X` comments, though, including `;TYPE:Support`
and `;TYPE:Support interface` (confirmed by slicing
`slicing/models/overhang_test.stl`, a shape deliberately designed to need
support). `slice.py --gcode-out` preserves that gcode (normally thrown
away once converted), and `app/supports.py` parses it for support-tagged
extrusion moves into a simplified list of line segments - deliberately not
a faithful toolpath reproduction or a per-layer scrub control (see
README.md's To do list for why that's out of scope for now).

Downsampling for that has to happen **by whole layer, never by individual
segment** - an earlier version sampled every Nth segment out of one flat
chronological list spanning the whole print, which for a support-dense
model produces isolated, disconnected fragments scattered across many
unrelated layers: each one too short to read as a line at normal zoom
(renders as a dot), with no visual relationship to its neighbors (a real
bug, caught by the user: "many dots... don't seem to have anything to do
with supporting a part of the model"). `supports.py` now groups by layer
(Z) first and drops whole layers, never partial ones.

**Render every real layer whenever it fits the budget - don't thin
pre-emptively.** A second real bug, on a genuinely complex model (a
detailed 262k-triangle ship hull): the layer-selection logic started by
thinning to a target count *before* even checking whether the full set
would fit, so it stayed artificially sparse even when there was budget to
spare. Two consequences, both confirmed against that model, not assumed:
skipped layers can jump between two entirely different, unrelated support
towers (a detailed model has many separate overhangs, each with its own
column near the bed, converging higher up - centroid position jumped
30-45mm between "adjacent" kept layers before the fix), and rendering as
thin lines rather than solid tubes made even correctly-placed supports
look like a scatter of dots rather than material (a UX clarification from
the user, not a data problem: PrusaSlicer's organic supports really are
individual rings stacked on each other, but its *rendering* shows their
sides as gap-free). Fixed by rendering tubes (`preview.js`'s
`SUPPORT_TUBE_RADIUS`, InstancedMesh so it stays cheap per-instance) sized
comparably to a real layer height, and by fixing `supports.py` to try
every real layer first, only thinning via `MAX_SEGMENTS` as a last resort
for a pathological case - confirmed this model's real 310 layers (0.2mm
apart) all fit comfortably under it. Worth watching on lower-power
hardware: full density here means several million triangles just for
supports on a model this complex.

**A third, more fundamental bug turned up after both of those were fixed:
OrcaSlicer was silently rotating the model during slicing.** The model and
its own generated supports still looked perfectly aligned in isolation, but
disagreed with `preview.js`'s render of the raw uploaded STL - a rotation
mismatch, not the translation/rendering issues above (visible as the model
and its supports pointing in visibly different headings in the live
preview). Root cause: OrcaSlicer's CLI defaults `--arrange` and `--orient`
to "auto" unless told otherwise, and reoriented the single object on the
plate during `--slice` even though nothing asked it to - confirmed by
comparing the raw STL's own bounding box (198mm x 37mm, a long thin hull)
against the sliced gcode's (came out ~147mm x ~148mm, nearly square - only
rotation does that, translation can't). Fixed in `slicing/slice.py` by
passing `--arrange 0 --orient 0` explicitly: we already center the model
onto the bed ourselves (`stl_to_3mf.center_vertices`) and every profile
here is one plate/one object, so auto-arrange/orient never had anything
useful to do. **Lesson for next time a "generated alongside the model"
artifact needs verifying: checking it against the model pulled from the
*same* output only proves internal self-consistency, not correctness -**
both were produced by whatever transform OrcaSlicer chose, so of course
they agreed with each other. The only check that catches a systematic
transform bug is comparing against an independent third source (here: the
original STL's own dimensions) directly.

## Layout

- `main.py` - app setup: session middleware, static files, the
  `AuthRedirect` -> real HTTP redirect exception handler, router mounting.
- `templates_env.py` - the one shared `Jinja2Templates` instance every
  router renders through (rather than each router making its own, as
  before), so a Jinja global set once - `APP_VERSION`, read from the
  `VERSION` file - reaches every template. Every page extends
  `templates/base.html`, which is what actually prints the footer.
- `models.py` - `User`, `Admin`, `Job` (with `JobStatus`), `BackupRecord`
  tables (SQLModel).
- `db.py` - SQLite engine/session. One file, no separate DB server - this
  runs on one Pi next to one printer.
- `storage.py` - the scratch/queue/archive directory layout and file-moving
  helpers, plus reading a `.makerbot`'s slice-time duration estimate back
  out of its `meta.json`.
- `pipeline.py` - calls `../slicing/slice.py` as a subprocess (deliberately
  not imported - see the module docstring for why) to turn an uploaded STL
  into a `.makerbot`, optionally with supports enabled and the intermediate
  gcode preserved for `supports.py`.
- `supports.py` - parses that gcode into the simplified support-material
  line segments shown in the 3D preview (see "3D preview" above).
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
- `routers/admin.py` - login/logout/dashboard (the queue view), the
  approve/reject/release/mark_done/mark_failed actions, and user account
  management (`/admin/users`).
- `routers/jobs.py` - serves a job's model/supports and the 3D preview
  page, usable by either the job's owner or any admin (not role-specific
  like the two routers above).
- `create_admin.py` - CLI to provision an admin account.
- `templates/`, `static/htmx.min.js`, `static/vendor/three/` - htmx and
  Three.js are both vendored locally rather than loaded from a CDN, since
  this may run on a network that restricts external domains and the whole
  point is it works reliably on the LAN.

## Note: bcrypt directly, not passlib

`passlib` (unmaintained since 2020) ships a self-test for an old bcrypt
wraparound bug that hashes a >72-byte string to detect it - current
`bcrypt` releases (>=4.1) now raise `ValueError` on inputs over 72 bytes
instead of silently truncating, which crashes that self-test on first use.
Calling `bcrypt.hashpw`/`bcrypt.checkpw` directly (see `auth.py`) sidesteps
the dead wrapper entirely - our secrets (PINs, passwords) are always well
under 72 bytes anyway, so there's no capability lost.
