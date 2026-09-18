#!/usr/bin/env python3
"""Follow-up probe: request_camera_stream was accepted (unlike
request_camera_frame/get_available_cameras/etc, which are "method not
found" on this firmware). Does it actually push frame data afterward as
an unsolicited notification? Raw-byte capture again, not assuming JSON
framing once the stream is running - see camera_probe.py for why."""

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
print("handshake:", read_json_reply().get("result", {}).get("machine_name"))

send("authenticate", {"access_token": token}, req_id=1)
auth_reply = read_json_reply()
if "error" in auth_reply:
    print("AUTH FAILED:", auth_reply["error"])
    sock.close()
    raise SystemExit(1)
print("authenticated OK")

send("request_camera_stream", {}, req_id=2)
print("request_camera_stream reply:", read_json_reply())

print("Listening for 10s for any pushed data (frames, notifications)...")
sock.settimeout(10)
raw = b""
start = time.time()
try:
    while time.time() - start < 10:
        chunk = sock.recv(65536)
        if not chunk:
            print("(connection closed by printer)")
            break
        raw += chunk
        print(f"  ...received {len(chunk)} bytes just now, {len(raw)} total so far")
except socket.timeout:
    print("  (timed out, no more data)")

print(f"\nCaptured {len(raw)} raw bytes total.")
with open("camera_probe2_raw.bin", "wb") as f:
    f.write(raw)

jpeg_start = raw.find(b"\xff\xd8\xff")
print("JPEG SOI marker found at offset:", jpeg_start)
print("First 400 bytes (repr):")
print(repr(raw[:400]))

# try ending the stream cleanly
send("end_camera_stream", {}, req_id=3)
try:
    print("end_camera_stream reply:", read_json_reply())
except Exception as e:
    print("end_camera_stream: couldn't read a clean reply:", e)

sock.close()
