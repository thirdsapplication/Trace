"""Secure storage for the Anthropic API key (handoff §5).

Rules carried over from the command line:
- store it securely, never in the repo / logs / results;
- prefer the macOS Keychain (via ``keyring``); fall back to a permission-
  restricted (0600) JSON file outside the repo (``~/.trace/config.json``);
- expose only whether a key is saved, never the key itself, to the UI.

The key is read back only to (a) test it and (b) inject it into the reconstruction
process's ``ANTHROPIC_API_KEY``. It is never returned over the HTTP API or logged.
"""

import json
import os
import stat

_SERVICE = "trace-engine"
_ACCOUNT = "anthropic_api_key"

_FALLBACK_DIR = os.path.expanduser("~/.trace")
_FALLBACK_PATH = os.path.join(_FALLBACK_DIR, "config.json")


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


def save_key(key):
    """Persist the key. Returns the backend used ('keychain' or 'file')."""
    key = (key or "").strip()
    if not key:
        raise ValueError("empty key")
    kr = _keyring()
    if kr:
        kr.set_password(_SERVICE, _ACCOUNT, key)
        return "keychain"
    os.makedirs(_FALLBACK_DIR, exist_ok=True)
    # write then tighten perms to 0600 (owner read/write only)
    with open(_FALLBACK_PATH, "w", encoding="utf-8") as f:
        json.dump({_ACCOUNT: key}, f)
    os.chmod(_FALLBACK_PATH, stat.S_IRUSR | stat.S_IWUSR)
    return "file"


def get_key():
    """Return the saved key, or None. (Internal use only — never expose this.)"""
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(_SERVICE, _ACCOUNT)
            if v:
                return v
        except Exception:
            pass
    try:
        with open(_FALLBACK_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get(_ACCOUNT) or None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_key():
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(_SERVICE, _ACCOUNT)
        except Exception:
            pass
    try:
        os.remove(_FALLBACK_PATH)
    except FileNotFoundError:
        pass


def info():
    """Safe-to-expose status: whether a key is saved and where, never the value.
    Also returns a masked hint (last 4 chars) purely for operator reassurance."""
    key = get_key()
    masked = None
    if key and len(key) >= 4:
        masked = "…" + key[-4:]
    return {"saved": bool(key), "backend": backend_name(), "masked_tail": masked}
