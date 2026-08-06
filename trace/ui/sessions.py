"""Session storage for the test UI.

Dual-run: Supabase (cloud) or filesystem-only (local dev).

Each capture-and-reconstruct is a dated session directory under a sessions root:

    <root>/<session_id>/
        raw_events.jsonl       the captured stream (engine artifact)
        event_log.json         the reconstruction (engine artifact, after Reconstruct)
        validation_report.json the review flags / repairs (after Reconstruct)
        meta.json              UI bookkeeping (created/stopped/reconstructed times, counts)

When Supabase is configured:
- Session metadata lives in the ``sessions`` table (created_at, stopped_at, label, etc.)
- JSONB columns hold discovery, reviewed_bundles, scorecard, validation_report
- Large files (raw_events.jsonl, event_log.json) are uploaded to Supabase Storage
- The filesystem remains the working directory for the pipeline (capture writes here)
- ``local_sid`` maps the filesystem timestamp ID to the DB UUID

When unset: pure filesystem, exactly as before.
"""

import datetime
import json
import os
import re

from . import supabase_client as supa

RAW = "raw_events.jsonl"
EVENT_LOG = "event_log.json"
VALIDATION = "validation_report.json"
DISCOVERY = "discovery.json"
SCORECARD = "scorecard.json"
REVIEWED = "reviewed_bundles.json"
SPECS_DIR = "specs"
CONNECTION = "connection.json"      # Phase 4: per-session (per-customer) connection record
META = "meta.json"


def connection_filename():
    return CONNECTION


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new_session_id():
    # filesystem-safe, sortable
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def session_dir(root, sid):
    return os.path.join(root, sid)


def path(root, sid, name):
    return os.path.join(session_dir(root, sid), name)


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _sb_row_to_meta(row):
    """Convert a Supabase sessions row to the meta dict shape callers expect."""
    return {
        "id": row.get("local_sid") or str(row.get("id")),
        "db_id": str(row.get("id")),
        "label": row.get("label"),
        "owner_user_id": row.get("owner_id"),
        "created_at": row.get("created_at"),
        "stopped_at": row.get("stopped_at"),
        "reconstructed_at": row.get("reconstructed_at"),
        "discovered_at": row.get("discovered_at"),
        "built_at": row.get("built_at"),
        "deployed_at": row.get("deployed_at"),
        "event_count": row.get("event_count"),
        "fixture": row.get("fixture"),
    }


def _get_db_id(root, sid):
    """Resolve a local session id to the Supabase DB UUID.

    Looks up by local_sid first; if not found, returns None.
    """
    if not supa.is_configured():
        return None
    admin = supa.get_admin()
    r = admin.table("sessions").select("id").eq("local_sid", sid).maybe_single().execute()
    return str(r.data["id"]) if r.data else None


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

def create_session(root, label=None, owner_id=None):
    """Create a new session. Returns the local filesystem sid."""
    sid = new_session_id()
    d = session_dir(root, sid)
    os.makedirs(d, exist_ok=True)
    meta = {"id": sid, "label": label, "created_at": _now(),
            "stopped_at": None, "reconstructed_at": None}
    if owner_id:
        meta["owner_user_id"] = owner_id

    if supa.is_configured() and owner_id:
        admin = supa.get_admin()
        admin.table("sessions").insert({
            "owner_id": owner_id,
            "label": label,
            "local_sid": sid,
            "status": "created",
        }).execute()

    write_meta(root, sid, meta)
    return sid


def _unique_session_id(root):
    """A session id whose directory does not yet exist."""
    import secrets
    sid = new_session_id()
    while os.path.exists(session_dir(root, sid)):
        sid = f"{new_session_id()}-{secrets.token_hex(3)}"
    return sid


def create_upload_session(root, owner_user_id, raw_text):
    """Phase 2: materialize a session from raw events uploaded by a desktop client."""
    sid = _unique_session_id(root)
    os.makedirs(session_dir(root, sid), exist_ok=True)
    meta = {"id": sid, "label": None, "created_at": _now(),
            "stopped_at": _now(), "reconstructed_at": None,
            "owner_user_id": owner_user_id}
    write_meta(root, sid, meta)
    with open(path(root, sid, RAW), "w", encoding="utf-8") as f:
        f.write(raw_text or "")
    count = artifacts(root, sid)["raw_event_count"]
    update_meta(root, sid, raw_event_count=count)

    if supa.is_configured() and owner_user_id:
        admin = supa.get_admin()
        admin.table("sessions").insert({
            "owner_id": owner_user_id,
            "local_sid": sid,
            "status": "stopped",
            "event_count": count,
            "stopped_at": _now(),
        }).execute()
        # Upload raw_events to Storage
        _upload_to_storage(root, sid, RAW, owner_user_id)

    return sid, count


def read_meta(root, sid):
    try:
        with open(path(root, sid, META), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"id": sid}


def write_meta(root, sid, meta):
    p = path(root, sid, META)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, p)


def update_meta(root, sid, **fields):
    meta = read_meta(root, sid)
    meta.update(fields)
    write_meta(root, sid, meta)
    return meta


def sync_to_supabase(root, sid, **fields):
    """Sync session fields to the Supabase sessions table.

    Called after pipeline steps complete. ``fields`` maps Supabase column names
    to values (e.g. ``stopped_at``, ``reconstructed_at``, ``event_count``,
    ``discovery`` (JSONB), ``scorecard`` (JSONB), etc.).
    """
    if not supa.is_configured():
        return
    db_id = _get_db_id(root, sid)
    if not db_id:
        return
    admin = supa.get_admin()
    admin.table("sessions").update(fields).eq("id", db_id).execute()


def write_json_artifact(root, sid, column, data):
    """Write a JSONB artifact to the Supabase sessions table.

    ``column`` must be one of: discovery, reviewed_bundles, scorecard, validation_report.
    Also writes to the local filesystem as the canonical JSON file.
    """
    # Map column to local filename
    col_to_file = {
        "discovery": DISCOVERY,
        "reviewed_bundles": REVIEWED,
        "scorecard": SCORECARD,
        "validation_report": VALIDATION,
    }
    fname = col_to_file.get(column)
    if fname:
        p = path(root, sid, fname)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    if supa.is_configured():
        db_id = _get_db_id(root, sid)
        if db_id:
            admin = supa.get_admin()
            admin.table("sessions").update({column: data}).eq("id", db_id).execute()


def _upload_to_storage(root, sid, filename, owner_id):
    """Upload a file to Supabase Storage (artifacts bucket)."""
    if not supa.is_configured():
        return
    filepath = path(root, sid, filename)
    if not os.path.exists(filepath):
        return
    storage_path = f"{owner_id}/{sid}/{filename}"
    admin = supa.get_admin()
    with open(filepath, "rb") as f:
        admin.storage.from_("artifacts").upload(
            storage_path, f.read(),
            {"content-type": "application/octet-stream", "upsert": "true"})
    # Update the path column in sessions
    col = "raw_events_path" if filename == RAW else "event_log_path" if filename == EVENT_LOG else None
    if col:
        db_id = _get_db_id(root, sid)
        if db_id:
            admin.table("sessions").update({col: storage_path}).eq("id", db_id).execute()


# ---------------------------------------------------------------------------
# Artifact inspection (unchanged — filesystem is the source of truth during pipeline)
# ---------------------------------------------------------------------------

def _count_lines(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0


def artifacts(root, sid):
    """Which engine artifacts exist for this session, plus simple counts."""
    raw_p = path(root, sid, RAW)
    log_p = path(root, sid, EVENT_LOG)
    val_p = path(root, sid, VALIDATION)
    log_count = None
    if os.path.exists(log_p):
        try:
            with open(log_p, "r", encoding="utf-8") as f:
                log_count = len(json.load(f))
        except (json.JSONDecodeError, OSError):
            log_count = None
    disc_p = path(root, sid, DISCOVERY)
    bundle_count = None
    if os.path.exists(disc_p):
        try:
            with open(disc_p, "r", encoding="utf-8") as f:
                d = json.load(f)
            bundle_count = len(d.get("bundles", [])) + len(d.get("anti_bundles", []))
        except (json.JSONDecodeError, OSError):
            bundle_count = None
    specs_dir = path(root, sid, SPECS_DIR)
    spec_count = (len([f for f in os.listdir(specs_dir) if f.endswith(".md")])
                  if os.path.isdir(specs_dir) else 0)
    return {
        "raw": os.path.exists(raw_p),
        "raw_event_count": _count_lines(raw_p),
        "event_log": os.path.exists(log_p),
        "event_count": log_count,
        "validation": os.path.exists(val_p),
        "discovery": os.path.exists(disc_p),
        "candidate_count": bundle_count,
        "scorecard": os.path.exists(path(root, sid, SCORECARD)),
        "reviewed": os.path.exists(path(root, sid, REVIEWED)),
        "specs": spec_count,
    }


def list_specs(root, sid):
    """Return generated spec files as [{filename, markdown}], or []."""
    d = path(root, sid, SPECS_DIR)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".md"):
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                out.append({"filename": fn, "markdown": f.read()})
    return out


def _safe_id(bid):
    return "".join(c if c.isalnum() else "_" for c in str(bid))


def agent_yaml_name(bid, mode):
    return f"agent_{_safe_id(bid)}.{mode}.yaml"


def test_report_name(bid):
    return f"test_report_{_safe_id(bid)}.json"


def run_record_name(bid):
    return f"run_{_safe_id(bid)}.json"


def interview_filename(bid):
    return f"interview_{_safe_id(bid)}.json"


def grounding_name(bid):
    return f"grounding_{_safe_id(bid)}.json"


def deploy_record_name(bid):
    return f"deploy_{_safe_id(bid)}.json"


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def list_runs(root, sid):
    """Every run_<id>.json pointer record for this session (manual-eval mode)."""
    d = path(root, sid, SPECS_DIR)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if re.match(r"^run_.+\.json$", fn):
            rec = _read_json(os.path.join(d, fn))
            if rec is not None:
                out.append(rec)
    return out


def list_deploy_records(root, sid):
    """Every deploy_<id>.json live-session record (Phase 6)."""
    d = path(root, sid, SPECS_DIR)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if re.match(r"^deploy_.+\.json$", fn):
            rec = _read_json(os.path.join(d, fn))
            if rec is not None:
                out.append(rec)
    return out


def list_agents(root, sid):
    """Return Stage-4 outputs per bundle: agent envelopes + the run pointer (if any)."""
    d = path(root, sid, SPECS_DIR)
    if not os.path.isdir(d):
        return []
    bundles = {}
    for fn in sorted(os.listdir(d)):
        m = re.match(r"^agent_(.+)\.live\.yaml$", fn)
        if m:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                bundles[m.group(1)] = f.read()
    out = []
    for bid, live_yaml in bundles.items():
        grounding = _read_json(os.path.join(d, f"grounding_{bid}.json"))
        dep = _read_json(os.path.join(d, f"deploy_{bid}.json"))
        out.append({"bundle_id": bid, "live_yaml": live_yaml,
                    "grounding": grounding, "deploy": dep})
    return out


def list_sessions(root, owner_id=None):
    """List sessions, optionally filtered by owner.

    When Supabase is configured and ``owner_id`` is given, query the DB for
    that user's sessions (fast, RLS-friendly). Otherwise fall back to
    filesystem scan.
    """
    if supa.is_configured() and owner_id:
        admin = supa.get_admin()
        r = admin.table("sessions").select("*") \
            .eq("owner_id", owner_id) \
            .order("created_at", desc=True).execute()
        out = []
        for row in (r.data or []):
            meta = _sb_row_to_meta(row)
            local_sid = row.get("local_sid") or str(row.get("id"))
            # Merge local artifacts if directory exists
            if os.path.isdir(session_dir(root, local_sid)):
                meta["artifacts"] = artifacts(root, local_sid)
            else:
                meta["artifacts"] = _artifacts_from_db(row)
            out.append(meta)
        return out

    # Filesystem fallback
    if not os.path.isdir(root):
        return []
    out = []
    for sid in sorted(os.listdir(root), reverse=True):
        if not os.path.isdir(session_dir(root, sid)):
            continue
        meta = read_meta(root, sid)
        meta = {**meta, "artifacts": artifacts(root, sid)}
        out.append(meta)
    return out


def _artifacts_from_db(row):
    """Build an artifacts dict from Supabase columns when no local dir exists."""
    return {
        "raw": bool(row.get("raw_events_path")),
        "raw_event_count": row.get("event_count") or 0,
        "event_log": bool(row.get("event_log_path")),
        "event_count": row.get("event_count"),
        "validation": row.get("validation_report") is not None,
        "discovery": row.get("discovery") is not None,
        "candidate_count": None,
        "scorecard": row.get("scorecard") is not None,
        "reviewed": row.get("reviewed_bundles") is not None,
        "specs": 0,
    }


def read_json_artifact(root, sid, name):
    """Return parsed JSON for event_log/validation, or None if missing."""
    p = path(root, sid, name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_raw_events(root, sid):
    """Return the raw stream as a parsed array (one object per line)."""
    p = path(root, sid, RAW)
    rows = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        return None
    return rows
