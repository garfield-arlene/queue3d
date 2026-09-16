"""Client for the printer's network protocol - the real hardware
integration behind jobs.release(). Vendored from the validated
proof-of-concept in ../test-print/ (see that directory's README for the
full reverse-engineered protocol writeup - MakerBot never published this,
so treat it as "best understanding," not a spec) rather than imported:
test-print/ isn't an importable package (its directory name has a hyphen),
and its job - proving the protocol works at all against real hardware -
is done. This is now the one, real client the app uses.

Wire summary: TCP JSON-RPC 2.0 on port 9999 (messages are concatenated
JSON objects, framed by brace-counting - no length prefix); a separate
plaintext HTTP endpoint (port 80, "/auth") handles one-time pairing,
producing an access token that authenticates the JSON-RPC socket; print
jobs are pushed over that same socket - "print" announces the filename,
then the file streams in chunks via put_init / put_raw (raw bytes
immediately follow each put_raw's JSON) / put_term.

**A pairing token is good for exactly one authenticated session, full
stop - confirmed live against the real printer, three separate ways (see
README.md's Printer to-do section and project memory
makerbot-network-protocol for the investigation): a second simultaneous
connection with the same token is rejected while the first stays open; a
new connection after cleanly closing the first also fails; and it still
fails even after a deliberately graceful close that rules out our own
client sending a TCP reset the printer could be reacting to. This is not
a firmware bug - 2.6.2 build 734, what this printer runs, is the last
firmware MakerBot ever shipped for the Replicator+ line - and it's
probably a deliberate one-token-per-session design, not a defect.

That means connecting fresh per call (the original design here) only
ever works for the *first* call after any given pairing - every `release()`
after that would fail authentication. `_PersistentConnection` below is
the fix: one connection, authenticated once, held open and reused for
the app's entire lifetime, reconnecting (and needing a fresh dial-press
re-pairing) only if that connection actually drops - which matches
exactly what was confirmed to work without incident during the
investigation (one connection, kept open, used repeatedly)."""

import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from db import DATA_DIR

CLIENT_ID = "MakerWare"
CLIENT_SECRET = "queue3d"  # arbitrary app identifier, not a real secret - the
# printer's physical dial press during pairing is the actual security boundary.
CHUNK_SIZE = 50_000

AUTH_PATH = DATA_DIR / "printer_auth.json"


def printer_host() -> str:
    return os.environ.get("QUEUE3D_PRINTER_HOST", "192.168.1.250")


def printer_port() -> int:
    return int(os.environ.get("QUEUE3D_PRINTER_PORT", "9999"))


def load_access_token() -> str | None:
    if not AUTH_PATH.exists():
        return None
    return json.loads(AUTH_PATH.read_text()).get("access_token")


def save_access_token(token: str) -> None:
    AUTH_PATH.write_text(json.dumps({"access_token": token}, indent=2))


class PrinterError(Exception):
    """Anything that goes wrong talking to the printer - unreachable, not
    paired, a JSON-RPC error from the printer itself. release() catches
    this and surfaces it as a JobActionError, so a failed send never marks
    a job 'printing'."""


class _MakerBotError(Exception):
    """Internal: the printer returned a JSON-RPC error for one request.
    Always caught and re-raised as PrinterError before leaving this module."""


class _MakerBotClient:
    def __init__(self, host, port=9999, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._lock = threading.Lock()
        self._pending = {}
        self._next_id = 0
        self._reader_thread = None
        self._stop = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc_info):
        self.close()

    def connect(self):
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(None)  # the reader thread blocks on recv() instead
        self._stop = False
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def close(self):
        self._stop = True
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None

    # ---- framing: pull complete top-level JSON objects out of the stream ----

    def _read_loop(self):
        buf = b""
        while not self._stop:
            try:
                chunk = self._sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                msg, buf = self._extract_message(buf)
                if msg is None:
                    break
                self._dispatch(msg)

    @staticmethod
    def _extract_message(buf):
        """Return (first complete top-level JSON object bytes, remainder), or
        (None, buf) if buf doesn't yet contain one complete object."""
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

    def _dispatch(self, raw):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        msg_id = msg.get("id")
        if msg_id is None:
            # Unsolicited notification (e.g. system_notification with live
            # progress). Not consumed yet - see project memory
            # makerbot-slicing-pipeline / app-progress for the open question
            # on live status reporting.
            return
        with self._lock:
            q = self._pending.get(msg_id)
        if q is not None:
            q.put(msg)

    # ---- request/response ----

    def request(self, method, params=None, timeout=None):
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            q = queue.Queue(maxsize=1)
            self._pending[req_id] = q

        payload = {"id": req_id, "jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params

        try:
            self._sock.sendall(json.dumps(payload).encode("utf-8"))
            try:
                msg = q.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise TimeoutError(f"No response to '{method}' within {timeout or self.timeout}s")
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

        if "error" in msg:
            raise _MakerBotError(f"{method} -> {msg['error']}")
        return msg.get("result")

    def send_raw(self, data):
        """Write raw (non-JSON-RPC) bytes directly to the socket. Used for
        the file-content chunks during put_raw."""
        self._sock.sendall(data)

    def handshake(self):
        """Works even before authentication; returns basic machine info
        (name, type, firmware version, serial...)."""
        return self.request("handshake", {})


# ---- one-time pairing (see pair_printer.py) ----


def _auth_get(host, **params):
    qs = urllib.parse.urlencode(params)
    url = f"http://{host}/auth?{qs}"
    try:
        # host is the printer's own LAN address (QUEUE3D_PRINTER_HOST or
        # the default - see this module's docstring/README), never web
        # request input, so this isn't the SSRF-style risk bandit's
        # urlopen check generically flags.
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec B310
            return json.load(resp)
    except urllib.error.URLError as e:
        raise PrinterError(f"Couldn't reach {url}: {e}") from e


def pair(host, on_waiting=None, poll_interval=2, timeout=120) -> str:
    """Run the pairing flow, returning a permanent access_token. Requires
    someone at the printer to physically press its dial to accept."""
    resp = _auth_get(host, response_type="code", client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    answer_code = resp["answer_code"]

    if on_waiting:
        on_waiting()

    deadline = time.time() + timeout
    while True:
        resp = _auth_get(
            host,
            response_type="answer",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            answer_code=answer_code,
        )
        answer = resp.get("answer")
        if answer == "accepted":
            auth_code = resp["code"]
            break
        if answer == "rejected":
            raise PrinterError(
                "Pairing was rejected. Either the knob press was declined, or "
                "another pairing session is already active on the printer - "
                "check its screen."
            )
        if time.time() > deadline:
            raise PrinterError("Timed out waiting for the knob press on the printer.")
        time.sleep(poll_interval)

    resp = _auth_get(
        host,
        response_type="token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        auth_code=auth_code,
        context="jsonrpc",
    )
    if resp.get("status") != "success":
        raise PrinterError(f"Failed to obtain access token: {resp}")
    return resp["access_token"]


# ---- the persistent connection (see this module's docstring for why) ----


class _PersistentConnection:
    """Holds one authenticated _MakerBotClient for the app's entire
    lifetime, instead of connecting fresh per call. `self._lock` (an
    RLock) is held for the full duration of one logical operation
    (connect-if-needed, then do the real work) - that's coarser than it
    needs to be for true request/response pipelining, but this client
    only ever does one real thing at a time in practice (release a job,
    eventually poll status), and it guarantees two things that matter
    more here: two operations can never interleave their raw bytes on the
    wire (critical during a file upload - a put_raw's announcement and
    its raw bytes have to land back-to-back with nothing else between
    them), and a health-check from one caller can't race a real upload
    from another.
    """

    def __init__(self):
        self._client: _MakerBotClient | None = None
        self._lock = threading.RLock()

    def _connected_client(self) -> "_MakerBotClient":
        """Returns a live, authenticated client - the existing one if a
        cheap health check still passes, otherwise a fresh connection.
        Must be called with self._lock held."""
        if self._client is not None:
            try:
                self._client.request("handshake", {}, timeout=5)
                return self._client
            except Exception:
                # Dead (printer rebooted, network dropped, etc.) - drop it
                # and fall through to reconnect rather than fail here.
                self._client.close()
                self._client = None

        host = printer_host()
        port = printer_port()
        token = load_access_token()
        if not token:
            raise PrinterError("Printer isn't paired yet - run pair_printer.py once.")

        client = _MakerBotClient(host, port)
        try:
            client.connect()
            client.handshake()
            client.request("authenticate", {"access_token": token})
        except (_MakerBotError, OSError, TimeoutError) as e:
            client.close()
            # A token is only ever good for one session (see this
            # module's docstring) - if this is a *second* connection
            # attempt with the same saved token (the previous persistent
            # connection died and we're reconnecting), authentication is
            # expected to fail here every time until someone re-pairs.
            # Say so plainly rather than leaving "authentication failed"
            # unexplained.
            raise PrinterError(
                f"Couldn't establish a connection to the printer - if one was "
                f"already established and this is a reconnect, the printer's "
                f"pairing tokens are only good for one session, so this needs "
                f"re-pairing (run pair_printer.py again): {e}"
            )
        self._client = client
        return client

    def send_print_job(self, makerbot_path: Path) -> None:
        with self._lock:
            client = self._connected_client()
            try:
                _upload_and_print(client, makerbot_path)
            except PrinterError:
                # An error here could mean the connection itself died
                # mid-upload (not just the printer rejecting the request)
                # - drop it so the *next* call reconnects fresh instead of
                # repeatedly retrying against a socket already known bad.
                self._client.close()
                self._client = None
                raise

    def close(self) -> None:
        """Cleanly closes the connection, if one is open - called on app
        shutdown (see main.py) so a restart doesn't leave the old
        process's socket lingering. Not required for correctness (the OS
        reclaims it on process exit either way), just tidy; the next call
        to send_print_job() reconnects lazily regardless."""
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None


_connection = _PersistentConnection()


def close_connection() -> None:
    _connection.close()


def send_print_job(makerbot_path: Path) -> None:
    """Upload+start a print job over the app's one persistent connection
    to the printer (connecting/authenticating first if it isn't already
    connected). Raises PrinterError on any failure - callers must not
    consider the job 'printing' unless this returns without raising,
    since a failure partway through the upload leaves no guarantee the
    printer actually started."""
    _connection.send_print_job(makerbot_path)


def _upload_and_print(client: "_MakerBotClient", makerbot_path: Path) -> None:
    filename = makerbot_path.name
    data = makerbot_path.read_bytes()
    total_len = len(data)
    crc = zlib.crc32(data) & 0xFFFFFFFF

    try:
        client.request("print", {"filepath": filename, "transfer_wait": True})

        try:
            # Emulates confirming "build plate is cleared" on the printer's UI.
            client.request("process_method", {"method": "build_plate_cleared"})
        except _MakerBotError:
            pass  # not every firmware state acknowledges this - continue regardless

        client.request(
            "put_init",
            {
                "block_size": CHUNK_SIZE,
                "file_id": "1",
                "file_path": f"/current_thing/{filename}",
                "length": total_len,
            },
        )

        for offset in range(0, total_len, CHUNK_SIZE):
            chunk = data[offset : offset + CHUNK_SIZE]
            client.request("put_raw", {"file_id": "1", "length": len(chunk)})
            client.send_raw(chunk)

        client.request("put_term", {"crc": crc, "file_id": "1", "length": total_len})
    except _MakerBotError as e:
        raise PrinterError(f"Printer rejected the request: {e}")
