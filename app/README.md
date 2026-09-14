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

## Database migrations

No framework (Alembic, etc.) - deliberately, for something this small - so
`db.py`'s `init_db()` (called on every startup, `main.py`'s
`on_event("startup")`) does its own minimal version check instead. The
schema version *is* the app version (`VERSION`, `version.py`) - not a
second, separately-incrementing number - per the user: a schema change
should always come with a version bump, so there's exactly one number to
keep track of, not two that can quietly drift apart. **This is a real
policy, not just a mechanism: touching a table's columns and bumping
`VERSION` are the same commit, always.** The first time this schema
changed, it didn't - which is the entire reason this section and
`schemaversion` exist at all.

1. Read the version stored in `schemaversion` (a single row -
   `models.SchemaVersion`; briefly a small integer before this scheme
   existed, transparently translated to the version string it actually
   corresponds to). No row/table yet, but `job` already exists -> an old
   database that predates this mechanism, treated as older than
   everything in `MIGRATIONS`. No `job` table either -> genuinely fresh,
   nothing to migrate.
2. Run whichever entries in `db.MIGRATIONS` (a `{version: fn}` dict, each
   keyed by the app version it shipped in) are newer than the stored
   version, oldest first.
3. `SQLModel.metadata.create_all()` - creates any table that's still
   missing (including `schemaversion` itself, and any brand new table a
   migration didn't need to touch, like `Settings` was when it first
   showed up).
4. Record the *running app's* current `VERSION` - not just the latest
   migration key, since most version bumps won't have a schema change at
   all, and this still needs to reflect that the current code has looked
   at this database.

**Why this exists - a real incident, not foresight:** `create_all()` only
ever creates tables that don't exist yet; it never alters an existing one.
The slice/submit-split change (see "Drafts and expiry" below) renamed
`Job.submitted_at` to `created_at` and added `queued_at`, without a
version bump alongside it - on a database from before that change,
neither existed under their new names, and every query referencing
either crashed the app immediately after login, on a real running dev
deployment nobody had touched by hand. `db.MIGRATIONS["2.1.0"]` is that
specific fix (rename the column, add the new one, backfill `queued_at`
for already-queued jobs from their old `created_at`, since under the old
model a successful slice meant immediately queued - there was no separate
submit step yet to record a truer timestamp for) - tagged with the
version it should have shipped alongside the first time.

Checked on every startup rather than a one-time manual step, per the
user - upgrading this app is "bump `VERSION`, pull, restart," not "pull,
restart, and remember which script to run and whether you already ran
it." Verified by testing all the cases that matter, not just the one that
broke: a fresh database (creates the current schema outright, migrations
are a no-op), the actual pre-fix database recovered from the real
incident (migrates, backfills, and lands on the current version), a
database still carrying the brief legacy-integer version value
(translated and caught up correctly), and re-running `init_db()` against
an already-current database (no-ops cleanly, safe on every startup
indefinitely). Confirmed against the user's own real dev database
directly (not just isolated copies) both times: its `uvicorn --reload`
picked up the code change on its own and self-migrated - note that
`--reload` only watches `.py` files, so bumping `VERSION` alone needs an
actual restart (or any trivial `.py` save) to be picked up, unlike a code
change.

**Adding a future migration:** bump `VERSION`, write a new
`_migrate_to_<that version>(conn)` function next to `_migrate_to_2_1_0`,
and add it to `MIGRATIONS` keyed by that same version string. Test it the
same way - fresh, an old real database, and re-running against an
already-migrated one.

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
submitted -> sliced -> queued (an explicit "submit" action)
    -> approved (admin greenlit it, waiting its turn)
        -> printing (admin explicitly released it) -> done | failed
    -> rejected (admin declined, with a note - terminal)
submitted -> slice_failed (can retry: re-slice in place, or let it expire)
{submitted, sliced, slice_failed} -> expired (never submitted, past the
    admin-configured age threshold - see "Drafts and expiry" below)
```

Users submit directly into the one queue - there's no separate
*admin* pre-review gate before something counts as "in the queue." But
slicing and submitting *to* that queue are two distinct, explicit actions
on the user's own side, not one combined step - see "Drafts and expiry"
right below for why and how. Only one job can be `printing` at a time
(enforced in `jobs.release`); `approve` and `release` are separate actions
since an admin may want to greenlit several jobs while only one at a time
can actually be on the printer.

Files move between three directories on one filesystem as a job's status
changes (see `storage.py` for why one filesystem, not physical drives per
stage): `data/scratch/` for anything not yet submitted (`models.
DRAFT_STATUSES` - a draft can now sit here indefinitely, not just
briefly), `data/queue/{job_id}.stl`+`.makerbot` for anything actually in
the queue (`models.QUEUE_STATUSES`), moved to `data/archive/` the moment a
job reaches any terminal state (`models.TERMINAL_STATUSES` -
`done`/`failed`/`rejected`/`expired`). Files are named by job id, never
the submitter's original filename, to sidestep collisions and
path-traversal entirely.

### Drafts and expiry

Uploading used to both slice a model *and* commit it to the shared queue
in one step - iterating on support settings meant spamming the
admin-visible queue with abandoned attempts just to preview a different
style. Slicing and submitting are now two distinct, explicit actions:

- Uploading creates a `submitted` job and slices it in the background (see
  "Upload and slicing progress" below); success lands on `sliced`, not
  `queued`. A `sliced` (or `slice_failed`) job is a **draft**: private to
  its own user, invisible to admins, not counted in the queue, sitting in
  `data/scratch/` for as long as it stays one.
- **Editing a draft** (`GET /jobs/{id}/edit`, `templates/job_edit.html`) -
  "Edit" on the dashboard for any draft row - is meant to feel like
  picking up the upload flow again with this model still loaded: the same
  3D preview as the job-preview page (model + its currently selected
  support material overlaid), plus the support settings as an editable
  form right there, pre-filled with whatever's currently set. Deliberately
  not the raw checkbox+dropdown+button that used to sit inline in the
  dashboard's row - editing settings is its own real step, not a table
  action, and this is where any future editing controls belong too (a
  resize, say) since it's already the "load the model and its settings
  back up" page. While a (re-)slice is running the page shows a spinner
  and reloads itself every couple seconds until it's done - a plain
  reload, not htmx swapping the DOM, because the live Three.js scene on
  this page keeps its state tied to the *current* preview container; a
  partial swap would leave that state pointing at a detached element
  instead of reinitializing, where a full reload just starts fresh.
  Visiting this page for a job that isn't a draft any more (submitted,
  or - a stale link - since expired) redirects to the dashboard instead
  of showing an edit form for something it can no longer apply to.
- `jobs.start_reslice` + `slice_and_update`, driven from that edit page,
  let a draft be re-sliced with different settings, reusing the same
  already-uploaded file - no new upload needed - as many times as the
  user wants (`POST /jobs/{id}/reslice`, redirecting back to the same
  edit page either way, success or a settings error, so iterating on
  settings stays a loop on one page).
- `jobs.submit_draft` is the explicit "submit to queue" action, reachable
  from both the dashboard row and the edit page
  (`POST /jobs/{id}/submit`): moves the draft's files from `scratch/` to
  `queue/`, sets `queued_at`, and only *then* does it become admin-visible
  (`jobs.active_jobs`, an explicit `QUEUE_STATUSES` allow-list, not just
  "not terminal" - a draft is also not terminal, so that distinction has
  to be explicit now) and counted in `queue_position`.
- `queue_position` orders by `queued_at`, deliberately **not**
  `created_at` (when the file was first uploaded) - someone who sits on a
  sliced draft for hours before submitting must not cut ahead of everyone
  who submitted right away in the meantime. `created_at` still orders a
  user's own submissions list, and is what draft expiry (below) measures
  against.
- **What happens to a draft nobody ever submits** - the open question this
  feature originally raised: per the user, it auto-expires after an
  admin-configurable number of days (`Settings.draft_expiry_days`,
  `/admin/settings`), not left as permanent clutter and not a fixed
  constant either, since there's no one right threshold for every
  deployment's traffic and storage. `cleanup_drafts.py` (same run-from-cron
  pattern as `backup.py` - see "Backups" above) finds every draft
  (including a `submitted` one stuck mid-slice, as a safety net for a
  crashed background task) older than that threshold and expires it,
  moving its files to `archive/` the same way a finished job's are - an
  expired draft is *gone from scratch/*, not deleted outright, consistent
  with the rule that nothing this app finishes with just disappears.

  ```
  0 4 * * * /path/to/.venv/bin/python3 /path/to/app/cleanup_drafts.py
  ```

Verified live (Playwright against an isolated instance, not just read as
correct): a freshly-sliced job lands on `sliced` and is absent from the
admin queue view; re-slicing with different settings updates the draft in
place and clears a stale error/supports path from a previous attempt;
submitting moves it into the queue and makes it admin-visible; two jobs
uploaded in one order but submitted in the *other* order get queue
positions reflecting submission order, not upload order; the settings page
persists a new threshold and rejects an invalid one (client-side via the
input's own `min`, and independently server-side, confirmed by posting
directly past the browser); `cleanup_drafts.py` against a backdated
draft actually moves its files to `archive/` and flips it to `expired`;
the edit page's own settings form starts pre-filled with a draft's
current settings and its 3D preview actually renders; clicking re-slice
there stays on that same page through the "slicing…" reload loop and
lands back on it reflecting the new settings and a fresh preview;
submitting from the edit page ends on the dashboard with the job queued;
and visiting a queued job's edit URL directly redirects to the dashboard
instead of showing a stale form.

### Upload and slicing progress

Slicing (OrcaSlicer + `mbotmake`, both real subprocesses) can take minutes
for a large or support-dense model. `routers/user.py`'s `upload()` used to
block on that entirely before responding - the request just hung, with no
way to tell "still working" from "stuck." It now does the fast part
(validate, save the file, create the `Job` row as `submitted`) and hands
the slow part to a FastAPI `BackgroundTask` (`jobs.slice_and_update`,
opening its own `Session` since the request's is already closed by the
time a background task runs) - the response comes back immediately, and
the job finishes out of band.

Two independent indicators cover the two slow parts, deliberately using
different mechanisms because they're different kinds of "slow":

- **Receiving** (the file transfer itself) needs real byte-level progress,
  which only an XHR's own `upload.progress` event provides - a plain
  `<form>` submission gives no hook to show that at all. `user_dashboard.html`
  intercepts the form's submit, sends it manually via `XMLHttpRequest`, and
  drives a `<progress>` bar off that event. Since the server always ends up
  redirecting to `/dashboard` regardless of outcome (see below), the
  completion handler doesn't need to inspect the response - it just
  navigates there for real once the transfer finishes.
- **Slicing** (server-side, duration unknown up front) is handled by
  `templates/_jobs_table.html`, included by the dashboard and also served
  standalone at `GET /dashboard/jobs-table`. While any row is still
  `submitted` it carries `hx-trigger="every 2s"` and polls itself; a
  `submitted` row shows a plain indeterminate `<progress></progress>` (no
  `value` attribute - browsers animate that on their own, no JS needed for
  the animation itself). Polling stops **on its own** the moment slicing
  finishes: the next re-rendered table simply doesn't carry the
  `hx-trigger` attribute any more once nothing is `submitted`, so there's
  nothing left telling htmx to keep asking - no separate "stop polling"
  signal to send or forget to send.

A validation failure (wrong extension, empty file, too large) is flashed
into the session (`request.session["upload_error"]`) and redirected the
same way a successful upload is, rather than re-rendering the dashboard
directly as the POST response - keeps `/upload`'s response shape
uniform (always a redirect to `/dashboard`) for the JS above, and is a
better-behaved POST-redirect-GET regardless: refreshing the dashboard
after a failed upload no longer re-triggers a "confirm form resubmission"
browser prompt the way re-rendering the POST response used to.

Verified live (Playwright driving a real isolated instance, not just read
as correct): the redirect after upload returns in a fraction of a second
even though the background slice is still running; a throttled transfer
showed the progress bar unhide and track real intermediate byte counts;
the dashboard row visibly moved from "slicing…" to `sliced`; and polling
requests stopped the moment it did, confirmed by watching request counts
stay flat several seconds afterward. (This predates the slice/submit
split below, which is why the end state here is `sliced` rather than the
`queued` this was originally verified against - re-confirmed after that
change, not just assumed still true.)

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

### Browsing finished jobs, and the audit log

**Why this exists:** a real report, not a planned feature landing on
schedule. A rejection had actually recorded correctly (confirmed directly
in the database - `status='rejected'`, the note, timestamps, all there)
but there was nowhere in the UI to go see that, since `active_jobs()`
(deliberately, for the queue view) only ever shows `QUEUE_STATUSES` -
so the row just vanished, and from the admin's side that read as "nothing
happened" rather than "it worked, and here's where it went."

Three views, kept separate on purpose (they answer different questions):

- **`/admin/jobs/finished`** (`jobs.finished_jobs`, most-recently-finished
  first) - browsing past jobs: everything in `TERMINAL_STATUSES`
  (rejected/done/failed/expired), with the rejection note if there is one
  and a working "View 3D" link, since a job's files and preview are
  completely unaffected by its status leaving the active queue list (only
  confirmed by testing directly - it's easy to mistake "the row is gone"
  for "the data is gone," which it isn't).
- **`/admin/log`** (`jobs.all_events`, most-recent first, capped at
  `ACTIVITY_LOG_LIMIT` = 500) - **the actual "admin log view"**, per the
  user's direct correction to the first version of this: one flat table
  of every event across every job, not something you have to open one job
  at a time to see. First cut of this feature only had the per-job view
  below and linked to it from the queue/finished-jobs lists, which reads
  as "a log of each submission" - not what was being asked for, which was
  "what's been happening, at a glance," across everything. Filtering this
  down (by job, user, action, date range) is explicitly deferred - "I
  will ask for log filters later" - so for now it's just recent activity,
  capped, not a complete searchable history yet.
- **`/admin/jobs/{id}/log`** (`jobs.job_events`, oldest first) - one job's
  complete history in isolation, for when the global log's job link is
  clicked, or the "Log" link is followed straight from a queue/finished-
  jobs row: every submit, slice attempt, re-slice, queue-submit, approve,
  reject (with the note), release, and outcome for *that job specifically*,
  each with who did it and when. `Job` itself only ever holds the
  *latest* review snapshot (`reviewed_at`/`reviewed_by_admin_id`/
  `admin_note`) - this (and the global log above, which draws on the same
  underlying table) is the full sequence that snapshot alone can't show.

Both log views read from the same `models.JobEvent` table rather than
cramming a whole history into more columns on `Job`. `jobs.log_event()`
is the one place every entry gets written, called
right alongside the `session.add(job)`/`commit()` for whatever change
it's recording (never as an afterthought in a different transaction),
from every function in `jobs.py` that changes a job's status, plus
`routers/user.py`'s `upload()` (the initial `submitted`) and
`cleanup_drafts.py` (`expired`, actor `"system"` - the one case with no
person behind the action). `actor` is a plain label
(`"user:<name>"`/`"admin:<username>"`/`"system"`) rather than a real
foreign key to either `User` or `Admin` - simpler than a polymorphic FK
for something only ever displayed, never joined against.

Verified live, every action type, not just the one that prompted this:
submit, slice success, slice failure, re-slice (both outcomes), queue
submission, approve, reject, and both `mark_finished` outcomes each
produce the right log entry with the right actor - confirmed by reading
the entries back out of a real running instance's database directly, not
assumed from the code alone. Also confirmed: a rejected job now
correctly disappears from the active queue *and* shows up in the
finished-jobs list as `rejected` with its note, closing the actual gap
that was reported; the global log (`/admin/log`) shows entries from
multiple different jobs and users interleaved in true most-recent-first
order, not grouped by job.

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
- `db.py` - SQLite engine/session (one file, no separate DB server - this
  runs on one Pi next to one printer), plus `init_db()`'s own small
  migration mechanism - see "Database migrations" above.
- `storage.py` - the scratch/queue/archive directory layout and file-moving
  helpers (including moving a draft's files, not just a queued job's -
  see "Drafts and expiry" above), plus reading a `.makerbot`'s slice-time
  duration estimate back out of its `meta.json`.
- `pipeline.py` - calls `../slicing/slice.py` as a subprocess (deliberately
  not imported - see the module docstring for why) to turn an uploaded STL
  into a `.makerbot`, optionally with supports enabled and the intermediate
  gcode preserved for `supports.py`.
- `supports.py` - parses that gcode into the simplified support-material
  line segments shown in the 3D preview (see "3D preview" above).
- `jobs.py` - queries (`jobs_for_user`, `active_jobs`, `finished_jobs`,
  `queue_position`, `job_events`, `all_events`) and the actual
  state-transition logic
  (`approve`/`reject`/`release`/`mark_finished`/`submit_draft`/
  `start_reslice`), kept out of the routers so it's independently
  testable. Also `slice_and_update`, run as a background task by
  `routers/user.py`'s `upload()` and `reslice()` so neither request
  blocks on slicing - see "Upload and slicing progress" above - and
  `log_event`, the one function that writes to the audit log
  (`models.JobEvent`), called by every state-transition function here plus
  `routers/user.py`'s `upload()` and `cleanup_drafts.py` - see "Browsing
  finished jobs, and the audit log" above.
- `cleanup_drafts.py` - expires abandoned drafts past the admin-configured
  age threshold; see "Drafts and expiry" above. Same run-from-cron
  pattern as `backup.py` below.
- `printer.py` - the printer's network protocol client (vendored from
  `../test-print/`, see its docstring for why not imported) and
  `send_print_job()`, the real hardware call behind `jobs.release()`.
- `pair_printer.py` - CLI for the one-time printer pairing step.
- `auth.py` - hashing (bcrypt, called directly - see note below) and the
  `require_user`/`require_admin` FastAPI dependencies that redirect to
  the right login page when not authenticated.
- `routers/user.py` - signup/login/logout/dashboard/upload, plus a
  draft's own edit page and actions (`GET /jobs/{id}/edit`,
  `POST /jobs/{id}/reslice`, `POST /jobs/{id}/submit`) - user-only, so
  they live here rather than in `routers/jobs.py` even though they share
  that URL prefix.
- `routers/admin.py` - login/logout/dashboard (the queue view), the
  approve/reject/release/mark_done/mark_failed actions, user account
  management (`/admin/users`), settings (`/admin/settings` - currently
  just `Settings.draft_expiry_days`), the finished-jobs view
  (`/admin/jobs/finished`), and the two log views - the global activity
  log (`/admin/log`) and one job's own (`/admin/jobs/{id}/log`).
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
