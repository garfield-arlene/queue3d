# queue3d

Do you have a shared local 3D printer? If so, queue3d lets users upload and
slice their own models and submit them to a queue. An admin reviews each
submission and releases approved jobs to the printer when ready.

**Printer support:** only the MakerBot Replicator+ (with a Tough Smart
Extruder+) is supported at this time, over its reverse-engineered network
protocol - see `test-print/README.md`. No other printer models or brands
are supported yet.

## Features

- **Self-serve user accounts** - sign up with just a name and PIN, no
  email or password reset flow.
- **Admin accounts** provisioned separately (no self-service admin
  signup) - reviewing and releasing jobs is a position of trust over
  shared printer time.
- **Upload and automatic slicing** - submit an STL, it's sliced
  server-side (OrcaSlicer + a patched `mbotmake`) into a print-ready file,
  no separate slicer software needed on the user's end.
- **Live duration estimate and queue position** shown to the submitter as
  soon as slicing finishes.
- **One shared queue** - submissions land directly in it; there's no
  separate pre-review step before something counts as queued.
- **Admin review** - approve, or reject with a required note explaining
  why, before anything reaches the printer.
- **Release to the printer over the network** - an approved job is sent
  and started directly; no walking a file over on a flash drive.
- **One job on the printer at a time**, enforced - releasing a second job
  while one is already printing is blocked with a clear error.
- **Automated backups** - the database and finished-job archive back up
  automatically on a schedule, rotating between two targets, with a
  dashboard indicator if a backup hasn't run recently.
- **Built for offline deployment** - runs entirely on a local network with
  no internet access required; no CDN dependencies.

## To do

Basic functionality works end to end (accounts, upload/slicing, the queue,
admin review/release) - these are the gaps between that and the
functionality this is meant to have:

**Security & CI**
- A pipeline to run security checks automatically (e.g. dependency
  vulnerability scanning, static analysis, secret scanning) rather than
  relying on manual review.

**Upload**
- A progress bar or other "receiving/slicing" indicator during upload.
  Right now the request blocks silently until slicing finishes entirely
  (which can take a while) - the only sign anything is happening is the
  browser's own loading state (e.g. the reload button turning into an
  "X"), which looks identical whether it's working or stuck.

**Job review & feedback**
- A visual preview of a submitted model, on both the user's and the
  admin's view of a job - lets a user judge before submitting whether a
  model looks right and is likely to print successfully, and lets an
  admin judge the same thing during review, plus whether it's appropriate
  to print at all, without that being a blind approve/reject on a filename.
- Show failure reasons to the user, not just rejection notes - rejection
  notes already display (required, and shown on the user's dashboard); a
  failed print currently has no reason at all (`mark_failed` only flips
  status, no note field), and slicing-error detail is currently only
  visible to an admin (as a hover tooltip), never shown to the user.
- Break the estimated print duration into days/hours/minutes - it's
  currently total minutes only.

**Appearance**
- Light/dark theme, with a toggle.
- Selectable wallpaper/background themes.

**Printer**
- Live print progress/status while a job is printing - `mark_done`/
  `mark_failed` are still a manual admin action; the printer's protocol
  has a status-notification mechanism that isn't consumed yet.
- More robust pairing: after a power-on, the printer's HTTP pairing
  service has been observed to take roughly a minute to come up after its
  network/JSON-RPC service already answers, causing pairing to fail if
  attempted too early; and a saved pairing token has been observed to stop
  working at least once (cause not confirmed - possibly related to
  dismissing a printer error via its dial). Pairing should retry through
  the former automatically and the app should detect and recover from the
  latter without needing someone to notice and manually re-pair.
- Bed adhesion tuning in the slicing profile - a test print completed
  without error but didn't stick to the bed (first-layer/Z-offset/brim
  settings need dialing in for the actual printer).
- Support for printer models/brands beyond the MakerBot Replicator+.

**Accounts**
- An in-app way for an existing admin to promote a regular user to admin,
  instead of requiring direct server/CLI access for every new admin.
- Rate-limiting or lockout on login attempts - PINs are short by design
  for low signup friction, which also makes them easier to guess; nothing
  currently slows down repeated attempts.

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
