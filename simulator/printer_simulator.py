#!/usr/bin/env python3
"""Stands in for the real MakerBot Replicator+ on the dev network, now
that the real printer has shipped to the actual deployment site (see
project memory queue3d-printer-onsite-status) - lets development on
pairing, uploads, live status polling, and auto-finish detection keep
moving without physical hardware on hand. Implements just enough of
the real, reverse-engineered wire protocol (see printer.py's own
module docstring for the full writeup) to drive app/printer.py's real
client through its actual code paths: the plaintext HTTP /auth
pairing dance, and the TCP JSON-RPC 2.0 socket (concatenated JSON
objects, brace-counted framing, no length prefix) for handshake/
authenticate/print/put_init/put_raw/put_term/get_system_information/
request_camera_stream/end_camera_stream.

Deliberately NOT a protocol-accurate firmware clone - real return
values and the full space of real error conditions aren't reproduced,
only the shapes app/jobs.py and app/printer.py actually read (see each
handler below for exactly which fields matter and why). What IS
reproduced faithfully, because the app's own retry/reconnect logic
depends on it: a pairing token is good for exactly one authenticated
session, never reusable even by a clean reconnect (see _TokenState
and _PairingState below, and printer.py's module docstring for the
real investigation this mirrors).

Run against the dev database only - never the real deployment's. A
simulated print finishes in seconds, not hours; jobs._duration_correction_factor
learns a running median of real-print-time-over-estimate from every
`done` job's actual duration, and a database that's seen even a
handful of these fast fake completions would skew every future ETA
shown to real users. Dev and the real Pi are already two entirely
separate SQLite files on two machines with no path between them, so
this can't happen by accident - just don't point this at a copy of
the production database for some other kind of testing.

Usage:
    python3 printer_simulator.py
    python3 printer_simulator.py --print-duration 20 --fail-at error
    sudo python3 printer_simulator.py --http-port 80   # needed for
        # pair_printer.py unmodified - it hits http://<host>/auth with
        # no port in the URL at all, hardcoded to assume 80, same as
        # the real printer. --http-port on a non-privileged port still
        # works for poking the simulator directly (curl, a REPL), just
        # not for an unmodified pairing attempt from the app.

Then, same as against real hardware: set the printer's address on
/admin/settings (or QUEUE3D_PRINTER_HOST for an older checkout) to
this machine's own LAN address, port 9999 (or --rpc-port's value), and
run pair_printer.py once.
"""

import argparse
import json
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# A real, decodable 320x240 JPEG, labeled so it's unmistakably not a
# real capture when it shows up on a job's "Photo" link - found by
# testing that an earlier, technically-valid-but-degenerate 2x2 JPEG
# here rendered unusably small/broken-looking in a real browser, not
# the uniform gray swatch it should have been. Shipped as a real file
# (fake_frame.jpg, next to this script) rather than inlined as a hex
# literal - a few KB of image bytes has no business bloating the
# source as a giant hex string.
_FAKE_JPEG = (Path(__file__).resolve().parent / "fake_frame.jpg").read_bytes()


class _TokenState:
    """Models the one real behavior printer.py's own module docstring
    documents as confirmed, not just assumed - a pairing token
    authenticates exactly one session, full stop. Confirmed live
    against the real printer three separate ways (see that docstring):
    a second connection with the same token is rejected while the
    first stays open, a new connection after cleanly closing the first
    also fails, and a deliberately graceful close still doesn't help.
    A single "the current unused token, if any" slot reproduces all
    three: the moment it's used once (by anyone, on any connection),
    it's gone - a fresh /auth pairing round is the only way to get a
    new one, exactly matching reality."""

    def __init__(self):
        self._lock = threading.Lock()
        self._unused_token = None

    def issue(self) -> str:
        with self._lock:
            self._unused_token = uuid.uuid4().hex
            return self._unused_token

    def redeem(self, token: str) -> bool:
        with self._lock:
            if token and token == self._unused_token:
                self._unused_token = None
                return True
            return False


class _PairingState:
    """One pairing attempt at a time, matching the real dial-press flow
    pair() in printer.py drives: request a code, poll "answer" until a
    human (here: a timer, or --reject-pairing) decides, then exchange
    the resulting auth_code for a token."""

    def __init__(self, delay_s: float, reject: bool):
        self.delay_s = delay_s
        self.reject = reject
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}  # answer_code -> state

    def start(self) -> str:
        answer_code = uuid.uuid4().hex
        with self._lock:
            self._pending[answer_code] = {"requested_at": time.monotonic(), "auth_code": None}
        return answer_code

    def poll(self, answer_code: str) -> dict:
        with self._lock:
            state = self._pending.get(answer_code)
        if state is None:
            return {"answer": "rejected"}
        if self.reject:
            return {"answer": "rejected"}
        if time.monotonic() - state["requested_at"] < self.delay_s:
            return {"answer": "waiting"}
        with self._lock:
            if state["auth_code"] is None:
                state["auth_code"] = uuid.uuid4().hex
            auth_code = state["auth_code"]
        return {"answer": "accepted", "code": auth_code}

    def redeem(self, auth_code: str) -> bool:
        """Single-use, same as the real access token it produces (see
        _TokenState) - found by testing, not assumed: an early version
        of this let the same auth_code be exchanged for a fresh token
        repeatedly, which is realistic for nothing real this firmware
        does."""
        with self._lock:
            for key, state in list(self._pending.items()):
                if state["auth_code"] == auth_code:
                    del self._pending[key]
                    return True
            return False


class PrintState:
    """The simulated printer's one and only current_process - shared,
    not per-connection, since it represents the physical machine's own
    state (the same thing get_system_information's real reply
    describes). jobs.print_progress/check_and_finish_active_print read
    exactly these fields (filename, step, progress, time_remaining,
    complete, cancelled, error) - see each one's own docstring in
    jobs.py for why; nothing else in current_process is read by this
    app today, so nothing else is simulated."""

    def __init__(self, print_duration_s: float, fail_at: str):
        self.print_duration_s = print_duration_s
        self.fail_at = fail_at
        self._lock = threading.Lock()
        self._filename = None
        self._started_at = None
        self._outcome_applied = False

    def start(self, filename: str) -> None:
        with self._lock:
            self._filename = filename
            self._started_at = time.monotonic()
            self._outcome_applied = False

    def current_process(self) -> dict | None:
        with self._lock:
            if self._filename is None:
                return None
            elapsed = time.monotonic() - self._started_at
            # The first ~10% of the simulated duration (capped at 3s, so
            # a short --print-duration during fast iteration doesn't
            # spend most of it "heating") is final_heating - matches the
            # one other step value printer.py's own docstring/README
            # mention ever having actually seen. print_progress()
            # deliberately only ever treats "printing" as a real percent,
            # exactly to guard against a step like this one reusing the
            # same 0-100 scale for something unrelated (heating-to-
            # temperature, not print progress).
            heating_s = min(3.0, self.print_duration_s * 0.1)
            if elapsed < heating_s:
                return {
                    "filename": self._filename,
                    "step": "final_heating",
                    "progress": int(100 * elapsed / heating_s) if heating_s else 100,
                    "time_remaining": max(0, round(self.print_duration_s - elapsed)),
                    "complete": False,
                    "cancelled": False,
                    "error": None,
                }
            printing_elapsed = elapsed - heating_s
            printing_total = max(0.001, self.print_duration_s - heating_s)
            percent = min(100, int(100 * printing_elapsed / printing_total))
            remaining = max(0, round(self.print_duration_s - elapsed))
            if elapsed < self.print_duration_s:
                return {
                    "filename": self._filename,
                    "step": "printing",
                    "progress": percent,
                    "time_remaining": remaining,
                    "complete": False,
                    "cancelled": False,
                    "error": None,
                }
            # Past the simulated duration - report whichever terminal
            # outcome --fail-at asked for, exactly once reaching 100%/0s
            # remaining either way (a real failure mid-print wouldn't
            # necessarily show 100%, but nothing reads progress once
            # cancelled/error is set, so this doesn't need to be exact).
            return {
                "filename": self._filename,
                "step": "printing",
                "progress": 100,
                "time_remaining": 0,
                "complete": self.fail_at == "none",
                "cancelled": self.fail_at == "cancelled",
                "error": "Simulated failure (--fail-at error)" if self.fail_at == "error" else None,
            }


def _extract_message(buf: bytes):
    """Independent re-implementation of the same brace-counting framing
    printer.py's _MakerBotClient._extract_message uses to parse the
    real printer's replies - deliberately not imported from there (or
    from test-print/, whose own job is done - see printer.py's module
    docstring): a test double that shared the exact same framing code
    with the thing it's standing in for couldn't ever catch a framing
    bug in either one."""
    if not buf:
        return None, buf
    if buf[0:1] != b"{":
        idx = buf.find(b"{")
        if idx == -1:
            return None, b""
        buf = buf[idx:]
    depth = 0
    in_string = False
    escape = False
    for i, byte in enumerate(buf):
        char = chr(byte)
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return buf[: i + 1], buf[i + 1 :]
    return None, buf


class RPCHandler(socketserver.BaseRequestHandler):
    """One instance per TCP connection - the app only ever holds one at
    a time via _PersistentConnection, but ThreadingTCPServer handles a
    pairing retry or a deliberate second connection just as well."""

    server: "RPCServer"

    def setup(self):
        self._authenticated = False
        self._buf = b""
        self._upload = None  # {"file_id", "expected_length", "received", "data"}

    def handle(self):
        peer = self.client_address
        print(f"[rpc] connection from {peer}")
        try:
            while True:
                msg, self._buf = _extract_message(self._buf)
                if msg is None:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        break
                    self._buf += chunk
                    continue
                self._handle_message(msg)
        except (ConnectionError, OSError):
            pass
        finally:
            print(f"[rpc] connection from {peer} closed")

    def _reply(self, req_id, result=None, error=None):
        payload = {"id": req_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        self.request.sendall(json.dumps(payload).encode("utf-8"))

    def _handle_message(self, raw: bytes):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[rpc] couldn't parse: {raw!r}")
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        req_id = msg.get("id")
        print(f"[rpc] -> {method} {params if method != 'put_raw' else ''}")

        if method == "handshake":
            self._reply(req_id, result=self.server.handshake_info)
            return

        if method == "authenticate":
            if self.server.tokens.redeem(params.get("access_token")):
                self._authenticated = True
                self._reply(req_id, result={})
            else:
                self._reply(req_id, error={"message": "Authentication failed - token invalid, already used, or expired"})
            return

        if not self._authenticated:
            self._reply(req_id, error={"message": "Not authenticated"})
            return

        if method == "get_system_information":
            current = self.server.print_state.current_process()
            result = {"current_process": current} if current is not None else {}
            self._reply(req_id, result=result)
            return

        if method == "print":
            self._pending_filename = params.get("filepath")
            self._reply(req_id, result={})
            return

        if method == "process_method":
            # Real firmware doesn't acknowledge every one of these
            # (printer.py's own _upload_and_print tolerates that) -
            # acknowledging all of them here is simpler and just as
            # valid a response.
            self._reply(req_id, result={})
            return

        if method == "put_init":
            self._upload = {
                "file_id": params.get("file_id"),
                "expected_length": params.get("length", 0),
                "received": 0,
                "data": bytearray(),
            }
            self._reply(req_id, result={})
            return

        if method == "put_raw":
            length = params.get("length", 0)
            self._reply(req_id, result={})
            self._read_raw_chunk(length)
            return

        if method == "put_term":
            self._finish_upload(req_id, params)
            return

        if method == "request_camera_stream":
            self._reply(req_id, result={})
            self._camera_stop = threading.Event()
            self._camera_thread = threading.Thread(target=self._stream_camera_frames, daemon=True)
            self._camera_thread.start()
            return

        if method == "end_camera_stream":
            if getattr(self, "_camera_stop", None) is not None:
                self._camera_stop.set()
            self._reply(req_id, result={})
            return

        self._reply(req_id, error={"message": "Method not found"})

    def _read_raw_chunk(self, length: int) -> None:
        """Reads exactly `length` raw bytes directly off the socket,
        immediately after acknowledging put_raw - the real client sends
        them only once it's seen that ack (see printer.py's
        _upload_and_print: client.request(...) blocks for the reply,
        then client.send_raw(chunk) follows), so there's no ambiguity
        about when raw bytes vs. the next JSON message are coming."""
        needed = length - len(self._buf)
        while needed > 0:
            chunk = self.request.recv(min(65536, needed))
            if not chunk:
                break
            self._buf += chunk
            needed -= len(chunk)
        raw, self._buf = self._buf[:length], self._buf[length:]
        if self._upload is not None:
            self._upload["data"].extend(raw)
            self._upload["received"] += len(raw)

    def _finish_upload(self, req_id, params: dict) -> None:
        if self._upload is None:
            self._reply(req_id, error={"message": "put_term with no upload in progress"})
            return
        expected_crc = params.get("crc")
        expected_len = params.get("length")
        actual_crc = zlib.crc32(bytes(self._upload["data"])) & 0xFFFFFFFF
        actual_len = self._upload["received"]
        if self.server.reject_upload:
            self._reply(req_id, error={"message": "Simulated upload rejection (--reject-upload)"})
            self._upload = None
            return
        if actual_len != expected_len or actual_crc != expected_crc:
            print(
                f"[rpc] upload mismatch: got {actual_len}B/crc={actual_crc:08x}, "
                f"expected {expected_len}B/crc={expected_crc:08x}"
            )
            self._reply(req_id, error={"message": "CRC or length mismatch"})
            self._upload = None
            return
        print(f"[rpc] upload complete: {actual_len} bytes, starting simulated print ({self.server.print_state.print_duration_s:.0f}s)")
        self.server.print_state.start(getattr(self, "_pending_filename", None) or "unknown.makerbot")
        self._upload = None
        self._reply(req_id, result={})

    def _stream_camera_frames(self) -> None:
        """One camera_frame notification (no "id" - a push) plus its
        16-byte header + JPEG payload, repeated roughly once a second
        until end_camera_stream - matches the real continuous-MJPEG-
        style behavior printer.py's capture_one_frame docstring
        documents discovering (request_camera_stream starts a push
        stream; there's no true one-shot capture)."""
        while not self._camera_stop.is_set():
            try:
                header = struct.pack(">IIII", len(_FAKE_JPEG), 320, 240, 0)
                notification = json.dumps({"method": "camera_frame"}).encode("utf-8")
                self.request.sendall(notification + header + _FAKE_JPEG)
            except OSError:
                return
            self._camera_stop.wait(1.0)


class RPCServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, tokens: _TokenState, print_state: PrintState, handshake_info: dict, reject_upload: bool):
        super().__init__(addr, RPCHandler)
        self.tokens = tokens
        self.print_state = print_state
        self.handshake_info = handshake_info
        self.reject_upload = reject_upload


class AuthHandler(BaseHTTPRequestHandler):
    server: "AuthServer"

    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/auth":
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        response_type = (qs.get("response_type") or [""])[0]

        if response_type == "code":
            answer_code = self.server.pairing.start()
            body = {"answer_code": answer_code}
        elif response_type == "answer":
            answer_code = (qs.get("answer_code") or [""])[0]
            body = self.server.pairing.poll(answer_code)
        elif response_type == "token":
            auth_code = (qs.get("auth_code") or [""])[0]
            if self.server.pairing.redeem(auth_code):
                token = self.server.tokens.issue()
                body = {"status": "success", "access_token": token}
            else:
                body = {"status": "error", "message": "Invalid or already-used auth_code"}
        else:
            self.send_response(400)
            self.end_headers()
            return

        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class AuthServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, pairing: _PairingState, tokens: _TokenState):
        super().__init__(addr, AuthHandler)
        self.pairing = pairing
        self.tokens = tokens


def _detect_lan_ip() -> str | None:
    """Best-effort LAN IP for the "set this on /admin/settings" message
    below - binding "0.0.0.0" (every interface) is correct and
    necessary, but not something to actually type into that setting,
    and making the person go look it up separately (`ip addr`,
    `hostname -I`, the router's own DHCP page) defeats most of the
    point of printing this message at all. The UDP "connect" trick:
    connecting a UDP socket never actually sends a packet (UDP is
    connectionless) - it only asks the OS which local address would be
    used to route to that destination, which works even fully offline,
    as long as some default route exists at all (true on any ordinary
    LAN-connected machine, same as deploy/remote_install.sh's own
    `hostname -I` detection assumes for the real Pi). Falls back to
    parsing `hostname -I` directly (not available on every OS, but
    worth trying) if that somehow doesn't work; None if neither does,
    so the caller can fall back to just showing the literal bind
    address instead."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
        first = result.stdout.split()
        if first:
            return first[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    parser.add_argument("--rpc-port", type=int, default=9999, help="JSON-RPC TCP port (default: 9999, matches the real printer)")
    parser.add_argument("--http-port", type=int, default=80, help="/auth HTTP port (default: 80, matches the real printer - needs root; see this script's own docstring)")
    parser.add_argument("--print-duration", type=float, default=45, help="seconds a simulated print takes end to end, capped at 60 (default: 45)")
    parser.add_argument("--pairing-delay", type=float, default=2, help="seconds before auto-accepting a pairing request, simulating the dial press (default: 2)")
    parser.add_argument("--reject-pairing", action="store_true", help="always reject pairing instead of accepting it, to test that path")
    parser.add_argument("--fail-at", choices=["none", "error", "cancelled"], default="none", help="simulated outcome once print-duration elapses (default: none - completes successfully)")
    parser.add_argument("--reject-upload", action="store_true", help="always fail put_term, to test the app's upload-failure handling")
    parser.add_argument("--machine-name", default="Simulated Replicator+", help="name reported by handshake")
    args = parser.parse_args()

    if args.print_duration > 60:
        print(f"--print-duration {args.print_duration} exceeds the 60s cap - clamping to 60.")
        args.print_duration = 60

    tokens = _TokenState()
    pairing = _PairingState(delay_s=args.pairing_delay, reject=args.reject_pairing)
    print_state = PrintState(print_duration_s=args.print_duration, fail_at=args.fail_at)
    handshake_info = {
        "machine_name": args.machine_name,
        "machine_type": "Replicator+",
        "firmware_version": "2.6.2-simulated",
        "serial_number": "SIMULATOR-0001",
    }

    try:
        rpc_server = RPCServer((args.host, args.rpc_port), tokens, print_state, handshake_info, args.reject_upload)
        auth_server = AuthServer((args.host, args.http_port), pairing, tokens)
    except PermissionError:
        # The real failure mode found by testing: binding port 80 (the
        # default --http-port, matching the real printer) without root
        # raises this - a bare traceback here is useless noise, not an
        # actual explanation. run_simulator.sh checks for this ahead of
        # time and offers to re-run itself under sudo; this still needs
        # to fail clearly on its own for anyone running this script
        # directly instead.
        privileged_port = args.http_port if args.http_port < 1024 else args.rpc_port
        print(
            f"\nCouldn't bind port {privileged_port} - that needs root. Either "
            f"run this with sudo, or pass --http-port 8080 (or similar) if you "
            f"only need RPC-side testing, not real pairing through pair_printer.py "
            f"unmodified (see this script's own docstring).",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as e:
        print(f"\nCouldn't start listening: {e}", file=sys.stderr)
        sys.exit(1)

    rpc_thread = threading.Thread(target=rpc_server.serve_forever, daemon=True)
    auth_thread = threading.Thread(target=auth_server.serve_forever, daemon=True)

    # args.host is "0.0.0.0" (every interface) by default - correct to
    # bind, but not a usable address to actually type into
    # /admin/settings. Show the real LAN IP there instead when that's
    # what's bound; an explicit --host already is a specific, usable
    # address, so leave it alone.
    display_host = args.host
    if args.host in ("0.0.0.0", "::"):
        detected = _detect_lan_ip()
        display_host = detected or args.host

    print(f"Simulated printer listening:")
    print(f"  JSON-RPC: {args.host}:{args.rpc_port}")
    print(f"  /auth:    {args.host}:{args.http_port}")
    print(f"  print duration: {args.print_duration:.0f}s, fail-at: {args.fail_at}, pairing-delay: {args.pairing_delay:.0f}s" + (" (rejecting)" if args.reject_pairing else ""))
    print(f"Set {display_host}:{args.rpc_port} as the printer's address on /admin/settings, then run pair_printer.py. Ctrl-C to stop.")

    rpc_thread.start()
    auth_thread.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        rpc_server.shutdown()
        auth_server.shutdown()


if __name__ == "__main__":
    main()
