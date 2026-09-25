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

**A second real incident, worse than the first: `VERSION` got bumped
before the matching migration existed, not just alongside a code
change.** While adding `Job.failure_reason` (schema `3.4.0`): `models.py`
was edited first, then `VERSION` was bumped to `3.4.0`, then - before
`_migrate_to_3_4_0` and its `MIGRATIONS` entry were even written - a
*different*, unrelated `.py` file was edited for the same feature. That
save triggered `--reload`, which ran `init_db()` against the real
database with `APP_VERSION` already reading `"3.4.0"` but `MIGRATIONS`
still topping out at `"3.3.0"` - so nothing was pending to run, yet
`init_db()`'s final step unconditionally records
`schemaversion.version = APP_VERSION` regardless. The real database was
left claiming `3.4.0` while still missing the actual column, crashing
every query touching `Job` - and unable to self-heal even once the
migration function was later written, since a stored version of `3.4.0`
means nothing is ever "pending" for it again. Caught while testing the
new migration against a copy of the real database (as always) - the
copy was already in this broken state, meaning it was already live.
Fixed by hand (the same `ALTER TABLE` the migration function itself
would have run) rather than by any change to the mechanism, since the
mechanism did exactly what it's specified to do - the ordering of the
*edits*, not the code, was the bug. **Revised rule:** the migration
function must be written and saved *before* `VERSION` is bumped, not
just in the same commit - on this project, where any `.py` save can
trigger a reload against the real live database at any moment, `VERSION`
is the one edit in a schema change that should always happen last,
right before committing, with no further `.py` edits still to come
after it.

## Deployment: zero internet access, by design

This runs on an isolated "island" LAN (Pi + printer wired to a router,
client devices join over wifi, nothing on that network ever reaches the
internet - see project memory `queue3d-deployment-network`). Consequences:

- The real deployment (see `deploy/`) runs this behind an nginx reverse
  proxy, not exposed directly - `uvicorn` itself binds `127.0.0.1:8000`
  only (`deploy/queue3d.service`), and nginx (`deploy/nginx-queue3d.conf`)
  is the actual public listener, terminating https with a self-signed
  cert (there's no CA reachable at the deployment site) and redirecting
  plain port 80 to it. Client devices on the LAN reach it at
  `https://<host>/` with no port number to remember, the app itself is
  never directly exposed to the network at all, and every browser shows
  a one-time "not trusted" warning per device for the self-signed cert -
  expected on an offline network, not a sign of a problem. This changed
  after a real first-deploy session revealed everyone would otherwise
  need to type `:8000`, then again once plain http wasn't considered
  good enough even on an isolated LAN - for a quick local-only dev check
  (not the real deployment), plain `--host 0.0.0.0 --port 8000` still
  works fine, same as always.
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

### Uploading `.obj` and `.zip` files

**Why this exists:** per the user - many real-world downloads (a
Thingiverse-style "thing" in particular) come as a zip of several
separate `.stl`/`.obj` files (variants, accessories, a multi-part
model), not one bare `.stl`. Only `.stl` was ever accepted before this.

**The one real design question, asked and confirmed before writing any
code:** what happens when a zip has more than one model file? PrusaSlicer
loads all of them together onto one build plate. This app's entire
slicing pipeline is built around **one object per job** on purpose
(`slicing/stl_to_3mf.py` explicitly disables auto-arrange - `--arrange
0` - specifically because "every profile here is one plate/one object");
matching PrusaSlicer's actual combined-plate behavior would mean building
real bin-packing/arrangement logic from scratch, a multi-object 3D
preview, and re-verifying supports still line up correctly across
multiple objects at once. Confirmed with the user instead: **each model
file in a zip becomes its own separate job/draft** - the exact same
upload→slice→draft flow every plain `.stl` upload already goes through,
just run once per file found. Zero changes needed to slicing, the 3D
preview, or support generation - the entire feature is upload-time
extraction plus a loop.

**`.obj` is converted to a real `.stl` immediately on upload, not taught
to the rest of the pipeline as a second format** - `app/mesh.py`'s
`parse_obj()`/`write_stl_binary()` (no third-party mesh library, matching
`slicing/stl_to_3mf.py`'s own from-scratch STL parser - OBJ is a
comparably small, dependency-free format not worth a new pip dependency
for). This keeps every downstream piece - `storage.py`'s job-id-based
`.stl` paths, `stl_to_3mf.py`'s parser, the client-side STL preview,
re-slicing - working completely unchanged: there is exactly one on-disk
model format past the moment of upload, same as there always has been.
`Job.original_filename` still shows the true "vase.obj" for display;
what's actually stored and sliced (`Job.stl_path`, still named that) is
a losslessly-converted `.stl` holding the identical geometry. Verified
against the real pipeline, not just our own parser's round-trip: an
OBJ-derived cube was sliced all the way through OrcaSlicer and
`mbotmake` into a genuine `.makerbot`, and the resulting job's
`/jobs/{id}/model.stl` serves real STL bytes with no route changes at
all.

**`storage.extract_model_files()`** pulls every `.stl`/`.obj` entry out
of an uploaded zip (case-insensitive, at any folder depth - a zip is
often wrapped in one containing folder) and ignores everything else
(a README, a photo, a license file) rather than erroring on it. Capped
at `MAX_ZIP_MODEL_FILES` (10) - a sane ceiling on how many simultaneous
slicing background tasks one upload can kick off at once (see the
concurrency note below) - and each entry checked against the existing
`MAX_UPLOAD_BYTES` from its *recorded* uncompressed size, before ever
decompressing it, a real defense against a small zip expanding into
something much bigger, not just a courtesy. Deliberately never calls
`extractall()` or builds a filesystem path from an entry's own name (the
classic "zip slip" path-traversal footgun - an entry literally named
`../../etc/cron.d/x`) - only `zf.read()` into memory, and only an
entry's basename is ever kept, for display, never for path construction.
Confirmed directly: a deliberately path-traversal-shaped entry name in a
test zip came back with its directory components stripped, not honored.

**One bad file in a zip doesn't sink the rest** - `routers/user.py`'s
`upload()` processes every extracted entry independently (validating/
converting each via the same `_stl_bytes_from_upload()` a plain upload
uses), skipping one that fails (unreadable OBJ, individually too large)
and naming it in the flash message rather than rejecting the whole zip
over one bad part. A zip with zero usable model files, or an outright
invalid zip, is rejected outright with a clear message.

**Client-side instant preview (parsing the chosen file in-browser before
upload, no round-trip - see "3D preview" below) still only understands
`.stl`,** the one loader already vendored - an `.obj` or `.zip` selection
skips it with a plain "Preview available after upload" note instead of
adding a second vendored loader just for that instant, before-upload
look. The real preview (post-slice, always from the server's genuine
`.stl` copy) works identically regardless of what was originally
uploaded, once slicing finishes - this only affects the very first,
optional glance.

**A theorized limitation here turned out to be wrong when actually
checked, later in this project - see "Fixing a real multi-model zip
upload" further down.** This originally claimed a zip of several files
kicks off that many *concurrent* background slicing tasks - checked
directly (real process monitoring during a real 15-file upload, not
assumed) and that's false: FastAPI's `BackgroundTasks` added within one
request run strictly sequentially, confirmed by never seeing more than
one real OrcaSlicer/`mbotmake` process alive at a time across many
checks. `MAX_ZIP_MODEL_FILES` was raised on the strength of that
finding - it was never actually bounding concurrency risk, just
turning away legitimate multi-part uploads for no real reason.

Verified end-to-end: a plain `.stl` upload (regression check), a
standalone `.obj` upload converting and slicing correctly, a zip with
two real `.stl` files producing two independent jobs (both sliced
successfully), a zip mixing `.stl`/`.obj`/an ignored `.txt` file
extracting only the two real models, a wrong extension rejected, a
malformed standalone `.obj` rejected with a clear message, an all-bad
zip rejected, and a zip with one good file and one malformed `.obj`
uploading the good one while clearly naming the skipped one - all
through the real HTTP routes and the real OrcaSlicer/`mbotmake`
pipeline, not just the parsing functions in isolation.

**A real report, weeks later: uploading a real multi-model zip "did
nothing," with "422 Unprocessable Content" logged server-side.**
Checked the real database directly before guessing at a cause: no job
row existed at all from the failed attempt - meaningful, because every
path through `upload()`'s own code either creates a job or calls its
local `fail()` helper, which always sets a flash message and redirects.
A 422 that never reaches either means the request failed FastAPI's own
validation *before* the route body ever ran at all - not a bug in
`storage.extract_model_files()`'s splitting logic, which never got the
chance to run.

Ruled out directly against the real production server, not guessed:
Starlette's own multipart size limits (`formparsers.py`'s
`max_part_size`, 1MB) only apply to non-file form fields - confirmed by
reading the actual installed library source - and even when it does
trip, a `MultiPartException` there surfaces as a 400, not a 422, so
this can't be that regardless. A realistic zip built to resemble a real
Thingiverse download (nested folders, a README, a stray non-model file)
uploaded and split into separate jobs correctly against the real
production server. Couldn't reproduce the actual 422 without the
specific file that triggered it - recorded as an open README to-do
item; the next occurrence needs either that file or a browser
Network-tab capture of the failed request to pin down further.

**Immediate correction from the user, worth recording plainly rather
than quietly editing away: the "split into separate jobs" claim above
was wrong for real multi-model zips.** "Don't count the zip split yet
b/c that isn't working. What is working is a zip with 1 model file."
Every synthetic zip built to investigate the 422 (including the
realistic multi-file one just described) tested clean against the real
server - which means either something about the specific real file
(content, size, encoding) triggers the 422 that no synthetic test has
reproduced, or the real failure is intermittent rather than affecting
every multi-file zip categorically. Both README.md's Features list and
its To do item were corrected to state plainly that only a single-model
zip is confirmed working right now - a multi-model zip should be
treated as broken in practice despite once testing clean, until the
real cause is found. Lesson worth remembering generically: a synthetic
test passing is evidence the *general mechanism* isn't broken, not
proof the *specific real-world case* works - when a user directly
reports the opposite of what testing showed, the user's real-world
report is the one to trust and correct the record around, not the one
to explain away.

**A real, independent bug found and fixed while investigating this,
regardless of whatever the 422's root cause turns out to be:** the
dashboard's own upload JS sent the form via a manual `XMLHttpRequest`
(for genuine upload-byte progress - see "Upload and slicing progress"
below) and unconditionally navigated to `/dashboard` the instant the
request completed, on the reasoning (accurate for every error path this
app's own code controls, since `fail()` always redirects with a flash
message set) that "the server always ends up there anyway." That
reasoning silently breaks for exactly this class of failure - a request
that fails before the route runs never redirects anywhere at all - which
is precisely why the user saw nothing: the JS still just navigated to a
perfectly ordinary dashboard, with no job and no error text, indistin-
guishable from the upload having done nothing. Fixed: the JS now checks
`xhr.status` (same-origin redirects are followed transparently by the
browser, so a genuine success *or* an in-app `fail()` both still read
as a final 2xx by the time `"load"` fires - only a failure that never
got redirected surfaces as non-2xx here) and shows its own error text
for anything outside that, or a network failure via the `"error"`
event. Verified the normal path still works unchanged (a real upload
still redirects to the dashboard with no error text shown) before
considering this done.

Also noticed in passing while investigating: a `slice_failed` draft has
no delete route at all (only `queued`/`approved` jobs can be deleted,
by either a user or an admin) - recorded as its own README to-do item,
then built the same day, see below.

### Deleting a `slice_failed` draft

**Why this was its own small gap, not just an oversight:** a draft that
never successfully sliced has no "submit" option (nothing to submit)
and, until this, no delete option either - the only ways out were
re-slicing with different settings (which might not help - some models
genuinely can't slice, see the bed-centering investigations elsewhere
in this file) or waiting out the existing draft-expiry cleanup, which
exists for abandoned drafts in general, not as a real "I want this
gone now" action.

**The actual code gap, once looked at directly:** `jobs.py`'s shared
`_delete_job_genuinely()` (used by both the admin's `delete_old_job()`
and a user's own `delete_own_job()`) gated on `queued`/`approved` only.
Widening that for `delete_own_job()` alone (not the admin path, which
never needs it - a draft is never admin-visible in the first place, per
`models.QUEUE_STATUSES` excluding it entirely) meant refactoring the
hardcoded status check into a parameter each caller passes explicitly,
rather than loosening one shared constant both callers relied on.

**A second, less obvious gap the same change surfaced: `storage.
delete_job_files()` only ever looked in `queue/`.** That was never
wrong before, because the only jobs ever passed to it were `queued`/
`approved` - both already moved out of `scratch/` by `submit_draft`. A
`slice_failed` draft's files, per `storage.py`'s own three-directory
lifecycle (`scratch/` for anything in `models.DRAFT_STATUSES`, `queue/`
for `models.QUEUE_STATUSES`), are still sitting in `scratch/` - deleting
by looking in `queue/` would have silently done nothing to the actual
files (no error, since the function is already tolerant of a missing
path - a real "looks like it worked but didn't" trap). Fixed by making
`delete_job_files()` itself check the job's own status and pick
`scratch_paths()` vs `queue_paths()` accordingly, rather than pushing
that decision up into `jobs.py` - the file-location logic already lived
in `storage.py` for every other lifecycle transition, so this keeps it
there rather than splitting it across two modules.

**Deliberately scoped to exactly what was asked, not generalized to
every draft status:** a plain `sliced` draft (successfully sliced, not
yet submitted) still has no delete route - it wasn't part of this ask,
and already has two ways forward (submit it, or keep adjusting
settings before submitting). Widening this further would be a natural
follow-up but wasn't assumed.

Verified end-to-end against a real isolated instance, not just read as
correct: uploaded a deliberately-too-small (1mm) test cube (a shape
already known from earlier in this file to trip `mbotmake`'s
bed-centering assertion) to get a genuine `slice_failed` status through
the real pipeline, confirmed its `.stl` sat in `scratch/`, deleted it
through the real HTTP route, and confirmed all three afterward: the
file gone from `scratch/`, the job row gone from the database, and its
prior event history purged down to a single new `job_deleted` entry -
the same pattern every other genuine delete in this app already uses.
Also re-verified the existing `queued`/`approved` delete path
end-to-end after the refactor (upload, submit to queue, delete, confirm
the file leaves `queue/`) to make sure sharing the status list via a
parameter rather than a hardcoded tuple didn't regress the original
behavior.

### Fixing a real multi-model zip upload

**Real report, with a real file this time:** the earlier 422
investigation (see "A real report, weeks later" above) never found a
reproducible cause with synthetic test zips - the user later supplied
an actual real-world multi-part download (`Functional Differential Gear
System - 11836.zip`, a Thingiverse-style functional-print kit) and
asked directly to find a fix. Inspected the real file's structure
before touching any code: 15 real `.STL` model files, plus a nested
`.zip` (a variant sub-download, correctly ignored - not a model
extension), directory entries, images, a README, and a LICENSE file.

**Found the real cause immediately: `MAX_ZIP_MODEL_FILES = 10`, and
this legitimate file has 15.** Reproduced directly against a real
isolated instance with the actual file: the upload cleanly redirects to
`/dashboard` with a flash message, "Too many model files in this zip
(max 10)." - a working, non-broken rejection, not a 422 or silent
failure. This is a *different* bug from the still-unresolved 422 -
confirmable because this exact file does NOT reproduce a 422 with the
current code, only a clean, friendly-but-wrong-here rejection. Whether
this was also involved in the original 422 report is unknown (that
file was never available to test); what's certain is that this cap was
too low for a real, legitimate functional-print kit.

**Checked whether the cap's own stated justification actually held up,
rather than just raising the number blindly:** the code comment claimed
this bounds *concurrent* slicing background tasks a zip's worth of
uploads could kick off at once. Checked this directly against a real
15-file upload rather than trusting the comment: polled the process
table repeatedly through the entire slicing run and never saw more than
one real OrcaSlicer/`mbotmake` process alive at any moment - FastAPI's
`BackgroundTasks` added within a single request run strictly
sequentially (awaited one after another), not concurrently. The cap's
original justification was wrong; it was never bounding concurrency
risk at all, just serial total wait time and the memory
`extract_model_files()` holds for every matched file's bytes at once -
both real but much less restrictive concerns than "concurrency," so
raised `MAX_ZIP_MODEL_FILES` to 25 (real headroom above the 15 that
triggered this, not merely enough to pass) rather than a marginal bump.

Verified end-to-end against the real file, twice - once in an isolated
instance (all 15 parts uploaded, split into 15 independent jobs, and
*all 15 sliced successfully* with no failures, running strictly one at
a time exactly as predicted) and once against the real production
server directly (same result: 15 jobs created, no rejection), cleaned
up afterward through the real submit-then-delete routes rather than
left as clutter.

**Per the user, directly: "We need to note the limitation for the
users."** The upload form itself (`user_dashboard.html`) now states the
per-zip model limit right under the file picker, reading the same
`MAX_ZIP_MODEL_FILES` constant the enforcement itself uses (threaded
through `_dashboard_context()`) rather than a second, hand-typed number
that could drift out of sync with the real limit.

### Restoring an archived job, and one-click reprint

**Why this exists:** asked directly, "what's next most important" -
recommended this since a `rejected`/`failed`/`done`/`expired` job had
no way back except a completely fresh upload, discarding any tuning
(a specific rotation that fixed a real slicing failure, an auto-fit
scale) it took real investigation to find earlier this same session.
The user agreed and asked to build it, then immediately extended the
ask mid-build: "Also, add a reprint option to print another as it was
queued" - a deliberately different, narrower action from restore,
covered second below.

**"Restore & edit" (`jobs.restore_job()`, any `rejected`/`failed`/
`done`/`expired` job) copies the archived `.stl` into a brand-new,
independent draft - never moves it, so the original archived job and
its own event history are completely untouched, exactly per the
original to-do item's own requirement.** The new draft starts
pre-filled with the archived job's own `scale_factor`/`rotate_x/y/z`/
`supports_enabled`/`support_style` rather than plain defaults - the
whole point is reusing a previous submission including whatever tuning
it took to get there, not resetting to 100%/0°/0°/0° and risking the
exact same slicing failure all over again. Immediately schedules the
same `slice_and_update()` background task a fresh upload triggers
(seeded with those carried-over settings as the *first* attempt, not
defaults), and redirects straight to the new draft's own edit page -
unlike a fresh upload's redirect to the dashboard, there's always
exactly one resulting job here, never a zip's worth of several, so
there's no ambiguity about where to send the user.

**Correcting a real error in how this was originally scoped, caught
while actually building it, not left to ship wrong:** the original to-do
item's own wording listed `slice_failed` alongside `rejected`/`failed`/
`done` as something to "restore" - but a `slice_failed` job is a
*draft* (`models.DRAFT_STATUSES`), not an archived one at all; its files
still live in `scratch/` and it already has a complete edit/re-slice/
delete path on the exact same edit page every draft uses (including the
delete route built earlier this same session). Restore is scoped to
exactly `models.TERMINAL_STATUSES` (`rejected`/`done`/`failed`/
`expired`) - the actual jobs whose files genuinely moved to `archive/`
and have no path back otherwise.

**"Reprint" (`jobs.reprint_job()`, `done` only) is a different action
entirely, not restore-with-an-extra-step: it skips slicing altogether.**
A job that already finished printing successfully doesn't need to prove
itself again - reusing the exact archived `.makerbot` byte-for-byte is
both faster and more certain to reproduce the same result than
re-running OrcaSlicer/`mbotmake` against identical geometry and settings
a second time. Copies the archived `.stl`/`.makerbot`/`.supports.json`
straight into `queue/` (not `scratch/` - there's no draft stage at all
here) with a fresh `queued_at` (matching `submit_draft()`'s own
convention - genuinely joins the back of the line, not the original's
old position), reads the reused `.makerbot`'s own duration estimate the
same way a real slice would, and lands back on the dashboard already
`queued`.

**Deliberately narrower than restore, and why each excluded status
stays excluded:** not `rejected` (an admin turned it away for a reason
that reprinting the identical file unchanged doesn't address - "Restore
& edit" is the right tool there, letting an actual change happen before
resubmitting); not `expired` (a draft that never actually printed at
all has nothing proven to reprint); not `failed` (a failed *print* -
distinct from a failed *slice* - might have failed for a physical
reason, like this very session's own bed-adhesion incident, worth
checking or fixing before blindly retrying the identical file rather
than assuming the file itself was ever the problem).

Verified end-to-end in a real isolated instance for every path, not
assumed from reading the code: staged a real `done` job (release()
itself needs the actual printer hardware, unavailable here - staged the
status transition directly the same way this project's own screenshot
generation already does for hard-to-reach states) and confirmed Reprint
produces an immediately-`queued` job with the exact reused `.makerbot`
file, correct carried-over duration estimate, and a clean audit-log
entry; confirmed a real `rejected` job's "Restore & edit" produces a
new draft that automatically re-slices to `sliced` with its own correct
audit trail, while the original rejected job's own status and archived
files are completely unaffected; confirmed both routes reject the wrong
status cleanly (reprinting a `rejected` job, say) with a clear flash
message via the same `JobActionError` pattern every other job action
already uses, rather than a raw error or silent no-op.

### Downloadable support bundle

**Why this exists:** asked directly "what's next most important," this
was recommended and built the same session - per the user's own earlier
ask, "create a 'tar.gz' file that contains errors, model files, logs,
etc. that would be helpful for offline bugfixes... After I setup the
app/Pi, network, and printer in place, I want to be able to show up and
collect the support files." This deployment has zero internet access at
all (see project memory `queue3d-deployment-network`) - there's no way
to relay a live problem back for help the normal way, so the plan is
physical: generate the bundle on the spot, carry it out.

**Contents chosen from what every real bug investigated this project
has actually needed, not guessed at:** a safe, consistent copy of the
whole database (`backup.py`'s own `backup_database()` - the exact same
online-backup-API copy the automated backup feature already uses, not
a raw file read that could grab a half-written page mid-write, reused
rather than reimplemented), the original model file for every job that
ever recorded a `slice_error` (whether it ultimately failed outright or
an automatic rotation retry fixed it - kept either way, since a
"fixed by auto-rotation" job's file is exactly the kind of thing worth
double-checking later), and the full activity log as plain text. The
database alone is genuinely the single most useful thing here - every
investigation this session actually ran (the fighter jet, Flexi_Seal,
the zip upload cap) started from knowing a job's exact settings/status/
history, and without the real model file alongside it, none of those
could have been reproduced or diagnosed at all.

**Deliberately not included, and documented plainly rather than
silently left out: a persistent application log file.** This app's own
progress output goes straight to whatever terminal `uvicorn` happens to
be running in, not a file - there's nothing on disk to collect yet. If
the real deployment eventually runs this under systemd, its journal
would be the natural next thing to add here; not attempted now since
that setup doesn't exist yet to test against.

**A real design question resolved rather than glossed over: this
bundle contains real user names, job filenames, and the database's
stored (hashed, not plaintext) PIN/password secrets.** Not a new
exposure - an admin generating this already has that same access on the
live server - but worth being explicit about in both the bundle's own
`manifest.txt` and here, rather than assumed harmless without saying so,
since the file is meant to leave the device once generated.

`GET /admin/support-bundle` builds the bundle into a fresh temp file
and streams it back as a real file download (`Content-Disposition:
attachment`, a timestamped filename), then deletes the temp file via a
`BackgroundTask` once the response has actually gone out - the same
"clean up after the response is sent, not before" shape used wherever
this app hands back a generated file. Reachable from a plain link on
the admin dashboard, right next to the existing backup-status line -
the same place an admin would already be looking when something needs
investigating.

Verified end-to-end against a real isolated instance: seeded one normal
job and one that genuinely fails to slice (reusing the same tiny-cube
shape that reliably trips `mbotmake`'s bed-centering assertion
elsewhere in this file), downloaded the actual bundle through the real
route, and confirmed all four pieces are correct - the manifest's own
counts match, the activity log reads back the real event sequence, the
database copy contains the exact captured `slice_error` text, and the
failing job's own `.stl` is present under `models/` while the
successful job's is correctly not. Also confirmed the temp file is
genuinely gone from disk immediately after the download completes, and
that a non-admin hitting the route is redirected rather than handed the
file.

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

### A real stuck-slicing incident: subprocess stdin inheritance

**What happened, reported directly by the user:** "I uploaded an obj
file and started the slicing process. The status shows submitted, but
the progress bar is showing that it's still working... is it stuck?"
Checked the real running process, not just the database: both `slice.py`
and its own child `mbotmake` were genuinely still alive, several minutes
in, but at essentially zero CPU time - not computing, blocked. `mbotmake`
has an internal `input()` call (line 929) on certain errors of its own -
here, a bed-centering sanity check (`assert -0.15 < xrel < 0.15`,
comparing the printed toolpath's actual bounding-box center to zero,
relative to its own width) failing for this particular model's geometry.
None of the three `subprocess.run()` calls in this pipeline
(`pipeline.run_slice`'s own, plus `slice.py`'s two - OrcaSlicer and
mbotmake) ever set `stdin` explicitly, so all three inherit whatever
stdin the app's own process has - a real terminal in normal dev/
deployment use, confirmed directly (`/proc/<pid>/fd/0` pointed at a real
`/dev/pts/0`). That `input()` call was blocking forever waiting for a
keystroke nobody would ever type, rather than raising `EOFError`
immediately the way it does when stdin is already closed - which is
exactly what happened when this got reproduced from a context with no
real terminal attached, initially making it look like a one-off that
couldn't be reproduced until stdin was deliberately checked and
controlled for.

**Fixed with `stdin=subprocess.DEVNULL` on all three calls** - a job
that hits this now fails in well under a second with a clean error
instead of hanging for however long it takes the outer 600-second
`subprocess.run` timeout to fire (and even then, only the direct child
gets killed by that timeout - the blocked grandchild `mbotmake` would
otherwise leak indefinitely as an orphaned process, never actually
cleaned up). Confirmed both ways on the real failing model: with stdin
left alone, it reproduces the exact hang; with `stdin=subprocess.DEVNULL`
in place, the identical input fails fast (well under a second) with a
clean `RuntimeError` instead. Also re-verified a normal successful slice
still works unchanged with the fix in place - this only changes what an
*already-failing* run does, not the success path.

**The Christmas-tree model's own slicing failure is real and separate,
not something patched over** - an asymmetric shape whose naive
bounding-box centering (`stl_to_3mf.center_vertices`, based on the raw
mesh's min/max) doesn't line up closely enough with where the actual
print material ends up once sliced, and `mbotmake` refuses to proceed
rather than risk a mispositioned print. Deliberately not "fixed" by
loosening that assertion - it's third-party vendored code whose exact
tolerance reasoning isn't fully understood here, and weakening an
unfamiliar safety check to make one model pass risks silently producing
bad real-world prints for others instead of a clean, honest failure.

**Follow-up, same day: actually tried the centroid-based centering
angle, rather than leaving it as a filed idea, once the user reported a
second upload failing too** ("Now both obj files that were uploaded
failed... I still don't have working support for uploading and slicing
obj files"). Investigated properly before touching anything: confirmed
`center_vertices()` itself was working exactly as designed (the raw
mesh's bounding box really did land at X/Y = 0,0 after it ran) - the
mismatch was that the model's bounding-box center and its actual
*surface* aren't in the same place for this shape. Computed three
candidate reference points for the real failing mesh directly: the
bounding-box center (what shipped originally), a plain vertex average
(tessellation-dependent - biased toward wherever the mesh happens to
have more/smaller triangles, not a sound choice), and an area-weighted
triangle centroid (tessellation-independent - a large triangle counts
the same as many small ones covering the same real area). The
area-weighted centroid came out meaningfully closer to zero than the
bounding-box center in the direction that mattered.

**`center_vertices()`/`surface_centroid_xy()` (`slicing/stl_to_3mf.py`)
switched from bounding-box to area-weighted-centroid centering**, and
`static/preview.js`'s `showModel()` updated with an identical
`surfaceCentroidXY()` calculation to match - the two have to stay in
lockstep (see either's own comment) or this reintroduces the exact
"preview and slice disagree on where an off-center model actually
sits" bug `center_vertices()` was originally built to prevent. Verified
directly on the real failing model before believing any of this helped:
re-slicing the identical mesh moved `yrel` from -0.232 (its original,
clearly-failing value) to -0.162 - a real, measured improvement in the
right direction, using the actual OrcaSlicer + `mbotmake` pipeline, not
just reasoning about the vertex math. Also re-verified two previously-
working models (a plain STL, and the overhang-supports test model with
supports enabled) still slice successfully and still produce correct
support-preview geometry with the new centering - a real regression
check, not assumed safe just because the failing case improved.

**Fully honest about the actual, incomplete result: this specific model
is asymmetric enough that -0.162 still narrowly misses the ±0.15
tolerance** - the fix is a genuine, verified improvement (it will
likely resolve moderately-asymmetric models that would have failed
under pure bounding-box centering), not a claim that this particular
Christmas tree model now slices. A full volume-centroid calculation
(where the material really *is* in 3D, not just projected surface area)
might close the remaining gap, but needs a watertight, consistently-
wound mesh to compute correctly - a real risk for a mesh converted from
an arbitrary user-supplied OBJ that hasn't been validated as clean, and
not attempted here without first checking that precondition. Recorded
as the next concrete step on the "model repair"/centering to-do item,
with the specific numbers this attempt got to, rather than restarting
the investigation from scratch next time.

**Second real occurrence: `Flexi_Seal.stl` - same failure class, and the
volume-centroid follow-up actually tried, with a real answer.** Reported
by the user, who'd already tried resizing it (no effect) and asked to
find the cause. Confirmed mathematically first, before touching
anything, why scaling specifically could never help: `mbotmake`'s check
is a *ratio* - `(x_max + x_min) / (x_max - x_min)` - and a uniform scale
multiplies both the numerator and denominator by the identical factor,
leaving the ratio completely unchanged. Scaling this model was never
going to work, for any scale factor.

Checked the "next concrete step" noted above properly, now that a real
failing case was in hand: this mesh's edges are 99.98% manifold (19
non-manifold edges out of 78,779, a small, real but minor defect, not a
disqualifying one) and its signed volume comes out positive (consistent
winding overall), so a true volume centroid was actually computable, via
the standard signed-tetrahedron-decomposition algorithm. Tried it -
**and it made things worse, not better**, confirmed against the real
pipeline: `xrel` moved from 0.251 (the deployed area-weighted-surface
centering) to 0.311 with volume-centroid centering, in the wrong
direction. Reported honestly rather than pretending the "obvious next
step" panned out - it didn't.

**Real, working answer found instead: rotating the model.** Reasoned
through why the centroid shift made things worse rather than better:
`mbotmake`'s check specifically measures where *infill* (not the whole
model's surface or volume) ends up, and sparse infill only exists in a
model's actual solid interior - concentrated wherever the shape happens
to be thick, which a whole-mesh surface- or volume-centroid has no way
to know about without slicing first. That pointed at reorientation, not
a smarter static centering formula, as the real fix - directly the
"model controls" rotation feature already on the to-do list, not a
coincidence. Tested empirically rather than assumed: rotated the actual
failing mesh (re-centered after each rotation, same as any real upload
would be) at a sweep of angles about the vertical axis and re-sliced
each one through the real pipeline. A pure 90° rotation flipped which
axis failed (X started passing, Y started failing instead) rather than
fixing both at once - genuinely informative on its own, since it
confirms rotation *does* change the outcome, just not trivially. A 45°
rotation passed both checks (`xrel` 0.09, `yrel` 0.11, both comfortably
inside ±0.15) and produced a real, complete `.makerbot` file.

**Immediate, real workaround exists today, before rotation controls are
built:** rotating a model roughly 45° about its vertical axis in any
external tool before uploading can resolve this exact failure class -
told to the user directly for this specific file. The in-app "rotate on
any axis" control (see README.md's model-controls to-do item) remains
the real fix, and this investigation is now a second, independent, real
data point motivating it - not just the Christmas tree's "stand it up
on its base" case, but confirmed general-purpose: some rotation, found
by testing rather than guessed, can resolve this class of failure when
no amount of resizing or recentering-only ever could.

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

Pairing (`pair_printer.py`, needs someone physically at the printer's
dial) saves an access token to `data/printer_auth.json`; `QUEUE3D_PRINTER_HOST`/
`QUEUE3D_PRINTER_PORT` env vars override the printer's address if it's
not at the default. **Not actually a one-time-ever step, despite how
that reads** - see "Persistent printer connection" right below for why,
and what that does and doesn't fix.

Not yet built: live print progress/status polling (the printer's own
`get_system_information` exists in the protocol but isn't wired up - see
`printer.py`'s `_dispatch` for where unsolicited status notifications
would need to be consumed) and detecting completion automatically -
`mark_done`/`mark_failed` are still a manual admin action for now.

**A countdown from the original estimate, in place of that missing live
status - explicitly not a substitute for it.** Per the user, after
noticing the printer's own on-device timer runs inaccurate: while a job
is `printing`, both dashboards show a live "~N min remaining" (or "~N
min over the estimate" once it runs past zero, rather than freezing at
0:00 or hiding - the estimate is already known to run off in practice,
and pretending otherwise would be worse than just saying so) instead of
the flat total estimate shown for a queued/approved job.
`jobs.printing_eta()` computes `released_at + duration_estimate_s` once,
server-side; `static/countdown.js` re-renders it from the client's own
clock every 15s, so it keeps ticking between page loads and htmx polls
without a matching request each time. Deliberately reads the DOM fresh
on every tick rather than caching element references - some of these
spans live inside `_jobs_table.html`, which htmx replaces wholesale on
its own polling cycle, and a `<script>` tag doesn't re-run just because
it got swapped back in.

**A real bug caught before shipping, worth remembering:** `released_at`
comes back from SQLite as a tzinfo-*naive* datetime, even though it's
always written as UTC (`datetime.now(timezone.utc)`) - same gotcha
`event.at.strftime(... 'UTC')` already works around elsewhere in this
app. A plain `{{ eta.isoformat() }}` in the template would silently omit
the UTC offset, and a browser's `new Date(...)` parses an offset-less
ISO string as *local* time - every countdown would have been off by
however many hours from UTC, on every viewer's own clock, in a way that
would never show up testing from a system already set to UTC. Caught
only by checking what the rendered attribute value actually was and
reasoning about how the browser would parse it - a passing render test
alone (attribute present, page loads) would not have caught this, and it
was not caught by simply trying it against the real printer once.

### Live print progress

**Why this exists:** the user noticed the printer's own on-device timer
runs inaccurate, and asked whether percent-complete polling might be
better - it is. `get_system_information` (JSON-RPC, requires
authentication - `printer.system_information()`) returns a
`current_process` object while something's printing, undocumented by
MakerBot anywhere and never fully decoded in the earlier protocol
investigation (only confirmed to exist). Investigated live, deliberately
carefully rather than assumed, since a wrong read here would be *worse*
than no read at all - a confidently-wrong percentage is worse than an
honest "estimate only":

- **Idle**: `current_process` is `null`.
- **Heating** (`step: "final_heating"`): `progress` climbs 0→100+ as the
  extruder approaches its target temperature - confirmed by comparing it
  to `(current_temperature - room_temp) / (target - room_temp)`, which
  landed within a couple points of the reported value. This is heating
  progress, not print progress.
- **Printing** (`step: "printing"`): `progress` resets to a low number
  and climbs again from there - **confirmed to track genuine print
  state, not just elapsed time**, by comparing two live samples against
  simple `elapsed_time / time_estimation` math: the gap between the
  reported `progress` and that naive ratio *grew* over time (roughly 1
  point of gap at one sample, 5 points at a later one) rather than
  staying constant, which it would if `progress` were just re-deriving
  the same time-based estimate. Also independently confirmed to match
  what the printer's own on-device screen showed at the same moment,
  live, side by side. `time_remaining`, by contrast, *did* match simple
  `time_estimation - elapsed_time` subtraction almost exactly at both
  samples - useful as a live number, but not shown to be smarter than
  what `printing_eta()` below already computes from the original
  estimate alone.

Given that, `jobs.print_progress(job)` only ever returns a percentage
for `step == "printing"` - every other step (including ones never
observed, since this firmware isn't documented) shows just its own name
instead of guessing what its `progress` scale means. Matched to the
right job by comparing `current_process.filename` against
`job.makerbot_path` (current_process carries no job id of its own) -
without that check, a stale reply or a reply belonging to a completely
different job could get attributed to the wrong row.

**Read-only and best-effort throughout, same as the camera capture
above:** `print_progress()` returns `None` - "no live reading right
now," not "0% done" - on any failure (printer unreachable, no active
process, filename mismatch), and the UI simply falls back to
`printing_eta()`'s estimate-based countdown when that happens, exactly
as if this feature didn't exist. A failed live read never blocks
anything else.

`GET /jobs/{id}/progress` (`routers/jobs.py`, same owner-or-admin access
as everything else there) serves the live fragment
(`_print_progress.html`) that both dashboards embed; `hx-trigger="load,
every 10s"` fires an immediate first fetch (so it isn't blank on page
load) and then keeps polling only for as long as the job is actually
`printing` - the same self-terminating pattern `_jobs_table.html`
already uses for slicing progress.

**`/admin/printer/info`** (`admin_printer_info.html`) shows the raw
`get_system_information` reply as-is - built for this investigation, and
kept as a real diagnostic page rather than thrown away, since MakerBot
never documented this method and a future investigation (see the
`complete`/`cancelled`/`error` fields noted in README.md's Printer to-do
list, towards detecting a print's outcome automatically) will need to
look at the raw shape again.

**Correcting the fallback estimate itself, from real history.** Even
with live progress now available, `printing_eta()`'s estimate-based
countdown still matters as the fallback for whenever a live reading
isn't - and the slicer's own `duration_estimate_s` was observed running
well short in real use (the first real completed print took 43.5% longer
than estimated). `_duration_correction_factor()` computes the median
ratio of actual (`finished_at - released_at`) to estimated duration
across past `done` jobs, and `printing_eta()` scales the current job's
estimate by it. Deliberately narrow about what counts as a valid data
point:

- Only `done` jobs, never `failed` ones - a failed print's duration says
  nothing about how long a full print takes; it could have been cut
  short at any point; averaging that in would corrupt the correction
  rather than improve it. (Confirmed necessary directly: 2 of the first
  3 finished jobs in real use were cancellations for bed adhesion,
  finishing in well under their estimated time - including those would
  have corrected the estimate *downward*, exactly backwards.)
- Median, not mean, so one unusually slow print doesn't dominate every
  future estimate as more data accumulates.
- `max(1.0, ...)` - only ever corrects upward, since underestimating is
  the specific, observed problem. No evidence yet that a future estimate
  running long needs correcting the other way, and assuming so on no
  evidence could make things worse, not better.

**A known, accepted imprecision, not silently glossed over:**
`finished_at` is when an admin clicked "Mark done," not confirmed to be
the exact moment the printer itself actually finished - any delay
between the two inflates every ratio computed from it. Precisely fixing
this would mean capturing the printer's own `current_process.elapsed_time`
(see `print_progress` above) at the moment of that click instead - not
built here, since that reading has often been unavailable exactly when
needed during this same investigation (the connection dying being the
common case that motivated the printer status banner/pairing button in
the first place). Still meaningfully better than trusting the raw,
uncorrected slicer estimate outright - see README.md's Printer to-do
list for capturing the printer's own elapsed time as a future
refinement.

**Applied everywhere a duration is shown, not just the live printing
countdown.** Caught live: a job showed a 20-minute estimate at queue
time, then a *different*, corrected number once released - the same
job's own estimate silently depending on which page happened to look at
it, when it should just be the one best-available number everywhere,
consistently. `corrected_duration_estimate_s(session, job)` is the one
place any displayed duration goes through now (queued/approved/sliced
jobs' "est. N min," not just `printing_eta()`), computed once per row in
`_dashboard_context()` (both routers) and passed down as
`row.duration_estimate_s` rather than templates reaching for
`job.duration_estimate_s` directly.

### Automatic completion detection

**Why this exists:** per the user, after this exact investigation:
every real photo-capture failure that day traced back to the same root
cause - the connection dying in the gap between a print *actually*
finishing and an admin *noticing* and clicking "Mark done." Closing that
gap is the whole point, not just saving a click - `jobs.mark_finished()`
now runs the moment the printer itself reports the print is over, using
whichever connection was already live from the most recent progress
poll, not one that's had time to go idle or get killed by a cancel while
nobody was looking.

**A background thread, not tied to any request or open browser tab.**
Relying on the live-progress UI polling (`GET /jobs/{id}/progress`)
alone would only catch completion while someone happened to have the
dashboard open - `jobs.start_auto_finish_poller()` (started once, from
`main.py`'s startup handler) runs independently on a daemon thread,
checking every 15 seconds via `jobs.check_and_finish_active_print()`:
is anything `printing`? If not, skip the printer entirely - no network
call, no cost, most of the time. If so, read `system_information()`
(the same call `print_progress()` already uses) and act only on an
*explicit* positive signal from `current_process` - `complete`,
`cancelled`, or a truthy `error` - never on absence or ambiguity. If
`current_process` has already gone missing or stopped matching the
job's file by the time this polls (the printer cleared it before an
in-between check caught the transition, or the connection simply isn't
reachable that round), this does nothing and leaves the job `printing`,
same as if the feature didn't exist - it never guesses at an outcome it
can't actually confirm.

**The manual Mark done/Mark failed buttons are completely unchanged** -
this is a safety net layered on top of them, not a replacement. A
detected outcome is logged with actor `"system"` (same convention
`cleanup_drafts.py` already uses for automated draft expiry), with a
`detail` prefix distinguishing *why* ("detected automatically - print
complete" / "... cancelled at the printer" / "... printer reported an
error: ..."), so the activity log always shows whether a given
done/failed was a human's click or the poller's own.
`mark_finished()`'s `admin` parameter is `Admin | None` accordingly -
`None` for this automatic path, a real `Admin` for the existing manual
one - both go through the exact same status transition, photo capture,
and archiving logic either way.

**Verified in isolated testing before deploying, not assumed correct:**
a mocked `system_information()` reply confirmed all three outcomes
separately - a job left `printing` untouched while genuinely still
printing, correctly marked `done` on `complete: true`, and correctly
marked `failed` (with the right reason in its detail) on `cancelled:
true` - including a filename-matched job's photo-capture attempt still
running (and failing gracefully, exactly as the manual path already
does) rather than being skipped for the automatic path.

### `finished_at` now stamped after the photo attempt, not before

Per the user, once the above meant `mark_finished()` typically runs
within ~15s of the printer actually reporting a print over: there's no
longer a real reason to stamp `Job.finished_at` at the very start of
that function, before spending a few seconds trying to reach the
camera, rather than letting the whole "wrap this job up" sequence
(status, archiving, photo attempt) finish first. `capture_photo()`'s own
connect+capture timeouts keep the worst case bounded - well under a
minute even on total camera failure (see `printer.py`) - so this can't
reintroduce anything close to the multi-minute drift a slow-to-notice
manual "Mark done" click used to cause, which is what this timestamp's
accuracy actually matters for: `_duration_correction_factor` feeds
directly off `finished_at - released_at` for every future print's ETA.

Verified directly, both outcomes, with a stubbed `capture_photo` rather
than just reasoned about: a simulated 2s successful capture delayed the
recorded `finished_at` by exactly ~2s; a simulated 1s failed capture
(`PrinterError`) still correctly delayed it by ~1s while leaving
`photo_path` `None` and `failure_reason` set - confirming the reorder
changes *when* `finished_at` lands, not what else gets recorded.

### Persistent printer connection

**Why this exists - a real, live-confirmed hardware limitation, not
theoretical:** a pairing token is only good for exactly one authenticated
session. Confirmed three separate ways against the real printer, each
needing its own fresh dial-press pairing to test cleanly: a second,
*simultaneous* connection with the same token is rejected while the first
stays open; a new connection after cleanly closing the first also fails;
and - to rule out our own client sending something the printer could
reasonably react badly to, like an abrupt TCP reset - it still failed
after a deliberately graceful close (half-closed write side, drained to a
confirmed zero unread bytes, only then closed). That third result is what
makes this conclusive: it isn't a disconnect-handling bug in
`_MakerBotClient`, it's how the printer's tokens actually behave. Checked
against MakerBot's own firmware release notes too (their support site is
JS-rendered - a plain fetch gets nothing, needed a real browser to see
it): `2.6.2` build `734`, what this printer runs, is the *last* firmware
MakerBot ever shipped for the Replicator+ line, so this isn't a bug an
update would fix even if one existed. Best guess, not confirmed: a
deliberate one-token-per-session design, probably matching how MakerBot's
own client software already behaves.

The original design here connected fresh and re-authenticated for every
single `send_print_job()` call - which the finding above means would only
ever have worked for the *first* release after any given pairing, and
failed authentication on every one after that. `_PersistentConnection`
(`printer.py`) is the fix: one `_MakerBotClient`, authenticated once, held
open and reused across every call for as long as it stays healthy, rather
than reconnecting per action. `send_print_job()`'s own public signature
didn't change at all - `jobs.release()` needed zero changes - the whole
fix is internal to `printer.py`.

- Before reusing the held connection, a cheap `handshake` call acts as a
  health check; if that fails (printer rebooted, network dropped, the
  connection just isn't alive any more), the dead client is dropped and a
  fresh one is connected+authenticated in its place - which will itself
  fail if the *previous* session already spent the saved token, correctly
  surfacing as "this needs re-pairing," not a confusing raw exception.
- `self._lock` (an `RLock`) is held for the full duration of one logical
  operation (health-check-then-do-the-real-thing), not just around
  individual `request()` calls - coarser than true request/response
  pipelining would need, but it guarantees two things that actually
  matter here: a file upload's `put_raw` announcement and its raw bytes
  always land back-to-back with nothing else interleaved on the wire, and
  a health check from one caller can never race a real upload from
  another.
- `close_connection()` (called from `main.py`'s shutdown handler) closes
  the held connection cleanly on app shutdown/restart - not required for
  correctness (the OS reclaims the socket on process exit regardless),
  just tidy; the next call reconnects lazily either way.

**What this does and doesn't actually fix - stated plainly, since it's
easy to oversell:** it fixes releasing *multiple* jobs without needing to
re-pair between each one, for as long as the app keeps running and the
connection stays healthy - previously broken outright. It does **not**
make pairing a true one-time-forever step: restarting the app, or the
printer being power-cycled (its normal day-to-day usage pattern here),
still drops the connection and needs one more dial-press before the next
release. That's an inherent consequence of how the printer's tokens work,
not something client-side code can engineer around.

Verified live against the real printer, not assumed from the fix's
design alone: three separate calls through `_PersistentConnection`
returned the exact same connection object and stayed authenticated
throughout, with no reconnect needed between them; separately, a second
process was started fresh specifically to exercise the failure path, and
correctly got the clear "needs re-pairing" `PrinterError` rather than a
confusing raw exception, since that process's connection attempt was
inherently a second session against an already-spent token.

### Detecting a print started outside the app entirely

**A real incident, not a hypothetical:** after a bed-adhesion failure
mid-print, the user paused and cancelled the job using the printer's
own physical dial (triggering `check_and_finish_active_print`'s normal
`cancelled` detection, correctly marking it `failed`), fixed the bed,
then reprinted directly from the printer's own on-device menu - never
touching the app at all. The app had no way to notice: `check_and_
finish_active_print` only ever calls `system_information()` when its
own database *already* believes a job is `printing` - a print started
any other way was completely invisible to it, then and after.

**`jobs.printer_currently_busy()`** breaks that assumption - it reads
the printer's live `current_process` unconditionally, not gated behind
the app's own belief about what's happening. Genuinely still in
progress (not yet `complete`/`cancelled`/`error` - the same three-way
read `check_and_finish_active_print` already uses) means busy,
regardless of how it started. `jobs.untracked_print_in_progress()`
layers the database on top of that: busy, but nothing in the queue is
marked `printing` - it must have started some other way.

**Two places this now matters, per the user** ("we should guard against
sending a job while it's already mid-print"):
- `release()` checks live printer state in addition to its existing
  database-only "already printing" check - a second job can no longer
  be sent while the printer is physically busy, even if nothing in the
  app's own records says so. The existing DB check still runs first
  (cheaper, and gives the more specific "job #N is already printing"
  message when it applies); the live check only matters for exactly the
  case the DB check can't see.
- The admin dashboard shows a live banner - checked fresh on every
  page load, never cached, so it can't show stale - whenever this
  mismatch exists. The background poller (`_log_untracked_print_once`,
  called alongside `check_and_finish_active_print` every 15s) also logs
  it once per episode, not on every tick for however long it continues,
  so it's in the permanent activity log too, not just visible while an
  admin happens to have the dashboard open at the time.

**Verified with a mocked printer reply, every real case, not just
reasoned about:** a genuinely-printing reply, an idle one, a
just-completed one, and an unreachable printer (`PrinterError`, which
must fail open rather than block a release on a check it couldn't
actually perform) all produced the correct busy/not-busy read;
`release()` actually raised and left the job untouched (still
`approved`) when the printer disagreed with the database, and still
succeeded normally when genuinely idle; a job already correctly tracked
as `printing` was never misidentified as "untracked" even though the
printer legitimately reports busy for it; the poller logged exactly
once when an untracked episode began, stayed silent through repeated
ticks of the same episode, and logged again for a genuinely new one
after the first cleared. Confirmed end-to-end over real HTTP too, not
just at the function level: the dashboard banner rendered correctly and
a real release attempt was actually blocked with the intended message.

**The banner resolves the printer's raw filename back to a real job,
not just a bare number.** Per the user, after seeing it originally show
only "29.makerbot" and correctly guessing that number meant something:
every file this app ever sends is named exactly `"<job id>.makerbot"`
(see `storage.queue_paths`/`archive_paths`), and the printer's own
on-device "reprint" option resends that exact same file - so the
filename genuinely *is* the original job's id, not a meaningless
string. `jobs._job_from_makerbot_filename` parses that id back out and
looks the job up (regardless of its current status - by now it's most
likely `done`/`failed`/whatever, never still `printing`, which is the
whole point), and `untracked_print_in_progress` resolves its submitter
alongside it. The dashboard banner now reads "job #4 ('bed_adhesion_
test.stl', submitted by alex) - recorded here as 'failed'" with a link
to that job's own log, rather than a number an admin would have had to
go cross-reference by hand - in this feature's own real motivating
incident, that "failed" status is exactly what confirms it's the same
job being reprinted at the dial. Falls back to the raw filename,
unchanged, if it doesn't parse as one of this app's ids at all or that
id no longer exists. Verified directly (a real job resolves correctly,
including through a directory-prefixed filename; an unrecognized name
and a numeric-but-nonexistent id both correctly fall back to no match)
and end-to-end over real HTTP, confirming the full rendered message.

### Printer camera

**Why this exists:** per the user, both the submitting user and an admin
should be able to see what actually happened to a print, not just a status
word - and an admin specifically needs to be able to visually confirm
which physical print on the bed belongs to which submitter's claim.
`jobs.mark_finished()` now captures a photo of the build plate,
automatically, the moment a job is marked done or failed - regardless of
which outcome - and links it from the job's own log, the global activity
log, and the submitting user's dashboard row.

**The protocol (reverse-engineered, undocumented by MakerBot):** there's
no true one-shot "take a photo" method on this firmware -
`request_camera_frame`/`get_available_cameras`/`get_camera_frame` are all
`method not found`, confirmed live. What actually works is
`request_camera_stream`: the printer immediately starts pushing frames
continuously on the same JSON-RPC socket, each one a 16-byte big-endian
binary header (`frame_size, width, height`, and a 4th field whose meaning
isn't identified) immediately followed by exactly `frame_size` bytes of a
real JPEG image - 640x480, roughly 4fps, roughly 33-34KB/frame observed.
`end_camera_stream` stops it. `capture_one_frame()` (`printer.py`) drives
this: request the stream, hand the *first* frame back to the caller,
request the stream to stop.

**Two bugs this surfaced, both found by testing against the real
printer, not in review:**

1. **Binary frame data isn't JSON, and can't share a naive parser with
   the frames that are.** The wire format is otherwise "concatenated JSON
   objects, framed by brace-counting" (see `_extract_message`) - fine for
   ordinary request/response traffic, but raw JPEG bytes routinely contain
   byte values equal to `{`/`}`, and brace-counting straight into one
   crashes with a `UnicodeDecodeError` trying to `json.loads` binary
   nonsense. The first fix attempt paused the background reader thread,
   had the calling thread take over the raw socket directly, then
   restarted the reader thread afterward - genuinely broken, caught by
   live testing (a `mark_done` call hung indefinitely) before it ever
   shipped: a thread blocked in `recv()` doesn't notice a "please stop"
   flag until data actually arrives, so the two threads ended up racing to
   read the same socket. The real fix keeps all of it on the *one* reader
   thread that's already reading the socket (`_read_loop`/
   `_consume_camera_frame`), handing a captured frame to the waiting
   caller through a `queue.Queue` instead.

   Getting frame boundaries right within that one thread took a second
   round, also only caught live: the original assumption was that every
   single frame is preceded by its own `camera_frame` JSON-RPC
   notification (matching how the very first frame looked), so
   `_read_loop` would go back to normal JSON parsing after each frame,
   expecting another notification next. That assumption was wrong -
   subsequent frames in the stream aren't necessarily preceded by a fresh
   notification - and guessing wrong meant trying to brace-count straight
   into the next frame's raw binary header, the exact same crash as above.
   The fix doesn't guess: while a capture is active (or was, recently -
   see `_camera_mode_until`, a *sliding* deadline that keeps extending as
   long as frames keep arriving, since the printer keeps pushing for an
   unpredictable stretch after `end_camera_stream`), `_read_loop` peeks at
   the next byte before attempting to parse anything as JSON at all. A raw
   frame's binary header can never start with `{` - that would make its
   declared `frame_size` a nonsense value in the billions - so this cheap,
   structural check tells raw frame data apart from a real JSON message
   (a notification, or the `end_camera_stream` reply) without needing to
   assume which one is coming next.

2. **Killing a process that holds the connection with `SIGKILL` (`kill
   -9`) instead of letting it shut down cleanly can wedge the printer's
   session state**, observed directly while iterating on the fix above:
   after a hard-killed test process (whose reader thread had already
   crashed from bug #1, leaving its socket open but unread), a *brand
   new* pairing's very first `authenticate` call was rejected outright
   with `AuthenticationException` - not the already-understood
   already-spent-token case (see "Persistent printer connection" above),
   since this was a token that had never been used. This is why
   `close_connection()` (`main.py`'s shutdown handler) matters in
   practice, not just tidiness: a normal shutdown (`SIGTERM`) reaches it
   and closes the socket cleanly; forcibly killing the process does not,
   and the printer's firmware appears not to reliably notice the
   connection is gone until something like a power cycle. Not something
   client-side code can engineer around further - just a real operational
   note (and the reason a couple of test cycles during this feature's own
   development needed the printer power-cycled to recover).

3. **A successfully-captured frame was being discarded because of a
   failure in the cleanup step *after* it, not the capture itself** -
   caught live, from the very first real end-to-end success of automatic
   completion detection (see that section above): the job's log showed
   `"photo capture failed: No response to 'end_camera_stream' within
   10s"` - that specific error only happens *after* a real frame is
   already sitting in hand, while waiting for the printer to acknowledge
   "okay, I'll stop pushing frames now." The old code required that
   acknowledgment to succeed before returning the frame at all, so a
   slow/missing reply (plausibly the printer being genuinely busy
   settling right at print completion - the exact moment this now gets
   called from) threw the photo away for a reason that had nothing to do
   with whether the photo itself was good. Fixed: `end_camera_stream`'s
   own failure is now caught and ignored - safe to do, since the sliding
   grace-period window (`_camera_mode_until`) already keeps the reader
   thread correctly consuming/discarding any further trailing frames
   regardless of whether this specific acknowledgment ever arrives.
   Verified with a focused unit test (mocking `request()` to simulate
   exactly this sequence) before deploying, not just reasoned about.

**Failure handling:** `capture_photo()` (`printer.py`) raises
`PrinterError` on any failure - camera unreachable, printer powered off,
a capture that time out - and `mark_finished()` treats that as
"no photo this time," never as a reason to block recording the job's
actual outcome. The reason for a missing photo is still recorded in that
job's log entry either way (`"photo captured"` or `"photo capture failed:
..."`), so a missing photo reads as "camera unavailable at that moment,"
not silence.

`Job.photo_path` (nullable, added in schema `2.4.0`) points at
`archive/{job_id}.photo.jpg`, saved directly there rather than through
`scratch/`/`queue/` first - unlike the stl/makerbot/supports files, a
job's photo only ever exists once the job has already reached a terminal
state. Served via `/jobs/{id}/photo.jpg` (`routers/jobs.py`), same
access rule as the model/supports files: the job's own owner, or any
admin.

### Printer status and in-app pairing

**Why this exists:** a real incident during this feature's own
development, not a hypothetical - repeated test pairings against the
same physical printer left the real production app's long-held
connection unable to reconnect, and the only way to find out was a
release failing with a raw "couldn't establish a connection" error and
no indication of what to do about it. The admin dashboard now shows the
printer's connection state plainly (`_printer_status.html`,
`printer.connection_status()`), and a "Pair printer" button
(`POST /admin/printer/pair`) starts pairing right from the dashboard,
instead of needing shell access to run `pair_printer.py` by hand.

**Deliberately never checks the actual connection to answer "what's the
status":** `connection_status()` only reports the outcome of the most
recent *real* attempt (a release or a photo capture) - it never itself
opens a connection or tries to authenticate just to answer a status
question. This isn't a shortcut, it's a hard requirement given how the
printer's tokens work (see "Persistent printer connection" above): a
token is good for exactly one authenticated session, so a speculative
"let's just check if this token still works" call, if it happened to
succeed, would spend that session before the real work ever gets to use
it - confirmed the hard way in this same debugging session, when a
verification check run purely to confirm a fresh pairing worked ended up
being the thing that used up its one shot, requiring yet another
dial-press to actually fix anything. One of four states, tracked on
`_PersistentConnection`:

- `connected` - currently holding a live, authenticated connection.
- `needs_pairing` - never paired, or the last real attempt's failure
  looked like an authentication problem (the printer's own
  `AuthenticationException`).
- `unreachable` - the last real attempt's failure looked like a network
  problem instead (timeout, connection refused).
- `unknown` - paired at some point, but nothing has actually been
  attempted against the printer yet this run, so whether that token
  still works genuinely isn't known without trying it for real. Shown
  as a neutral "not yet verified this session," not an error.

**The "Pair printer" button is shown for all four states, not just
`needs_pairing` - a deliberate change from this feature's first version,
made after a real incident exposed why that gating was wrong.** This
status is in-memory only (see `_PersistentConnection.__init__`) and
therefore can't survive an app restart - a real one happened between a
photo-capture failure that correctly recorded `needs_pairing` and the
next page load, silently resetting the banner back to `unknown` with no
button, even though the connection genuinely still needed re-pairing.
Rather than try to make the status survive restarts (persisting it to
disk was considered and explicitly rejected - the deployment pattern
this is actually built for, per the user, is being turned on once each
weekday morning, i.e. restarting is the normal case, not the exception,
so a disk-persisted "last known status" would just as often be stale
*information* pretending to be current), the fix is structural: the
status text stays best-effort and is never load-bearing for whether the
fix is available. Clicking "Pair printer" is safe regardless of the
current status - `start_pairing()` only ever requests a new token over
HTTP and saves it; it never touches or re-authenticates an
already-`connected` client, so it can't break a connection that's
actually still working unless that new pairing is actually completed
(dial pressed). The only real side effect of clicking it unnecessarily:
if a job is actively printing, it pops a pairing prompt on the printer's
own screen - a momentary surprise for whoever's standing there, not
something that touches the physical print itself (same control-plane/
physical-printing separation already established throughout this
section).

**Pairing runs in a background thread, not inline in the request:**
`pair()` blocks for up to two minutes waiting on a real dial-press -
tying up an HTTP request handler for that long would be its own problem.
`printer.start_pairing()` starts it on a daemon thread and returns
immediately; the dashboard redirects back to itself, sees pairing is now
in progress, and polls `GET /admin/printer-status` every 2 seconds via
htmx until it finishes - the exact same self-terminating pattern
`_jobs_table.html` already uses for slicing progress (see "Upload and
slicing progress" above): the re-rendered fragment simply stops carrying
`hx-trigger` once there's nothing left to wait for, so there's no
separate "stop polling" signal to manage. A failed attempt's error stays
visible on the dashboard (via `pairing_status()`) until either a retry
succeeds or someone tries again - it doesn't just silently disappear.

**A real, live report caught a confusing message right after a genuine
success:** pairing via the button, then pressing the dial, then seeing
the dashboard say "connection not yet verified this session" reads as
"that didn't work" - even though it did (confirmed: the saved token's
file had just been rewritten, and a real request right afterward
succeeded). The wording was accurate but not distinguishing "never tried
anything" from "just succeeded, deliberately not verified yet" (see
above for why a fresh token is never speculatively checked) - both
looked identical. `pairing_status()`'s `just_succeeded` flag (set the
moment `start_pairing()`'s background thread actually saves a new token,
cleared the moment a new attempt starts) lets the dashboard say "Pairing
succeeded - ready for the next release or capture" specifically for that
window, instead of the generic "not yet verified" message that reads as
a possible failure.

### Account actions in the activity log

**Why this exists:** per the user, "all actions should be captured in
the activity log" - registration, disable, re-enable, and delete, not
just job actions. `JobEvent.job_id` (schema `2.5.0`) is now nullable for
exactly this: `None` for an account lifecycle action that isn't tied to
any one job, with the affected user's name in `detail` instead of a
job's filename. `all_events()`'s join with `Job` became an outer join
accordingly - an inner join would silently drop every account row from
the global log, since there's no job on the other side of it to match.
`admin_log.html` shows `(account)` in the File column for these rows
instead of a job link. Logged from `routers/user.py`'s `signup` (actor is
the new user themselves, action `user_registered`) and
`routers/admin.py`'s disable/enable/delete endpoints (actor is the
admin, action `user_disabled`/`user_enabled`/`user_deleted`) - "delete
all" logs one row per user actually deleted, not one combined entry, so
each is still individually visible in the log.

**A different kind of migration than the ones before it:** every prior
`MIGRATIONS` entry was a plain `ALTER TABLE ... ADD COLUMN` - additive,
one statement. Relaxing an existing column's `NOT NULL` constraint isn't
something SQLite supports via `ALTER TABLE` at all, so `_migrate_to_2_5_0`
uses SQLite's standard workaround instead: create a new table with the
relaxed schema, copy every row across unchanged, drop the old table,
rename the new one into its place, recreate the index. Tested the same
way as every migration here - a fresh database (this shape comes
straight from `create_all()`, no migration involved), and a real copy of
the user's own production database (confirmed: version recorded
correctly, every existing row preserved with its original `job_id`
intact, not touched by the "constraint" that's now just permissive
rather than required).

**Caught only after the fact, worth remembering:** `models.py`,
`db.py`, and every router are the *actual* files the user's live dev
server runs, not a copy - `uvicorn --reload` watches them directly, so
saving this migration mid-development applied it to the user's real
production database automatically, the moment the file changed, well
before it had been reviewed or tested in isolation. It happened to be
safe here (a purely additive relaxation can't corrupt data that already
satisfied the stricter constraint), but it's a real gap in how this
project's schema changes get validated: there's no separation between
"editing the code" and "it's live on the real database" when the dev
server has `--reload` watching the actual repo. Test a *risky* migration
(a rename, a backfill, anything that touches existing data - see
"Database migrations" above for why `2.1.0` needed exactly that
distinction) against an isolated copy of the database first, not the
live one, regardless of how confident the migration looks on paper.

### Themes

**Why this exists:** per the user, wanting to add more themes later
(color changes, wallpaper, light/dark) - starting with converting the
existing look into a real, named "Default" theme rather than just
"whatever the CSS happens to say," so a future theme is a genuine
alternative to switch to, not a rewrite of the only option that exists.

**Per-account, not per-browser, and not site-wide.** Explicitly decided
by the user over the two real alternatives: a per-browser preference
(`localStorage`, no schema change needed) wouldn't follow someone to a
different device, and the user wants it to "persist across logins";
a single site-wide choice (one admin-set theme for everyone, like the
shared printer this app is built around) was the other option, rejected
in favor of letting each person - user or admin - pick their own.
`User.theme`/`Admin.theme` (both nullable - `None` means "no preference
set, use the default") are the real schema change this needs (`3.1.0`).

**`base.html`'s existing styles, refactored into CSS custom properties
under `:root` - "Default" + "Light" - with zero visible change.** A
future theme/mode combination adds a `[data-theme="<id>"]` and/or
`[data-theme="<id>"][data-mode="dark"]` block overriding just the tokens
it wants different, not a full copy of every rule in the file.
`themes.py` is the one list of selectable theme ids and mode ids ->
display names, shared by both settings pages and used to validate a
submitted choice, so a bad/stale value can never get saved and silently
fail to match anything in the CSS.

**Light/dark is its own axis, separate from theme, not folded into
it** - `User.theme_mode`/`Admin.theme_mode` (schema `3.2.0`, added right
after `theme` itself), so picking a theme and a mode are two independent
choices; every theme is expected to define both a light and a dark
palette, not just Default. This actually *superseded* an earlier design
choice in this same section: the first version of this feature had
`--bg`/`--text` default to the CSS Color 4 system keywords `Canvas`/
`CanvasText` specifically so the page kept following the OS's own light/
dark preference automatically. Once an explicit, saved, per-account mode
toggle existed, that became the wrong behavior, not just an unrelated
old decision to leave alone - a viewer who explicitly picks "Light"
should get light even if their OS is set to dark, and `Canvas`/
`CanvasText` can't do that; they just track the OS regardless of what
was chosen. Replaced with real hardcoded colors for both modes, and
`color-scheme` set explicitly to `light` or `dark` to match (not "light
dark") so native form controls (checkboxes, scrollbars) follow the
chosen mode too, not the OS.

**`templates_env.current_theme(request)`** is a Jinja *global* function,
not something threaded through every route's own context - `base.html`
(which every page extends) needs a viewer's theme on every single
render, and `request` is already available in every template regardless
of what its own route passed in (Starlette's Jinja2Templates adds it
automatically), so this only needs a global function reading
`request.session`, not a bigger context-passing change touching every
router. **Caught in testing before this shipped:** a settings page's own
context happened to also use the name `current_theme` for the *selected
theme string* being displayed in its dropdown - since Jinja resolves a
page's own context over a same-named global, `base.html`'s
`current_theme(request)` call ended up trying to call that *string*,
crashing every settings page with `TypeError: 'str' object is not
callable`. Fixed by renaming the per-page variable to `selected_theme` -
worth remembering as a real trap: a Jinja global and a template context
key sharing a name silently shadows the global, and only breaks whatever
tries to call it as a function.

**Everything self-hosted, no exceptions - this app runs with zero
internet access (see "Deployment: zero internet access, by design"
above)**: any future theme's fonts, wallpaper images, or anything else
must ship as local static files, never a CDN or external URL.

**A real bug from a legitimate, deliberate usage pattern: two roles,
two tabs, one browser.** The user keeps a user session and an admin
session open side by side in the same browser on purpose - and nothing
about logging in as one role has ever cleared the other's session key
(not new to this feature, just never mattered until something actually
read both `admin_id` and `user_id` from the same session). The first
version of `current_theme()`/`current_mode()` unconditionally preferred
`admin_id` whenever it was present, so in a dual-session browser, the
*admin's* theme silently applied to the user's own pages too - not a
data bug (every account's stored preference was always correct going in
and coming out), a resolution bug in which account's preference got
looked at for a given page. Deliberately not fixed by clearing the other
role's session key on login - that would have broken the exact dual-tab
workflow that surfaced this - fixed instead in
`templates_env._signed_in_account()` by checking which role's page a
given request is actually for (`/admin/...` vs. everything else, the
same split `require_admin`/`require_user` already use), so each tab
resolves to its own role's preference regardless of what the other tab
in the same browser is doing. Verified with the exact reported scenario
in isolated testing: one shared cookie holding both a user session (mode
`dark`) and an admin session (mode `light`) at once, confirming
`/dashboard` and `/admin/dashboard` each independently resolved to the
right one.

### The "Console" theme - sidebar nav, bordered sections, full width

**Why this exists:** per the user - the first real theme this app has
ever had beyond "Default" (see "Themes" above for the machinery this
was all built for, ahead of any second theme actually existing yet):
page links as tabs down the left instead of a top row, each page's
sections enclosed in a border with a contrasting title bar, and the
whole layout using the full browser width instead of the existing
720px-max, centered column.

**Achievable in CSS alone against the exact same markup every page
already renders, except for one specific thing.** The sidebar is just
`<header>` (already flex, already holding `{% block nav %}`'s plain list
of `<a>` tags plus the logout `<form>`) restyled from a horizontal bar
into a `flex-direction: column` sidebar with `align-items: stretch` so
each link/button fills the full width - no template touched anything
for that. The one thing pure CSS genuinely cannot do: group a `<h3>`
and the content that follows it - up to the next `<h3>`, or the end of
the page - into one bordered box. CSS has no "these siblings, up to a
stopping point" combinator, and every theme before this one only ever
needed color-token swaps, so nothing existed to hook into. Solved with a
small inline script instead of touching every template to add explicit
section markup: it walks every `<h3>` under `<main>`, wraps it and its
following siblings in a new `.console-section` div, and re-runs after
every htmx swap (`htmx:afterSwap`) - some sections, like the jobs table,
replace themselves wholesale via polling, and a swapped-in fragment
needs re-wrapping the same way the initial page load did. Gated on
`document.documentElement.getAttribute("data-theme") === "console"` at
the top, so it's a complete no-op under Default or any future
color-only theme.

**`<main>` added around `{% block content %}` and the version footer**
(schema/markup change in `base.html`, not just CSS) - the one structural
template change this needed, since Console's sidebar-plus-content layout
requires a second flex sibling next to `<header>` to size against, and
before this, the footer and the content block were separate top-level
siblings of `<header>` with nothing grouping them into one column. A
plain block element with no styling of its own under Default, so this
is a genuine no-op there - confirmed directly, not just reasoned about
(every existing page rendered pixel-identical before and after).

**A real bug caught only by actually looking at a rendered page, not by
reading the CSS:** the section-wrapping script's original stopping
condition was "the next `<h3>`" alone - on any page whose last section
had nothing after it but the "queue3d vX.Y.Z" footer, that footer got
swept inside the section's own bordered box too. Fixed by also stopping
at a `<footer>` element, not just the next heading.

**Two of my own edits broke the app outright while writing this, in the
exact same way twice** - explaining the nav's plain `<a>` tags and the
footer-stopping fix both used the literal text `{% block nav %}` /
`{% block content %}` inside a *CSS comment*, describing the markup
being styled. Jinja parses `{%...%}` sequences anywhere in the file,
with zero awareness of "this is inside a `/* CSS comment */`, not a real
template tag" - so both comments became phantom, unclosed `{% block %}`
tags, and every single page on the entire site 500'd with
`TemplateSyntaxError: Unexpected end of template` until each was found
(via `grep -n '{%\|%}'` across the file) and reworded to describe the
same thing in plain English instead.

**Colors** - light: a cool off-white page/sidebar (`#eef0f4`)  against a
plain white content area, a deep navy (`#2b3a67`) section-title bar with
light text. Dark: a near-black page (`#14171c`) with a slightly lighter
sidebar/content split, and a brighter indigo (`#3b5bdb`) title bar - a
color that reads as "accent" against a dark background needs
meaningfully more saturation/lightness than the one that works against
white, not the same hex value carried over unchanged the way `--error`
happens to be.

Verified visually, not just by reading the CSS - a real isolated copy,
seeded with actual jobs/admins, screenshotted with Playwright (a
throwaway venv, cleaned up after, per this project's own testing
convention) across three different pages and both modes: the admin
queue (a top-level `<h3>Queue</h3>` section), the admin Admins page (a
`<h3>Add an admin</h3>` section sitting below an *unboxed* table that
has no heading of its own - confirming only actual `<h3>`-marked
sections get the border treatment, not everything on the page), and the
user dashboard (`_jobs_table.html`'s `<h3>Your submissions</h3>`, the
one case where the heading sits nested inside its own wrapper div rather
than a direct child of `<main>` - confirmed the wrapping logic still
groups correctly at that depth, and that the *unheaded* upload form
above it correctly stays unboxed). No console errors on any page. A
same-session test-script bug (not an app bug) was caught and fixed the
same rigorous way: an unscoped `button[type="submit"]` selector in the
test itself matched the sidebar's own newly-full-width "Log out" button
before the intended form's button, silently logging the test account out
mid-script - fixed by scoping the selector to the actual form, not by
changing anything in the app.

### Login rate-limiting

**Why this exists:** per the user - PINs are short by design (low
signup friction), which also makes them easier to guess, and nothing
previously slowed down repeated attempts at all, for either account
type. An admin's password is a higher-stakes target than any one
user's PIN, so this applies to both, not just the short-PIN case that
motivated it.

**Deliberately simple: a fixed threshold and a fixed lockout duration**
(`auth.LOGIN_LOCKOUT_THRESHOLD = 5`, `LOGIN_LOCKOUT_DURATION = 15
minutes`), not escalating durations or per-IP tracking. **Per-account,
not per-IP or global** - the actual threat here is one person guessing
a specific other person's credentials, not general anti-abuse; an IP
on a shared LAN says nothing useful about who's actually attempting a
login, and a global limit would let one person's failed attempts lock
everyone else out.

**`User.failed_login_attempts`/`locked_until` and the identical pair on
`Admin`** (schema `3.3.0`) - both account types work identically via
plain duck typing in `auth.py`'s `check_lockout()`/
`record_failed_login()`/`record_successful_login()`, rather than a
shared base class, matching how little else in this codebase bothers
abstracting over the two account types. `check_lockout()` runs
*before* even checking the submitted password/PIN - both so a
locked-out login doesn't do needless bcrypt work and so a correct
password/PIN submitted while locked out is still rejected with the
"try again in N minutes" message, not "didn't match" - a lockout can't
be probed around by anyone who happens to already know the real
credentials. Reaching the threshold resets the attempt counter back to
0 (rather than letting it climb forever) as it sets `locked_until`;
a real successful login clears both fields outright.

**Caught in isolated testing, the same naive/aware `datetime` gotcha
this app has already hit for `released_at`/`finished_at`/`event.at`:**
the first version of `check_lockout()` did
`account.locked_until - datetime.now(timezone.utc)` directly, and
crashed with `TypeError: can't subtract offset-naive and
offset-aware datetimes`. `locked_until` is always *written* as UTC,
but SQLite round-trips a written datetime back as tzinfo-naive once
re-read - while a value just set moments ago on the same in-memory
object (not yet re-fetched from the DB) is still tzinfo-aware, so this
can't be fixed by just assuming one or the other. Fixed by stripping
tzinfo from both sides (`.replace(tzinfo=None)`) before comparing.
Verified by retesting the exact failing scenario (a correct password/
PIN submitted while locked out) after the fix, confirming the correct
"Too many failed attempts" message renders instead of crashing.

Verified in isolated testing: 4 failed attempts (below threshold)
leaves the account usable with no lockout; the 5th sets `locked_until`
~15 minutes out and resets the counter; a correct password/PIN
submitted while locked out is still rejected with the lockout message;
manually expiring `locked_until` into the past lets a correct
password/PIN through normally and clears both fields; identical
behavior confirmed for admin login; and the `3.3.0` migration applies
cleanly against both a fresh database and a real copy of the actual
production database, correctly defaulting every existing account (4
users, 1 admin) to `failed_login_attempts=0, locked_until=None`.

### Failure reasons shown to the user

**Why this exists:** per README.md's own to-do list - rejection already
shows a required note on the submitter's dashboard, but a `failed` print
showed nothing at all beyond the bare status word, and slicing errors
were believed to be admin-only. Checking the code first (rather than
guessing from the to-do item's own wording, which turned out to be
stale) found the slicing-error half already done: `job_edit.html` (a
user's own draft-editing page) has shown `Job.slice_error` in a
collapsed `<details>` disclosure since that page was built, identically
to the admin dashboard's own tooltip. The real, only gap was
`mark_finished`'s manual failure path having no reason field at all.

**`Job.failure_reason`** (schema `3.4.0`, nullable/additive) is set
whenever a job is actually marked failed - required from an admin's
manual "Mark failed" click (`reason: str = Form(...)` in
`routers/admin.py`, the same required-field pattern `reject()`'s
`admin_note` already uses), and always supplied by the automatic
poller (`check_and_finish_active_print`, e.g. `"detected automatically
- cancelled at the printer"`) - so there is never a `failed` job with
a reason silently omitted going forward, only ones that predate this
change. `jobs.mark_finished()`'s existing `detail` parameter (already
used to prefix the photo-capture log note - see "A build-plate photo on
every finished job") was renamed to `reason` and reused for both jobs:
the exact same string that already explained *why* in the activity log
is now also the one shown directly to the submitter, rather than
inventing a second, separately-worded field for the same fact. Ignored
on success - a `done` job has nothing to explain. Shown in
`_jobs_table.html` ("print failed - nozzle clogged", falling back to
"no reason given" for a `failed` job that predates this field) and in
`admin_finished_jobs.html`'s existing "Note" column, alongside
`admin_note` (a job is only ever one or the other, never both, since
`rejected` and `failed` are different terminal statuses).

**A second, worse real incident from the same migration-ordering class
this project has already hit once - see [[queue3d-version-policy]]'s
2026-09-18 addendum for the full story.** `VERSION` got bumped to
`3.4.0` before `_migrate_to_3_4_0` itself was written, and a later,
unrelated `.py` save in the same work session triggered `--reload`
in between - `init_db()` ran against the real database with the new
version number already in `VERSION` but no matching entry in
`MIGRATIONS` yet, so nothing was pending, nothing migrated, and its
final step still unconditionally recorded `schemaversion.version =
"3.4.0"` regardless. The real database was left *claiming* `3.4.0`
while still missing the `failure_reason` column outright - confirmed
directly (`PRAGMA table_info(job)`, no such column;
`SELECT * FROM job` raised `OperationalError: no such column:
job.failure_reason`) - and, worse than the first incident, this
couldn't self-heal on any later restart either, since a stored version
of `3.4.0` means `init_db()` never sees anything pending for it again,
even once the migration function exists. Caught while testing the new
migration against a copy of the real database, as always - the copy
was already in this broken state, meaning it had already gone live
that way. Fixed by hand: the exact same `ALTER TABLE job ADD COLUMN
failure_reason VARCHAR` the migration function itself performs, applied
directly - not a mechanism change, since `init_db()` did exactly what
it's specified to do; the bug was purely in the *order* the edits were
saved in. The background auto-finish poller never crashed outright
during the broken window (it wraps every tick in a bare
`except Exception: pass`, by design - see "Automatic completion
detection" below) but would have silently missed detecting any print
finishing during it, and any real dashboard load touching `Job` in that
window would have hit a raw 500. Revised rule going forward, recorded in
[[queue3d-version-policy]]: the migration function must be written and
saved *before* `VERSION` is bumped, not just in the same commit -
`VERSION` should always be the last edit in a schema change, with no
further `.py` edits still to come after it.

### Admin PIN reset for users

**Why this exists:** the last open item in README.md's Accounts to-do
list - today there's no recovery path at all for a forgotten PIN short
of signing up under a new name (losing submission history) or an admin
deleting and recreating the account outright.

**No self-service reset flow, by design** - there's no email in this
deployment (see "Deployment: zero internet access, by design") to send
a reset link to, and no security question would mean anything for a
name+PIN account anyway. The only real recovery path in a LAN-only,
in-person deployment is an admin doing it directly: `/admin/users`
grows a "Reset PIN" button per row (`POST
/admin/users/{id}/reset_pin`), same place disable/enable/delete already
live.

**`auth.generate_pin()`** produces a random 4-digit numeric PIN
(`secrets.choice`, not `random` - still a credential, even a
short-lived low-stakes one) rather than taking one typed into a form:
nothing for the admin to type or get wrong, and no chance of a genuinely
guessable choice. No schema change needed - this just re-hashes
`User.pin_hash`, the same field signup already sets.

**Shown back to the admin exactly once**, via the same session-flash
pattern `routers/user.py` already uses for `flash_error`
(`request.session["flash_notice"]`, popped - not just read - by
`users_page`) - a page refresh must not keep re-displaying a credential
that's already been relayed. Styled with a new `.notice` class in
`base.html`, built from the same neutral `--detail-bg`/`--border-strong`
tokens `details.tech-detail` already uses rather than inventing a new
semantic color, since this is the only other place that needs any
highlight beyond plain text or `.error`.

**Logged like any other account action, deliberately without the PIN
itself in the log:** `log_event(session, None, _admin_actor(admin),
"pin_reset", detail=user.name)` - matching `user_disabled`/
`user_enabled`/`user_deleted`'s exact shape (see "Account actions in the
activity log"). The activity log is visible to every admin indefinitely;
a plaintext credential belongs in the one-time flash message an admin
sees and relays immediately, never in a permanent, broadly-visible
record.

Verified end-to-end against an isolated instance, through the real HTTP
routes: the old PIN logs in successfully before a reset, an admin reset
generates and displays a new one, the flash is gone on a second page
load, the old PIN is then rejected ("Name and PIN didn't match" - the
generic mismatch message, not a special "your PIN was reset" one, since
from the login form's own perspective this is indistinguishable from
any other wrong PIN), the new one logs in successfully, and the activity
log shows `admin:<username>` / `pin_reset` / the user's name - never the
PIN value.

### Admins creating other admins, and a permanently-unremovable bootstrap admin

**Why this exists:** a real, direct need, not a planned feature landing
on schedule - the user hit "No module named 'sqlmodel'" trying to run
`create_admin.py` on the actual Pi (wrong Python - needed the app's own
`.venv`, then needed to run as the `queue3d` service account with
`QUEUE3D_DATA_DIR` set, since the real database lives on the external
drive, owned by that account, not wherever a personal login happens to
have write access), and doesn't want to repeat that whole process for
every admin the site will ever need: "I will need to create 2 admin
accounts when I deploy on site. I want existing admins to be able to
successfully create other admin accounts."

**`/admin/admins`** (`routers/admin.py`'s `admins_page`/`add_admin`/
`delete_admin`, `templates/admin_admins.html`) - reachable from every
admin page's nav, same as Users/Colors/etc. Any already-signed-in admin
can create another one directly from here: username + password + confirm,
the same validation `create_admin.py` itself applies (non-empty,
not already taken, both password fields matching, 8+ characters).
Deliberately still not *open* self-signup the way `/signup` is for
users - reaching this page at all already requires `require_admin`, so
this only ever grows the admin group from inside it, never from outside.
No disable/reset-password for another admin here, unlike the Users page -
not asked for, and every admin today has identical, full permissions
with no scoping between them yet ("There may be other admin accounts
later with limited permissions; but, that will be decided later," per
the user - see README.md's to-do list).

**`models.Admin.unremovable`** (schema 6.4.0) is what makes any of this
safe to add at all - per the user, directly: "Let's mark the admin
created from the cmd we just did as 'unremovable'. That means that other
admins cannot delete this user at all." `create_admin.py` now sets it on
every admin it creates; a fresh admin made through the new web UI gets
the column's real default, `False`. Nothing anywhere can ever flip it
in either direction through the UI - not an oversight, the whole point:
since `create_admin.py` is the only way to get the very first admin at
all (there's no UI yet to log into before that), at least one admin
created that way has to exist for the deployment to be usable in the
first place, so marking every one of them permanent means a deployment
can never end up with zero surviving admins, no matter what happens to
any admin created afterward. `delete_admin` checks this before anything
else and refuses outright if it's set; separately (and for a completely
different reason - not permission, just avoiding invalidating the very
session the request is running under) it also refuses an admin deleting
their own currently-signed-in account, full stop, regardless of
`unremovable` - `admin_admins.html` doesn't even render a Delete button
on an admin's own row for exactly that reason, though the route itself
checks it too, not just the missing button.

**The migration backfills every *existing* admin row to `unremovable=1`**,
not the column's own `False` default the way a purely additive column
normally would get here (`_migrate_to_6_4_0`, `db.py`) - deliberate,
and it's the one migration in this project that isn't just "add the
column": every Admin row that exists at the moment this migration runs
was necessarily created via `create_admin.py`, since the web UI this
ships alongside is the *only* other way one can ever come to exist -
there was no such thing as a non-CLI-created admin before this exact
migration. Backfilling this way satisfies the user's own request
literally ("mark the admin created from the cmd we just did") with no
need to know which username(s) to single out by hand, and stays exactly
consistent with the rule `create_admin.py` applies going forward.

Verified in an isolated copy before touching the live database, same
methodology as every other migration: a genuinely fresh database (the
`create_all()` path, a brand new admin correctly starts `unremovable=False`)
and a simulated pre-6.4.0 one (an admin row inserted before adding the
column, then the migration run for real) both produced the expected
result - confirmed live afterward too. Then verified the actual feature
end-to-end over real HTTP, not just the schema: created a second admin
through the UI, confirmed it's listed with a working Delete button and
`unremovable=0` in the database; confirmed deleting the original
CLI-created admin is refused with a clear message; logged in *as* the
new admin and confirmed it can't delete itself (no button shown, and the
same request crafted directly against the real ID is still refused
server-side); confirmed a *different* admin can still delete it.

### Disable/reset-password for other admins, and self-service for everyone

**Why this exists:** the very next question after the previous section
shipped, per the user, directly: "Add reset-password, disable/enable for
other admins (not permanent) now. Also, all users (admins included)
should be able to reset their own password."

**`Admin.disabled`** (schema 6.5.0) is the same idea as `User.disabled`,
added for the same reason and enforced the same way: `auth.get_current_admin`
checks it on every request, not just at login, so disabling someone logs
them out of an already-open session immediately - confirmed live, not
just reasoned about (logged in as a second admin, disabled that same
account from another session, the next request from the disabled one's
own session bounced straight to `/admin/login`). The admin login route
also refuses a disabled account outright, same wording pattern as the
user login route's own "This account has been disabled" message.

**"(not permanent)" - `disable_admin`/`reset_admin_password` both refuse
an `Admin.unremovable` target**, exactly like `delete_admin` already
does, and for the identical reason: disabling (or silently resetting the
password out from under) a permanent admin is a functionally-equivalent
way around the whole point of `unremovable` - it doesn't delete the
account, but it locks it out just as completely. `enable_admin` has no
such check (or a self-check) - re-enabling someone can't lock anyone out
of anything, so there's nothing to guard against.

**Self-targeting is split across three different rules, not one,
because each route has a different actual reason to care:**
- `delete_admin`/`disable_admin` both refuse your own currently-signed-in
  account outright, full stop - not a permission question (that account
  might not even be `unremovable`), purely because either action would
  invalidate the very session the request is running under.
  `admin_admins.html` doesn't even render the buttons on your own row for
  either, though both routes check it server-side too, not just the
  missing button (confirmed directly: a request crafted against the real
  ID from that same session is still refused, not just hidden from the
  UI).
- `reset_admin_password` (another admin generating a random replacement
  for you) has **no** self-check - it doesn't touch `request.session` at
  all, so it can't lock anyone out - but `admin_admins.html` still hides
  the button on your own row anyway, pointing at the dedicated
  self-service form instead (see below), since resetting your own known
  password to a random one you'd have to go read off a flash message is
  just worse than picking your own.
- The self-service change-password/change-PIN routes below work
  regardless of `unremovable`, on purpose - that flag only ever
  restricts what *other* accounts can do to this one, never what it can
  do to itself.

**Self-service, for real this time:** `POST /settings/change_pin`
(`routers/user.py`) and `POST /admin/settings/change_password`
(`routers/admin.py`) - a new form on each account type's own Settings
page. Both require the *current* credential before accepting a new one,
unlike an admin resetting someone *else's* (`reset_user_pin`/
`reset_admin_password`, both pre-existing or added just above) - that
distinction is the actual point, not an inconsistency: an admin acting on
someone else's account is already gated behind a *different*, currently-
authenticated admin's own session, so there's nothing more to prove; this
route is reachable by anyone with an open, unattended session on the
account being changed, so proving the current PIN/password first is the
real security boundary standing between that and a silent takeover.
Logged the same minimal way as every other credential-change event in
this app (`pin_changed`/`password_changed`, no detail beyond who did
it) - the log records that a change happened, never the credential
itself, old or new, matching `pin_reset`/`admin_password_reset`.

**`auth.generate_password(length=12)`** is `generate_pin`'s admin-password
counterpart, for `reset_admin_password` - letters and digits only
(no punctuation, and no visually-ambiguous characters: no `0`/`O`,
`1`/`l`/`I`), since this is relayed in person off a screen or a
handwritten note, not pasted from a password manager. Comfortably clears
`create_admin.py`'s own 8-character minimum.

Verified in an isolated copy: fresh-DB and simulated-pre-6.5.0-migration
paths both produce the correct `Admin.disabled` schema (the migration
itself does *not* backfill anything, unlike 6.4.0's `unremovable` -
every existing admin simply reads as "not disabled," which was already
implicitly true of all of them). Then the full flow over real HTTP:
disabling/resetting an `unremovable` admin refused with a clear message;
a regular admin disabled, confirmed refused at login, confirmed an
already-open session for that same account is kicked to `/admin/login`
on its very next request; re-enabled and logged in again; password reset
by another admin, the generated password shown once via the same
one-time flash pattern `reset_user_pin` already uses, confirmed gone on
a second page load; self-service password change confirmed to reject a
wrong current password, reject mismatched new passwords, reject a too-
short new password, then succeed and take effect immediately (the old
password rejected, the new one accepted) - all mirrored for a user's own
PIN change on the user side. Every new action type
(`admin_disabled`/`admin_enabled`/`admin_password_reset`/
`password_changed`/`pin_changed`) confirmed showing up correctly in the
activity log.

### A deleted admin's past reviews don't go silently orphaned

**Why this exists:** the last open item from the Accounts to-do list,
once admin deletion actually existed to make it a real question - per
the user, directly: "Can the references for admins being deleted be
replaced with admin's name as a string with deleted in parentheses?"

**The actual reference in question turned out to be narrower than it
sounded** - `Job.reviewed_by_admin_id`, a real foreign key to `Admin.id`
set by `jobs.approve()`/`reject()`, is the *only* FK anywhere in this
schema pointing at `Admin` (confirmed by grepping every
`foreign_key="admin.id"` in `models.py` - there's exactly one).
`admin_note` (the rejection reason) doesn't reference an admin at all,
just what they typed. And the activity log's own "who did this" column
was never at risk in the first place: `JobEvent.actor` is a plain string
(`"admin:<username>"`) captured at write time, not a live FK - see that
model's own docstring - so a deleted admin's past approvals/rejections
already showed up correctly on `/admin/jobs/{id}/log` before any of this,
confirmed directly (deleted an admin who'd approved a job, the per-job
log still read "admin:teacher2 / approved," completely unaffected).
`reviewed_by_admin_id` itself isn't rendered anywhere today either - so
this was a real, but currently invisible, latent data-integrity gap, not
a visible bug.

**`Job.reviewed_by_name`** (schema 6.6.0) is the fix, mirroring
`JobEvent.actor`'s own already-correct design instead of patching the FK
in place: `approve()`/`reject()` now set this plain string alongside
`reviewed_by_admin_id`, every time. `routers/admin.py`'s `delete_admin`
finds every `Job` where `reviewed_by_admin_id` matches the admin being
deleted and, right before the row itself is actually removed, rewrites
`reviewed_by_name` to `"<username> (deleted)"` and clears
`reviewed_by_admin_id` to `None` - not left dangling, since SQLite can
reuse a deleted row's integer id for an unrelated admin created later
(no `AUTOINCREMENT` on this table), and a stale FK pointing at a
recycled id would silently resolve to the *wrong* account instead of
just being empty. From that point on, `reviewed_by_name` is the only
thing anything should ever display.

**The migration backfills `reviewed_by_name` for every already-reviewed
job**, not just new ones going forward (`_migrate_to_6_6_0`, `db.py`) -
by joining against whichever admin still exists with that id *right
now*. A job whose reviewer had already been deleted before this
migration ever ran has no admin row left to join against - genuinely,
permanently unrecoverable, not a bug in the migration - so those get a
generic `"(unknown - admin no longer exists)"` placeholder instead of a
real name, with `reviewed_by_admin_id` cleared the same way `delete_admin`
clears it going forward.

Verified in an isolated copy: simulated a pre-6.6.0 database with two
reviewed jobs - one reviewed by an admin who still exists (backfilled to
their real username) and one reviewed by an id that no longer resolves
to anything at all (backfilled to the generic placeholder, FK cleared) -
both came out exactly as designed. Then the real flow over live HTTP:
created a second admin, had them approve a real job (confirmed
`reviewed_by_name` set to their username immediately), deleted that
admin, and confirmed the job's `reviewed_by_admin_id` was cleared and
`reviewed_by_name` now reads `"teacher2 (deleted)"` - while the per-job
activity log, completely unaffected as expected, still correctly showed
`admin:teacher2 / approved`.

### Duration estimates as days/hours/minutes

**Why this exists:** raw total minutes reads badly once a print's
estimate crosses an hour, and outright unreadable past a day - "1500
min" instead of "1d 1h" - per README.md's to-do list.

**`jobs.format_duration(seconds)`** is the one place this formatting
happens, called wherever a duration was previously rendered as
`(seconds / 60) | round | int` directly in a template
(`admin_dashboard.html`, `_jobs_table.html`) - both routers'
`_dashboard_context()` now compute `row.duration_display` once per row,
same pattern already established for `duration_estimate_s` itself (see
"A history-corrected time estimate"), rather than repeating the
formatting logic in Jinja. Rounds to the nearest whole *minute* first,
then decomposes that single number into days/hours/minutes - not each
unit rounded separately, which risks e.g. 59.6 minutes independently
rounding its own minutes-place to `"1h 0m"` from an input that should
just round to `"1h"` as a whole. Drops leading *and* trailing zero-value
units (`"2h"`, not `"0d 2h 0m"`), but always shows at least `"0m"` for
the (unrealistic but not impossible) case of an estimate under 30
seconds.

**`static/countdown.js`'s live "time remaining"/"over the estimate"
countdown gets the identical treatment**, via a parallel
`formatDuration()` written directly in JS rather than shared code -
this app has no build step to share a module between a Jinja-rendered
page and a plain `<script>` tag, and the logic is small and stable
enough that a second copy is the simpler choice. Same algorithm (floor/
modulo decomposition of one whole-number input, not per-unit rounding),
operating on the already-rounded `totalMin` the countdown already
computed from `Date` arithmetic, so this is genuinely the same
formatting rule applied twice, not two different ones that happen to
agree on short durations.

Verified in isolated testing: `format_duration()` against a table of
boundary cases (under a minute, exactly on an hour, exactly on a day,
a value that would round differently unit-by-unit than as a whole,
zero), and both dashboards' rendered HTML for a sliced/queued/approved
job seeded with a >24-hour estimate, confirming `"1d 1h"` renders
correctly end-to-end through the real routes and templates, not just
from the function in isolation. The `countdown.js` side was checked by
direct algorithmic parity and manual trace of the same boundary cases
against the already-verified Python version, not a live browser render
- this project has no JS runtime or browser-automation tool available
in the environment it's being built in right now, worth being upfront
about rather than claiming a check that didn't actually happen.

### Submission timestamp and queue-wait for still-waiting jobs

**Why this exists:** per README.md's to-do list, right after the
duration-formatting item above and phrased almost identically ("the
days/hours/minutes display") - the two are adjacent but distinct asks.
This one is specifically about surfacing *when a job joined the queue*
and *how long it's been waiting since*, not the print's own estimated
length. Ties directly into the next to-do item (an admin-configurable
"old jobs" age threshold) - this is the timestamp/duration that feature
will actually split on.

**`jobs.queue_wait_seconds(job)`** - `job.queued_at` (when
`submit_draft()` actually moved it into the queue) is already recorded
and already used for `queue_position()`'s own ordering; this is the
first thing to actually *display* it. Deliberately `queued_at`, not
`created_at` (upload time) - same reasoning `queue_position()` already
documents: time spent sitting on a draft before submitting isn't queue
wait anyone actually experienced. Same naive/aware handling as
`auth.check_lockout()` (see that function's docstring) - `queued_at` is
always written as UTC but comes back tzinfo-naive once round-tripped
through SQLite, while a freshly-created "now" is tzinfo-aware; both
sides have tzinfo stripped before subtracting. Feeds the same
`jobs.format_duration()` the print-duration estimates already use (see
above) - "waiting 2d 3h 15m" is the identical formatting rule, not a
second one that happens to look similar.

**Scoped to `queued`/`approved` only, not `printing`** - both routers'
`_dashboard_context()` compute `row.queue_wait_display` unconditionally
(it's cheap, and `queued_at` is still set once a job starts printing),
but the templates only ever reference it inside the queued/approved
branch. A printing job already shows live progress and an ETA countdown
(see "Live print progress" and "What release does") - a "waiting since"
figure would read as stale or actively wrong once a job is no longer
waiting on anything, it's being acted on.

**Shown on the admin dashboard as a new "Queued" column** (date/time +
"waiting Xh Ym", the raw timestamp printed directly since `job.queued_at`
is already in scope - only the elapsed-time half needs server-side
computation) and **folded into the user's own dashboard's existing
"position N in queue" line** (`_jobs_table.html`) rather than a new
column there - that page's row already reads as one flowing sentence
per status, and this is one more clause in it, not a separate fact that
needs its own column.

Verified end-to-end against an isolated instance: two real queued/
approved jobs seeded with distinct `queued_at` values (one ~5 minutes
ago, one just over 2 days ago) rendered as `"waiting 5m"` and
`"waiting 2d 3h 15m"` respectively through the real routes/templates on
both dashboards, and a still-`sliced` draft (never queued) correctly
showed neither a timestamp nor a wait time on either page.

### Admin-configurable display timezone

**Why this exists:** per README.md's Appearance to-do list - every
timestamp shown anywhere in the app was UTC, unlabeled as such in most
places even though that's genuinely what was stored and compared
against internally. "Should apply everywhere at once, not per-page" per
the user - a site-wide admin setting, not a per-account preference like
theme/mode.

**`Settings.display_timezone`** (schema `4.4.0`, an IANA zone name
string, defaults to `"UTC"`) - the shared, site-wide `Settings` singleton
table already used for `draft_expiry_days` gets its second field, per
that model's own stated policy of adding columns there rather than
reaching for a generic key/value store until there's a real need.
Deliberately site-wide, not per-account: unlike theme/mode (see
"Themes"), there's no reasonable case here for two people looking at the
same job's timestamp to see two different times - a shared printer used
in one physical place has one real local time.

**`templates_env.local_time`** - a Jinja *filter*
(`{{ some_utc_datetime | local_time }}`), not a global function like
`current_theme`/`current_mode`, since this operates on a value being
displayed rather than reading `request` - most timestamp displays just
swap a raw `.strftime(...)` call for the filter directly. Converts to the
configured zone and formats with `%Z` by default, so what's shown is a
real zone abbreviation ("EST"/"EDT"/"UTC") rather than the old hardcoded
`"UTC"` string every display used to have baked into its own format
regardless of whether that was still accurate. Same naive/aware handling
as `auth.check_lockout()` and `jobs.queue_wait_seconds()` (see either's
docstring) - every stored datetime is UTC but comes back tzinfo-naive
from SQLite, while one just created in-process is still tzinfo-aware;
`.replace(tzinfo=None)` first normalizes either case the same way before
attaching real UTC tzinfo and converting. `dt=None` returns `""` rather
than requiring a separate `{% if %}` guard in every template that uses
it - several existing call sites (`admin_finished_jobs.html`'s
`finished_at`, for a job that hasn't finished yet) had exactly that
guard, now redundant and removed.

**Cached in-process, not re-read from the DB on every call - a real
design choice, not a premature optimization:** `local_time()` runs once
per *timestamp shown*, not once per page - `/admin/log` alone can render
hundreds of rows. A module-level global (`templates_env`'s
`_display_timezone`/`_display_timezone_name`, updated via
`set_display_timezone()`) avoids hundreds of redundant single-row
lookups for a value that only ever changes when an admin explicitly
saves a new one. Safe specifically because this app is single-process
(one Pi, one SQLite file - see "Deployment: zero internet access, by
design") - there's no other worker process that could see a stale
value. Primed once at startup (`main.py`, from the stored `Settings` row
- falling back to a fresh `Settings()`'s own "UTC" default on a brand
new database with no row yet) and updated immediately in
`routers/admin.py`'s `update_settings()` the moment a new value is
actually saved - a change takes effect for every viewer right away, no
restart required, confirmed live in isolated testing (saved a new
timezone through the real route, then re-rendered the dashboard and
activity log in the same running process and saw both switch
immediately).

**Validated against Python's own `zoneinfo.available_timezones()`**
(`templates_env.is_valid_timezone`) - 598 real IANA names on this
machine, backed by the system's own tzdata (confirmed working with no
`tzdata` pip package installed - Python's `zoneinfo` module falls back
to the OS's `/usr/share/zoneinfo`, present by default on essentially
every Linux distribution, so this needs no extra dependency and no
internet access to work, consistent with this project's zero-internet
deployment target). An invalid submitted value is rejected with a clear
error and never saved, same pattern as every other settings-form
validation in this app; `set_display_timezone()` itself also falls back
to UTC defensively for a bad *stored* value (should never happen given
that validation, but a template filter is the wrong place to let a bad
value take down every page that shows a timestamp). The settings page
offers the full sorted list in a plain `<select>` rather than a curated
short list - a school deployment could be anywhere, and there's no way
to guess which handful of zones would actually be relevant.

**A genuinely different migration-ordering outcome than either previous
incident (see [[queue3d-version-policy]]) - this time following the
revised rule worked exactly as intended, worth recording precisely.**
The migration function was written and registered in `MIGRATIONS`
*before* `VERSION` was bumped, per that rule. Because `init_db()`
compares `MIGRATIONS`' own keys against the *stored* version - not
against `APP_VERSION` - the `4.4.0` migration actually ran on the very
next `.py`-triggered reload, *before* `VERSION` was bumped at all:
confirmed directly by copying the real production database mid-task and
finding `display_timezone='UTC'` already present while
`schemaversion.version` still read `"4.3.0"`. This is a real, different
side effect from either prior incident (the first: a stored version
correctly bumped, no schema change; the second: a stored version bumped
too early, the promised column never added) - here the opposite
happened, an under-reported version with the column already genuinely
correct - and it's the *safe* direction to be wrong in: `VERSION`
merely lagged reality for one bump cycle rather than a table missing a
column it was recorded as already having. Confirmed self-correcting:
once `VERSION` was actually bumped to `4.4.0`, the next reload re-ran
`_migrate_to_4_4_0` (a safe no-op, guarded by the same "column already
exists" check every migration here uses) and `schemaversion.version`
caught up to the true state.

### Old jobs (age-threshold backlog view)

**Why this exists:** per README.md's to-do list - a still-undecided
queued/approved job could sit indefinitely with nothing surfacing that
it's been forgotten. Builds directly on the queue-wait timestamp/display
work above (same "Queued" section) - this is the exact data that feature
made visible, now actually split on.

**The one design question the to-do list itself left open - confirmed
directly with the user rather than guessed:** does the threshold apply
to `printing` jobs too, or only `queued`/`approved`? Answer: queued/
approved only. A printing job is being actively acted on, not sitting in
an undecided backlog, and already has its own live progress/ETA display
(see "Live print progress") - a second, different kind of staleness
signal mixed into the same view would just be confusing. `jobs.is_old_job()`
encodes this scoping in one place, matching the same reasoning
`queue_wait_seconds()` already uses.

**`Settings.old_job_threshold_days`** (schema `4.5.0`, defaults to 30 -
the to-do list's own example value) - the shared, site-wide `Settings`
singleton gets its third field, same pattern as `draft_expiry_days`/
`display_timezone`.

**Mutually exclusive, not just flagged - a real split, per the user:**
`_dashboard_context()` (the normal `/admin/dashboard` queue) now excludes
anything `is_old_job()` returns true for, and `/admin/jobs/old`
(`admin_old_jobs.html`) shows exactly that excluded set, computed by
`_old_jobs_context()` from the same `active_jobs(session)` query filtered
the other way - one shared source of truth, not two independently-built
lists that could drift apart. The main dashboard still flags the count
(`old_job_count`) with a link straight to the backlog view, so a growing
backlog doesn't go unnoticed just because it's out of the way.

**Fully actionable from the old-jobs page, not just a read-only list -
a real design wrinkle this raised:** since an old job is excluded from
the main dashboard entirely, approve/reject/release have to actually
*work* from `/admin/jobs/old` too - there's nowhere else left to reach
them from. `_perform_action()` (shared by every queue-action route)
gained a `return_to` form field and a small `_RETURN_TARGETS` lookup
(return path -> which template/context re-renders an error) - every
pre-existing form on the main dashboard keeps working unchanged (it
never sends the field, so it defaults to `/admin/dashboard`), while the
old-jobs page's own copies of those same forms send
`return_to=/admin/jobs/old` so a successful action - or an inline error,
same as always - lands back on the page the admin was actually looking
at, not silently back on the main dashboard.

**Two new actions specific to this page, added after the user tried it
and asked for both as immediate follow-ups:**
- **`jobs.requeue_job()`** - "move to back of queue" - resets
  `queued_at` to now without touching status, so the job goes back to
  being a normal, fully live queue entry (and, since `queue_position()`
  orders by `queued_at`, genuinely back of the line, not just redisplayed
  differently). The "still relevant, just needs another chance" option
  next to outright deleting one.
- **`jobs.delete_old_job()`/`delete_all_old_jobs()`** - a *genuine*
  delete, not another terminal status like `reject()` (which deliberately
  keeps the job and archives its files as a permanent record) - per the
  user, "removes the job... and deletes the model files, with no undo,"
  the same delete semantics as the separate (not yet built) to-do item
  for a user deleting their own queued job. `storage.delete_job_files()`
  is the one place this app actually unlinks files outright rather than
  archiving them. The job's own prior event history (submit, slice,
  queue-submission, etc.) is deleted right along with it - once the job
  itself is gone, an orphaned row a global log join can no longer resolve
  to a filename is clutter, not history worth keeping - and what actually
  persists is one new, `job_id=None` event recording the deletion itself
  (who did it, the filename, and who originally submitted it), the exact
  same pattern `user_deleted` already uses for a `User` that's gone by
  the time anyone reads that log entry back. `delete_all_old_jobs()` is
  the same logic looped over every currently-old job (re-checked fresh,
  not trusting whatever the page happened to render a moment earlier),
  one commit per job rather than a single batched one - simple over
  optimal for what's expected to be a handful of jobs at once, not
  thousands.

Verified end-to-end against an isolated instance, through the real HTTP
routes: a queued job past the threshold and an approved one past a
different threshold both excluded from the main dashboard and shown on
`/admin/jobs/old`; a job seeded as `printing` with an old `queued_at`
confirmed to stay off the old-jobs list entirely; approving and
rejecting from the old-jobs page confirmed to redirect back to it, not
the main dashboard; deleting one job confirmed to remove its DB row,
its queued files, and its own prior event history, while leaving exactly
one `job_deleted` entry (with the right actor/filename/submitter) in the
global log; requeuing confirmed to reset `queued_at` to a fresh
timestamp, move the job back onto the main dashboard, and log a
`requeued` event; and "delete all" confirmed to remove every currently-
old job in one request while correctly sparing a job that was never old
to begin with.

### Delete your own queued job

**Why this exists:** per README.md's to-do list - a user submitting a
model they've since changed their mind about had no way to remove it
short of asking an admin. Directly completes the "Audit log" to-do item
about logging a delete, alongside the admin-side equivalent above.

**Genuinely shares its delete mechanics with `delete_old_job()`** (see
"Old jobs" just above) via a new private `_delete_job_genuinely()`
helper both now call - a real, unrecoverable delete (own event history
purged, files unlinked via `storage.delete_job_files()`, one new
`job_id=None` "job_deleted" event persisting), not another terminal
status like `reject()`. Only the actor and the log detail's wording
differ: `jobs.delete_own_job(session, job, user)` logs actor
`user:<name>` with just the filename in the detail (no "submitted by"
clause - the actor already says who, since here the actor and the
submitter are always the same person), while `delete_old_job()` logs
the admin's own actor plus who originally submitted it.

**Originally scoped identically to the admin version - `queued`/
`approved` only, not "any job the user owns."** Once released and
`printing`, an admin is already acting on that job; deleting it out
from under that would be a materially different, riskier action the
to-do item never asked for - `_delete_job_genuinely()`'s shared
`_require_status()` check enforces this the same way for both callers.
Since extended twice, each time to exactly what was actually asked for
rather than every terminal status at once: `slice_failed` (a draft that
never successfully sliced has nothing worth keeping and no "submit"
option either), and `rejected` (per the user - "I don't want to keep
rejected jobs around," old USN `ddg.stl` jobs rejected back when the
supports calculations were off, with no way to get rid of them, only
"Restore & edit," which leaves the original rejected record sitting
there regardless). Deliberately still not `done`/`failed`/`expired` -
those raise different questions of their own (a done job is a real
completed-print record; a failed one might be worth keeping to see why;
an expired draft never even reached a decision) worth their own
consideration, not bundled in by assumption.

Extending to `rejected` surfaced a real bug in
`storage.delete_job_files`, not just a one-line allowed-statuses
change: it only ever knew about `scratch/` (drafts) and `queue/`
(everything else) - a rejected job's files actually live in `archive/`
(moved there by `reject()`), so deleting one without fixing this would
have removed the database row while leaving the real files behind as
permanently orphaned garbage, unreachable by anything since nothing
else ever looks in `archive/` for a job that no longer exists. Now
branches on `TERMINAL_STATUSES` too, and removes the archived photo
file if one exists. Verified directly against real files, not just
reasoned about: created actual `archive/` files (stl, makerbot, photo)
for a job, rejected it, deleted it, and confirmed all three were
genuinely gone afterward alongside the database row and the correct
audit log entry; separately confirmed a `done` job - deliberately still
out of scope - still refuses deletion.

**`routers/user.py`'s `_owned_draft()` helper got renamed to
`_owned_job()`** - it was always a plain ownership check with no actual
draft-specific logic in its body (the draft-status check itself always
lived in each *caller*, e.g. `edit_draft()`), and this route needed the
identical ownership check for a job that's very much not a draft
(`queued`/`approved`). Renaming the one shared helper to match what it
actually does, rather than adding a near-duplicate under a
draft-specific name, keeps `edit`/`reslice`/`submit`/`delete` sharing
one ownership check.

Verified end-to-end against an isolated instance through the real HTTP
routes: the delete button renders only for a `queued`/`approved` row and
not for a `printing` one; attempting to delete another user's job
returns a 404 (ownership enforced); attempting to delete one's own
`printing` job is rejected with the same inline flash-error pattern
every other job action already uses, leaving the job untouched;
deleting an owned `queued` job removes the DB row, its files, and its
own event history while leaving exactly one `job_deleted` entry (actor
`user:<name>`, just the filename) in the global log; and an unrelated
job belonging to a different user is confirmed untouched throughout.

### Resize and auto-fit (model controls, part one)

**Why this exists:** the user's full "model controls" ask - rotate on
any axis, "snap to surface," resize maintaining aspect ratio, and a
one-click auto-fit - spelled out in full before pausing the OBJ feature
mid-session to build this instead. Sequenced deliberately: resize/
auto-fit first (a scale number, no new 3D interaction needed), rotation/
snap-to-surface as a separate, materially bigger follow-up (real
face-picking and rotation UI) - not attempted in the same pass.

**`Job.scale_factor`** (schema `5.5.0`, defaults `1.0`, always uniform -
never per-axis, so proportions can never distort, per the user
explicitly: "maintaining the aspect ratio"). Applied in
`slicing/stl_to_3mf.build_3mf()` by scaling every vertex *before*
`center_vertices()` runs, not after - deliberate ordering: a uniform
scale from the origin doesn't change where a mesh's area-weighted
centroid sits relative to its own geometry, only its absolute size, so
scale-then-center gives the same result centering-then-scaling would,
but only if centering runs last against the already-final geometry.
Threaded all the way through the existing subprocess chain (`slice.py`
gains a `--scale` CLI flag, `pipeline.run_slice()` and
`jobs.slice_and_update()`/`start_reslice()` each gain a `scale_factor`
parameter) rather than becoming a second, parallel pipeline.

**Bounded** (`MIN_SCALE_FACTOR`/`MAX_SCALE_FACTOR`, 1%-1000%) and
validated in `start_reslice()` before anything about the job changes -
an out-of-range value is rejected with a clear message and the job's
previous state (status, scale) is left completely untouched, same
"validate before mutating" shape every other job action here already
uses.

**Where this lives: the existing draft edit page (`job_edit.html`),
not the upload form** - every one of the user's own motivating examples
(a model that failed to slice, a model that doesn't fit) is about fixing
something *already uploaded*, which is exactly what this page already
exists for (re-slicing with new support settings). Scoped to
`sliced`/`slice_failed` drafts only, matching `start_reslice()`'s
existing status guard - not yet extended to an already-`queued`/
`approved` job, which raises the still-open "re-slice in place or count
as a new submission" question a different to-do item already flags.

**Live, before-you-commit preview - no server round trip to see the
effect of a scale change**, matching this app's existing "instant
client-side preview" philosophy (see "Upload and slicing progress").
`static/preview.js` now keeps the model exactly as loaded/parsed
(`rawGeometry`, never mutated) separately from what's actually
displayed, so a new exported function (originally `setPreviewScale(factor)`,
later folded into the combined `applyTransform()` once rotation joined it
- see "Rotate and snap to surface" below) can always compute fresh from
the true original size - repeated scale changes never compound - and a
new `autoFitScale()` computes the largest factor
(capped at 1, so this only ever shrinks an oversized model, never grows
one that already fits) that would bring the *original* geometry within
the build plate on all three axes at once. The one-click "Auto-resize
to fit build plate" button in `job_edit.html` just calls that and
applies the result; the scale number field re-renders live on every
keystroke via the same function. The **read-only "View 3D" page for an
already-submitted job** (`job_preview.html`) needed the identical
treatment for a different reason: the stored model file is always the
original, unscaled upload (scaling only ever happens transiently inside
`build_3mf()`, never rewriting the file itself), so without reapplying
`job.scale_factor` there too, that page would have silently shown the
wrong size for anything actually sliced at a non-default scale.

Verified against the real pipeline, not just the vertex math: re-slicing
a real test model at 50% scale produced an actual `.makerbot` whose own
recorded print height was exactly half the unscaled version's (a
same-model X-axis comparison came out less clean-looking at first - a
pre-existing skirt/purge-line artifact in the print profile inflating
the smaller print's proportional footprint, already documented
elsewhere in this project, not a scaling bug - confirmed by checking the
input mesh's own vertex extents directly, which scaled to exactly 50%
in both axes). End-to-end HTTP flow verified too: an out-of-bounds scale
rejected with the job's prior state intact, a valid 50% re-slice
persisting correctly and producing a correctly half-sized real output
file. The interactive/browser half was verified with a real headless
browser (not just read as correct): live info-text updates as the scale
input changes, the "too large" error state appearing and clearing
correctly, and auto-fit computing the exact right shrink factor for a
genuinely oversized synthetic model (limited by whichever bed dimension
was tightest) with zero console/page errors throughout.

### Rotate and snap to surface (model controls, part two)

**Why this exists:** the second half of the user's "model controls" ask,
resumed the same session after a real slicing failure (`Flexi_Seal.stl`,
see "A real stuck-slicing incident" below) made it concrete: resizing a
genuinely asymmetric model can never fix `mbotmake`'s bed-centering
check (that check is a scale-invariant ratio), but *reorienting* it can
- confirmed by testing the actual failing file at a sweep of rotation
angles through the real pipeline before writing any UI for this at all.

**The highest-stakes correctness question this raised, verified
numerically before trusting any of it:** does `THREE.BufferGeometry`'s
`.rotateX().rotateY().rotateZ()` (what the live preview already uses)
compose the same way as a matching sequence of rotation matrices in
Python (what has to run inside the actual slicing subprocess)? Confirmed
yes, to float32 precision, across 6 test cases including large/negative
angles - by actually loading this project's own vendored Three.js build
in a real browser and comparing its output point-for-point against
`slicing.stl_to_3mf.rotate_vertices()`, not by reasoning about
conventions from documentation. That numeric parity is what
`Job.rotate_x/y/z` (schema `5.6.0`, degrees, defaults `0.0`) and
`rotate_vertices()` depend on being true - a silent mismatch here
wouldn't just look wrong in the browser, it would mean an approved job
prints in a different orientation than whatever anyone actually looked
at and signed off on.

**A second, separate correctness trap found the same way, this time by
being *wrong* first and catching it before shipping:** "snap to
surface" needs to compose an *additional* rotation on top of whatever's
already dialed in, then express the combined result back as three
angles a future re-slice can reproduce. The natural-looking approach -
`new THREE.Quaternion().setFromEuler(new THREE.Euler(x, y, z, "XYZ"))`
to represent "the same rotation as calling `.rotateX(x).rotateY(y)
.rotateZ(z)`" - is simply false; verified this directly (built both,
compared results, they disagreed) before it ever reached working code.
Three.js's `"XYZ"` Euler order is *intrinsic* (each axis is the model's
own, already-tilted-by-the-previous-rotation axis); `rotateX/Y/Z` calls
compose *extrinsically* (each axis is the fixed world axis, unaffected
by earlier rotations) - two genuinely different rotations that happen
to share a label. The actual equivalent, confirmed by a full compose-
then-decompose-then-reapply round trip (not just a single-stage check)
across three test cases including angles past 90°: Three.js's
*intrinsic* `"ZYX"` order. `preview.js`'s `computeSnapRotation()` is
built entirely on that confirmed equivalence, commented with the
reasoning directly in the code so a future change to this can't
casually reintroduce the mistake without at least reading why it's
there.

**Order of operations in `stl_to_3mf.build_3mf()`: rotate, then scale,
then center** - rotate first so scaling and centering both act on the
model's actual print orientation, not its as-authored one; rotate/scale
order between those two specifically doesn't matter (uniform scale
commutes with any rotation), but centering has to run last regardless,
since it needs the final, already-transformed geometry to compute the
right centroid and the right new Z-floor (rotating can change which
point is actually lowest). Threaded through the same subprocess chain
resize already established (`slice.py --rotate-x/-y/-z`,
`pipeline.run_slice()`, `jobs.slice_and_update()`/`start_reslice()`) -
no bounds check on the angles the way scale gets one, since sin/cos are
periodic and there's no such thing as "too rotated" the way there's a
"too small/too large" for scale.

**The UI: three degree fields (live preview per keystroke, same
`applyTransform()` resize already uses, now also taking `rotateX/Y/Z`)
plus a "Snap to surface" mode** - click the button, then click a face on
the model itself; a raycast against the currently-displayed mesh finds
which face was clicked, and `computeSnapRotation()` returns the new
total rotation that makes that face the new bottom. Click-vs-drag is
distinguished by movement distance between pointer-down and pointer-up
on the preview container (over ~5px counts as a drag, e.g. orbiting the
camera via `OrbitControls`, and is ignored) rather than by which DOM
element fired the event - the canvas is the same element either way.
`autoFitScale()` (see "Resize and auto-fit" above) was made
rotation-aware at the same time: reorienting a model changes its actual
footprint on the plate, so fitting it has to account for whatever
rotation is currently applied, not just the as-uploaded shape.

Verified end-to-end, both halves: the actual `Flexi_Seal.stl` file, run
through the real production `--rotate-z 45` code path (not a one-off
script), reproduced the exact passing result found during the original
investigation and produced a genuine `.makerbot`. In the browser (a
real headless browser, not just read as correct): manual rotation
inputs live-updating the preview and correctly changing reported
dimensions; a miss-click in snap mode changing nothing and staying in
snap mode; a genuine drag across the model also changing nothing
(confirmed separately that the drag *did* orbit the camera, ruling out
"nothing happened because the drag didn't register" as a false
explanation); and a real click that hits the model updating all three
rotation fields, exiting snap mode, and changing the reported
dimensions to match the new orientation. Getting a *reliable* real click
onto the model at all took its own debugging - dead-center of the
preview canvas turned out to miss a hole in the middle of this
particular shape, and a synthetic `page.mouse.click()` at fixed
coordinates proved less trustworthy in this environment than Playwright's
own locator-relative `click(position=...)` - worth remembering as a
real, environment-specific quirk if this class of test needs writing
again, not evidence the underlying feature was ever broken.

### A real regression, found immediately after shipping the above: a canvas-sizing race condition

**What happened, reported directly:** "This is a major regression
failure" - the 3D preview rendered as a completely blank box, on pages
that had nothing to do with rotation at all (a previously-and-still
successfully-sliced plain `.stl`, viewed on the ordinary read-only
"View 3D" page). Investigated the obvious suspects first, and ruled
each out with real evidence rather than assumption: the real server was
serving byte-identical, correct files (diffed directly against a known-
good copy); the exact template rendered correctly server-side for the
real job in question; and the identical code, driven through a real
headless browser in an isolated test, produced a working preview with
correct dimensions. All of that pointed away from the code being
broken - until the user reported the actual fix that worked: switching
their browser to fullscreen and back made the preview reappear.

**That one detail was the real diagnosis.** `preview.js`'s scene setup
already had exactly the right idea - call `resizeToContainer()` once
immediately, so the canvas is sized correctly from the very first
frame, rather than waiting on the asynchronous `ResizeObserver` callback
that also keeps it sized correctly later. The bug was in the timing of
that *immediate* call: it runs right after `container.innerHTML = ""`
and appending a brand new `<canvas>`, reading `container.clientWidth`/
`clientHeight` at that exact moment - which can occasionally race the
browser's own layout pass and catch a transitional value before the
container has actually taken on its real, CSS-computed size. Whatever
wrong size that first call catches, the camera's aspect ratio and the
renderer's pixel buffer get locked to it, and nothing was ever queued to
correct it afterward - unless something else happened to trigger the
`ResizeObserver` later, which is exactly what a manual window resize
does. A live page that never gets resized by hand would have stayed
blank indefinitely.

**Fixed by scheduling one more, guaranteed-correctly-timed resize via
`requestAnimationFrame`, right alongside the existing immediate call -
not by replacing it.** `requestAnimationFrame` callbacks run after the
browser's next layout/paint pass completes, so this second call is
certain to see the container's real, settled size even on the rare
occasion the immediate one didn't. Cheap enough to always do rather than
trying to detect "was that first read actually wrong" from inside the
function, which isn't reliably knowable at all from there. Verified the
fix doesn't regress the ordinary, already-working case: loading a real
model in a real headless browser still produces a canvas sized exactly
to the container's CSS dimensions and a correctly-rendered preview.

**Explicitly not claimed as a deterministically-reproduced fix** - this
is a genuine browser layout timing race, not a logic bug with a fixed
input/output to assert against, and automated headless testing (this
project's main verification tool throughout) tends to run against an
already-fully-laid-out page, which is exactly the condition under which
this race doesn't occur - worth remembering as a real gap in what this
project's testing approach can catch on its own; a live user's
real-world page load remains the only way this particular class of bug
actually surfaces.

### On-canvas drag handles (model controls, part three)

**Why this exists:** immediately after the rotation feature above
shipped, the user asked directly: "Is it possible to have handles for
the object in the preview to resize and rotate visually instead of only
by numbers in the fields?" - the number fields plus click-to-snap cover
precision and one specific reorientation, but not general-purpose
"grab it and turn/resize it by eye," which is how most 3D editors work.

**Built on Three.js's own `TransformControls` addon** (vendored at the
exact same r160 revision as the rest of this project's Three.js build,
per [[queue3d-deployment-network]]'s no-CDN rule - fetched once,
unmodified, and committed verbatim rather than hand-rolled, since
reimplementing a drag-gizmo's picking/highlighting/screen-space-sizing
logic from scratch would just be reproducing a well-tested library
worse). Two buttons, "Rotate (drag)" and "Resize (drag)", each toggling
the gizmo on in that mode - mutually exclusive with each other and with
"Snap to surface" (all three would otherwise fight over pointer events
on the same canvas), same pattern the existing snap-mode toggle already
used.

**The integration challenge, and how it resolves cleanly with this
app's existing architecture:** `TransformControls` expects to freely
manipulate an attached Object3D's own position/quaternion/scale - but
every other transform in this file is deliberately baked directly into
vertex data instead, with the displayed mesh's own Object3D transform
always kept at identity (see "Rotate and snap to surface" above, and
`computeSnapRotation`'s own comment on why). Rather than fight that
invariant, the gizmo is allowed to freely manipulate the mesh's
Object3D transform *during* a drag (cheap, and exactly what the library
expects), and only on release (`TransformControls`' own `"mouseUp"`
event - fired once a handle is actually let go, not on a mere hover)
does `commitGizmoTransform()` read the resulting delta back out,
compose it onto a running baseline, re-bake the combined result via the
same `applyTransform()` every other control uses, and re-attach the
gizmo to the freshly-created mesh `applyTransform` always produces -
restoring the identity-transform invariant before the next drag ever
starts. `dragging-changed` (a `TransformControls` event fired the
instant a drag actually starts/ends) disables `OrbitControls` for the
duration, so grabbing a handle can't also spin the camera - both listen
on the same canvas element.

**Reused, not reinvented, the rotation math this session had already
verified numerically:** the mesh's own quaternion after a drag session
(starting from identity, since it's always freshly rebaked before each
drag) *is* that drag's rotation delta - composing it onto the running
baseline via the confirmed intrinsic-`"ZYX"`-Euler equivalence is
exactly `computeSnapRotation`'s own `snapQuat.multiply(currentQuat)`
pattern, reused rather than re-derived.

**A hard product requirement needed its own small fix, not a
work-around avoided:** "resize... while maintaining the aspect ratio"
- but `TransformControls`' scale mode ships with three independent
per-axis handles plus one uniform corner handle, matching a general-
purpose 3D editor's needs, not this app's specific one. Rather than try
to hide the individual X/Y/Z handles (their visibility logic is bound
up with the same flags used for rotate/translate axes too, and there's
no clean way to keep only the uniform corner visible), every drag in
scale mode is forced uniform directly: a listener on `TransformControls`'
own `"objectChange"` event (fired continuously *during* a drag, not
just at the end) copies whichever axis actually changed into the other
two, every frame - so which handle was grabbed stops mattering at all;
every scale drag behaves as one single resize.

**A real, numerically wild bug found by testing the actual drag
interaction, not just reading the code as correct:** `TransformControls`'
own free/uniform ("XYZ" corner) scale handle computes its factor as
`pointEnd.length() / pointStart.length()` - the ratio of distances from
the object's origin to where the drag's start/end points intersect an
invisible helper plane. A drag that happens to *start* very close to
that origin makes `pointStart.length()` near zero, and the resulting
ratio can come out wildly large, or even negative (dividing by a value
that crossed zero) - reproduced directly while testing this feature: a
single drag turned a 100% scale into `-558084917.9%`. This is a real,
known characteristic of the underlying library's own math (not a bug
introduced here, and not something hiding the handle would fix, since
any sufficiently-central click has the same issue) - but "resize while
keeping proportions" should never actually show a negative or
astronomical result regardless of what the widget's own math allows.
Fixed by clamping the committed result in `commitGizmoTransform()` to
the exact same `MIN_SCALE_FACTOR`/`MAX_SCALE_FACTOR` bounds
`jobs.start_reslice()` already enforces server-side (1%-1000%),
falling back to the pre-drag scale entirely for a non-finite or
non-positive result rather than clamping a meaningless number into
range. Verified directly: the same degenerate near-origin drag that
previously produced the billion-percent result now lands safely inside
bounds every time, while an ordinary, well-clear-of-center drag still
produces its genuine, unclamped ratio.

**A real test-harness bug worth remembering the shape of, caught while
verifying this rather than shipped as a false "it works":** an
automated click on the "Rotate (drag)"/"Resize (drag)" buttons -
sitting further down the settings form than the preview canvas -
scrolled that button into view, which pushed the canvas (much higher up
the page) entirely out of the viewport; screen coordinates computed
*before* that click were then stale, silently producing "hits" at
off-screen coordinates that a real drag can never reach. The actual
raycasting/projection math was correct throughout - the bug was in
trusting a canvas position computed before an intervening action that
could move it, the same general lesson this session's snap-to-surface
testing already hit once (a stale click position computed before a
drag had orbited the camera). Fixed in the test by re-fetching the
canvas's position (scrolling it back into view first) immediately
before every interaction, never reusing a position computed earlier in
the run.

Verified end-to-end in a real headless browser: a real drag on the
free-rotate handle changes all three rotation fields and exits nothing
else's mode; a real drag on the free-scale handle (from a well-clear-of-
center starting point) changes the scale field to a sane, positive
value; the same degenerate near-origin drag is safely clamped rather
than producing an absurd number; turning on either drag mode correctly
turns off "Snap to surface" (and vice versa, per the existing mutual-
exclusivity code); and zero console/page errors throughout.

### A real bug found using the feature for real: whole-number-only percent/degree fields

**User report, using "Auto-resize to fit build plate" on a real model
(a fighter jet): "The resize field accepts only whole number
percentages, not float with a decimal."** All four of the scale/
rotation `<input type="number">` fields (`scale-percent`, `rotate-x/y/
z`) had `step="1"` - a leftover from when these fields were first added
and always expected to be hand-typed in whole units, never revisited
once `autoFitScale()`, `computeSnapRotation()`, and the drag gizmo's
`commitGizmoTransform()` all started *computing* values to 2 decimal
places (`Math.floor(factor * 100 * 100) / 100`, `Math.round(x * 100) /
100`, etc.) and writing them straight into these same fields.

**Confirmed the actual failure mode directly rather than assumed from
the symptom description:** typing or programmatically setting a decimal
value into a `step="1"` number input is allowed - the field displays it
fine, and this app's own live-preview `"input"` listener fires and
updates the 3D view correctly regardless, since `parseFloat()` doesn't
care about `step` at all. The break is specifically at **submission**:
a plain `<form method="post">` submit (this page uses one, unlike the
upload form's manual XHR) runs the browser's native constraint
validation first, and a `step="1"` field holding a non-integer value
has `validity.stepMismatch === true` - the browser silently refuses to
submit at all, showing only its own native tooltip instead of doing
anything this app's code could catch or report. Reproduced directly:
setting `63.47` into a `step="1"` copy of this field reported
`checkValidity() === false` / `stepMismatch: true` and never reached
the server at all; the identical value against a `step="0.01"` field
reported valid and reached `/jobs/{id}/reslice` correctly.

**Fixed, not documented as a limitation** - per the user's own
framing ("if that's a limitation, then the page must state that"),
the right call here was to check whether it actually needed to *be* a
limitation first, and it didn't: the server side already accepted a
plain `float` for every one of these fields with no integer
requirement (`routers/user.py`'s `reslice()`, `jobs.start_reslice()`),
so the `step="1"` attributes were a client-side-only restriction with
no reason behind them once the auto-computed features existed.
Changed all four to `step="0.01"`, matching the exact precision every
value-producing feature already rounds to - min/max bounds (`min="1"
max="1000"` on scale) are untouched, so out-of-range values are still
caught the same way they always were, just no longer also silently
blocking any in-range value with more than zero decimal places.

### Auto-fit's blind spot: asymmetric models, and automatic rotation retry

**Real report, using "Auto-resize to fit build plate" on an actual F-35
fighter jet STL model, followed by a genuine slicing error:** OrcaSlicer
refused to slice the auto-fit result at all. Investigated the real
error directly rather than guessed at it - OrcaSlicer's own terse CLI
output ("run found error, return -50, exit...") gives almost nothing to
go on, but re-running it with `--debug 4 --logfile` produces a much more
detailed internal log, which named the actual reason plainly: `"Plate 1:
... Nothing to be sliced, Either the print is empty or no object is
fully inside the print volume before apply."`

**Root cause, confirmed by direct computation against the real mesh:**
`autoFitScale()` and the "too large" warning both measured the model's
raw bounding-box span against the bed dimensions, implicitly assuming
the model would end up centered by that same bounding box once placed.
It doesn't - `renderGeometry()` (and `stl_to_3mf.center_vertices()` on
the server side) center on the area-weighted surface centroid instead
(see "Rotate and snap to surface" above for why that centering exists
at all), which for a strongly asymmetric shape can sit nowhere near the
bounding-box middle. Computed directly for the real jet mesh: raw Y
span was 2000.8mm, needing a ~9.75% shrink to fit the bed's 195mm depth
by span alone - but the actual surface centroid sat at Y=297.9, while
the bounding box's own middle would have been roughly Y=-1.4 - a ~299mm
difference. At the auto-fit-computed 9.74% scale, the *span* fit
(194.9mm, just under 195mm) but the *centered* placement didn't:
Y ranged from -126.6mm to +68.3mm relative to the bed's own -97.5/+97.5
half-depth - one side alone extended ~29mm past the edge, even though
the total footprint was small enough to fit if it had been centered
some other way.

**Fixed by measuring the real constraint - not "does the span fit," but
"does each side, measured from the centroid, fit its own half of the
bed":** `autoFitScale()` now computes `halfExtentX/Y` as the larger of
`|min - centroid|` and `|max - centroid|` per axis, and divides the
bed's own half-width/half-depth by that instead of the old
`bedDimension / span`. This is a strict generalization, not a special
case bolted on: for any roughly-symmetric model (where the centroid
already sits at the bounding-box middle), `halfExtentX` reduces to
exactly `size.x / 2`, reproducing the old formula's result bit-for-bit
- only a genuinely asymmetric model gets a different (correct) answer.
`renderGeometry()`'s "does this fit" check (which drives the red/blue
model color and the info-text warning) got the equivalent fix: instead
of comparing the raw pre-centering span to the bed dimension, it now
recomputes the bounding box *after* centering and checks each side
against the bed's actual half-extent directly - the same shape of fix,
applied to the check that runs on every render rather than only on an
auto-fit click.

Verified against the real file, not just the arithmetic: the corrected
formula computes 7.50% for this model (not the old, wrong 9.74%), and
re-centering the actual mesh at that scale through the real Python
pipeline confirmed all three axes now genuinely fit (Y lands exactly on
the boundary, -97.5, as expected for the axis that's the binding
constraint). Re-ran the real OrcaSlicer CLI at the corrected scale and
it no longer refuses to slice - confirmed via a real headless-browser
test too: auto-fit on the actual model now reports 7.5% and clears the
"doesn't fit" warning, with zero console errors.

**A second, separate, already-known failure surfaced right underneath
once the fit itself was fixed:** at the corrected scale, OrcaSlicer
proceeded but `mbotmake`'s own bed-centering safety check
(`assert -0.15 < yrel < 0.15` - see "A real stuck-slicing incident" and
the Flexi_Seal investigation elsewhere in this file) still rejected it,
with `yrel = -0.2995` - the same failure class as those two earlier
investigations, on a third, independent real model. Swept a handful of
Z rotations against the real file to check whether the same fix would
apply again: 45° and 60° both produced a genuine `.makerbot`; 15° and
30° did not. Confirmed general, not a one-off.

**This is what prompted the automatic-rotation-retry feature, built the
same session:** per the user, directly - "Rotating the object resolved
the slicing error. When receiving slicing errors, suggest rotating the
object," followed immediately by "Or, try rotating the object
automatically when running into slicing errors," and, once asked how
aggressive that should be: "I don't think it would hurt to attempt
rotation and reslice until all reasonable rotations have been tried."

`jobs.AUTO_ROTATE_CANDIDATES` is a fixed, bounded set of 11 rotations -
quarter/eighth turns about Z (every real fix observed across all three
investigated models landed here), plus the four ways to lay the model
on one of its other faces (±90° about X or Y) - not an exhaustive
multi-axis grid, which would multiply the real per-attempt cost
(a full OrcaSlicer+`mbotmake` run each, genuinely minutes for a large
model) combinatorially for a search with no particular reason to
expect a better answer than a simpler sweep would find.
`jobs._slice_with_rotation_retry()` tries the orientation actually
requested first (never overriding an explicit choice), and only sweeps
the candidates if that fails, stopping at the first success. If a
candidate other than the requested one is what worked,
`slice_and_update()` updates `Job.rotate_x/y/z` to what was *actually*
sliced (not silently leaving the fields showing the orientation that
failed) and repurposes `Job.slice_error` - normally error-only - as a
one-time informational note on an otherwise-successful `sliced` job,
explicitly saying what was requested and what was substituted;
`job_edit.html` renders it as a plain note (not styled as an error) and
it clears itself the next time a re-slice succeeds without needing to
fall back to a candidate. If every candidate also fails, the *original*
requested orientation's own failure is what gets reported (not
whichever candidate happened to run last) - the one the user actually
asked for is the most relevant thing to show - and the `slice_failed`
page's own hint text was updated to say a broad rotation sweep was
already tried automatically, so a user doesn't waste time manually
retrying rotations the app already ruled out.

Verified end-to-end against a real, previously-failing file, through
the real pipeline: uploaded the Flexi_Seal model fresh (rotation
defaulting to 0/0/0, deliberately not pre-rotated), let the real
background slicing task run to completion, and confirmed it landed on
`sliced` (not `slice_failed`) with `Job.rotate_z` automatically set to
`45.0` and a clear note on the edit page explaining exactly what
happened and why the rotation fields show a value nobody manually
entered.

**A related observation from the user, correctly identifying a real
remaining gap: "The preview always shows the model in the center. If
it's off center, that's not displayed visually."** True, and distinct
from the calculation bug above (which is fixed - the red/blue color and
info text are now numerically correct for exactly this case). The
camera itself still frames around the *model's own* size and centroid
(`camera.position.set(radius * 1.4, ...)`, `radius` derived from the
model, not the bed), not the bed's fixed physical dimensions - so every
model, whether it comfortably fits or genuinely hangs off one edge,
gets framed to look similarly "centered in the picture," and the bed's
true, fixed-size rectangle can be easy to miss as the actual frame of
reference. The color/text warning is real and correct, but a viewer
who's only glancing at the picture rather than reading the info line
could still miss an overhang. Not addressed here - would need the
camera (or at least the bed-plate rendering) to hold a consistent,
recognizable scale/position across every model rather than re-framing
per-model, which is a real design change to how the preview frames
itself, not a quick follow-up to this fix.

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
  down by actor/action/date range was explicitly deferred at the time
  ("I will ask for log filters later") - now built, see "Filters" below.
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

### Filters, on every job/log/user listing

Per the user: "Let's add filters for all tables. The filters should be
substrings, color, status, est print time, user (if admin), date/time
range, action (activity log), etc." - and, when asked to clarify scope:
"All tables should get filters. The filters that should be available are
the ones that contain that data." Confirmed directly (not guessed): a
plain GET query-param form, fields laid out left-to-right in the same
order as the table's own columns, sitting on the same page directly
above the table rather than a separate page - "no meaningful performance
difference" between that and an htmx-based live-filter approach was
confirmed too (the underlying SQL query cost is identical either way;
only the response payload size differs, and negligibly at this app's
realistic scale) - plus an explicit "Clear filters" link back to the
bare, unfiltered URL on every one of these forms.

**Why GET, not POST, and why query params at all:** a filtered view's URL
is bookmarkable and shareable this way, and works with zero JavaScript -
consistent with this app's general server-rendered-page philosophy (see
e.g. the plain `<form method=post>` uploads elsewhere). It's also what
makes "carry the current filter forward through an action" (below)
possible at all: a query string is just part of the URL, so redirecting
or re-rendering with it intact is a plain string operation, not session
state to manage.

**`app/filters.py`** is the shared layer every listing route builds on -
built once here rather than a slightly different version in each of the
six-plus routes that need some subset of it:

- **`apply_job_filters(query, ...)`** - the workhorse, applied to a
  `select(Job)`-based query by `jobs.jobs_for_user`/`active_jobs`/
  `finished_jobs` alike. `q` (filename substring), `color` (exact match,
  plus a `"__any__"` sentinel meaning "no color set at all" - a real
  `Color.name` can never equal this), `status` (exact match), `min_minutes`/
  `max_minutes` (compare against `Job.duration_estimate_s` converted from
  stored seconds, matching what every duration this app displays is
  already rendered in), a date range on whichever single timestamp
  column that particular view already shows as its own "date" (
  `created_at` for the user's own dashboard - the closest thing a draft
  that's never been queued has; `queued_at` for the live queue and old-jobs
  view, matching their "Queued" column; `finished_at` for finished jobs,
  matching its "Finished" column), and `user` (a submitter-name
  substring, admin views only - a user's own dashboard has no submitter
  column to filter on at all).
- **`local_date_bounds(date_from, date_to)`** - turns two plain
  `YYYY-MM-DD` strings into the UTC instants bounding that whole range of
  *local* calendar days, in the admin-configured display timezone (see
  `templates_env.local_time`, and the new `get_display_timezone()`
  accessor added alongside it) - not literal UTC midnight, which would
  silently shift the filtered range by however many hours the display
  timezone is offset from UTC. `date_to` is inclusive of the entire day
  (bounded by the start of the *next* local day) - "through the end of
  that day," matching what someone picking an end date actually means.
- **`apply_event_filters(query, ...)`** - the activity log's own version,
  since `JobEvent` isn't a `Job` listing at all: `q` matches either the
  joined job's filename or the event's own `detail` text (an
  account-lifecycle event has no job to match a filename against at
  all), `actor` a substring, `action` an *exact* match (the log page
  offers this as a dropdown of real recorded actions - see
  `jobs.distinct_event_actions` - not a freeform field), plus the same
  date-range handling as above, against `JobEvent.at`.
- **`apply_user_filters(users, ...)`** - deliberately a plain in-Python
  filter over an already-fetched `list[User]`, not a SQL `WHERE` builder
  like the two above: the users table is realistically tiny for a
  single-printer, single-school deployment (unlike `Job`/`JobEvent`,
  exactly why those two got real indexes - see below), so there's no
  performance reason to push this into SQL.
- **`job_filter_params`/`event_filter_params`/`user_filter_params`** -
  plain functions used as FastAPI dependencies (`Depends(...)`) on every
  listing route, so the full set of query params a filter form can ever
  submit is bound in exactly one shared place. `min_minutes`/
  `max_minutes` are typed `str | None`, not `float | None`, on purpose -
  a real bug caught before shipping: typing them as `float` let FastAPI's
  own query-param coercion reject `""` with a 422, and a GET form submits
  *every* one of its fields regardless of whether it has a value - so
  leaving either field blank (the overwhelmingly common case) broke
  *every* ordinary use of the filter form outright. Confirmed live
  against a real running instance before and after the fix, not just
  reasoned about - the failure mode isn't obvious from reading the code
  alone, since a hand-built query string with only the params actually
  wanted (exactly what manual testing tends to do first) never
  reproduces it.
- **`job_filters_from_query_params(request.query_params)`** - same
  result as `job_filter_params`, but reading from a plain
  `request.query_params` instead of FastAPI's own binding - needed by
  `routers/admin.py`'s queue-action routes (approve/reject/release/...),
  which are POSTs with no query-param dependency injection of their own.

**Carrying a filter through an action, not just a page load:** a real
gap caught before shipping, not just the read side - every admin queue
action (approve/reject/release/mark done/mark failed/requeue/delete) is
a `POST` to a fixed URL, and a plain `RedirectResponse("/admin/dashboard")`
after one would silently drop back to unfiltered every single time, even
though the action itself succeeded. Fixed two ways together:
`routers/admin.py`'s `_query_suffix(request)` appends the current
request's own query string to a redirect target, and every action
`<form>`'s own `action=` attribute in `admin_dashboard.html`/
`admin_old_jobs.html`/`admin_users.html` does the same, so the POST
itself arrives carrying the filter along too (`request.query_params` is
otherwise empty on a POST to a bare relative URL - a browser does *not*
inherit the current page's query string into a form's `action` unless
it's explicitly there). Verified directly, not assumed: approving a job
from a `?color=Red`-filtered dashboard was confirmed (via a real request/
response, not just reading the template) to redirect back to
`/admin/dashboard?color=Red`, and triggering a real `JobActionError` from
that same filtered view was confirmed to re-render with both the error
*and* the filter's own submitted value still showing in the form.

**...unless the action itself empties that filter.** Per the user:
"performing an action with the filter in place should keep the filter in
place, unless that action results in 0 records for that filter." Keeping
`?status=queued` after approving the *only* queued job matching it would
land back on a real page that just looks broken - the filter's own
fields still showing what was typed, the table showing nothing, with no
obvious way back to everything else. `filters.filtered_redirect(path,
request, still_has_rows)` (shared by both routers - see below) re-runs
the exact same filtered query right after the action (a real, fresh
count - not the pre-action count minus one, which would be wrong the
instant the action itself changes whether another row matches too, not
just removes the acted-on row some other way) and only keeps the query
string if that still returns at least one row. Verified directly against
a real queue with two jobs sharing a color: rejecting the first (one
still matches) kept `?color=Red` on the redirect; rejecting the second
(now the last match) redirected to the bare, unfiltered
`/admin/dashboard` instead. Same confirmed on the users page: disabling
the one remaining user matching `?status=active` dropped that filter on
redirect, not kept it pointing at an empty table.

**A real gap in the first version of this fix, caught by the user
directly:** "The delete operation is not retaining the filter when there
were 2 before the operation. The filter I'm using is `status=slice_failed`
as a user." `filtered_redirect`/`query_suffix` had only been wired into
`routers/admin.py` - the equivalent user-side actions
(submit/delete/restore/reprint on `/dashboard`, and the reported one)
were still doing a plain, unconditional `RedirectResponse("/dashboard")`
with no filter carried at all, and `_jobs_table.html`'s own action
`<form>`s (unlike `admin_dashboard.html`/`admin_old_jobs.html`/
`admin_users.html`, all fixed the first time) never got a query-string
suffix on their `action=` attribute either - meaning `request.query_params`
would have been empty on those POSTs even if the redirect logic had been
there. Fixed by moving `query_suffix`/`filtered_redirect` out of
`routers/admin.py` into `filters.py` itself (both are generic - neither
one ever referenced anything admin-specific), and wiring them into
`routers/user.py`'s four dashboard-returning actions the same way, plus
adding the missing `{{ qs }}` suffix to `_jobs_table.html`'s forms. The
upload flow's client-side redirect (`user_dashboard.html`'s upload JS,
which navigates on its own after the XHR completes rather than
following the server's actual redirect target) got the equivalent fix -
`window.location.search` appended - though with no "still has rows"
check, since an upload only ever *adds* a job, never removes one a
filter was already matching. Verified by reproducing the user's exact
report: two `slice_failed` jobs, `?status=slice_failed` active - deleting
the first (one still matches) kept the filter on redirect; deleting the
second (now the last match) correctly dropped it.

**"Delete all" bulk actions respect the active filter, not just the
display:** `jobs.delete_all_old_jobs` and `delete_all_users` used to
always operate on the *entire* backlog regardless of what a page
happened to be showing. Once a filter could hide part of that backlog
from view, an unfiltered "delete all" became a real trap - the button's
own confirm() text already said "delete ALL N jobs/users **shown here**"
(true before filters existed, since nothing could hide anything then),
so leaving the underlying action unfiltered would have made that text a
lie the moment someone actually used a filter. Both now take the same
filter kwargs as the read side and only touch what's actually displayed.

**Indexes (schema 6.3.0):** `Job.color_name`/`created_at`/`queued_at`/
`finished_at` and `JobEvent.at`/`action` all gained `index=True` -
`_migrate_to_6_3_0` in `db.py` is the migration, since `CREATE INDEX IF
NOT EXISTS` still needs a real migration function even though it doesn't
touch a column: `create_all()` happily builds every index a *new*
database needs from the current model definitions, but (same limitation
it has for columns) never retrofits one onto a table that already
exists. `User`/`Admin`/`Color` were deliberately left unindexed - a
realistic deployment's users/colors lists stay small for years, and nothing
in the user's own filter description emphasized those tables the way it
did "every job table." Verified in an isolated copy before touching the
live database: a genuinely fresh database (the `create_all()` path) and
a simulated pre-6.3.0 one (dropping the six indexes and rolling
`schemaversion` back, to force the actual migration path to run) both
produced the identical final index set, and running `init_db()` a second
time changed nothing (`CREATE INDEX IF NOT EXISTS` is idempotent by
construction) - confirmed live afterward too, not just in the isolated
copy.

Verified end-to-end against a real seeded database (several users, jobs
across every status/color/duration/date combination, and a mix of job
and account-lifecycle log events) on every one of the six filtered
pages: substring, color (including the "no color set" sentinel), status,
duration range, date range, and submitter each independently confirmed
to narrow the result to exactly the expected rows - not just that the
page returned 200.

**The user's own dashboard table got a real "Date" column too**, per the
user ("move the date/time stamp to its own column"). Before this, the
only date/time ever actually shown on that table was `queued_at`, buried
inline in the Details column's prose for a queued/approved row only
(`"position N in queue - queued <timestamp>, waiting <duration>"`) -
every other status showed no date at all. Unlike the admin queue/
finished-jobs views (each scoped to one status subset, so a single
"Queued"/"Finished" column heading always applies to every row), this
table mixes every status a job can ever be in at once, so a bare "Date"
heading needs a per-row label to say what it's actually showing:
`created_at` ("uploaded", for submitted/sliced/slice_failed - the one
timestamp that's never null, since these predate ever being queued),
`queued_at` ("queued", for queued/approved/printing), or `finished_at`
("finished", for rejected/done/failed/expired) - the same three columns
`filters.apply_job_filters` already understands (see above), just always
shown here per-row instead of picked one-at-a-time by view. The raw
`queued_at` stamp is gone from the Details column now that it lives in
its own; the computed "waiting `<duration>`" text stays there, since
that's a derived duration, not the stamp itself. Verified against a real
seeded job in each of the nine statuses: every row showed the correct
label and timestamp for its own status, and the queued/approved Details
text no longer repeated it.

### Filament color selection, and a best-effort low-inventory notice

Per the user's full spec: admins manage a color list (`/admin/colors` -
add/remove, set rolls and grams on hand, enable/disable which ones
users can currently pick from), a user picks exactly one color per job
at upload time from whatever's currently enabled (or "Any available,"
so an admin doesn't have to change filament for them) and can change it
later from the job's edit page, and an admin sees a clear notice when a
job's own recorded filament use exceeds what's tracked as available for
its color. Explicitly **best effort**, stated in the UI itself (the
colors page, the upload form) not just here: the printer has no way to
report what's actually loaded or how much is left, so the whole
low-inventory check is only ever as fresh as the last time an admin
updated it by hand.

Before building any of it, investigated whether "how much filament will
this use" was even answerable at all, per the user's own conditional
framing ("if this is possible, let's add that too") - real test slice,
not assumed: OrcaSlicer's gcode already carries `; filament used [g] =
5.67`-style comments, but the sliced `.makerbot`'s own `meta.json`
(mbotmake's real output, the same file `read_makerbot_duration_s`
already reads `duration_s` from) carries the identical number as
`extrusion_mass_g` - no pipeline changes needed at all, just a new
`storage.read_makerbot_filament_g` parallel to the existing duration
reader. `jobs.slice_and_update` (and `reprint_job`, which copies an
already-sliced `.makerbot` byte-for-byte) sets `Job.filament_grams` from
it the same moment `duration_estimate_s` gets set.

`Job.color_name` is a plain string snapshot - **not** a foreign key to
the new `Color` table. An admin renaming or removing a color later must
never silently change what an already-submitted job says it was printed
in; that job's own history is whatever was actually selected, at the
time it was selected, full stop. The tradeoff this accepts: once a
color is deleted, there's no live row left to check a job's usage
against any more.

**Real bug, caught by the user immediately after uploading a job with
"Any available" selected: the required-filament figure itself was
disappearing, not just the inventory comparison.** `jobs.filament_status`
originally returned `None` outright - hiding the *entire* result,
required amount included - the moment there was no specific color with
a tracked gram total to compare against (no color selected, that color
since deleted, or an admin simply never entered a gram total for it).
That conflated two genuinely different things: the required amount
(known the instant slicing succeeds, exactly like the duration estimate
that kept showing fine right alongside it) needs neither a color nor
any inventory data at all; only the *comparison* against how much is on
hand does. Fixed: `filament_status` now returns `{required_g,
available_g, enough}` in every case where the job's actually been
sliced, with `available_g`/`enough` staying `None` (not the whole
result) whenever there's nothing to compare against - "unknown," not
"not enough." Every caller checking `.enough` had to change from `not
X.enough` to `X.enough == false` accordingly, since `not None` is `True`
in both Python and Jinja - the original check would have shown the
"not enough" warning for precisely the "nothing to compare" case this
fix exists for, the moment the required amount started rendering there
too. Verified directly across all four real cases (no color, an
untracked color, a tracked-but-short color, a tracked-and-sufficient
one) landing on exactly `None`/`None`/`False`/`True`, and end-to-end
over real HTTP confirming the rendered page.

Changing a job's color (`POST /jobs/{id}/color`, reachable from the
edit page) is deliberately a separate, lightweight route from
`/reslice`, not one more field bundled into that same form - color has
zero effect on the actual sliced geometry, so routing a pure color
change through a full OrcaSlicer+mbotmake re-slice would be pure wasted
CPU/memory on a Pi for something that changes nothing about the print
itself. Verified directly: `makerbot_path`/`filament_grams` are
provably untouched by a color-only change (identical values before and
after), confirming this path never re-slices.

Verified end-to-end in an isolated instance before touching production,
including the real slicing pipeline (not stubbed): a color's full add/
update/delete lifecycle from the admin page; the user-facing dropdown
actually reflecting only currently-enabled colors; a real upload with a
color selected; `filament_grams` landing at the exact value the real
`.makerbot`'s `meta.json` reported; the low-filament notice genuinely
rendering on the admin dashboard once a color's tracked amount was set
below what a real job needed; and, after deleting that color outright,
the job's `color_name` still reading correctly while
`jobs.filament_status` cleanly returned `None` for it rather than
erroring. Production's own migration (schema 6.2.0 - new `color` table,
`Job.color_name`/`filament_grams`) re-confirmed separately once these
changes were actually applied there.

### Full editing for a queued/approved job, not just color

A real course-correction, not the original design: the first version of
"can a queued job's color be changed" reused `job_edit.html` but
deliberately scoped it to color only for anything past draft status,
reasoning that resize/rotate/supports all genuinely require a re-slice
and a queued job shouldn't need one. Per the user, that was wrong -
"Edit was supposed to be all edit capability... same as the edit before
queuing" - it was this project's own assumption, made without
confirming it, not something actually asked for.

Reopening full editing for an already-queued job raises two real
questions this project's own to-do list had already flagged as open
(the "does editing an active job re-slice in place, or count as a new
submission" question), and both were confirmed explicitly rather than
guessed at:

- **While it's mid-reslice** (a real background operation - can take
  minutes, same as a draft's own first slice), the job is pulled out of
  admin view/action entirely, not left approvable/releasable. Achieved
  for free, no new status value needed: `jobs.start_reslice` puts a
  queued/approved job into the exact same `'submitted'` status a brand
  new upload already sits in while its first slice runs - not in
  `models.QUEUE_STATUSES`, so `active_jobs()` (the admin dashboard's own
  query) already excludes it automatically. It genuinely can never be
  safe to let an admin release a file that's still being written to the
  same path a background task is writing it to.
- **On success, it rejoins the queue with a fresh `queued_at`** - any
  edit sends it to the back of the line, the same as a genuinely new
  submission, not the position it already held.

Mechanically: `start_reslice` now accepts `queued`/`approved` as valid
starting statuses too (alongside the existing `sliced`/`slice_failed`),
and when it's one of those, moves the job's files from `queue/` back to
`scratch/` first - the mirror image of what `submit_draft` does going
the other way - before doing anything else, so every step downstream of
that point can treat every starting status identically. It returns
`(stl_path, was_queued)` rather than just `stl_path`, since by the time
the background `slice_and_update` task actually runs, `job.status` has
already been flipped to `'submitted'` and there's no way to recover
"was this queued a moment ago" from the job itself any more - `was_queued`
has to be threaded through explicitly (as `resubmit_to_queue`) from
`routers/user.py`'s `reslice()` route into that task's own arguments.

On success, `slice_and_update` checks `resubmit_to_queue`: if set, it
moves the freshly-sliced files `scratch/` -> `queue/` inline (right
there, not via a separate call to `submit_draft`) and sets
`status = queued`, `queued_at = now()` - never back to `approved`, even
if that's what it was a moment ago, since a materially different file
hasn't been re-reviewed by anyone yet. If it's *not* set (an ordinary
draft re-slice), behavior is completely unchanged: lands on `sliced`,
waiting for the existing manual "Submit to queue" button. On failure,
also unchanged either way: `slice_failed`, a draft, invisible to admins
until fixed and resubmitted - for the queued case specifically, this
means the job genuinely and correctly gives up its claim on a print
slot until it can actually produce a valid file again, not a bug: an
unprintable file has no business still holding a place in line.

`job_edit.html` itself no longer branches on draft-vs-queued at all -
the full settings form, 3D preview, and gizmo editor render identically
regardless, exactly matching "the edit before queuing." The dashboard's
existing "Edit" link now sits alongside a new "Change color" link -
same page, same route, just anchored straight to its `#color` section
(a plain HTML fragment, no new backend route) for anyone who only wants
that without scrolling past the full settings form.

**Verified end-to-end against the real slicing pipeline, not stubbed:**
uploaded, sliced, and queued a real job; confirmed visible on the admin
dashboard with the edit page showing the full settings form; triggered
a real re-slice at 150% scale and confirmed, immediately, the job
vanished from the admin dashboard and its files had genuinely moved to
`scratch/`; after the real background slice completed, confirmed status
back to `queued`, files genuinely back in `queue/`, `scale_factor`
actually applied (`1.5`), `queued_at` strictly later than the original,
the job reappeared on the admin dashboard, and the full audit trail
(`submitted` -> `sliced` -> `queued` -> `reslice_started` -> `sliced` ->
`queued`) was exactly right. Separately verified the failure path
(stubbed `run_slice` for a deterministic, instant failure) correctly
lands on `slice_failed` with the job still gone from the admin
dashboard, and that the new "Change color" link renders with the
correct `#color`-anchored `href`.

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

### A real bug found by the user: zooming out (or in) far enough hid the model entirely

**Orbit worked fine; only zoom showed the symptom - a real clue, not a
coincidence.** `renderGeometry()`'s own camera framing scales
`camera.near`/`camera.far` to each model's radius (needed so a tiny
calibration cube and a bed-filling model both start in frame - a fixed
far plane would clip a large model, a fixed near plane would swallow a
small one - see the comment right above it), but `OrbitControls`' own
zoom distance was never bounded to match. Its defaults (`minDistance`
`0`, `maxDistance` `Infinity`) let the camera dolly straight past
either clipping plane on a big-enough scroll/pinch - orbiting doesn't
change distance at all, so it was never affected, which is exactly why
turning kept working while zooming made the model vanish. Confirmed via
`git blame`: a genuine pre-existing bug from the original centroid-fix
commit (`3592f35`), not a regression from anything built this session -
the user just happened to hit it now.

First fix (`controls.minDistance`/`maxDistance` scaled to the same
`radius` `near`/`far` already use) turned out to be real but
**incomplete** - the user's own follow-up screenshot (one mouse-wheel
click zoomed in enough to fill the entire frame with a single flat
close-up surface, camera essentially jammed against the model) didn't
match "unbounded zoom eventually clips," it matched "one click jumped
almost instantly to the closest point allowed." That pointed at the
zoom *step* itself, not just the missing bounds.

**The actual root cause, found by reading `OrbitControls.js`'s real
dolly math rather than guessing again:** `getZoomScale()` computes
`normalized_delta = |delta| / (100 * (window.devicePixelRatio | 0))`.
`x | 0` truncates toward zero - so any `devicePixelRatio` below `1` (a
browser zoomed under 100%, some display-scaling/remote-desktop setups)
collapses the denominator to literal `0`, dividing by zero, giving
`Infinity`, which collapses `Math.pow(0.95, Infinity)` to `0` - the
zoom scale for *every* wheel event, not just large ones. One click was
enough to send the camera essentially straight to whichever bound
(`minDistance` or `maxDistance`, depending on scroll direction) was in
place, rather than the intended gradual ~5%-per-click step.

Patched the one line (`Math.max(window.devicePixelRatio, 1)` instead of
the truncating `| 0`) directly in the vendored `OrbitControls.js` -
consistent with this project's existing precedent of patching a
vendored dependency in place when a real bug surfaces (see the vendored
`mbotmake` bugfix) - since this file will never receive upstream
updates anyway and the fix is minimal, well-understood, and doesn't
change behavior for `devicePixelRatio >= 1` (the overwhelming common
case). The `minDistance`/`maxDistance` bounds from the first fix stayed
in place too, as a legitimate safety net independent of this root
cause.

**Verified directly, not just reasoned about - a real regression is
worth a real test, not a second guess:** a throwaway Playwright
install (never the project's own venv - cleaned up after, including
the browser cache), with `device_scale_factor=0.75` specifically to
reproduce the exact failure condition (confirmed via
`page.evaluate("window.devicePixelRatio")` actually reading back
`0.75`), driving a real page through the exact upload-preview code path
(`previewFile` → `showModel` → `renderGeometry`, the same camera setup
every viewer in this app shares) and a real simulated mouse-wheel
click. With the patch: a single click produced a small, gradual
zoom, exactly as intended - confirmed visually from the actual
screenshots, not inferred. With the original unpatched line (reverted
in this isolated copy only, to close the loop on the diagnosis itself):
one click sent the camera rocketing to the opposite extreme instead
(the cube shrank from filling the frame to a tiny distant speck) -
the same underlying collapse-to-zero bug, manifesting as a jump to
whichever bound the scroll direction pointed at, matching both the
user's original report (zooming either direction made the model
disappear) and this exact screenshot (one click, jammed up against the
model) precisely.

**Even after that fix was genuinely pushed, the user kept reporting the
identical broken behavior - a third real report, not the same one
repeated.** Turned out the fix was correct both times; the browser was
silently serving the *pre-fix* `OrbitControls.js` from its own cache the
whole time, never even asking the server. `StaticFiles` sends no
`Cache-Control` header at all by default, so browsers fall back to
heuristic caching - especially aggressive for ES module imports like
this one - with nothing forcing a revalidation on each load. A
`@app.middleware("http")` in `main.py` now sets `Cache-Control: no-cache`
on every `/static/` response - forces a round-trip to check with the
server on every load, but doesn't disable caching or force a full
re-download: `StaticFiles` already sends a real content-based `ETag`,
so an unchanged file still comes back as a fast `304` either way.
Verified directly: a fresh request carries the header, and a
conditional request with a matching `ETag` still correctly returns
`304`, not a full body. **The user's own next real-browser retest is
what actually confirmed the zoom fix itself was right all along** -
this caching fix exists so that confirmation loop can't cost this much
back-and-forth again for any future change to a vendored/static asset.

## Security checks (CI)

`.github/workflows/security.yml` runs on every push and pull request -
automatic, per the README's To do list, rather than relying on remembering
to check by hand. Three independent jobs, each answering a different
question:

- **`dependency-audit`** (`pip-audit -r app/requirements.txt`) - does
  anything the app depends on have a known CVE. Run locally first before
  writing the gate: clean, no known vulnerabilities, at the time this was
  added.
- **`static-analysis`** (`bandit -r app slicing test-print -ll`) - does the
  code itself do anything bandit flags as risky. `-ll` fails the build on
  medium/high severity only; low-severity informational findings (mostly
  "you imported subprocess" - true, and necessary, since this app shells
  out to OrcaSlicer/mbotmake by design) still print in the log but don't
  block anything.
- **`secret-scan`** (`gitleaks`, full history via `fetch-depth: 0`) - did a
  credential/token/key almost get committed. This repo also has GitHub's
  own native secret scanning enabled already (a free default for a public
  repo) - gitleaks here makes the same check an explicit, visible part of
  the pipeline itself, not just a separate alert somewhere else.

**Known, reviewed findings are suppressed inline, not globally, and only
the specific ones actually reviewed:** two `urllib.request.urlopen()`
calls (`app/printer.py`, `test-print/pairing.py`) trip bandit's B310
check, which generically flags urlopen as an SSRF-style risk - a
reasonable default, but a false positive here specifically, since `host`
in both cases is always the printer's own LAN address from local
config/env vars (`QUEUE3D_PRINTER_HOST`), never web request input. Marked
`# nosec B310` right at each call, with a comment explaining why - not a
blanket exemption for B310 everywhere, so a *new* urlopen call elsewhere
in the codebase still fails the build until it's reviewed the same way.

`.github/dependabot.yml` complements the audit job: `pip-audit` catches a
*known* vulnerability on every push; Dependabot proactively opens a PR to
bump a dependency (the pip ones in `app/requirements.txt`, and the GitHub
Actions themselves, e.g. `actions/checkout`) on a weekly schedule, before
a scan even has to catch one.

## Layout

- `main.py` - app setup: session middleware, static files, the
  `AuthRedirect` -> real HTTP redirect exception handler, router mounting,
  and closing the printer's persistent connection on shutdown (see
  "Persistent printer connection" above).
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
  `../test-print/`, see its docstring for why not imported),
  `_PersistentConnection` (see "Persistent printer connection" above),
  and `send_print_job()`, the real hardware call behind `jobs.release()`.
- `pair_printer.py` - CLI for the printer pairing step - see "Persistent
  printer connection" above for why this isn't actually one-time-ever.
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
