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
- **A build-plate photo on every finished job** - marking a job done or
  failed automatically captures a photo from the printer's onboard camera
  (over the same persistent JSON-RPC connection) and links it from the
  job's own log, the global activity log, and the submitting user's own
  dashboard row - so both the user and an admin can see what actually
  happened, and an admin can visually confirm which physical print
  belongs to which submitter's claim. A failed capture (camera or printer
  unreachable at the moment) never blocks recording the job's own
  outcome - it's logged as a failed-capture detail instead, not an error
  that stops the done/failed action. **Confirmed working for real**, not
  just in isolated testing - job #15's automatically-detected completion
  produced and saved a real, viewed photo of the finished print on the
  build plate, after several rounds of real-world failures (always the
  connection or a cleanup-step bug, never the core capture logic) that
  are all documented and fixed. See `app/README.md`'s "Printer camera"
  section for the protocol write-up and the bugs this surfaced.
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
- **Per-account theme and light/dark mode selection** - both users and
  admins get their own settings page to pick a UI theme and a light/dark
  mode independently, persisting across logins/devices (not a
  browser-only preference). The current look is now a real, named
  "Default" theme (in its "Light" mode) rather than just "whatever the
  CSS says" - `base.html`'s styles are CSS custom properties a future
  theme/mode combination overrides selectively, with zero visible change
  to how the app looked before this. Only "Default" exists as an actual
  theme choice so far, though both Light and Dark are real, fully
  working modes for it already; see the To do list for adding more
  themes. See `app/README.md`'s "Themes" section.
- **Automatic completion detection** - a background poller notices a
  print finishing, failing, or being cancelled on its own (via the same
  printer status read as the live progress bar above) and records the
  outcome immediately, without waiting for an admin to click "Mark
  done"/"Mark failed" - closing the exact gap that caused every real
  photo-capture failure so far (the connection dying between a print
  actually finishing and someone noticing). The manual buttons are
  unchanged - this is a safety net on top of them, not a replacement.
  Logged with actor `"system"` so the activity log always shows whether
  a given outcome was a human's click or the poller's own. See
  `app/README.md`'s "Automatic completion detection" section.
- **Login rate-limiting** - both account types lock out for 15 minutes
  after 5 failed attempts in a row, since PINs are short by design (low
  signup friction) and an admin password is a higher-stakes target -
  nothing previously slowed down repeated guessing at all. Per-account,
  not per-IP or global: the actual threat is one person guessing a
  specific other person's credentials, not general abuse. A correct
  password/PIN submitted while locked out is still rejected with the
  lockout message, not "didn't match" - so a lockout can't be probed
  around by anyone who happens to already know the real credentials.
  See `app/README.md`'s "Login rate-limiting" section.
- **Failure reasons shown to the user, not just admins** - a manual
  "Mark failed" now requires an admin to say why, same as rejecting
  already required a note, and that reason shows right on the
  submitter's own dashboard row instead of just "print failed" with no
  explanation. Automatic completion detection (above) always supplies
  its own reason on the same field. Slicing-error detail was already
  visible to the user (on the draft's own edit page) once checked - only
  the manual failure case was really missing an explanation.
- **Admin PIN reset for users** - the only recovery path for a forgotten
  PIN, since there's no email to send a reset link to: an admin resets
  it from the Users page, a random new PIN is generated and shown once,
  right there, to relay to the user in person. The old PIN stops working
  immediately. Logged in the activity log like any other account action
  - who did it and for whom, never the PIN value itself.
- **Print duration estimates shown as days/hours/minutes**, not raw
  total minutes - "1d 1h" or "2h 5m" instead of "1500 min"/"125 min",
  everywhere a duration is displayed (queued/approved/sliced estimates,
  and the live "time remaining"/"over the estimate" countdown for a job
  that's printing).
- **Submission timestamp and queue-wait shown for still-waiting jobs** -
  both the admin queue and a user's own dashboard now show exactly when
  a `queued`/`approved` job actually joined the queue and how long it's
  been waiting since (days/hours/minutes, same formatting as duration
  estimates above) - not just its position in line.
- **Admin-configurable display timezone** - one setting
  (`/admin/settings`, any IANA zone name) controls what timezone every
  timestamp in the app is shown in - the activity log, job history,
  "finished at", queue-wait, backup times, all of it, everywhere at
  once, not per-page or per-account. Data is still stored and compared
  internally as UTC regardless; this only changes what a viewer reads
  on the page, and now shows a real zone abbreviation (EST/EDT/UTC/etc.)
  instead of a hardcoded "UTC" label that wasn't always accurate to
  what was actually displayed. Takes effect immediately for every
  viewer on save, no restart needed.
- **An "Old jobs" backlog view for still-undecided jobs** - a
  queued/approved job that's been waiting longer than an admin-configured
  threshold (`/admin/settings`, default 30 days) moves out of the normal
  queue entirely into a separate view, so a growing backlog doesn't get
  lost among everything else. Fully actionable there - approve, reject,
  release, or move it back to the queue with a fresh wait clock - or
  delete it outright (one at a time or all at once), which genuinely
  removes the job and its model file with no undo, unlike every other
  outcome in this app. Deliberately excludes `printing` jobs, confirmed
  with the user - a print actively running is being acted on, not
  sitting in an undecided backlog. The main dashboard flags how many
  are waiting, with a link straight to the backlog view.
- **Delete your own queued job** - changed your mind about a submission
  still awaiting a decision? Delete it directly from the dashboard,
  with a clear "this cannot be undone" confirmation first. Genuinely
  removes the job and its model file - the same real, unrecoverable
  delete an admin can do to a stale one (see "Old jobs" above), just
  reachable by the submitter instead. Only available while still
  `queued`/`approved`; once released and printing, an admin is already
  acting on it and the option disappears. Logged in the activity log
  like any other change.
- **Restore & edit, and Reprint** - two ways to reuse a job that's
  already reached a final outcome, instead of only being able to start
  over with a fresh upload. "Restore & edit" (any `rejected`, `failed`,
  `done`, or `expired` job) copies the model into a brand-new draft,
  pre-filled with its previous scale/rotation/support settings, and
  drops you straight onto that draft's edit page to tweak and resubmit -
  the original archived job is never touched, just copied from. "Reprint"
  (a `done` job only) skips the edit step entirely for the case that
  doesn't need it: it reuses the exact already-sliced file and goes
  straight back into the queue, ready for an admin to release, with no
  re-slicing wait at all.
- **Upload `.obj` files directly, and `.zip` files of one or more
  `.stl`/`.obj` models** (a common Thingiverse shape - several separate
  parts plus a README/photo that's just ignored). Each model in a zip
  becomes its own separate job/draft, up to `MAX_ZIP_MODEL_FILES` (25) -
  stated right on the upload form, not just in this README - rather
  than a combined-plate print, confirmed as the right design with the
  user since this app's whole pipeline is built around one object per
  job. An `.obj` upload is converted to a real `.stl` immediately
  (losslessly - same geometry, different container) so nothing
  downstream (slicing, the 3D preview, re-slicing) needs to know it was
  ever anything but one; the original filename still displays as
  uploaded. A real multi-part functional-print kit (15 separate model
  files) confirmed working end-to-end, including every part slicing
  successfully - see `app/README.md`'s "Fixing a real multi-model zip
  upload" for the earlier, narrower cap this exposed and the (wrong)
  concurrency assumption it was based on.
- **Resize and auto-fit on the job edit page** - a draft's own edit page
  has a scale control (always uniform - proportions can never distort)
  with a live, before-you-commit 3D preview as you change it, and a
  one-click "Auto-resize to fit build plate" button for a model that's
  too large, computing exactly the shrink needed rather than making the
  user guess a percentage by hand. The read-only "View 3D" page for an
  already-submitted job shows it at whatever scale it was actually
  sliced at too, not just the original file size.
- **Automatic rotation retry on a slicing failure** - if a model fails to
  slice at whatever orientation was requested, the app automatically
  tries a handful of likely rotations (quarter/eighth turns, and
  standing the model on each of its other faces) before giving up,
  since real models have repeatedly turned out to need nothing more
  than a different rotation to slice successfully at all. If one of
  those works, the job's rotation is updated to match and a clear note
  says so (not silently applied without explanation); if none do, the
  job fails as before, with a note that this was already tried so a
  manual re-attempt at the same rotations won't help.
- **Rotate and snap to surface** - free rotation on any axis (three
  degree fields, live preview as you type), plus a "Snap to surface"
  button: click it, then click any face on the model, and it reorients
  to stand on that face - useful both for fit (a diagonal rotation can
  let an oversized model fit the plate) and for print success. Directly
  confirmed to fix a real slicing failure that resizing alone never
  could: a model whose asymmetric shape failed the printer's own
  bed-centering safety check slices successfully once rotated to a
  sensible printing orientation. Auto-fit accounts for whatever rotation
  is currently applied too, since reorienting changes the model's actual
  footprint on the plate - and, for an asymmetric model, accounts for
  exactly how far off-center its actual centered placement will be, not
  just its raw size, so a lopsided model can no longer pass this check
  while still genuinely hanging off one edge of the bed. On-canvas drag
  handles (rotate freely, or uniform resize) are available too,
  alongside the number fields and
  snap-to-surface - three different ways to reach the same rotate/scale
  values, kept in sync with each other.
- **Automated backups** - the database and finished-job archive back up
  automatically on a schedule, rotating between two targets, with a
  dashboard indicator if a backup hasn't run recently.
- **Downloadable support bundle** - an admin can generate a `.tar.gz`
  on demand (a safe copy of the database, the full activity log, and
  every model that ever hit a slicing error) for offline bugfixing at
  this app's zero-internet-access deployment, with no need for remote
  access to the device itself.
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
- ~~Accept `.obj` files directly~~ **Done** - see Features above.
- ~~`.zip` files containing multiple models, splitting into one job per
  model.~~ **Done, confirmed against a real multi-part file** - a
  real-world functional-print kit (15 separate `.STL` files) uploaded,
  split into 15 jobs, and every one sliced successfully. This had
  previously been marked broken after the user reported it not working
  in real use following an earlier round of synthetic-only testing
  (see `app/README.md`'s "Fixing a real multi-model zip upload" for the
  full account) - the real cause turned out to be `MAX_ZIP_MODEL_FILES`
  being set too low (10) for a legitimate multi-part kit, not a
  FastAPI-level failure; raised to 25, and now stated directly on the
  upload form per the user ("We need to note the limitation for the
  users"), not just here. Confirmed independently by the user in real
  use, not just by this session's own testing - the stronger of the two
  claims this project distinguishes between.
- **Separately, still open:** the *original* 422 report (a different
  real multi-model zip, before the file above was available to test)
  never got a confirmed root cause - that specific file was never
  available to reproduce against directly, and every synthetic zip
  built to investigate it tested clean. Whether it was the same
  too-low-cap issue (plausible - a `ValueError` there produces a clean
  flash-message redirect, not literally the "422 Unprocessable Content"
  originally reported, so it may not be) or a genuinely separate
  request-validation failure is unresolved. Needs either that original
  file or a browser Network-tab capture of a future failed request to
  pin down further.
- A real, independent bug found and fixed while investigating the above,
  regardless of the 422's root cause: the dashboard's own upload JS
  (`user_dashboard.html`) unconditionally redirected to `/dashboard` on
  any completed request, on the (mostly but not always true) assumption
  that the server always ends up there - true for every error this
  app's own code controls (`fail()` always redirects with a flash
  message set), but not for a request that fails validation before the
  route runs at all, which never redirects anywhere. That's exactly why
  a failure like this looked like "nothing happened" instead of showing
  an error. **Done** - the JS now checks the response status and shows
  its own error message for anything outside 2xx-after-redirect, or a
  network failure.
- ~~Related gap noticed while investigating the above: a `slice_failed`
  draft has no delete route at all (only `queued`/`approved` jobs could
  be deleted, by a user or an admin) - it could only ever be re-sliced
  or left to the existing draft-expiry cleanup.~~ **Done** - a
  `slice_failed` draft now shows the same Delete button/confirmation a
  queued/approved job does (`jobs.delete_own_job`'s allowed statuses
  extended; `storage.delete_job_files` now looks in `scratch/` rather
  than `queue/` for anything still in `models.DRAFT_STATUSES`).
  Deliberately NOT extended to a plain `sliced` draft (successfully
  sliced, not yet submitted) - not part of this ask, and it already has
  a path forward (submit it, or keep adjusting settings). Still open,
  cross-referenced below: "allow rejected jobs to be edited and
  requeued" is the same underlying gap for a different terminal status.
- `.3mf` upload support - not yet built, and a meaningfully bigger lift
  than `.obj`/`.zip` turned out to be: unlike OBJ (a flat, transform-free
  mesh format converted to STL in a few dozen lines - see `app/mesh.py`),
  a real-world `.3mf` can bundle multiple objects with their own
  placement transforms in one file (the same "one object per job"
  question `.zip` already answered - each object would become its own
  job, matching that precedent) and the client-side instant preview
  would need Three.js's heavier `3MFLoader` (plus its own `fflate`
  dependency) vendored, not just a small loader file like OBJ's. Worth
  doing, but as its own follow-up rather than folded into the
  OBJ/zip work.
- Model repair (like PrusaSlicer/OrcaSlicer's "Fix through Netfabb") -
  confirmed OrcaSlicer's CLI has no repair flag to lean on (that's a
  GUI-only feature there), so this would mean a dedicated repair pass
  before slicing - `trimesh` (Python, fill holes/fix normals/fix winding)
  or `admesh` (a small purpose-built STL repair CLI) are the two realistic
  options to build it on.
- ~~Centering an uploaded model by its actual geometric centroid, not
  just its bounding-box center - a real asymmetric model was seen to
  fail `mbotmake`'s own bed-centering sanity check this way.~~ **Done,
  but confirmed only a partial fix** - `slicing/stl_to_3mf.center_vertices`
  now uses an area-weighted surface centroid (matched in
  `static/preview.js`, which has to stay in lockstep - see either's own
  comment), a real, measured improvement (moved the actual failing
  model's `yrel` from -0.232 to -0.162 against the real pipeline), but
  that specific model is asymmetric enough to still narrowly miss the
  ±0.15 tolerance. A full volume-centroid (not just projected surface
  area) might close the remaining gap, but needs a watertight,
  consistently-wound mesh to compute correctly - a real precondition to
  check first for a mesh converted from an arbitrary uploaded OBJ, not
  yet attempted. See `app/README.md`'s "A real stuck-slicing incident"
  section for the full numbers.

**Job review & feedback**
- ~~Show failure reasons to the user, not just rejection notes - rejection
  notes already display (required, and shown on the user's dashboard); a
  failed print currently has no reason at all (`mark_failed` only flips
  status, no note field), and slicing-error detail is currently only
  visible to an admin (as a hover tooltip), never shown to the user.~~
  **Done** - see Features above. (Slicing-error detail turned out to
  already be visible to the user, via the draft edit page - only the
  manual "Mark failed" gap was real.)
- ~~Break the estimated print duration into days/hours/minutes - it's
  currently total minutes only.~~ **Done** - see Features above.
- ~~Show the date/time a job was submitted, and how long it's been
  sitting in the queue since (days/hours/minutes) - the timestamp is
  already recorded (`Job.created_at`/`queued_at`), it's just not
  displayed anywhere yet.~~ **Done** - see Features above.
- ~~Let admins configure an age threshold (e.g. 30 days) and split
  still-waiting jobs into two separate views by it: the normal queue view
  for anything younger than the threshold, and a separate "old jobs" view
  for anything at or past it - mutually exclusive, not shown in both.
  Builds directly on the submitted-at timestamp/duration-in-queue item
  above. Open question: does this apply only to `queued`/`approved` jobs
  (still awaiting a decision), or also to ones that are `printing` (already
  being acted on, so arguably shouldn't count as stale backlog). Jobs in
  this "old jobs" view should have an admin delete option - see "Audit
  log" below, since that delete has to be logged like any other change.~~
  **Done** - see Features above (confirmed by the user: queued/approved
  only, not printing).
- ~~Let a user restore an archived model (`rejected`, `failed`, or
  `done` - any job whose files ended up in `archive/`) back into their
  working space to modify and resubmit, rather than only being able to
  start over from scratch.~~ **Done** - "Restore & edit" on the
  dashboard for any `rejected`/`failed`/`done`/`expired` job copies its
  model into a brand-new draft (never moves the archived original - its
  own record/history is completely untouched) pre-filled with its
  previous scale/rotation/support settings, and lands straight on that
  new draft's edit page to tweak and resubmit like any other draft - a
  fresh `queued_at` once actually submitted, genuinely joining the back
  of the line, not the original's old position. (Explicitly re-confirmed
  by the user for the `rejected` case specifically: "allow rejected jobs
  to be edited and requeued.") Corrected from the original wording of
  this item: `slice_failed` was never actually part of this gap - a
  `slice_failed` job is a draft, not an archived one (its files live in
  `scratch/`, never `archive/`), and it already has a full edit/re-slice/
  delete path of its own on the same edit page every draft uses.
- ~~Add a "reprint" option to print another exactly as it was queued.~~
  **Done**, per the user, as a one-click alternative to "Restore & edit"
  for the case that doesn't need editing at all: a `done` job's own
  "Reprint" button skips re-slicing entirely (reusing the exact archived
  `.makerbot` byte-for-byte) and goes straight into the shared queue,
  ready for an admin to release. Deliberately scoped to `done` only -
  not `rejected` (turned away for a reason an admin should reconsider,
  not silently resubmit unchanged), `expired` (never actually printed,
  nothing proven to reprint), or `failed` (a failed print might have
  failed for a reason worth checking before blindly retrying the
  identical file - "Restore & edit" is the right tool for all three of
  those instead).
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
- ~~**Model orientation/sizing controls** - the user's full spec: rotate
  on any axis, "snap to surface," resize maintaining aspect ratio, and
  one-click auto-fit.~~ **Done** - see Features above ("Resize and
  auto-fit" and "Rotate and snap to surface"). Directly confirmed to
  resolve a real, previously-unfixable slicing failure: the Flexi_Seal
  model that failed `mbotmake`'s bed-centering check (see `app/README.md`'s
  "A real stuck-slicing incident" section) slices successfully once
  rotated ~45° about its vertical axis - found by testing the real file
  through the real pipeline, not guessed. Scoped to draft
  (`sliced`/`slice_failed`) jobs only, on the existing job-edit page -
  not the upload form, and not yet extended to an already-`queued`/
  `approved` job (the "does editing an active job re-slice in place or
  count as a new submission" question two bullets up is still open, and
  out of scope for what's built so far).
- **Real bug found using auto-fit on an actual model (an F-35 fighter
  jet STL): "Auto-resize to fit build plate" could compute a scale that
  still didn't actually fit.** Root cause: auto-fit and the "too large"
  warning both measured the model's raw bounding-box span against the
  bed, assuming it would be centered by that same bounding box - but the
  model is actually centered on its area-weighted surface centroid (see
  the Flexi_Seal fix above), which for a strongly lopsided shape can sit
  nowhere near the bounding-box middle. This exact jet model, shrunk to
  fit its own total span, still hung ~29mm off one edge of the bed once
  centered on its real centroid. **Fixed** - both checks now measure the
  actual centroid-relative distance to each side independently, which
  is what genuinely determines whether it fits; unchanged for any
  roughly-symmetric model, where the two calculations agree anyway.
  Verified against the real file: auto-fit dropped from a wrong 9.74% to
  a correct 7.50%, and OrcaSlicer's own placement check accepted the
  result (previously refused with "no object is fully inside the print
  volume").
- ~~Automatic rotation retry on a slicing failure.~~ **Done** - see
  Features above. Per the user, after this same fighter-jet model
  (once correctly sized) still failed `mbotmake`'s own bed-centering
  check exactly like the Flexi_Seal model did, and rotating it by hand
  fixed it too: "I don't think it would hurt to attempt rotation and
  reslice until all reasonable rotations have been tried." A bounded
  sweep (not exhaustive) of 11 candidate rotations, tried automatically
  after the requested orientation fails, stopping at the first success.
- The 3D preview's camera always frames around the *model's own* size
  and position, not the bed's fixed physical dimensions - correctly
  identified by the user right after the auto-fit fix above: "The
  preview always shows the model in the center. If it's off center,
  that's not displayed visually." The numeric fit-check is now correct
  (see above) and shows as a red model + text warning, but a viewer
  only glancing at the picture rather than reading that line could
  still miss an overhang, since every model - fitting or not - gets
  framed to look similarly "centered in the picture." Would need the
  camera (or at least the bed-plate rendering) to hold a consistent
  scale/position across every model rather than re-framing per-model -
  a real design change to the preview, not a quick follow-up.
- ~~Let a user delete their own model from the queue (they may no longer
  want it) - with a clear warning first that this is permanent: it removes
  the job from the queue/list and deletes the model files, with no undo.
  Only allowed while a job is still `queued`/`approved` (before release) -
  once it's released and `printing`, the delete action is disabled/removed
  from the list; the job's status just updates normally from there
  (`done`/`failed`) like any other. Deleting does not need to also remove
  the job from any backup already taken before the delete. Like any other
  change to a job, this needs to be recorded in the job log - see "Audit
  log" below.~~ **Done** - see Features above.

**Audit log**
- The core log is built (`models.JobEvent`; `/admin/log` - one global,
  most-recent-first table across every job, which is the actual "admin
  log view"; `/admin/jobs/{id}/log` for one job's own history) - see
  Features below.
- Filters for the global log (`/admin/log`) - by job, user/admin, action
  type, date range. Explicitly deferred by the user rather than built
  alongside the log itself; currently just capped at the 500 most recent
  entries with no way to narrow that down.
- ~~Once the two delete features above (a user deleting their own queued
  model, an admin deleting an old one) actually exist, each needs its own
  log entry too, and an admin deletion specifically must say who did it -
  the log doesn't have anything to log yet for actions that don't exist.~~
  **Done** - both delete a user's own queued/approved job and an admin
  deleting an old one log a `job_deleted` event (actor + filename, plus
  the submitter's name for the admin-side one) - see "Old jobs" and
  "Delete your own queued job" in Features.

**Backups & recovery**
- Let admins see a list of backups taken and a manifest of what's actually
  in each one. Presentation undecided (a subpage, a pop-up list, something
  else). Restoring an individual model doesn't need this - see "restore an
  archived model" under Job review & feedback, which works directly off
  `archive/` instead; this is about visibility into the database-level
  backups themselves (see `backup.py`), for confirming they're actually
  capturing what's expected.
- Periodic disk-space checks on the relevant volumes - the OS disk and
  each mounted USB backup flash drive (see `backup.py`'s rotation between
  two targets) - so running low is surfaced before a backup silently
  fails or the queue itself can't accept new uploads, not discovered
  after the fact.
- A system performance view for admins - CPU and RAM usage, presumably
  alongside the disk-space check above on the same page, given a Pi is a
  real resource-constrained target and slicing (OrcaSlicer + mbotmake)
  is genuinely CPU/memory-heavy.

**Print options**
- Color selection for users - 1st/2nd/3rd preference, chosen from a
  dropdown populated by an admin-managed list of colors (admins check or
  uncheck which colors are currently available, based on inventory).
- ~~Controls for resizing a model before submitting.~~ Folded into the
  fuller "Model orientation/sizing controls" item under Job review &
  feedback above, once the user spelled out the full scope (rotate,
  snap-to-surface, resize, auto-fit) - not a separate item any more.

**Appearance**
- ~~An admin setting for the display timezone - every timestamp shown
  anywhere in the app (the activity log, job history, "finished at",
  etc.) is UTC today, unlabeled as such in most places even though it's
  what's actually stored and compared against. Should apply everywhere
  at once, not per-page.~~ **Done** - see Features below.
- ~~Convert the current look into a real, named "Default" theme, with a
  per-user/per-admin settings page to pick one, persisting across
  logins~~ **Done** - see Features below ("Per-account theme and
  light/dark mode selection") and `app/README.md`'s "Themes" section.
  Only "Default" actually exists as a theme choice today - the
  infrastructure (settings pages, persistence, the CSS token structure a
  theme/mode overrides) is what's built; more themes is genuinely new
  work, not just filling in a dropdown.
- ~~Light/dark mode~~ **Done** - a separate toggle from theme, per the
  user, not folded into it - every theme (so far just "Default") gets
  both a light and a dark palette. See Features above and
  `app/README.md`'s "Themes" section.
- More themes beyond "Default" - color changes, wallpaper, as their own
  selectable options (each needing both a light and dark palette, per
  the user - see "Themes" above). Everything must ship as local static
  files - no CDN fonts, no external image URLs (see "Deployment: zero
  internet access" - this app runs with none, ever).
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
- ~~Detect a print finishing or failing automatically, rather than
  relying on an admin to click `mark_done`/`mark_failed` by hand~~
  **Done** - see Features below ("Automatic completion detection") and
  `app/README.md`'s section of the same name.
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
- ~~Camera access confirmed, photo capture wired into the job record~~
  **Done** - see Features above ("A build-plate photo on every finished
  job") and `app/README.md`'s "Printer camera" section. Confirmed with
  an actual real photo, not just isolated testing - job #15's
  automatically-detected completion produced and saved one for real.
- A dedicated camera, independent of the printer's own flaky single-
  session connection - per the user, after a string of real
  photo-capture failures (all since fixed - see Features above) that
  were always the connection or a related bug, never the core capture
  logic. Photo capture does now work end-to-end for real, but the
  printer's one-session-at-a-time design (see "Persistent printer
  connection") is a structural limit no amount of client-side code can
  fully engineer around - a dedicated camera would sidestep it entirely.
  Plan settled on, hardware not yet in hand: an ESP32-CAM
  (WiFi, not PoE - doesn't touch the router's limited LAN port budget,
  which the printer and the Pi already mostly use up), flashed with
  open-source firmware serving a plain local HTTP snapshot - no cloud
  possible at all, since there's no vendor service to even opt into.
  Free 3D-printable cases with a standard 1/4"-20 tripod thread exist
  already (e.g. Printables' "ESP32 CAM Case with Tripod Mount"), paired
  with a small clamp mount (a compact super-clamp + mini ball head, not
  a full articulating arm - the ESP32-CAM is featherweight) gripping an
  edge of the printer itself, Velcro not required. Deliberately not
  built yet - per the user, waiting until the actual hardware is in hand
  to test against rather than writing capture code blind. Once built:
  a generic "fetch a configured snapshot URL" capture path, swappable
  per printer (setting up cleanly for the already-planned second
  printer), with RTSP as a fallback for any future camera that only
  streams rather than serving a plain snapshot.
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
- ~~Bed adhesion tuning - a test print completed without error but
  didn't stick to the bed.~~ **Done** - resolved physically, not in the
  slicing profile: new bed tape and a glue stick fixed it. No
  first-layer/Z-offset/brim setting changes were needed.
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
- ~~Rate-limiting or lockout on login attempts - PINs are short by design
  for low signup friction, which also makes them easier to guess; nothing
  currently slows down repeated attempts.~~ **Done** - see Features above.
- ~~Let an admin reset a user's PIN, in case they forget it - today
  there's no recovery path at all short of the user just signing up
  under a new name (losing their submission history) or an admin
  deleting/recreating the account outright.~~ **Done** - see Features
  above.
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
- ~~A fixed IP or mDNS hostname for the server so users don't have to type
  or remember a raw IP address on the deployment network.~~ **Done** -
  the Pi's hostname is set to `q3d` via `raspi-config`, resolving as
  `q3d.local` through mDNS/Avahi from any client on the network (real
  DHCP + mDNS resolution issues hit and fixed during actual first-boot
  setup, not just assumed to work - see `deploy/README.md`).
- ~~Actually setting this up on the target Raspberry Pi~~ **Done, on the
  real hardware, not just verified in isolation** - first boot, SSH
  hardening (key-only auth, `PermitRootLogin no`, `PasswordAuthentication
  no`, a dedicated no-login `queue3d` service account), and a real
  `deploy.sh` run all completed against the actual Pi. Also grew well
  past its original scope along the way, all per the user, live: an
  nginx reverse proxy (so the app is reachable on a plain
  `https://q3d.local/`, no `:8000` to remember), a self-signed TLS cert
  generated once on first install (the only option - `.local` names can
  never get a real CA-signed cert), a pre-upgrade backup + rollback
  safety net with a post-deploy health check, and `remote_install.sh`
  now apt-installs its own missing OS packages (nginx/openssl/
  python3-venv) rather than requiring a separate manual step. Real bugs
  found and fixed only by actually running this against the hardware,
  not caught by any amount of isolated testing beforehand: a `PATH`
  issue breaking `useradd` under `sudo`/`ssh -t`, a `/opt` permission
  error from staging in the wrong directory, a Python 3.11-vs-3.13 wheel
  mismatch, and - the longest chase of the session - a Chrome-only
  `ERR_ADDRESS_UNREACHABLE` that turned out to be macOS's own Local
  Network privacy permission blocking Chrome, nothing to do with the
  deploy at all. Full account in `deploy/README.md`.
- Backup/cleanup cron jobs (`app/backup.py`, `app/cleanup_drafts.py`)
  still not wired into `remote_install.sh` or installed on the real
  Pi - the pre-upgrade safety backup added this session is a different,
  narrower thing (one snapshot right before an upgrade, kept 5 deep),
  not the ongoing daily disaster-recovery backup this item is about.
- The two rotating external USB backup drives (`backup.py`'s target)
  not yet physically connected/mounted on the real Pi - still runs
  against local disk only right now.
- ~~A downloadable "support bundle" (a `.tar.gz`) an admin can generate
  on demand, bundling whatever's needed for offline bugfixing after the
  real deployment (Pi/network/printer in place, zero internet access) -
  per the user: "After I setup the app/Pi, network, and printer in
  place, I want to be able to show up and collect the support files."~~
  **Done** - "Download support bundle" on the admin dashboard. Contents:
  a safe, consistent copy of the whole database (every job/user/setting/
  event - the single most useful thing for offline diagnosis, and what
  every real investigation this project has done actually started
  from), the original model file for every job that ever recorded a
  slice error (without the real geometry a slicing failure can't be
  reproduced at all), and the full activity log as plain text. Not
  included: a persistent application log file, since this app doesn't
  currently write one - see `app/README.md` for what that would take.

## Project layout

- `app/` - the web app (FastAPI). Start here for setup instructions.
- `slicing/` - the STL -> print-ready-file pipeline, usable standalone.
- `test-print/` - the printer network protocol client, usable standalone
  for testing connectivity without the rest of the app.
- `deploy/` - systemd unit + scripts for installing/upgrading on the real
  Raspberry Pi target with zero internet access at the deployment site.

Each has its own README with setup and implementation details.
