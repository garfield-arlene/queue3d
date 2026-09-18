#!/usr/bin/env python3
"""One pairing (one dial press), several tests against the SAME token, to
isolate why a freshly-paired token has been failing on every connection
after its first - without spending a dial-press per hypothesis. Tests, in
order:

  A. Baseline: does the token work at all, right after pairing?
  B. Concurrent: while connection A is still open, does a SECOND,
     simultaneous connection with the same token also authenticate? (If
     this fails while A succeeds, that points at "one active session at a
     time" rather than "invalidated by disconnect".)
  C. After clean disconnect: close A, then try a brand new connection.
     (If this fails, that's the "doesn't survive a reconnect" behavior
     already seen 3/3 - confirms it, doesn't yet explain it.)
  D. Time-based, only if C succeeds: wait a few seconds with no
     connection open at all, then try again - distinguishes "breaks
     immediately on any reconnect" from "has a short TTL".

Each step's result is printed clearly with which hypothesis it does or
doesn't support - this is exploration, not an assertion of the answer.
"""

import socket
import sys
import time

from config import load_config, load_access_token
from makerbot_client import MakerBotClient, MakerBotError

cfg = load_config()
host = cfg["printer_host"]


def try_auth(token, label):
    try:
        with MakerBotClient(host) as client:
            client.handshake()
            client.request("authenticate", {"access_token": token})
            print(f"  [{label}] AUTH OK")
            return True, client
    except MakerBotError as e:
        print(f"  [{label}] AUTH FAILED: {e}")
        return False, None


token = load_access_token()
print(f"Using token: {token}\n")

print("--- A. baseline: fresh connection, authenticate ---")
with MakerBotClient(host) as conn_a:
    conn_a.handshake()
    try:
        conn_a.request("authenticate", {"access_token": token})
        print("  [A] AUTH OK")
    except MakerBotError as e:
        print(f"  [A] AUTH FAILED: {e}")
        print("Token is already dead before any other test - stopping here.")
        sys.exit(1)

    print("\n--- B. while A is still open, try a SECOND simultaneous connection ---")
    conn_b = MakerBotClient(host)
    conn_b.connect()
    conn_b.handshake()
    try:
        conn_b.request("authenticate", {"access_token": token})
        print("  [B] AUTH OK (concurrent connections both work)")
    except MakerBotError as e:
        print(f"  [B] AUTH FAILED (concurrent auth rejected): {e}")
    conn_b.close()

    print("\n--- confirm A itself is still fine after B's attempt ---")
    try:
        info = conn_a.request("get_system_information", {})
        print(f"  [A still open] OK, current_process={info.get('current_process')}")
    except MakerBotError as e:
        print(f"  [A still open] BROKE: {e}")

print("\n--- C. A has now been cleanly closed. New connection, same token ---")
ok_c, conn_c = try_auth(token, "C")
if conn_c:
    conn_c.close()

if not ok_c:
    print("\nConclusion so far: token dies on/after a clean disconnect -")
    print("doesn't survive ANY reconnect, not a time-based expiry question.")
    sys.exit(0)

print("\n--- D. token survived one reconnect - waiting 8s with nothing open, try again ---")
time.sleep(8)
ok_d, conn_d = try_auth(token, "D")
if conn_d:
    conn_d.close()
