"""WS3 — end-user accounts + login sessions for the end-user UI.

Email + password accounts, passwords hashed+salted with stdlib ``hashlib.scrypt``
(no external dependency). Persisted to a 0600 JSON file outside the repo (the same
posture as ``keystore``). Login establishes a server-side session keyed by an opaque
token handed back as an httpOnly cookie; the token store is in-process (a restart logs
everyone out — acceptable for the MVP).

The logged-in ``user_id`` is the per-customer isolation key: it derives the Pipedream
``external_user_id`` (``trace-user-<id>``) and the per-customer vault namespace, and it
owns the user's pipeline sessions. Password material is never returned to callers — the
public shape carries only ``user_id``, ``email``, ``created_at``.

This module is for the END-USER surface only; the founder's Test UI never touches it.
"""

import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time

# Overridable for tests (mirrors keystore's file-fallback pattern).
_DIR = os.path.expanduser("~/.trace")
_PATH = os.path.join(_DIR, "users.json")

_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD = 8

# scrypt cost parameters (RFC 7914). n must be a power of two.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

# In-process login-session store: token -> {user_id, expires}.
_SESSIONS = {}
_SESSION_TTL = 7 * 24 * 3600
COOKIE_NAME = "trace_auth"


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# --- persistence (0600 JSON file) -----------------------------------------
def _load():
    try:
        with open(_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d):
    os.makedirs(_DIR, exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _PATH)
    try:
        os.chmod(_PATH, stat.S_IRUSR | stat.S_IWUSR)        # 0600
    except OSError:
        pass


# --- password hashing (scrypt, salted) ------------------------------------
def _hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"], salt=salt.hex(), hash=dk.hex())


def _verify_password(password, stored):
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _public(u):
    """The safe-to-return view — never the password hash."""
    return {"user_id": u["user_id"], "email": u["email"], "created_at": u["created_at"]}


# --- accounts -------------------------------------------------------------
def create_user(email, password):
    """Provision (or self-register) one account. Raises ValueError on a bad email, a
    too-short password, or a duplicate. Returns the public user."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("enter a valid email address")
    if len(password or "") < _MIN_PASSWORD:
        raise ValueError(f"password must be at least {_MIN_PASSWORD} characters")
    with _LOCK:
        d = _load()
        if any(u["email"] == email for u in d.values()):
            raise ValueError("an account with this email already exists")
        uid = "usr_" + secrets.token_hex(8)
        d[uid] = {"user_id": uid, "email": email,
                  "password_hash": _hash_password(password), "created_at": _now()}
        _save(d)
        return _public(d[uid])


def verify(email, password):
    """Return the public user for valid credentials, else None."""
    email = (email or "").strip().lower()
    for u in _load().values():
        if u["email"] == email:
            return _public(u) if _verify_password(password, u["password_hash"]) else None
    return None


def get(user_id):
    """The public user for an id, or None."""
    u = _load().get(user_id or "")
    return _public(u) if u else None


def external_user_id(user_id):
    """The stable Pipedream/vault isolation key for a user (per-customer namespace)."""
    return f"trace-user-{user_id}"


# --- login sessions (opaque token <-> user) -------------------------------
def start_session(user_id):
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {"user_id": user_id, "expires": time.time() + _SESSION_TTL}
    return token


def session_user(token):
    """The user_id for a live token, or None (expired tokens are reaped)."""
    s = _SESSIONS.get(token or "")
    if not s:
        return None
    if s["expires"] < time.time():
        _SESSIONS.pop(token, None)
        return None
    return s["user_id"]


def end_session(token):
    _SESSIONS.pop(token or "", None)
