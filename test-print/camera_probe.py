#!/usr/bin/env python3
"""Exploratory probe: does this printer support request_camera_frame, and
what does the raw response actually look like on the wire? Deliberately
NOT using MakerBotClient's existing reader thread - that assumes every
message on the socket is a complete JSON object (brace-counted), which
would misparse or hang on raw binary JPEG bytes if the camera_frame
notification includes them inline. This reads raw bytes directly and
dumps everything to a file for offline inspection instead of guessing at
the format up front.
"""

import json
import socket
import time

from config import load_config, load_access_token

cfg = load_config()
host = cfg["printer_host"]
token = load_access_token()

sock = socket.create_connection((host, 9999), timeout=10)


def send(method, params=None, req_id=0):
    payload = {"id": req_id, "jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    sock.sendall(json.dumps(payload).encode("utf-8"))


def read_json_reply():
    """Read exactly one brace-counted JSON object - safe for the plain
    handshake/authenticate replies before any binary data is in play."""
    buf = b""
    depth = 0
    in_string = False
    escape = False
    started = False
    while True:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
        c = chunk.decode("latin1")
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return json.loads(buf)
    return None


send("handshake", {}, req_id=0)
print("handshake reply:", read_json_reply())

send("authenticate", {"access_token": token}, req_id=1)
print("authenticate reply:", read_json_reply())

print("Requesting a single camera frame...")
send("request_camera_frame", {}, req_id=2)

# Now just hoover up raw bytes for a few seconds and dump them - don't
# assume anything about the format yet.
sock.settimeout(5)
raw = b""
start = time.time()
try:
    while time.time() - start < 6:
        chunk = sock.recv(65536)
        if not chunk:
            break
        raw += chunk
except socket.timeout:
    pass

print(f"Captured {len(raw)} raw bytes total.")
with open("camera_probe_raw.bin", "wb") as f:
    f.write(raw)

jpeg_start = raw.find(b"\xff\xd8\xff")
jpeg_end = raw.rfind(b"\xff\xd9")
print("JPEG SOI marker (ffd8ff) found at offset:", jpeg_start)
print("JPEG EOI marker (ffd9) found at offset:", jpeg_end)

# Show whatever's printable at the very start, for a look at the framing.
print("First 300 bytes (repr):")
print(repr(raw[:300]))

sock.close()
