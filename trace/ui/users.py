"""WS3 — end-user accounts + login sessions for the end-user UI.

Dual-run: Supabase Auth (when configured) or local file-based auth (dev fallback).

When ``SUPABASE_URL`` is set, accounts and sessions are managed by Supabase Auth:
- ``create_user`` → ``admin.auth.admin.create_user``
- ``verify`` → ``client.auth.sign_in_with_password`` (returns JWT)
- ``session_user`` → ``client.auth.get_user(jwt)`` (validates the JWT)
- ``start_session`` → returns the JWT from the sign-in response
- ``end_session`` → ``admin.auth.admin.sign_out(jwt)``

When unset (local dev), the original scrypt + file-based system is used unchanged.

The public user shape is always ``{user_id, email, created_at}``. The ``user_id`` is
the Supabase UUID (string) in cloud mode, or ``usr_<hex>`` locally.
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

from . import supabase_client as supa

# Overridable for tests (mirrors keystore's file-fallback pattern).
_DIR = os.path.expanduser("~/.trace")
_PATH = os.path.join(_DIR, "users.json")

_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD = 8

# scrypt cost parameters (RFC 7914). n must be a power of two.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

# In-process login-session store (file-mode fallback only).
_SESSIONS = {}
_SESSION_TTL = 7 * 24 * 3600
COOKIE_NAME = "trace_auth"

SESSION_SECRET = (os.environ.get("SESSION_SECRET") or "").strip() or None


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(v):
    """Render a DB timestamp as the same ISO string shape callers expect."""
    return v.isoformat(timespec="seconds") if hasattr(v, "isoformat") else str(v)


# --- file-mode persistence (0600 JSON file, local dev only) ----------------
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
        os.chmod(_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# --- file-mode password hashing (scrypt, salted) --------------------------
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
    """Provision one account. Returns ``(public_user, access_token)``.

    The access_token is the Supabase JWT (cloud) or an opaque token (local).
    Callers use it to set the auth cookie immediately after signup.
    Raises ValueError on bad input or duplicate email.
    """
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("enter a valid email address")
    if len(password or "") < _MIN_PASSWORD:
        raise ValueError(f"password must be at least {_MIN_PASSWORD} characters")

    if supa.is_configured():
        admin = supa.get_admin()
        # Check for existing user first (Supabase create_user gives a generic error)
        try:
            resp = admin.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True,
            })
        except Exception as e:
            msg = str(e).lower()
            if "already" in msg or "duplicate" in msg or "exists" in msg:
                raise ValueError("an account with this email already exists")
            raise ValueError(f"signup failed: {e}")
        user = resp.user
        # Sign in immediately to get a JWT for the cookie
        client = supa.get_client()
        login = client.auth.sign_in_with_password({"email": email, "password": password})
        token = login.session.access_token
        pub = {"user_id": str(user.id), "email": user.email,
               "created_at": _iso(user.created_at) if user.created_at else _now()}
        return pub, token

    # File-mode fallback
    uid = "usr_" + secrets.token_hex(8)
    pw = _hash_password(password)
    with _LOCK:
        d = _load()
        if any(u["email"] == email for u in d.values()):
            raise ValueError("an account with this email already exists")
        d[uid] = {"user_id": uid, "email": email,
                  "password_hash": pw, "created_at": _now()}
        _save(d)
        token = secrets.token_urlsafe(32)
        _SESSIONS[token] = {"user_id": uid, "expires": time.time() + _SESSION_TTL}
        return _public(d[uid]), token


def verify(email, password):
    """Return ``(public_user, access_token)`` for valid credentials, or ``(None, None)``.

    In Supabase mode the access_token is the JWT; locally it's an opaque token.
    """
    email = (email or "").strip().lower()
    if supa.is_configured():
        client = supa.get_client()
        try:
            login = client.auth.sign_in_with_password({"email": email, "password": password})
        except Exception:
            return None, None
        user = login.user
        token = login.session.access_token
        pub = {"user_id": str(user.id), "email": user.email,
               "created_at": _iso(user.created_at) if user.created_at else _now()}
        return pub, token

    # File-mode fallback
    for u in _load().values():
        if u["email"] == email:
            if _verify_password(password, u["password_hash"]):
                token = secrets.token_urlsafe(32)
                _SESSIONS[token] = {"user_id": u["user_id"],
                                    "expires": time.time() + _SESSION_TTL}
                return _public(u), token
            return None, None
    return None, None


def get(user_id):
    """The public user for an id, or None."""
    if supa.is_configured():
        admin = supa.get_admin()
        row = admin.table("profiles").select("id, email, created_at") \
            .eq("id", user_id or "").maybe_single().execute()
        if not row.data:
            return None
        d = row.data
        return {"user_id": str(d["id"]), "email": d["email"],
                "created_at": d.get("created_at") or _now()}
    u = _load().get(user_id or "")
    return _public(u) if u else None


def external_user_id(user_id):
    """The stable Pipedream/vault isolation key for a user (per-customer namespace)."""
    return f"trace-user-{user_id}"


def list_users():
    """Every account's public shape + external_user_id (admin portal only)."""
    if supa.is_configured():
        admin = supa.get_admin()
        users = admin.auth.admin.list_users()
        return [{"user_id": str(u.id), "email": u.email,
                 "external_user_id": external_user_id(str(u.id)),
                 "created_at": _iso(u.created_at) if u.created_at else ""}
                for u in users]
    return [{**_public(u), "external_user_id": external_user_id(u["user_id"])}
            for u in _load().values()]


# --- login sessions -------------------------------------------------------
# In Supabase mode the cookie holds a JWT; start_session just returns it.
# session_user validates the JWT via Supabase's get_user.

def start_session(user_id, token=None):
    """Return a session token for the given user.

    In Supabase mode, ``token`` is the JWT from sign_in and is returned as-is.
    In file mode, a fresh opaque token is minted.
    """
    if supa.is_configured():
        # The JWT was already obtained during verify/create_user
        return token
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {"user_id": user_id, "expires": time.time() + _SESSION_TTL}
    return token


def session_user(token):
    """The user_id for a live token, or None."""
    if supa.is_configured():
        if not token:
            return None
        client = supa.get_client()
        try:
            resp = client.auth.get_user(token)
            return str(resp.user.id) if resp and resp.user else None
        except Exception:
            return None
    s = _SESSIONS.get(token or "")
    if not s:
        return None
    if s["expires"] < time.time():
        _SESSIONS.pop(token, None)
        return None
    return s["user_id"]


def end_session(token):
    """Invalidate a session token (logout)."""
    if supa.is_configured():
        if token:
            try:
                supa.get_admin().auth.admin.sign_out(token)
            except Exception:
                pass  # token may already be expired/invalid
        return
    _SESSIONS.pop(token or "", None)
