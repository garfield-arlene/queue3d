# test-print

Smallest possible proof-of-concept: send a `.makerbot` file to the MakerBot
Replicator+ over the network and confirm it starts printing. This is step
zero for the bigger queue3d project - it only exists to de-risk the network
protocol before building the upload/slice/queue app around it.

## Background

MakerBot never published this protocol. What's implemented here is based on
reverse-engineering write-ups and existing open-source clients
(`tjhorner/node-makerbot-rpc`, `gryphius/makerbot-gen5-api`,
`charely6/makerbot-gen5-api`), not an official spec - treat it as "best
understanding," and expect to iterate if your firmware version behaves
slightly differently.

Summary of how it works:
- The printer runs JSON-RPC 2.0 on TCP port 9999.
- A one-time pairing handshake over plain HTTP (port 80, `/auth`) produces a
  permanent access token. Pairing requires physically pressing the dial on
  the printer to accept - this is the actual security boundary, not the
  client secret.
- Once authenticated on the JSON-RPC socket, a print job is: `print` (announce
  filename) -> `process_method: build_plate_cleared` -> `put_init` -> repeated
  `put_raw` (JSON header immediately followed by that many raw file bytes) ->
  `put_term` (with a CRC32 of the whole file).
- The printer only accepts its proprietary `.makerbot` container format, not
  raw STL or plain G-code.

## Prerequisites

- A `.makerbot` file you already know is good (sliced via MakerBot
  Print/Desktop previously). Put it in this directory, e.g. `test.makerbot`.
  We're deliberately not touching the Linux-slicing side yet - this test is
  only about the network path.
- The printer's IP in `config.json` (already set to `192.168.1.250`).
- Python 3, no third-party dependencies (stdlib only).

## Usage

Run these in order, from this directory:

```bash
# 1. Pure network smoke test - no pairing needed, just confirms the printer
#    is reachable and answers JSON-RPC.
python3 check_connection.py

# 2. One-time pairing. Watch the printer's screen - it will ask you to
#    press the dial to accept. Saves the resulting token to auth.json.
python3 pair.py

# 3. Upload the file and start the print.
#    WARNING: this actually starts heating/printing - clear the build
#    plate and be present to supervise the first run.
python3 send_print.py test.makerbot
```

`auth.json` is gitignored (it's a credential); `*.makerbot` test files are
gitignored too so we don't accidentally commit binary blobs.

## If something goes wrong

- `check_connection.py` fails to connect: check the IP in `config.json`,
  confirm the printer is on and its network icon shows it's connected (not
  just Wi-Fi AP mode), and that nothing else (MakerBot Print/Desktop's
  "conveyor" background service) is holding the connection on another
  machine on the LAN.
- `pair.py` times out: make sure you actually see - and press - the
  confirmation prompt on the printer's screen within 2 minutes.
- `send_print.py` errors on `print` or `put_init`: the printer may already
  be mid-job, or the build plate check may be blocking - check its screen
  for what it's waiting on.
