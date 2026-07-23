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
        json.dump(obj, f, default=str)          # default=str: a non-JSON-native payload can't crash persistence
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


def agents_all(root):
    """Every deployed agent across all users (admin portal)."""
    if db.is_configured():
        return db.agents_all()
    d = _load(root, "agents.json")
    return sorted(d.values(), key=lambda a: a.get("deployed_at") or "", reverse=True)


def agents_with_deployments(root):
    """Agents backed by a platform deployment (the sync loop's work list)."""
    if db.is_configured():
        return db.agents_with_deployments()
    d = _load(root, "agents.json")
    return sorted((a for a in d.values() if a.get("deployment_id")),
                  key=lambda a: a.get("deployed_at") or "", reverse=True)


def agent_set_deployment(root, agent_id, deployment_id):
    """Point one agent row at a new platform deployment (schedule changes recreate it)."""
    if db.is_configured():
        return db.agent_set_deployment(agent_id, deployment_id)
    with _LOCK:
        d = _load(root, "agents.json")
        if agent_id in d:
            d[agent_id]["deployment_id"] = deployment_id
            _save(root, "agents.json", d)


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


def run_seen_drun_ids(root, agent_id):
    """The deployment_run ids already recorded for one agent (sync-loop dedupe)."""
    if db.is_configured():
        return db.run_seen_drun_ids(agent_id)
    p = os.path.join(_dir(root), f"runs_{_safe(agent_id)}.jsonl")
    try:
        with open(p, encoding="utf-8") as f:
            return {json.loads(line).get("drun_id") for line in f if line.strip()} - {None}
    except FileNotFoundError:
        return set()


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


# --- agent direct messages (Instagram-style "My agents") --------------------
def message_add(root, rec):
    """rec: {agent_id, owner_user_id, sender ('user'|'agent'), text, read?}."""
    if db.is_configured():
        return db.message_add(rec)
    with _LOCK:
        d = _load(root, "messages.json")
        nid = str(max((int(k) for k in d), default=0) + 1)
        d[nid] = {"id": nid, "agent_id": rec.get("agent_id"),
                  "owner_user_id": rec.get("owner_user_id"),
                  "sender": rec.get("sender"), "text": rec.get("text"),
                  "read": bool(rec.get("read", False)), "created_at": _now_iso()}
        _save(root, "messages.json", d)


def messages_for_agent(root, owner_user_id, agent_id, limit=50):
    """One thread, oldest-first."""
    if db.is_configured():
        return db.messages_for_agent(owner_user_id, agent_id, limit)
    d = _load(root, "messages.json")
    mine = [m for m in d.values()
            if m.get("owner_user_id") == owner_user_id and m.get("agent_id") == agent_id]
    mine.sort(key=lambda m: (m.get("created_at") or "", int(m.get("id") or 0)))
    return mine[-limit:]


def messages_mark_read(root, owner_user_id, agent_id):
    if db.is_configured():
        return db.messages_mark_read(owner_user_id, agent_id)
    with _LOCK:
        d = _load(root, "messages.json")
        changed = False
        for m in d.values():
            if (m.get("owner_user_id") == owner_user_id and m.get("agent_id") == agent_id
                    and m.get("sender") == "agent" and not m.get("read")):
                m["read"] = True
                changed = True
        if changed:
            _save(root, "messages.json", d)


def messages_unread_counts(root, owner_user_id):
    """{agent_id: unread agent-sent count}."""
    if db.is_configured():
        return db.messages_unread_counts(owner_user_id)
    d = _load(root, "messages.json")
    out = {}
    for m in d.values():
        if (m.get("owner_user_id") == owner_user_id and m.get("sender") == "agent"
                and not m.get("read")):
            out[m["agent_id"]] = out.get(m["agent_id"], 0) + 1
    return out


# --- session-summary cache (activity view) ---------------------------------
def summary_get(root, session_id):
    if db.is_configured():
        return db.summary_get(session_id)
    return _load(root, "session_summaries.json").get(session_id)


def summary_set(root, session_id, summary, status):
    if db.is_configured():
        return db.summary_set(session_id, summary, status)
    with _LOCK:
        d = _load(root, "session_summaries.json")
        d[session_id] = {"summary": summary, "status": status, "created_at": _now_iso()}
        _save(root, "session_summaries.json", d)


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
