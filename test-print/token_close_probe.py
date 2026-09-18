#!/usr/bin/env python3
"""Isolate one specific hypothesis for the token-doesn't-survive-a-
reconnect finding: MakerBotClient.close() does shutdown(SHUT_RDWR) then
close() - if there's ever unread data still sitting in the socket's
receive buffer at that moment, the OS sends a TCP RST instead of a clean
FIN, which a server can reasonably treat as "client crashed" and
invalidate that session/token defensively. A genuinely graceful close
(finish writing, half-close the write side, drain any remaining incoming
bytes to EOF, *then* close) never triggers that RST.

This test does NOT use MakerBotClient's own close() at all - a raw
socket, authenticate, explicit graceful teardown, then a fresh connection
with the same token. If the reconnect works THIS time, the RST theory was
right and it's a client bug, not a printer/protocol limitation. If it
still fails, the token really is single-session no matter how politely
you leave, and that's a real design constraint to work around."""

import json
import socket
import time

from config import load_config, load_access_token

cfg = load_config()
host = cfg["printer_host"]
token = load_access_token()
print(f"Using token: {token}\n")


def read_json_reply(sock):
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


def send(sock, method, params=None, req_id=0):
    payload = {"id": req_id, "jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    sock.sendall(json.dumps(payload).encode("utf-8"))


print("--- connection 1: authenticate, then a deliberately GRACEFUL close ---")
sock1 = socket.create_connection((host, 9999), timeout=10)
send(sock1, "handshake", {}, req_id=0)
read_json_reply(sock1)
send(sock1, "authenticate", {"access_token": token}, req_id=1)
reply = read_json_reply(sock1)
if "error" in reply:
    print("AUTH FAILED immediately - token already dead, can't test close behavior:", reply["error"])
    raise SystemExit(1)
print("  AUTH OK")

# Graceful teardown: half-close the write side (tells the printer "no more
# requests coming, but I'm still listening"), drain anything it still
# wants to send us until it closes its end (EOF), THEN close our socket -
# never call close() while there's unread data sitting in the buffer.
print("  Half-closing write side...")
sock1.shutdown(socket.SHUT_WR)
sock1.settimeout(5)
drained = 0
try:
    while True:
        chunk = sock1.recv(4096)
        if not chunk:
            break  # clean EOF - printer closed its side too
        drained += len(chunk)
except socket.timeout:
    print(f"  (timed out waiting for printer's EOF after draining {drained} bytes - closing anyway)")
print(f"  Drained {drained} trailing bytes. Closing socket now (clean).")
sock1.close()

print("\n--- waiting 2s before reconnecting ---")
time.sleep(2)

print("--- connection 2: fresh socket, same token, after a truly graceful close ---")
sock2 = socket.create_connection((host, 9999), timeout=10)
send(sock2, "handshake", {}, req_id=0)
read_json_reply(sock2)
send(sock2, "authenticate", {"access_token": token}, req_id=1)
reply2 = read_json_reply(sock2)
if "error" in reply2:
    print(f"  AUTH FAILED: {reply2['error']}")
    print("\nConclusion: token is single-session regardless of close style -")
    print("not a client-side abrupt-disconnect bug. A real protocol constraint.")
else:
    print("  AUTH OK!")
    print("\nConclusion: graceful close let the token be reused - the ORIGINAL")
    print("close() (shutdown(SHUT_RDWR) then close()) was likely sending a TCP")
    print("RST that the printer treated as a crash and invalidated the session")
    print("for. This is a fixable client bug, not a protocol limitation.")
sock2.close()
