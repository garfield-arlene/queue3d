"""Minimal client for the MakerBot 5th-gen / Replicator+ network protocol.

This is a from-scratch, from-first-principles implementation based on
reverse-engineering write-ups and open-source clients (notably
tjhorner/node-makerbot-rpc and gryphius/makerbot-gen5-api). MakerBot has
never published this protocol, so treat this as "best understanding",
not a spec.

Wire summary:
  - The printer runs a JSON-RPC 2.0 server on TCP port 9999. Messages are
    just concatenated JSON objects with no length prefix or delimiter -
    you frame them by brace-counting.
  - A separate plaintext HTTP endpoint (port 80, path "/auth") handles the
    one-time pairing handshake that produces a long-lived access token
    (see pairing.py).
  - Once authenticated, print jobs are pushed over the *same* JSON-RPC
    socket: a "print" call announces the filename, then the file bytes are
    streamed in chunks via put_init / put_raw (+ raw bytes) / put_term.
"""

import json
import queue
import socket
import threading


class MakerBotError(Exception):
    """The printer returned a JSON-RPC error for a request."""


class MakerBotClient:
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
            # state/progress). Not needed for this smoke test - ignored.
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
            raise MakerBotError(f"{method} -> {msg['error']}")
        return msg.get("result")

    def send_raw(self, data):
        """Write raw (non-JSON-RPC) bytes directly to the socket. Used for the
        file-content chunks during put_raw."""
        self._sock.sendall(data)

    def handshake(self):
        """Handshake works even before authentication and returns basic
        machine info (name, type, firmware version, serial...)."""
        return self.request("handshake", {})
