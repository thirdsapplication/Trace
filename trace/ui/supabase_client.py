"""Shared Supabase client — the single import point for Auth + PostgREST + Storage.

Two clients:
- ``get_client()``  — uses the anon key; respects RLS (end-user calls).
- ``get_admin()``   — uses the service-role key; bypasses RLS (server-side ops).

Both read from the environment:
  SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY

When any of the required vars is missing, ``is_configured()`` returns False and the
getters raise RuntimeError, so callers that must tolerate a Supabase-less local run
should gate on ``is_configured()``.
"""

import os

_client = None
_admin = None


class _NoRow:
    """Stand-in for "no row matched".

    ``maybe_single().execute()`` returns **None** rather than a response object when the
    filter matches nothing, so the idiomatic ``r.data`` raises
    ``AttributeError: 'NoneType' object has no attribute 'data'`` before any
    ``if not r.data`` guard can run. Every such call site therefore reads
    ``....maybe_single().execute() or supa.NO_ROW``, so a miss yields ``data = None``
    instead of exploding.

    This bit hard: an agent with no schedule row crashed ``schedule_get``, which took the
    whole ``home_data`` request down with it, so the agents home rendered empty for a user
    whose agent had in fact deployed perfectly.
    """

    data = None


NO_ROW = _NoRow()


def is_configured():
    """True when Supabase credentials are present in the environment."""
    return bool(
        (os.environ.get("SUPABASE_URL") or "").strip()
        and (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
        and (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    )


# Every query in the app shares one client, hence one HTTP connection pool. Without
# an explicit deadline a stuck request has none the app controls, and a handler waits
# on it indefinitely — the failure mode that looks like a hang with no error anywhere.
REQUEST_TIMEOUT = 20


def _options():
    """ClientOptions carrying our timeout, or None when the installed supabase
    version does not accept one (the pin is a range; behavior must degrade, not
    break — the same lesson as the anthropic temperature incident)."""
    try:
        from supabase.lib.client_options import ClientOptions
        return ClientOptions(postgrest_client_timeout=REQUEST_TIMEOUT,
                             storage_client_timeout=REQUEST_TIMEOUT)
    except Exception:                                      # noqa: BLE001
        return None


def _create(url, key):
    from supabase import create_client
    opts = _options()
    if opts is not None:
        try:
            return create_client(url, key, options=opts)
        except Exception:                                  # noqa: BLE001 — shape drift
            pass
    return create_client(url, key)


def get_client():
    """Anon-key client (respects RLS). Cached after first call."""
    global _client
    if _client is None:
        url = (os.environ.get("SUPABASE_URL") or "").strip()
        key = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY not set")
        _client = _create(url, key)
    return _client


def get_admin():
    """Service-role client (bypasses RLS). Cached after first call."""
    global _admin
    if _admin is None:
        url = (os.environ.get("SUPABASE_URL") or "").strip()
        key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
        _admin = _create(url, key)
    return _admin
