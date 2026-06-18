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

-- Spec 1: durable login sessions (opaque tokens) so a signed-in session
-- survives an instance restart/redeploy. Distinct from the capture-`sessions`
-- table above. Reaped lazily on lookup by expires_at.
CREATE TABLE IF NOT EXISTS auth_sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS auth_sessions_user_id_idx
    ON auth_sessions (user_id);
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


# --- Spec 1: durable accounts + login sessions -----------------------------
# These are called by users.py only when DATABASE_URL is set; they keep all
# Postgres access inside this module. Connections use psycopg's context manager,
# which commits on a clean exit and rolls back on error.

def user_create(user_id, email, password_hash, external_user_id):
    """Insert a new account. Raises ValueError if the email already exists."""
    import psycopg
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, email, password_hash, external_user_id) "
                "VALUES (%s, %s, %s, %s)",
                (user_id, email, password_hash, external_user_id))
    except psycopg.errors.UniqueViolation:
        raise ValueError("an account with this email already exists")


def user_by_email(email):
    """(user_id, email, password_hash, created_at) for an email, or None."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, email, password_hash, created_at "
                    "FROM users WHERE email = %s", (email,))
        return cur.fetchone()


def user_by_id(user_id):
    """(user_id, email, created_at) for an id, or None."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, email, created_at FROM users WHERE user_id = %s",
                    (user_id,))
        return cur.fetchone()


def auth_start(token, user_id, ttl_seconds):
    """Persist a login token with a TTL so the session survives a restart."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO auth_sessions (token, user_id, expires_at) "
                    "VALUES (%s, %s, now() + (%s * interval '1 second'))",
                    (token, user_id, ttl_seconds))


def auth_session_user(token):
    """The user_id for a live token, or None. Reaps expired tokens first."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
        cur.execute("SELECT user_id FROM auth_sessions WHERE token = %s", (token,))
        row = cur.fetchone()
        return row[0] if row else None


def auth_end(token):
    """Delete one login token (logout)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM auth_sessions WHERE token = %s", (token,))
