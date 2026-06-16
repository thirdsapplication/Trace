"""Secure storage for the Anthropic API key and Trace's Pipedream Connect settings.

Rules carried over from the command line:
- store secrets securely, never in the repo / logs / results;
- prefer the macOS Keychain (via ``keyring``); fall back to a permission-
  restricted (0600) JSON file outside the repo (``~/.trace/config.json``);
- expose only whether a value is saved (and non-secret hints), never a secret itself.

Secrets are read back only to (a) test/use them and (b) inject them into the worker
processes / platform clients. They are never returned over the HTTP API or logged.
"""

import json
import os
import stat

_SERVICE = "trace-engine"
_ACCOUNT = "anthropic_api_key"

# Pipedream Connect settings (one account fronts all customers). The secret is
# sensitive; the project/client ids are not, but are stored together for simplicity.
_PD = {
    "project_id": "pipedream_project_id",
    "client_id": "pipedream_client_id",
    "client_secret": "pipedream_client_secret",
    "environment": "pipedream_environment",
}

_FALLBACK_DIR = os.path.expanduser("~/.trace")
_FALLBACK_PATH = os.path.join(_FALLBACK_DIR, "config.json")

# WS4: platform credentials may be preloaded from the server environment instead of the
# Test UI Settings, so an end-user surface never has to ask for them. A stored value
# (keychain/file) always wins; the env var is the fallback. Pipedream env names match
# config.py's own reads.
_ENV_KEY = "ANTHROPIC_API_KEY"
_PD_ENV = {
    "project_id": "PIPEDREAM_PROJECT_ID",
    "client_id": "PIPEDREAM_CLIENT_ID",
    "client_secret": "PIPEDREAM_CLIENT_SECRET",
    "environment": "PIPEDREAM_ENVIRONMENT",
}


def _keyring():
    """Return the keyring module if usable, else None. Never raises."""
    try:
        import keyring
        # A backend that refuses to store (e.g. the fail backend) is unusable.
        from keyring.backends import fail as _fail
        if isinstance(keyring.get_keyring(), _fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def backend_name():
    return "keychain" if _keyring() else "file"


# --- generic secure key/value over keychain (preferred) or a 0600 JSON file ---
def _load_file():
    try:
        with open(_FALLBACK_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_file(d):
    os.makedirs(_FALLBACK_DIR, exist_ok=True)
    with open(_FALLBACK_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.chmod(_FALLBACK_PATH, stat.S_IRUSR | stat.S_IWUSR)


def _set(account, value):
    """Store one value (read-modify-write so other fields are never clobbered).
    Returns the backend used ('keychain' or 'file')."""
    value = (value or "").strip()
    kr = _keyring()
    if kr:
        if value:
            kr.set_password(_SERVICE, account, value)
        else:
            try:
                kr.delete_password(_SERVICE, account)
            except Exception:
                pass
        return "keychain"
    d = _load_file()
    if value:
        d[account] = value
    else:
        d.pop(account, None)
    _save_file(d)
    return "file"


def _get(account):
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(_SERVICE, account)
            if v:
                return v
        except Exception:
            pass
    return _load_file().get(account) or None


def _clear(account):
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(_SERVICE, account)
        except Exception:
            pass
    d = _load_file()
    if account in d:
        d.pop(account, None)
        _save_file(d)


# --- Anthropic API key ---
def save_key(key):
    """Persist the key. Returns the backend used ('keychain' or 'file')."""
    key = (key or "").strip()
    if not key:
        raise ValueError("empty key")
    return _set(_ACCOUNT, key)


def get_key():
    """Return the saved key, or None. A stored value wins; otherwise fall back to the
    ANTHROPIC_API_KEY server environment variable (WS4 preload). Internal use only —
    never expose this."""
    return _get(_ACCOUNT) or (os.environ.get(_ENV_KEY) or "").strip() or None


def key_source():
    """'stored' | 'env' | None — where get_key resolved from (non-secret, for status)."""
    if _get(_ACCOUNT):
        return "stored"
    if (os.environ.get(_ENV_KEY) or "").strip():
        return "env"
    return None


def clear_key():
    _clear(_ACCOUNT)


def info():
    """Safe-to-expose status: whether a key is saved and where, never the value.
    Also returns a masked hint (last 4 chars) purely for operator reassurance."""
    key = get_key()
    masked = None
    if key and len(key) >= 4:
        masked = "…" + key[-4:]
    src = key_source()
    return {"saved": bool(key), "backend": ("env" if src == "env" else backend_name()),
            "masked_tail": masked, "source": src}


# --- Pipedream Connect settings ---
def save_pipedream(project_id=None, client_id=None, client_secret=None, environment=None):
    """Persist Trace's Pipedream Connect settings. Each field is written only when a
    non-empty value is supplied, so updating the project id (say) does not require
    re-entering the secret. Use ``clear_pipedream`` to remove them."""
    for field, value in (("project_id", project_id), ("client_id", client_id),
                         ("client_secret", client_secret), ("environment", environment)):
        if (value or "").strip():
            _set(_PD[field], value)
    return backend_name()


def _pd_field(field):
    """Stored value wins; else the server environment variable (WS4 preload)."""
    return _get(_PD[field]) or (os.environ.get(_PD_ENV[field]) or "").strip()


def get_pipedream():
    """The settings (secret included — internal use only, never exposed). Each field falls
    back to its server environment variable when not stored, so the platform credentials
    can be preloaded without the Settings UI (WS4)."""
    return {
        "project_id": _pd_field("project_id"),
        "client_id": _pd_field("client_id"),
        "client_secret": _pd_field("client_secret"),
        "environment": _pd_field("environment") or "production",
    }


def clear_pipedream():
    for account in _PD.values():
        _clear(account)


def pipedream_info():
    """Safe-to-expose status: ids/environment shown, the secret only as a boolean."""
    pd = get_pipedream()
    cid = pd["client_id"]
    return {
        "project_id": pd["project_id"],
        "client_id_tail": ("…" + cid[-4:]) if len(cid) >= 4 else None,
        "has_secret": bool(pd["client_secret"]),
        "environment": pd["environment"],
        "configured": bool(pd["project_id"] and pd["client_id"] and pd["client_secret"]),
        "backend": backend_name(),
    }

