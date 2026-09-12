#!/usr/bin/env python3
"""Step 3: upload a .makerbot file and start printing it.

Usage: python3 send_print.py path/to/test.makerbot

WARNING: this will actually start the printer heating up and printing.
Make sure the build plate is clear and you're present to supervise.
"""

import os
import sys
import zlib

from config import load_config, load_access_token
from makerbot_client import MakerBotClient, MakerBotError

CHUNK_SIZE = 50_000


def send_print(client, file_path):
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        data = f.read()
    total_len = len(data)
    crc = zlib.crc32(data) & 0xFFFFFFFF

    print(f"Announcing print: {filename} ({total_len} bytes)")
    client.request("print", {"filepath": filename, "transfer_wait": True})

    try:
        # Emulates confirming "build plate is cleared" on the printer's UI.
        client.request("process_method", {"method": "build_plate_cleared"})
    except MakerBotError as e:
        print(f"  (build_plate_cleared not acknowledged, continuing anyway: {e})")

    client.request(
        "put_init",
        {
            "block_size": CHUNK_SIZE,
            "file_id": "1",
            "file_path": f"/current_thing/{filename}",
            "length": total_len,
        },
    )

    sent = 0
    for offset in range(0, total_len, CHUNK_SIZE):
        chunk = data[offset : offset + CHUNK_SIZE]
        client.request("put_raw", {"file_id": "1", "length": len(chunk)})
        client.send_raw(chunk)
        sent += len(chunk)
        print(f"  sent {sent}/{total_len} bytes", end="\r")
    print()

    client.request("put_term", {"crc": crc, "file_id": "1", "length": total_len})
    print("Transfer complete. Printer should now be starting the print.")


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} path/to/file.makerbot", file=sys.stderr)
        sys.exit(1)
    file_path = sys.argv[1]
    if not os.path.isfile(file_path):
        print(f"No such file: {file_path}", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    host = cfg["printer_host"]
    port = cfg.get("jsonrpc_port", 9999)

    token = load_access_token()
    if not token:
        print("No saved access token. Run pair.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to {host}:{port} ...")
    with MakerBotClient(host, port) as client:
        client.handshake()
        client.request("authenticate", {"access_token": token})
        print("Authenticated.")
        try:
            send_print(client, file_path)
        except MakerBotError as e:
            print(f"Printer rejected the request: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
