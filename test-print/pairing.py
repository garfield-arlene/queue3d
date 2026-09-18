"""One-time pairing flow over the printer's plaintext HTTP endpoint.

This produces a long-lived access_token that's then used to authenticate
the JSON-RPC socket connection (see makerbot_client.py / send_print.py).
The printer requires a physical accept (pressing the dial) the first time
an unknown client pairs, exactly like the Roku/Apple TV pairing pattern.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "MakerWare"
CLIENT_SECRET = "queue3d-dev"  # arbitrary app identifier, not a real secret -
# the printer's physical dial press is the actual security boundary here.


def _auth_get(host, **params):
    qs = urllib.parse.urlencode(params)
    url = f"http://{host}/auth?{qs}"
    try:
        # host is always the printer's own LAN address, supplied by
        # whoever runs this one-time CLI pairing step (pair_printer.py) -
        # never web request input - so this isn't the SSRF-style risk
        # bandit's urlopen check generically flags.
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec B310
            return json.load(resp)
    except urllib.error.URLError as e:
        raise ConnectionError(f"Couldn't reach {url}: {e}") from e


def pair(host, on_waiting=None, poll_interval=2, timeout=120):
    """Run the pairing flow, returning a permanent access_token.

    Args:
        host: printer IP/hostname.
        on_waiting: optional callback invoked once we're waiting for the
            physical knob press, so the caller can print a prompt.
        poll_interval: seconds between polls while waiting for the press.
        timeout: give up after this many seconds of no response on the printer.
    """
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
            raise RuntimeError(
                "Pairing was rejected. Either the knob press was declined, or "
                "another pairing session is already active on the printer - "
                "check its screen."
            )
        if time.time() > deadline:
            raise TimeoutError("Timed out waiting for the knob press on the printer.")
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
        raise RuntimeError(f"Failed to obtain access token: {resp}")
    return resp["access_token"]
