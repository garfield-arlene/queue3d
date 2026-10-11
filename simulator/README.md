# Printer simulator

A fake MakerBot Replicator+ for dev work with no real printer on hand -
the real one has shipped to the actual deployment site (see project
memory `queue3d-printer-onsite-status`), and development on the printer
integration itself shouldn't have to stop because of that.

Implements just enough of the real, reverse-engineered wire protocol
(see `app/printer.py`'s own module docstring for the full writeup) to
drive the app's actual client code through its real paths: the plaintext
HTTP `/auth` pairing dance, and the TCP JSON-RPC socket for handshake,
authenticate, uploading a print, live status polling
(`get_system_information`), and the camera stream. **Not** a
protocol-accurate firmware clone - real return values and the full space
of real error conditions aren't reproduced, only the shapes the app
actually reads. One thing *is* reproduced faithfully, because the app's
own retry/reconnect logic depends on it: a pairing token (and the
auth_code that produces it) is good for exactly one use, never reusable -
see `app/printer.py`'s own module docstring for the real investigation
this mirrors.

## Running it

```bash
cd simulator
python3 printer_simulator.py
```

No dependencies beyond the standard library - nothing to install.

By default it listens on `0.0.0.0:9999` for the JSON-RPC socket (matches
the real printer) and `0.0.0.0:80` for `/auth` (also matches the real
printer - `pair_printer.py`'s HTTP calls hardcode port 80, same as the
real client always has, so testing an unmodified pairing flow needs this
on port 80, which needs root):

```bash
sudo python3 printer_simulator.py
```

Then, same as against real hardware: set the printer's address on
`/admin/settings` to this machine's own LAN address (port 9999, unless
`--rpc-port` was changed), and run `pair_printer.py` once.

## Options

- `--print-duration SECONDS` - how long a simulated print takes end to
  end, capped at 60s (default 45). Deliberately fast regardless of the
  job's real `duration_estimate_s` - see "Don't point this at a real
  database" below for why that's fine.
- `--pairing-delay SECONDS` - delay before auto-accepting a pairing
  request, simulating the dial press (default 2).
- `--reject-pairing` - always reject pairing instead of accepting, to
  test that path.
- `--fail-at {none,error,cancelled}` - what the simulated print reports
  once `--print-duration` elapses (default `none` - completes
  successfully). Drives `jobs.check_and_finish_active_print`'s automatic
  failure detection the same way a real cancel/error would.
- `--reject-upload` - always fail `put_term`, to test the app's
  upload-failure handling without a real CRC mismatch.
- `--rpc-port`, `--http-port`, `--host`, `--machine-name` - see
  `--help`.

## Don't point this at a real database

Run this against the dev database only - never the real deployment's.
`jobs._duration_correction_factor` learns a running median of
real-print-time-over-estimate from every `done` job's actual duration,
to correct future ETAs shown to real users. A simulated print finishes in
seconds, not hours; a database that's seen even a handful of these fast
fake completions mixed in with real ones would skew that median and
throw off every future estimate. In practice this isn't something that
can happen by accident - the dev machine and the real Pi are already two
entirely separate SQLite files on two separate machines with no network
path between them - just don't manually copy a production database here
for some other kind of testing while this is in use.

## What's simulated vs. not

Simulated: pairing (accept/reject/timeout), single-use tokens and
auth_codes, authenticated vs. unauthenticated RPC calls, a chunked
upload with real CRC32 validation against what was actually received,
live `current_process` progress through `final_heating` -> `printing` ->
a terminal outcome (`complete`/`cancelled`/`error`, chosen by
`--fail-at`), and a one-frame-per-second camera stream of a real,
decodable (if not very interesting) JPEG.

Not simulated: anything about the physical machine itself (temperature,
filament, actual G-code execution), any RPC method the app doesn't
actually call today, and the full space of real firmware error replies -
only the ones this app's own code paths are written to handle.
