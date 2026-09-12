#!/usr/bin/env python3
"""One-time pairing with the printer. Run this once on the deployed
server - you'll need to be physically at the printer, since its screen
will prompt for a dial press to accept the pairing request. Saves the
resulting access token to data/printer_auth.json for release() to use."""

import sys

from printer import pair, printer_host, printer_port, save_access_token


def main():
    host = printer_host()
    port = printer_port()
    print(f"Pairing with printer at {host}:{port} ...")

    def on_waiting():
        print("Check the printer's screen and press the dial to accept the pairing request...")

    try:
        token = pair(host, on_waiting=on_waiting)
    except Exception as e:
        print(f"Pairing failed: {e}", file=sys.stderr)
        sys.exit(1)

    save_access_token(token)
    print("Paired. The app can now release jobs to the printer - you won't need to do this again.")


if __name__ == "__main__":
    main()
