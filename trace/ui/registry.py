"""Specs 2/3/4 — the durable per-user index of deployed agents, their schedules,
and their run history, with the same dual-run switch as Spec 1:

- when ``DATABASE_URL`` is set (cloud), delegate to Postgres (``db.py``) so the
  state survives Render's ephemeral disk being wiped on restart/redeploy;
- otherwise (local dev / offline tests), persist to small JSON files under the
  sessions root, so nothing requires a database to run.

Callers pass ``root`` (the sessions root) for the file-mode path; it is ignored
in Postgres mode. ``cadence`` is handled here as a Python dict (serialized to
JSON text for Postgres) so callers never touch storage encoding.
"""

import json
import os
import threading
import time

from . import db

_LOCK = threading.Lock()


# --- file-mode helpers (local dev / tests) ---------------------------------
def _dir(root):
    d = os.path.join(root, "_registry")
    os.makedirs(d, exist_ok=True)
    return d


def _load(root, name):
    try:
        with open(os.path.join(_dir(root), name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(root, name, obj):
    p = os.path.join(_dir(root), name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


# --- agents ----------------------------------------------------------------
def agent_upsert(root, agent):
    """agent: {agent_id, owner_user_id, external_user_id, bundle_id, name,
    vault_ref, trigger, session_id}."""
    if db.is_configured():
        return db.agent_upsert(agent)
    with _LOCK:
        d = _load(root, "agents.json")
        d[agent["agent_id"]] = {**agent, "deployed_at": _now_iso()}
        _save(root, "agents.json", d)


def agents_for_user(root, owner_user_id):
    if db.is_configured():
        return db.agents_for_user(owner_user_id)
    d = _load(root, "agents.json")
    return sorted((a for a in d.values() if a.get("owner_user_id") == owner_user_id),
                  key=lambda a: a.get("deployed_at") or "", reverse=True)


# --- schedules -------------------------------------------------------------
def schedule_set(root, agent_id, owner_user_id, cadence, paused=False):
    if db.is_configured():
        return db.schedule_set(agent_id, owner_user_id, json.dumps(cadence), paused)
    with _LOCK:
        d = _load(root, "schedules.json")
        prev = d.get(agent_id) or {}
        d[agent_id] = {"owner_user_id": owner_user_id, "cadence": cadence,
                       "paused": paused, "last_run_epoch": prev.get("last_run_epoch")}
        _save(root, "schedules.json", d)


def schedule_clear(root, agent_id):
    if db.is_configured():
        return db.schedule_clear(agent_id)
    with _LOCK:
        d = _load(root, "schedules.json")
        d.pop(agent_id, None)
        _save(root, "schedules.json", d)


def schedule_get(root, agent_id):
    if db.is_configured():
        s = db.schedule_get(agent_id)
        if s and isinstance(s.get("cadence"), str):
            s["cadence"] = json.loads(s["cadence"])
        return s
    s = _load(root, "schedules.json").get(agent_id)
    if not s:
        return None
    return {"cadence": s["cadence"], "paused": s.get("paused", False),
            "last_run_at": _iso(s.get("last_run_epoch"))}


def schedule_list_active(root):
    """Active (not paused) schedules joined with their agent row, each carrying
    ``cadence`` (dict) and ``last_run_epoch`` (float|None) — what the scheduler needs."""
    if db.is_configured():
        out = db.schedule_list_active()
        for s in out:
            if isinstance(s.get("cadence"), str):
                s["cadence"] = json.loads(s["cadence"])
        return out
    agents = _load(root, "agents.json")
    scheds = _load(root, "schedules.json")
    out = []
    for agent_id, s in scheds.items():
        if s.get("paused"):
            continue
        a = agents.get(agent_id)
        if not a:
            continue
        out.append({**a, "cadence": s["cadence"], "last_run_epoch": s.get("last_run_epoch")})
    return out


def schedule_mark_run(root, agent_id):
    if db.is_configured():
        return db.schedule_mark_run(agent_id)
    with _LOCK:
        d = _load(root, "schedules.json")
        if agent_id in d:
            d[agent_id]["last_run_epoch"] = time.time()
            _save(root, "schedules.json", d)


# --- runs ------------------------------------------------------------------
def run_add(root, rec):
    """rec: {agent_id, owner_user_id, status, summary, details, run_session_id}."""
    if db.is_configured():
        return db.run_add(rec)
    with _LOCK:
        p = os.path.join(_dir(root), f"runs_{_safe(rec['agent_id'])}.jsonl")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({**rec, "ran_at": _now_iso()}) + "\n")


def runs_for_agent(root, agent_id, limit=20):
    if db.is_configured():
        return db.runs_for_agent(agent_id, limit)
    p = os.path.join(_dir(root), f"runs_{_safe(agent_id)}.jsonl")
    try:
        with open(p, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
    return list(reversed(rows))[:limit]


# --- pending (staged) actions — Spec 8 -------------------------------------
def pending_add(root, rec):
    """rec: {agent_id, owner_user_id, run_session_id, app, kind, summary, payload,
    content_hash}. Deduped on (agent_id, content_hash) against pending/executed items
    so a repeated scheduled run never re-stages the same proposal. Returns True if
    inserted, False if deduped."""
    if db.is_configured():
        return db.pending_add(rec)
    with _LOCK:
        d = _load(root, "pending.json")
        for it in d.values():
            if (it.get("agent_id") == rec.get("agent_id")
                    and it.get("content_hash") == rec.get("content_hash")
                    and it.get("status") in ("pending", "executed")):
                return False
        nid = str(max((int(k) for k in d), default=0) + 1)
        d[nid] = {**rec, "id": nid, "status": "pending",
                  "created_at": _now_iso(), "processed_at": None}
        _save(root, "pending.json", d)
        return True


def pending_for_user(root, owner_user_id, status="pending"):
    if db.is_configured():
        return db.pending_for_user(owner_user_id, status)
    d = _load(root, "pending.json")
    return sorted((it for it in d.values()
                   if it.get("owner_user_id") == owner_user_id and it.get("status") == status),
                  key=lambda it: it.get("created_at") or "", reverse=True)


def pending_get(root, pending_id):
    if db.is_configured():
        return db.pending_get(pending_id)
    return _load(root, "pending.json").get(str(pending_id))


def pending_set_status(root, pending_id, status):
    if db.is_configured():
        return db.pending_set_status(pending_id, status)
    with _LOCK:
        d = _load(root, "pending.json")
        it = d.get(str(pending_id))
        if it:
            it["status"] = status
            it["processed_at"] = _now_iso()
            _save(root, "pending.json", d)


def pending_count_for_user(root, owner_user_id):
    if db.is_configured():
        return db.pending_count_for_user(owner_user_id)
    d = _load(root, "pending.json")
    return sum(1 for it in d.values()
               if it.get("owner_user_id") == owner_user_id and it.get("status") == "pending")


# --- small utils -----------------------------------------------------------
def _safe(s):
    return "".join(c if c.isalnum() else "_" for c in str(s))


def _now_iso():
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(epoch):
    if not epoch:
        return None
    import datetime
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
