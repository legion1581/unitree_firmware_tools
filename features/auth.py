"""
auth.py — password login for the web UI.

One password (default "robolegion"), stored as a salted PBKDF2 hash in
/etc/unitree-fwtools/auth.json so it survives reboots and OTA updates of
/unitree. A login opens a session that lasts SESSION_TTL seconds; sessions live
in memory, so restarting the web UI logs everybody out.

Forgot it? `./start.sh --reset-password` puts the default back.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

AUTH_DIR = "/etc/unitree-fwtools"
AUTH_FILE = os.path.join(AUTH_DIR, "auth.json")
DEFAULT_PASSWORD = "robolegion"
SESSION_TTL = 3600
_ITER = 100_000

_sessions = {}          # sid -> expiry (epoch seconds)
_lock = threading.Lock()


def _hash(password, salt, iterations=_ITER):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()


def _load():
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_default():
    return _load() is None


def check_password(password):
    rec = _load()
    if rec is None:
        return hmac.compare_digest(password or "", DEFAULT_PASSWORD)
    got = _hash(password or "", rec["salt"], rec.get("iterations", _ITER))
    return hmac.compare_digest(got, rec["hash"])


def set_password(new, keep=None):
    """Store a new password; ends every other session (`keep` = the caller's)."""
    if not new or len(new) < 4:
        raise ValueError("password must be at least 4 characters")
    salt = secrets.token_hex(16)
    os.makedirs(AUTH_DIR, exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"salt": salt, "hash": _hash(new, salt), "iterations": _ITER}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)
    with _lock:
        for k in [k for k in _sessions if k != keep]:
            del _sessions[k]


def reset_password():
    try:
        os.remove(AUTH_FILE)
    except FileNotFoundError:
        pass
    with _lock:
        _sessions.clear()


def new_session():
    sid = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for k in [k for k, exp in _sessions.items() if exp < now]:
            del _sessions[k]
        _sessions[sid] = now + SESSION_TTL
    return sid


def session_valid(sid):
    if not sid:
        return False
    with _lock:
        exp = _sessions.get(sid)
        if exp is None:
            return False
        if exp < time.time():
            del _sessions[sid]
            return False
        return True


def session_expiry(sid):
    with _lock:
        return _sessions.get(sid)


def end_session(sid):
    with _lock:
        _sessions.pop(sid, None)
