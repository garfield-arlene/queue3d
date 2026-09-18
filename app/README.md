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
