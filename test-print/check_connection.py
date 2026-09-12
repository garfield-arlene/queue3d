#!/usr/bin/env python3
"""Step 1: pure network smoke test - no pairing/auth needed.

Connects to the printer's JSON-RPC port and performs a "handshake", which
the printer answers even to unauthenticated clients. If this works, the
network path and message framing are both confirmed good before we touch
pairing or file transfer at all.
"""

import sys

from config import load_config
from makerbot_client import MakerBotClient


def main():
    cfg = load_config()
    host = cfg["printer_host"]
    port = cfg.get("jsonrpc_port", 9999)

    print(f"Connecting to {host}:{port} ...")
    try:
        with MakerBotClient(host, port) as client:
            result = client.handshake()
    except (OSError, TimeoutError) as e:
        print(f"FAILED to connect/handshake: {e}", file=sys.stderr)
        sys.exit(1)

    print("Handshake OK. Printer says:")
    for key in ("machine_name", "machine_type", "bot_type", "firmware_version", "iserial"):
        if key in result:
            print(f"  {key}: {result[key]}")


if __name__ == "__main__":
    main()
