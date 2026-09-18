# queue3d

Do you have a shared local 3D printer? If so, queue3d lets users upload and
slice their own models and submit them to a queue. An admin reviews each
submission and releases approved jobs to the printer when ready.

**Printer support:** only the MakerBot Replicator+ (with a Tough Smart
Extruder+) is supported at this time, over its reverse-engineered network
protocol - see `test-print/README.md`. No other printer models or brands
are supported yet.

## Screenshots

Sample data below (names, filenames, queue contents) - not a real
deployment.

**User view** - uploading a model and tracking submissions through the
queue:

![User dashboard, showing the upload form and a table of submitted jobs in various states](docs/screenshots/user-dashboard.png)

The interactive 3D preview, here showing a model alongside its generated
support material:

![3D preview of a model with orange support material rendered underneath its overhangs](docs/screenshots/3d-preview-supports.png)

**Admin view** - reviewing the queue and approving, rejecting, or releasing
jobs to the printer:

![Admin dashboard, showing the queue with per-job approve/reject/release actions](docs/screenshots/admin-dashboard.png)

Managing user accounts:

![Admin user management page, listing users with disable/delete actions](docs/screenshots/admin-users.png)

## Features

- **Self-serve user accounts** - sign up with just a name and PIN, no
  email or password reset flow.
- **Admin accounts** provisioned separately (no self-service admin
  signup) - reviewing and releasing jobs is a position of trust over
  shared printer time.
- **User account management** - any admin can disable/re-enable or
  permanently delete a user's account, individually or all at once;
  disabling blocks login immediately, even from an already-open session.
  Deleting is blocked while that user still has an unfinished job.
- **Upload and automatic slicing** - submit an STL, it's sliced
  server-side (OrcaSlicer + a patched `mbotmake`) into a print-ready file,
  no separate slicer software needed on the user's end.
- **Live upload and slicing progress** - a byte-transfer progress bar while
  the file is being received, and a "slicing…" indicator on the dashboard
  (self-updating, no page reload) while it's converted server-side -
  slicing runs in the background rather than leaving the page hanging for
  however long that takes.
- **Slicing and submitting are separate actions** - uploading slices a
  model into a private draft (never visible to admins or counted in the
  queue) that can be re-sliced in place with different support settings,
  as many times as wanted, before deciding to submit it to the shared
  queue. A draft nobody ever submits expires automatically after an
  admin-configurable number of days (`/admin/settings`).
- **Live duration estimate and queue position** shown to the submitter as
  soon as slicing finishes.
- **Interactive 3D preview** - rotate and zoom a model on the real build
  plate before submitting (entirely in the browser, no upload needed just
  to look at it), and again afterward on the job's own page (for the
  submitter and for an admin reviewing it), this time showing generated
  support material as an overlay if supports were enabled for that job.
- **Choice of support style** - Automatic, Grid, Snug, Organic, or one of
  two tree-support variants, matching the shapes PrusaSlicer users would
  recognize by the same names.
- **One shared queue** - submissions land directly in it; there's no
  separate pre-review step before something counts as queued.
- **Admin review** - approve, or reject with a required note explaining
  why, before anything reaches the printer.
- **Browse finished jobs and a full audit log** - a rejected/done/failed/
  expired job leaves the live queue view but stays reachable
  (`/admin/jobs/finished`). A global activity log (`/admin/log`) shows
  every submit, slice attempt, re-slice, queue-submit, approve, reject
  (with the note), release, and outcome, across every job, most recent
  first, at a glance - plus each job's own history on its own page
  (`/admin/jobs/{id}/log`, linked from the queue, the finished-jobs list,
  and the global log).
- **Release to the printer over the network** - an approved job is sent
  and started directly; no walking a file over on a flash drive. One
  persistent, authenticated connection is held open for the app's whole
  run rather than reconnecting per release - the printer's own pairing
  tokens are only ever good for one authenticated session, confirmed live
  against the real hardware, so reconnecting fresh every time would have
  meant only the first release after any pairing ever actually worked.
- **One job on the printer at a time**, enforced - releasing a second job
  while one is already printing is blocked with a clear error.
- **Printer status on the admin dashboard** - rather than only finding
  out the printer needs re-pairing when a release actually fails, the
  dashboard shows its connection state plainly, and a "Pair printer"
  button starts pairing right from there (with on-screen instructions to
  go press the printer's dial) instead of needing server/CLI access to
  run `pair_printer.py` by hand.
- **A build-plate photo on every finished job, built but not yet
  confirmed live** - marking a job done or failed automatically captures
  a photo from the printer's onboard camera (over the same persistent
  JSON-RPC connection) and links it from the job's own log, the global
  activity log, and the submitting user's own dashboard row - so both
  the user and an admin can see what actually happened, and an admin can
  visually confirm which physical print belongs to which submitter's
  claim. A failed capture (camera or printer unreachable at the moment)
  never blocks recording the job's own outcome - it's logged as a
  failed-capture detail instead, not an error that stops the done/failed
  action. **Not counted as done per the user - a real photo has been
  captured and viewed in isolated testing, but every real attempt in the
  actual production app so far has failed, always on the connection
  being dead by the time a job is marked done/failed (idle timeout over
  a long print, a physical cancel, or one of this branch's own
  `--reload`-triggering deploys), never on the capture logic itself.**
  See `app/README.md`'s "Printer camera" section for the protocol
  write-up and the framing bug this surfaced.
- **Live print progress, read from the printer** - both dashboards show
  a real percent-complete and progress bar for the job that's currently
  printing, polled directly from the printer's own `get_system_information`
  reply (`current_process.progress`) rather than guessed from the
  original time estimate - confirmed live to track actual print progress
  (not just elapsed time) and to match what the printer's own screen
  shows. Falls back to the estimate-based countdown (see the persistent
  connection above) whenever a live reading isn't available. See
  `app/README.md`'s "Live print progress" section for how this was
  confirmed, and `/admin/printer/info` for the raw reply this is read
  from.
- **A history-corrected time estimate** - the fallback countdown shown
  whenever a live progress reading isn't available no longer trusts the
  slicer's own time estimate outright: it's scaled by the median
  actual-vs-estimated ratio across past successful prints (the first one
  in real use took 43.5% longer than estimated), so the estimate gets
  more realistic as more prints complete, without ever needing to be set
  by hand. Only learns from completed ("done") prints, never cancelled
  or failed ones, whose duration says nothing about how long a full
  print actually takes.
- **Automated backups** - the database and finished-job archive back up
  automatically on a schedule, rotating between two targets, with a
  dashboard indicator if a backup hasn't run recently.
- **Built for offline deployment** - runs entirely on a local network with
  no internet access required; no CDN dependencies.
- **App version number** shown as a footer on every page, read from
  `app/VERSION` at startup - bump that file to change what's shown, no code
  change needed.
- **Automated security checks** on every push and pull request
  (`.github/workflows/security.yml`) - dependency vulnerability scanning
  (`pip-audit`), static analysis for risky code patterns (`bandit`), and
  secret scanning (`gitleaks`, alongside GitHub's own native scanning on
  this public repo). Dependabot also opens a PR on its own when a
  dependency has a newer version, rather than waiting to be asked.

## To do

**Upload**
- Accept file types beyond `.stl` - `.3mf`, `.obj`, and `.zip` (presumably
  a zipped model file) were specifically asked for.
- Model repair (like PrusaSlicer/OrcaSlicer's "Fix through Netfabb") -
  confirmed OrcaSlicer's CLI has no repair flag to lean on (that's a
  GUI-only feature there), so this would mean a dedicated repair pass
  before slicing - `trimesh` (Python, fill holes/fix normals/fix winding)
  or `admesh` (a small purpose-built STL repair CLI) are the two realistic
  options to build it on.

**Job review & feedback**
- Show failure reasons to the user, not just rejection notes - rejection
  notes already display (required, and shown on the user's dashboard); a
  failed print currently has no reason at all (`mark_failed` only flips
  status, no note field), and slicing-error detail is currently only
  visible to an admin (as a hover tooltip), never shown to the user.
- Break the estimated print duration into days/hours/minutes - it's
  currently total minutes only.
- Show the date/time a job was submitted, and how long it's been sitting
  in the queue since (days/hours/minutes) - the timestamp is already
  recorded (`Job.created_at`/`queued_at`), it's just not displayed
  anywhere yet.
- Let admins configure an age threshold (e.g. 30 days) and split
  still-waiting jobs into two separate views by it: the normal queue view
  for anything younger than the threshold, and a separate "old jobs" view
  for anything at or past it - mutually exclusive, not shown in both.
  Builds directly on the submitted-at timestamp/duration-in-queue item
  above. Open question: does this apply only to `queued`/`approved` jobs
  (still awaiting a decision), or also to ones that are `printing` (already
  being acted on, so arguably shouldn't count as stale backlog). Jobs in
  this "old jobs" view should have an admin delete option - see "Audit
  log" below, since that delete has to be logged like any other change.
- Let a user restore an archived model (`slice_failed`, `rejected`,
  `failed`, or `done` - any job whose files ended up in `archive/`) back
  into their working space to modify and resubmit, rather than only being
  able to start over from scratch - useful both for fixing a failed/rejected
  submission and for reprinting or tweaking a past successful one. A
  restored resubmission goes to the end of the queue, not back to where the
  original was - it's a new submission, and the admin still decides when to
  release it like any other. Should copy the archived files rather than
  move them, so the original archived record/history isn't lost.
- The "View 3D" page (`/jobs/{id}/preview`) is view-only today - no way to
  resize a model or change its support settings (enable/style) from there,
  only at initial upload. This is really the same gap as the resize
  controls and restore-a-model items above, just noting where users will
  actually look for it: on the job's own page, not just at upload time or
  after it's already failed/been rejected. Open question this raises: for
  a job that's still active (`queued`/`approved`, not yet released) should
  changing something here re-slice it in place, keeping its position in
  the queue, or does any edit count as a new submission that goes to the
  end like a restored one does - those are different user expectations and
  worth deciding deliberately rather than defaulting to whichever is
  easier to build.
- Let a user delete their own model from the queue (they may no longer
  want it) - with a clear warning first that this is permanent: it removes
  the job from the queue/list and deletes the model files, with no undo.
  Only allowed while a job is still `queued`/`approved` (before release) -
  once it's released and `printing`, the delete action is disabled/removed
  from the list; the job's status just updates normally from there
  (`done`/`failed`) like any other. Deleting does not need to also remove
  the job from any backup already taken before the delete. Like any other
  change to a job, this needs to be recorded in the job log - see "Audit
  log" below.

**Audit log**
- The core log is built (`models.JobEvent`; `/admin/log` - one global,
  most-recent-first table across every job, which is the actual "admin
  log view"; `/admin/jobs/{id}/log` for one job's own history) - see
  Features below.
- Filters for the global log (`/admin/log`) - by job, user/admin, action
  type, date range. Explicitly deferred by the user rather than built
  alongside the log itself; currently just capped at the 500 most recent
  entries with no way to narrow that down.
- Once the two delete features above (a user deleting their own queued
  model, an admin deleting an old one) actually exist, each needs its own
  log entry too, and an admin deletion specifically must say who did it -
  the log doesn't have anything to log yet for actions that don't exist.

**Backups & recovery**
- Let admins see a list of backups taken and a manifest of what's actually
  in each one. Presentation undecided (a subpage, a pop-up list, something
  else). Restoring an individual model doesn't need this - see "restore an
  archived model" under Job review & feedback, which works directly off
  `archive/` instead; this is about visibility into the database-level
  backups themselves (see `backup.py`), for confirming they're actually
  capturing what's expected.

**Print options**
- Color selection for users - 1st/2nd/3rd preference, chosen from a
  dropdown populated by an admin-managed list of colors (admins check or
  uncheck which colors are currently available, based on inventory).
- Controls for resizing a model before submitting.

**Appearance**
- An admin setting for the display timezone - every timestamp shown
  anywhere in the app (the activity log, job history, "finished at",
  etc.) is UTC today, unlabeled as such in most places even though it's
  what's actually stored and compared against. Should apply everywhere
  at once, not per-page.
- Light/dark theme, with a toggle.
- Selectable themes - not just wallpaper/background/color, but ones that
  change the page layout itself, not only its palette.
- A logo for the app, shown on every page next to the "queue3d" title in
  the header (`templates/base.html`).

**Help / instructions**
- A how-to page (or a small set of them, split by what the reader is
  currently looking at, if that ends up clearer than one long page)
  walking through everything from registration to submitting a job with
  every feature along the way - supports, style choices, checking queue
  position, reading the log, viewing a finished job's photo, all of it.
  Linked from every page, for every user, not just buried somewhere.
- A parallel instructions page for admins - reviewing/approving/
  rejecting/releasing, reading the activity log, the printer status
  banner and pairing, managing user accounts. Also linked from every
  admin page. Should cover creating and managing *other admin* accounts
  once that feature exists (see "Accounts" below - not built yet) - add
  that section when that feature is actually built, not before.

**Printer**
- ~~Live print progress while a job is printing~~ **Done** - see Features
  below ("Live print progress, read from the printer") and
  `app/README.md`'s "Live print progress" section.
- Detect a print finishing or failing automatically, rather than relying
  on an admin to click `mark_done`/`mark_failed` by hand -
  `current_process`'s `complete`/`cancelled`/`error` fields (found
  alongside `progress`, see the print-progress feature above) look like
  a real path to this, just not built yet.
- ~~Correct the fallback time estimate using real completion history~~
  **Done** - see Features below ("A history-corrected time estimate")
  and `app/README.md`'s "Live print progress" section. What's still
  open: the correction is based on `finished_at` (when an admin clicked
  "Mark done"), not the printer's own recorded elapsed time for that
  print - the two automatic-detection and precise-history items above
  both point at capturing `current_process.elapsed_time` at the moment a
  job is marked finished, which would let the correction stop
  inheriting whatever delay elapsed between the physical print actually
  finishing and someone noticing.
- Camera access confirmed, photo capture wired into the job record - see
  Features above ("A build-plate photo on every finished job, built but
  not yet confirmed live"). **Not done yet, per the user**: real code,
  verified with a real captured-and-viewed photo in isolated testing,
  but every attempt in the actual production app has failed so far
  (always the connection, not the capture logic) - stays open until an
  actual real job's photo has been captured and viewed successfully.
- **The persistent-connection fix is built** (see Features below) - the
  investigation that found the underlying problem, and why the fix is
  architectural rather than a retry/backoff tweak, is in `app/README.md`'s
  "Persistent printer connection" section. Two things about it still
  worth remembering as real limits, not bugs to chase further: it doesn't
  make pairing permanent - an app restart or the printer being
  power-cycled (this printer's normal day-to-day usage pattern) still
  drops the connection and needs one more dial-press before the next
  release, same as before, just not *between every single release*
  within one continuous run any more; and the printer's HTTP pairing
  service has been observed to take roughly a minute to come up after its
  network/JSON-RPC service already answers, so pairing immediately after
  power-on can still fail on timing alone. Once during the investigation,
  a pairing request was also accepted server-side (got as far as "waiting
  for the dial press") but nothing ever rendered on the printer's screen
  to press - resolved on its own on a later attempt, cause not confirmed,
  possibly a UI-state issue from repeated pairing attempts in quick
  succession. Pairing should retry through the slow-HTTP-service case
  automatically.
- ~~Surface a clear "needs re-pairing" state on the admin dashboard,
  with a way to start pairing from there~~ **Done** - see Features below
  ("Printer status on the admin dashboard") and `app/README.md`'s
  "Printer status and in-app pairing" section.
- Bed adhesion tuning in the slicing profile - a test print completed
  without error but didn't stick to the bed (first-layer/Z-offset/brim
  settings need dialing in for the actual printer).
- Support for printer models/brands beyond the MakerBot Replicator+.
- An admin UI for managing printers - add/remove a printer and pair it,
  all from within the app, rather than today's CLI-only, server-access-
  required flow (`pair_printer.py`, one `data/printer_auth.json`/
  `QUEUE3D_PRINTER_HOST` implicitly assuming a single printer). Real
  prerequisite, not just a UI wrapper around what exists: the data model
  and `jobs.release()`'s one-job-at-a-time rule are currently written
  for exactly one printer - this needs an actual multi-printer design
  (which printer a job goes to, per-printer queues or one shared queue
  with printer selection, per-printer pairing state) before it's just a
  form.

**Accounts**
- Rate-limiting or lockout on login attempts - PINs are short by design
  for low signup friction, which also makes them easier to guess; nothing
  currently slows down repeated attempts.
- Let an admin reset a user's PIN, in case they forget it - today there's
  no recovery path at all short of the user just signing up under a new
  name (losing their submission history) or an admin deleting/recreating
  the account outright.
- ~~Capture account lifecycle actions (registration, disable, re-enable,
  delete) in the activity log, not just job actions~~ **Done** - see
  `app/README.md`'s "Account actions in the activity log" section.
- ~~Let admins manage user accounts from the UI~~ **Done** - any admin can
  disable/re-enable or permanently delete a user, individually or all at
  once, from `/admin/users`. Disabling blocks login immediately, even from
  an already-open session (re-checked on every request, not just at
  login). Deleting is blocked outright - with a clear reason, naming who -
  if the user (or, for "delete all", any user) still has a job that isn't
  finished yet (`queued`/`approved`/`printing`); a finished job's history
  is left alone either way. Both delete actions require a confirm dialog
  first.
- Extend the above to admins managing *other admins* too, not just users -
  deliberately left out of what was just built, since it raises a real
  safety question the users-only version didn't: what stops an admin from
  disabling or deleting the only remaining admin account, including
  themselves, locking everyone out of admin access. Also open: does
  "add an admin" mean creating a fresh admin credential (today's design -
  a separate username+password, unrelated to any user account) or
  promoting/converting an existing user's account, since those are two
  different tables today; and what happens to a deleted admin's existing
  references on past jobs (`reviewed_by_admin_id`/`admin_note`) - kept but
  orphaned, or removed too.

**Deployment**
- A fixed IP or mDNS hostname for the server so users don't have to type
  or remember a raw IP address on the deployment network.
- Actually setting this up on the target Raspberry Pi: OS install, the
  scratch/queue/archive + two backup-target USB drives mounted, and the
  backup cron job installed - all designed for, none yet done on real
  hardware.

## Project layout

- `app/` - the web app (FastAPI). Start here for setup instructions.
- `slicing/` - the STL -> print-ready-file pipeline, usable standalone.
- `test-print/` - the printer network protocol client, usable standalone
  for testing connectivity without the rest of the app.

Each has its own README with setup and implementation details.
