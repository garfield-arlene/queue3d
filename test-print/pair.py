#!/usr/bin/env python3
"""Step 2: one-time pairing. Run this once; it saves auth.json.

You'll need to be at the printer - its screen will prompt you to press the
dial to accept this app.
"""

import sys

from config import load_config, save_access_token
from pairing import pair


def main():
    cfg = load_config()
    host = cfg["printer_host"]

    def on_waiting():
        print("Check the printer's screen and press the dial to accept the pairing request...")

    try:
        token = pair(host, on_waiting=on_waiting)
    except Exception as e:
        print(f"Pairing failed: {e}", file=sys.stderr)
        sys.exit(1)

    save_access_token(token)
    print("Paired. Access token saved to auth.json - you won't need to do this again.")


if __name__ == "__main__":
    main()
