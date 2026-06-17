"""Phase 1 (cloud backend) — Postgres connection + schema.

Phase 1 only stands up the *connection* and the *schema*; it does not yet route
reads/writes through these tables (the email/password login logic and the
session-record migration are Phase 2, which builds directly on this schema).

Switched purely by configuration:
- when ``DATABASE_URL`` is set (Render injects it from the managed Postgres),
  connect and create the tables on startup;
- when it is unset (a local run), this whole module is a no-op, so the system
  boots with zero cloud dependencies and local stays first-class.

Tables are keyed by ``external_user_id`` — the same stable per-customer
isolation key the app already derives in ``users.external_user_id`` — so Phase 2
can adopt them without reshaping the contract.
"""

import os

# Tables mirror the existing JSON/file shapes (users.py, sessions.py meta) so
# Phase 2 can migrate onto them without a schema change. IF NOT EXISTS makes
# startup idempotent — safe to run on every boot against an existing database.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,
    email            TEXT UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,
    external_user_id TEXT UNIQUE NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    owner_user_id    TEXT REFERENCES users(user_id),
    external_user_id TEXT NOT NULL,
    label            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    stopped_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS sessions_external_user_id_idx
    ON sessions (external_user_id);
"""


def database_url():
    """The configured Postgres URL, normalized for psycopg, or None when unset.

    Render (and some hosts) hand out the legacy ``postgres://`` scheme;
    psycopg/SQLAlchemy expect ``postgresql://``. Normalize so either works.
    """
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        return None
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def is_configured():
    """True when a database is configured (i.e. we're running against Postgres)."""
    return database_url() is not None


def connect():
    """Open a new psycopg connection to the configured database.

    Raises if ``DATABASE_URL`` is unset or psycopg isn't installed — callers
    that must tolerate a DB-less (local) run should gate on ``is_configured()``.
    """
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    import psycopg  # imported lazily so a local run never needs the driver

    return psycopg.connect(url)


def init_db():
    """Create the schema if a database is configured; no-op otherwise.

    Returns True if the schema was ensured, False if no database is configured
    (local run). Never raises on a missing ``DATABASE_URL`` so startup stays the
    same code path locally and in the cloud.
    """
    if not is_configured():
        return False
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()
    return True
