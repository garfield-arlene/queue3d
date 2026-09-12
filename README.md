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

## Project layout

- `app/` - the web app (FastAPI). Start here for setup instructions.
- `slicing/` - the STL -> print-ready-file pipeline, usable standalone.
- `test-print/` - the printer network protocol client, usable standalone
  for testing connectivity without the rest of the app.

Each has its own README with setup and implementation details.
