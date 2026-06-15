"""Session storage for the test UI.

Each capture-and-reconstruct is a dated session directory under a sessions root:

    <root>/<session_id>/
        raw_events.jsonl       the captured stream (engine artifact)
        event_log.json         the reconstruction (engine artifact, after Reconstruct)
        validation_report.json the review flags / repairs (after Reconstruct)
        meta.json              UI bookkeeping (created/stopped/reconstructed times, counts)

The UI treats the three engine artifacts as read-only products. ``meta.json`` is
the only file this module writes beyond what the engine produces.
"""

import datetime
import json
import os
import re

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


def create_session(root, label=None):
    sid = new_session_id()
    d = session_dir(root, sid)
    os.makedirs(d, exist_ok=True)
    meta = {"id": sid, "label": label, "created_at": _now(),
            "stopped_at": None, "reconstructed_at": None}
    write_meta(root, sid, meta)
    return sid


def read_meta(root, sid):
    try:
        with open(path(root, sid, META), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"id": sid}


def write_meta(root, sid, meta):
    with open(path(root, sid, META), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def update_meta(root, sid, **fields):
    meta = read_meta(root, sid)
    meta.update(fields)
    write_meta(root, sid, meta)
    return meta


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
    """Return Stage-4 outputs per bundle: agent envelopes + the run pointer (if any).
    Grading is out of the flow in manual-evaluation mode, so ``run`` (not a graded
    report) is what the UI shows."""
    d = path(root, sid, SPECS_DIR)
    if not os.path.isdir(d):
        return []
    bundles = {}
    for fn in sorted(os.listdir(d)):
        m = re.match(r"^agent_(.+)\.(test|live)\.yaml$", fn)
        if m:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                bundles.setdefault(m.group(1), {})[m.group(2) + "_yaml"] = f.read()
    out = []
    for bid, data in bundles.items():
        grounding = _read_json(os.path.join(d, f"grounding_{bid}.json"))
        dep = _read_json(os.path.join(d, f"deploy_{bid}.json"))
        out.append({"bundle_id": bid, "test_yaml": data.get("test_yaml"),
                    "live_yaml": data.get("live_yaml"), "grounding": grounding,
                    "deploy": dep})
    return out


def list_sessions(root):
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
