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
import struct
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
        # Camera-frame handling (see capture_one_frame) - deliberately
        # state on this same reader thread rather than a second thread
        # racing it for the socket. _camera_mode_until is a monotonic
        # deadline (0 = not in camera mode); while now < that deadline,
        # _read_loop peeks at the next byte before trying to parse a JSON
        # message at all - a raw frame's binary header can't start with
        # '{', so this tells raw frame data apart from a real JSON message
        # (a "camera_frame" notification, or the end_camera_stream reply)
        # without having to assume which one the printer sends next; it's
        # a *sliding* window, not a fixed one (see _consume_camera_frame) -
        # the printer keeps pushing frames for an unpredictable stretch
        # after end_camera_stream, and it only closes once frames actually
        # stop arriving for _camera_grace_period seconds.
        self._camera_mode_until = 0.0
        self._camera_grace_period = 3.0
        self._camera_result: queue.Queue | None = None

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
            if not buf:
                try:
                    chunk = self._sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                continue

            # While a capture is active (or was, recently - see
            # _camera_mode_until's docstring), don't assume every frame is
            # preceded by its own "camera_frame" JSON notification: turns
            # out that's only sometimes true, and guessing wrong crashed
            # this thread by brace-counting straight into raw JPEG bytes
            # (some of which happen to equal '{'/'}'). A raw frame header
            # can never start with '{' (that'd make frame_size itself
            # nonsense, in the billions), so peeking at the next byte is a
            # cheap, structural way to tell the two apart regardless of
            # which framing the printer is actually using right now.
            if time.monotonic() < self._camera_mode_until and buf[:1] != b"{":
                buf = self._consume_camera_frame(buf)
                continue

            msg, buf = self._extract_message(buf)
            if msg is None:
                try:
                    chunk = self._sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                continue

            parsed = self._try_parse(msg)
            if (
                parsed is not None
                and parsed.get("method") == "camera_frame"
                and time.monotonic() < self._camera_mode_until
            ):
                # The 16-byte frame header + JPEG bytes immediately
                # follow this notification - not the start of another
                # JSON message. Read them here, in this same thread,
                # rather than have some other thread race this one for
                # the socket (see capture_one_frame's docstring for
                # why that was the original, buggier approach).
                buf = self._consume_camera_frame(buf)
                continue
            self._dispatch(parsed)

    def _consume_camera_frame(self, buf: bytes) -> bytes:
        """Reads this camera_frame notification's 16-byte header + JPEG
        payload directly off the socket (blocking further recv()s as
        needed, same as the outer loop would) and, if a capture is
        currently waiting for one (self._camera_result is set), delivers
        it there - the *first* frame after a request_camera_stream, not
        every one of the continuous stream that follows. Returns
        whatever's left in `buf` afterward. Must only be called from
        _read_loop, on its own thread.

        Slides _camera_mode_until forward on every frame actually consumed
        here (not just the first) - see that attribute's docstring in
        __init__ for why a fixed deadline isn't safe.

        A bounded per-recv timeout (_FRAME_RECV_TIMEOUT) is a deliberate
        safety net, not a normal-path expectation: this thread's socket
        otherwise has no timeout at all (see connect()), so any bug in
        this method - a miscomputed frame_size chief among them - would
        otherwise block forever with nothing to surface it. A timeout here
        propagates out of _read_loop and ends the thread; _connected_client's
        health check then notices the dead connection on the next call and
        reconnects (needing a fresh pairing) rather than hanging forever."""
        _FRAME_RECV_TIMEOUT = 30
        orig_timeout = self._sock.gettimeout()
        self._sock.settimeout(_FRAME_RECV_TIMEOUT)
        try:
            while len(buf) < 16:
                chunk = self._sock.recv(65536)
                if not chunk:
                    return b""
                buf += chunk
            header, buf = buf[:16], buf[16:]
            frame_size, _width, _height, _fourth = struct.unpack(">IIII", header)

            while len(buf) < frame_size:
                chunk = self._sock.recv(65536)
                if not chunk:
                    return b""
                buf += chunk
            jpeg_data, buf = buf[:frame_size], buf[frame_size:]
        finally:
            self._sock.settimeout(orig_timeout)

        self._camera_mode_until = time.monotonic() + self._camera_grace_period
        if self._camera_result is not None:
            self._camera_result.put(jpeg_data)
            self._camera_result = None  # only the first frame goes to a waiting caller
        return buf

    @staticmethod
    def _try_parse(raw: bytes):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

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

    def _dispatch(self, msg):
        if msg is None:  # failed to parse - nothing usable to do with it
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

    def get_system_information(self):
        """Requires authentication. Referenced in the original camera
        protocol investigation (project memory makerbot-network-protocol,
        test-print/camera_probe3.py) as returning a `current_process` field
        - never actually decoded there, just confirmed to exist. Exposed
        here (see admin_printer_info.html) so the app can find out what it
        actually contains - an active print's progress, hopefully -
        without needing a separate throwaway script each time."""
        return self.request("get_system_information", {})

    def capture_one_frame(self, timeout=15, grace_period=3.0) -> bytes:
        """Returns one JPEG frame's raw bytes from the printer's camera.

        There's no true one-shot capture method on this firmware -
        confirmed live (see project memory makerbot-network-protocol):
        request_camera_frame/get_available_cameras/get_camera_frame are
        all "method not found". What actually works is
        request_camera_stream, which makes the printer immediately start
        pushing, repeatedly, on this same socket: a `camera_frame`
        JSON-RPC *notification* (no "id" - a push, not a reply to
        anything we asked), immediately followed by a 16-byte big-endian
        binary header (frame_size, width, height, and an unidentified 4th
        field), immediately followed by exactly frame_size bytes of a
        real JPEG image - a continuous MJPEG-style stream, not one frame
        per call.

        An earlier version of this method tried to pause the background
        reader thread and take over the raw socket from the calling
        thread instead - genuinely broken, caught by testing before this
        ever shipped: a thread blocked in recv() doesn't notice a "please
        stop" flag until data actually arrives, so both threads ended up
        racing to read the same socket. The fix is _read_loop/
        _consume_camera_frame handling the binary payload inline, on the
        *same* thread that's already reading the socket - this method
        just requests the stream, waits on a queue for that thread to
        hand back the first frame, then requests the stream stop.

        `grace_period` is how long, after each frame actually seen,
        _read_loop keeps treating a *further* camera_frame notification
        specially (consuming its raw payload, just not delivering it
        anywhere) - a sliding window, not a fixed one (see
        _consume_camera_frame): the printer keeps pushing frames for an
        unpredictable stretch after end_camera_stream, and it only
        re-arms normal JSON parsing once frames actually stop arriving
        for this long.
        """
        result: queue.Queue = queue.Queue(maxsize=1)
        self._camera_result = result
        self._camera_grace_period = grace_period
        self._camera_mode_until = time.monotonic() + timeout

        try:
            self.request("request_camera_stream", {})
            try:
                jpeg_data = result.get(timeout=timeout)
            except queue.Empty:
                raise _MakerBotError("Timed out waiting for a camera frame")
            finally:
                self._camera_result = None  # in case our own wait timed out, not the read loop
            self.request("end_camera_stream", {})
            return jpeg_data
        finally:
            self._camera_mode_until = time.monotonic() + grace_period


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
        # Reflects the outcome of the most recent *real* connection
        # attempt, for connection_status() below to report - deliberately
        # never updated by a speculative check, only by an actual
        # send_print_job()/capture_photo() call, since probing "is the
        # saved token still good" on its own would risk spending a fresh
        # token's one-time session just to answer a status question
        # (found out the hard way: see project memory
        # makerbot-network-protocol). One of "unknown" (nothing's been
        # attempted this run yet - the saved token, if any, might be
        # perfectly fine), "needs_pairing" (the connection is now dead and
        # its token spent - either the last attempt's failure looked like
        # an authentication problem directly, or a *previously* healthy,
        # already-authenticated connection just failed for some other
        # reason mid-operation and got closed; either way the same token
        # won't authenticate a reconnect), or "unreachable" (never even
        # got as far as authenticating - a network problem instead,
        # printer off/unplugged, wrong host - re-pairing wouldn't help).
        self._last_error = "unknown"

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
            self._last_error = "needs_pairing"
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
            # unexplained. Distinguish that case (an actual reply from the
            # printer rejecting authentication) from a network-level
            # failure (can't even reach it) for connection_status().
            self._last_error = "needs_pairing" if isinstance(e, _MakerBotError) else "unreachable"
            raise PrinterError(
                f"Couldn't establish a connection to the printer - if one was "
                f"already established and this is a reconnect, the printer's "
                f"pairing tokens are only good for one session, so this needs "
                f"re-pairing (run pair_printer.py again): {e}"
            )
        self._client = client
        self._last_error = "unknown"  # healthy now; stale on the *next* failure, not before
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
                # _connected_client() already got this client past
                # authentication once, successfully - the token it used is
                # now spent (see this module's docstring), so reconnecting
                # will need a fresh one regardless of what actually went
                # wrong just now. Recording that here, not only on an
                # auth failure during _connected_client() itself, is what
                # this status is actually for: telling the dashboard "the
                # next real attempt will need a re-pair" as soon as that's
                # true, not only once something has already tried and
                # failed a *second* time to discover it.
                self._client.close()
                self._client = None
                self._last_error = "needs_pairing"
                raise

    def capture_photo(self) -> bytes:
        with self._lock:
            client = self._connected_client()
            try:
                return client.capture_one_frame()
            except (_MakerBotError, OSError, TimeoutError) as e:
                # Same reasoning as send_print_job's except clause above -
                # this client was already successfully authenticated by
                # _connected_client(), so its (now spent) token won't work
                # for the reconnect this failure forces either.
                self._client.close()
                self._client = None
                self._last_error = "needs_pairing"
                raise PrinterError(f"Couldn't capture a photo from the printer's camera: {e}")

    def system_information(self) -> dict:
        with self._lock:
            client = self._connected_client()
            try:
                return client.get_system_information()
            except (_MakerBotError, OSError, TimeoutError) as e:
                # Same reasoning as capture_photo's except clause above.
                self._client.close()
                self._client = None
                self._last_error = "needs_pairing"
                raise PrinterError(f"Couldn't read the printer's status: {e}")

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

    def status(self) -> str:
        """See connection_status() below - this just adds the lock."""
        with self._lock:
            if self._client is not None:
                return "connected"
            if not load_access_token():
                return "needs_pairing"
            return self._last_error


_connection = _PersistentConnection()


def close_connection() -> None:
    _connection.close()


def connection_status() -> str:
    """One of "connected" (currently holding a live, authenticated
    connection), "needs_pairing" (never paired, or a connection just died
    - whether during authentication itself or partway through a later
    release/capture - leaving its now-spent token unable to reconnect),
    "unreachable" (the last real attempt never even got as far as
    authenticating - a network failure instead, so pairing again won't
    help), or "unknown" (paired at some point, nothing's actually been
    attempted against the printer yet this run, so whether that token
    still works genuinely isn't known - see _PersistentConnection's
    docstring for why this is never checked speculatively). For display
    only (see admin_dashboard.html's printer status banner) - never
    itself touches the network."""
    return _connection.status()


# ---- background pairing, triggered from the admin dashboard ----
# pair() blocks for up to two minutes waiting on a real dial-press, so
# running it inline in a request handler would tie up that request the
# whole time. A single daemon thread plus this small bit of shared state
# lets the dashboard kick it off, then just poll (via htmx, same
# self-terminating pattern as _jobs_table.html's slicing-progress
# polling) until it's done.
_pairing_lock = threading.Lock()
_pairing_state: dict = {"in_progress": False, "error": None, "just_succeeded": False}


def start_pairing() -> bool:
    """Kicks off pairing in the background if one isn't already running.
    Returns False (a no-op) if one is - the caller should just show the
    existing in-progress state rather than start a second, competing
    pairing request against the printer."""
    with _pairing_lock:
        if _pairing_state["in_progress"]:
            return False
        _pairing_state["in_progress"] = True
        _pairing_state["error"] = None
        _pairing_state["just_succeeded"] = False

    def run():
        try:
            token = pair(printer_host())
            save_access_token(token)
            with _connection._lock:
                _connection._last_error = "unknown"  # untested-but-fresh, not a known failure
            with _pairing_lock:
                # A genuine, real success - distinct from connection_status()
                # still reporting "unknown" right afterward (see this
                # module's docstring for why a fresh token is never
                # speculatively verified): without this, a successful
                # pairing and "nothing's been tried yet" render as the
                # exact same generic message, reading as if the pairing
                # someone just did - dial press and all - hadn't worked.
                _pairing_state["just_succeeded"] = True
        except Exception as e:
            with _pairing_lock:
                _pairing_state["error"] = str(e)
        finally:
            with _pairing_lock:
                _pairing_state["in_progress"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def pairing_status() -> dict:
    """{"in_progress": bool, "error": str | None, "just_succeeded": bool} -
    error and just_succeeded are only ever set from the *previous*
    completed attempt (both cleared the moment a new one starts), so a
    failed attempt's message - or a successful one's - stays visible on
    the dashboard until either it changes or someone tries again."""
    with _pairing_lock:
        return dict(_pairing_state)


def send_print_job(makerbot_path: Path) -> None:
    """Upload+start a print job over the app's one persistent connection
    to the printer (connecting/authenticating first if it isn't already
    connected). Raises PrinterError on any failure - callers must not
    consider the job 'printing' unless this returns without raising,
    since a failure partway through the upload leaves no guarantee the
    printer actually started."""
    _connection.send_print_job(makerbot_path)


def capture_photo() -> bytes:
    """Returns one JPEG frame from the printer's camera, over the same
    persistent connection as send_print_job() (connecting/authenticating
    first if needed). Raises PrinterError on any failure - callers should
    treat a failed capture as "no photo this time," not something that
    should block recording a job's actual outcome (see
    jobs.mark_finished)."""
    return _connection.capture_photo()


def system_information() -> dict:
    """Raw `get_system_information` reply, over the same persistent
    connection as everything else here (connecting/authenticating first
    if needed). Raises PrinterError on any failure. Currently just for
    finding out what's actually in this, particularly `current_process`
    while a job is printing (see routers/admin.py's printer_info,
    admin_printer_info.html) - not documented by MakerBot anywhere, only
    ever confirmed to exist, not decoded."""
    return _connection.system_information()


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
