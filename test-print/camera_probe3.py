#!/usr/bin/env python3
"""Third camera investigation pass - now that the persistent-connection
fix is understood, this keeps ONE raw connection open for the WHOLE
investigation instead of reconnecting between steps (which is what
starved the earlier attempts of a stable session to actually listen on).
Raw sockets throughout, not MakerBotClient - its background reader thread
would race with any attempt to also read raw bytes directly off the same
socket from the main thread, and could silently swallow a pushed frame
as an "unsolicited notification, ignored" before this script ever saw it.

Steps, all on one connection:
  1. authenticate (re-pairs first if the saved token's already dead)
  2. sanity check (get_system_information)
  3. request_camera_stream
  4. listen on the raw socket for up to 20s for ANY pushed data
  5. re-confirm request_camera_frame is still "method not found"
  6. try the HTTP /camera?token=... endpoint while the JSON-RPC
     connection is still open/authenticated with an active stream
  7. end_camera_stream, close cleanly
"""

import json
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")
from config import load_config
from pairing import pair, _auth_get  # noqa: F401 (pair only, _auth_get unused here)


def load_token():
    try:
        with open("auth.json") as f:
            return json.load(f).get("access_token")
    except FileNotFoundError:
        return None


def save_token(token):
    with open("auth.json", "w") as f:
        json.dump({"access_token": token}, f, indent=2)


cfg = load_config()
host = cfg["printer_host"]


def read_json_reply(sock):
    buf = b""
    depth = 0
    in_string = False
    escape = False
    started = False
    while True:
        chunk = sock.recv(1)
        if not chunk:
            return None
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


def send(sock, method, params=None, req_id=0):
    payload = {"id": req_id, "jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    sock.sendall(json.dumps(payload).encode("utf-8"))


token = load_token()

# Don't pre-check the token with a separate throwaway connection - per the
# confirmed finding, that would itself spend the token's one session,
# leaving nothing for the real investigation below. Just attempt the real
# connection directly; only pair fresh if THIS attempt fails.
sock = socket.create_connection((host, 9999), timeout=10)
send(sock, "handshake", {}, 0)
read_json_reply(sock)
auth_reply = None
if token:
    send(sock, "authenticate", {"access_token": token}, 1)
    auth_reply = read_json_reply(sock)

if not token or "error" in auth_reply:
    print("Need a fresh pairing." if token else "No saved token.")
    sock.close()

    def on_waiting():
        print(">>> Check the printer's screen and press the dial now <<<")

    token = pair(host, on_waiting=on_waiting)
    save_token(token)
    print("Paired, token saved.\n")

    sock = socket.create_connection((host, 9999), timeout=10)
    send(sock, "handshake", {}, 0)
    read_json_reply(sock)
    send(sock, "authenticate", {"access_token": token}, 1)
    auth_reply = read_json_reply(sock)
    assert "error" not in auth_reply, auth_reply
else:
    print("Existing token still worked - no re-pairing needed.\n")

print("1. Connected and authenticated on one persistent connection.")

send(sock, "get_system_information", {}, 2)
info = read_json_reply(sock)
print("2. Sanity check, current_process:", info.get("result", {}).get("current_process"))

print("3. Calling request_camera_stream...")
send(sock, "request_camera_stream", {}, 3)
stream_reply = read_json_reply(sock)
print("   reply:", stream_reply)

print("4. Listening on the raw socket for up to 20s for any pushed frame data...")
sock.settimeout(20)
raw = b""
start = time.time()
try:
    while time.time() - start < 20:
        chunk = sock.recv(65536)
        if not chunk:
            print("   (connection closed by printer)")
            break
        raw += chunk
        print(f"   ...received {len(chunk)} bytes just now, {len(raw)} total")
except socket.timeout:
    print(f"   (20s elapsed, no more data - {len(raw)} bytes total)")

if raw:
    with open("camera_probe3_raw.bin", "wb") as f:
        f.write(raw)
    jpeg_at = raw.find(b"\xff\xd8\xff")
    print(f"   JPEG SOI marker found at offset: {jpeg_at}")
    print(f"   First 300 bytes: {raw[:300]!r}")
else:
    print("   Nothing was ever pushed after request_camera_stream.")

print("\n5. Re-confirming request_camera_frame on this SAME still-open connection:")
send(sock, "request_camera_frame", {}, 4)
sock.settimeout(10)
r = read_json_reply(sock)
print("  ", r)

print("\n6. Trying HTTP /camera endpoint while the JSON-RPC connection is still open...")
url = f"http://{host}/camera?token={token}"
try:
    with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310
        data = resp.read(200)
        print(f"   HTTP {resp.status}, content-type={resp.headers.get('Content-Type')}, first bytes: {data[:50]!r}")
except urllib.error.HTTPError as e:
    print(f"   HTTP error {e.code}: {e.reason}")
    try:
        print("   body:", e.read(300))
    except Exception:
        pass
except Exception as e:
    print(f"   {type(e).__name__}: {e}")

print("\n7. Ending the stream and closing cleanly...")
send(sock, "end_camera_stream", {}, 5)
try:
    print("   reply:", read_json_reply(sock))
except Exception:
    pass
sock.close()
print("Done.")
