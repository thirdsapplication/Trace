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

import json
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

-- Specs 2/3/4: the durable per-user index into the deployed agents (the MA
-- agents themselves live on Anthropic's platform; these rows survive a Render
-- restart so "all my agents", schedules, and run history don't reset).
CREATE TABLE IF NOT EXISTS agents (
    agent_id         TEXT PRIMARY KEY,
    owner_user_id    TEXT NOT NULL,
    external_user_id TEXT,
    bundle_id        TEXT,
    name             TEXT,
    vault_ref        TEXT,
    trigger          TEXT,
    session_id       TEXT,
    staged_apps      TEXT,                  -- Spec 8: JSON list of app slugs staged for approval
    deployed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agents_owner_idx ON agents (owner_user_id);

-- Spec 8 ("stage, don't execute"): outbound actions the agent proposed but did NOT
-- perform. Captured in full at run time, reviewed in Trace, and executed on approval.
CREATE TABLE IF NOT EXISTS pending_actions (
    id             BIGSERIAL PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    owner_user_id  TEXT NOT NULL,
    run_session_id TEXT,
    app            TEXT,
    kind           TEXT,
    summary        TEXT,
    payload        TEXT,                    -- JSON: the action's full arguments
    content_hash   TEXT,                    -- idempotency: identical proposals dedupe
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|executed|failed
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS pending_actions_owner_idx ON pending_actions (owner_user_id, status);

CREATE TABLE IF NOT EXISTS schedules (
    agent_id      TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL,
    cadence       TEXT NOT NULL,          -- JSON: {"kind":"interval","minutes":15} | {"kind":"hourly"} | {"kind":"daily","time":"09:00"}
    paused        BOOLEAN NOT NULL DEFAULT FALSE,
    last_run_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id             BIGSERIAL PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    owner_user_id  TEXT NOT NULL,
    ran_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    status         TEXT NOT NULL,         -- completed | failed | needs_approval
    summary        TEXT,
    details        TEXT,                  -- JSON list of in-Trace action labels
    run_session_id TEXT
);
CREATE INDEX IF NOT EXISTS agent_runs_agent_idx ON agent_runs (agent_id, ran_at DESC);
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
            # Spec 6: add the in-Trace action list to agent_runs tables created
            # before it existed (idempotent; no-op on a fresh schema above).
            cur.execute("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS details TEXT")
            # Spec 8: add the staged-apps column to agents tables created before it.
            cur.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS staged_apps TEXT")
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


# --- Specs 2/3/4: durable agents / schedules / runs ------------------------
# Pure-Postgres; the dual-run switch (Postgres vs filesystem) lives in
# trace/ui/registry.py, which calls these only when is_configured().

_AGENT_COLS = ("agent_id", "owner_user_id", "external_user_id", "bundle_id",
               "name", "vault_ref", "trigger", "session_id", "staged_apps", "deployed_at")


def _agent_row(row):
    d = dict(zip(_AGENT_COLS, row))
    d["deployed_at"] = (d["deployed_at"].isoformat(timespec="seconds")
                        if hasattr(d.get("deployed_at"), "isoformat") else d.get("deployed_at"))
    try:
        d["staged_apps"] = json.loads(d["staged_apps"]) if d.get("staged_apps") else []
    except (TypeError, ValueError):
        d["staged_apps"] = []
    return d


def agent_upsert(a):
    """Insert-or-update one deployed agent (keyed by agent_id)."""
    p = dict(a)
    p["staged_apps"] = json.dumps(list(a.get("staged_apps") or []))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (agent_id, owner_user_id, external_user_id, bundle_id, "
            "name, vault_ref, trigger, session_id, staged_apps) "
            "VALUES (%(agent_id)s, %(owner_user_id)s, %(external_user_id)s, %(bundle_id)s, "
            "%(name)s, %(vault_ref)s, %(trigger)s, %(session_id)s, %(staged_apps)s) "
            "ON CONFLICT (agent_id) DO UPDATE SET "
            "owner_user_id=EXCLUDED.owner_user_id, external_user_id=EXCLUDED.external_user_id, "
            "bundle_id=EXCLUDED.bundle_id, name=EXCLUDED.name, vault_ref=EXCLUDED.vault_ref, "
            "trigger=EXCLUDED.trigger, session_id=EXCLUDED.session_id, staged_apps=EXCLUDED.staged_apps",
            p)


def agents_for_user(owner_user_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_AGENT_COLS)} FROM agents "
                    "WHERE owner_user_id = %s ORDER BY deployed_at DESC", (owner_user_id,))
        return [_agent_row(r) for r in cur.fetchall()]


def schedule_set(agent_id, owner_user_id, cadence, paused=False):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (agent_id, owner_user_id, cadence, paused) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (agent_id) DO UPDATE SET cadence=EXCLUDED.cadence, paused=EXCLUDED.paused",
            (agent_id, owner_user_id, cadence, paused))


def schedule_clear(agent_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM schedules WHERE agent_id = %s", (agent_id,))


def schedule_get(agent_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT cadence, paused, last_run_at FROM schedules WHERE agent_id = %s",
                    (agent_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"cadence": row[0], "paused": row[1],
                "last_run_at": row[2].isoformat(timespec="seconds") if hasattr(row[2], "isoformat") else row[2]}


def schedule_list_active():
    """Active (not paused) schedules joined with their agent row — everything the
    scheduler needs to run headlessly. Returns dicts with agent fields + cadence +
    last_run_at (epoch seconds, or None)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join('a.' + c for c in _AGENT_COLS)}, "
            "s.cadence, EXTRACT(EPOCH FROM s.last_run_at) "
            "FROM schedules s JOIN agents a ON a.agent_id = s.agent_id "
            "WHERE s.paused = FALSE")
        out = []
        for r in cur.fetchall():
            d = _agent_row(r[:len(_AGENT_COLS)])
            d["cadence"] = r[len(_AGENT_COLS)]
            d["last_run_epoch"] = float(r[len(_AGENT_COLS) + 1]) if r[len(_AGENT_COLS) + 1] is not None else None
            out.append(d)
        return out


def schedule_mark_run(agent_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE schedules SET last_run_at = now() WHERE agent_id = %s", (agent_id,))


def run_add(rec):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (agent_id, owner_user_id, status, summary, "
            "details, run_session_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (rec.get("agent_id"), rec.get("owner_user_id"), rec.get("status"),
             rec.get("summary"), json.dumps(rec.get("details") or []),
             rec.get("run_session_id")))


def runs_for_agent(agent_id, limit=20):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT ran_at, status, summary, details, run_session_id "
                    "FROM agent_runs WHERE agent_id = %s ORDER BY ran_at DESC LIMIT %s",
                    (agent_id, limit))
        out = []
        for r in cur.fetchall():
            try:
                details = json.loads(r[3]) if r[3] else []
            except (TypeError, ValueError):
                details = []
            out.append({"ran_at": r[0].isoformat(timespec="seconds") if hasattr(r[0], "isoformat") else r[0],
                        "status": r[1], "summary": r[2], "details": details, "run_session_id": r[4]})
        return out


# --- Spec 8: staged (pending) actions --------------------------------------

_PENDING_COLS = ("id", "agent_id", "owner_user_id", "run_session_id", "app", "kind",
                 "summary", "payload", "content_hash", "status", "created_at", "processed_at")


def _pending_row(row):
    d = dict(zip(_PENDING_COLS, row))
    try:
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
    except (TypeError, ValueError):
        d["payload"] = {}
    for k in ("created_at", "processed_at"):
        if hasattr(d.get(k), "isoformat"):
            d[k] = d[k].isoformat(timespec="seconds")
    return d


def pending_add(rec):
    """Insert one staged action, unless an identical one (same agent + content_hash)
    is already pending or executed — the idempotency guard so a repeated scheduled run
    never re-stages the same proposal. Returns True if inserted, False if deduped."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pending_actions WHERE agent_id = %s AND content_hash = %s "
            "AND status IN ('pending', 'executed') LIMIT 1",
            (rec.get("agent_id"), rec.get("content_hash")))
        if cur.fetchone():
            return False
        cur.execute(
            "INSERT INTO pending_actions (agent_id, owner_user_id, run_session_id, app, "
            "kind, summary, payload, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (rec.get("agent_id"), rec.get("owner_user_id"), rec.get("run_session_id"),
             rec.get("app"), rec.get("kind"), rec.get("summary"),
             json.dumps(rec.get("payload") or {}), rec.get("content_hash")))
        return True


def pending_for_user(owner_user_id, status="pending"):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_PENDING_COLS)} FROM pending_actions "
                    "WHERE owner_user_id = %s AND status = %s ORDER BY created_at DESC",
                    (owner_user_id, status))
        return [_pending_row(r) for r in cur.fetchall()]


def pending_get(pending_id):
    try:
        pid = int(pending_id)
    except (TypeError, ValueError):
        return None
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_PENDING_COLS)} FROM pending_actions WHERE id = %s",
                    (pid,))
        row = cur.fetchone()
        return _pending_row(row) if row else None


def pending_set_status(pending_id, status):
    try:
        pid = int(pending_id)
    except (TypeError, ValueError):
        return
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE pending_actions SET status = %s, processed_at = now() "
                    "WHERE id = %s", (status, pid))


def pending_count_for_user(owner_user_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM pending_actions "
                    "WHERE owner_user_id = %s AND status = 'pending'", (owner_user_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0
