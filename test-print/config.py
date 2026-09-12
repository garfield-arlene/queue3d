"""Tiny config/auth-token loader shared by the test scripts."""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, "config.json")
AUTH_PATH = os.path.join(_DIR, "auth.json")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_access_token():
    if not os.path.exists(AUTH_PATH):
        return None
    with open(AUTH_PATH) as f:
        return json.load(f).get("access_token")


def save_access_token(token):
    with open(AUTH_PATH, "w") as f:
        json.dump({"access_token": token}, f, indent=2)
