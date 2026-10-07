"""
prefs.py — the user's on/off choices that must outlive the web UI process
(/etc/unitree-fwtools/prefs.json). A missing key means "never chosen", so the
default applies; once the user flips a switch, that choice is kept.
"""
import json
import os

PREFS_DIR = "/etc/unitree-fwtools"
PREFS_FILE = os.path.join(PREFS_DIR, "prefs.json")


def load():
    try:
        with open(PREFS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def set(key, value):
    p = load()
    p[key] = value
    os.makedirs(PREFS_DIR, exist_ok=True)
    tmp = PREFS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2)
    os.replace(tmp, PREFS_FILE)
