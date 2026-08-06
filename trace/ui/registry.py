"""Specs 2/3/4 — the durable per-user index of deployed agents, their schedules,
and their run history, with the same dual-run switch:

- when Supabase is configured, delegate to PostgREST via ``supabase_client``;
- otherwise (local dev / offline tests), persist to small JSON files under the
  sessions root.

Callers pass ``root`` (the sessions root) for the file-mode path; it is ignored
in Supabase mode. ``cadence`` is handled here as a Python dict (serialized to
JSON for storage) so callers never touch storage encoding.

Callers use the legacy dict shape with ``agent_id`` and ``owner_user_id`` keys;
this module translates to/from the Supabase column names (``id``, ``owner_id``).
"""

import json
import os
import threading
import time

from . import supabase_client as supa

_LOCK = threading.Lock()


# --- column mapping (caller shape ↔ Supabase shape) ------------------------

def _agent_to_caller(row):
    """Convert a Supabase agents row to the dict shape callers expect."""
    d = {
        "agent_id": str(row.get("id")),
        "owner_user_id": str(row.get("owner_id")) if row.get("owner_id") else None,
        "external_user_id": row.get("external_user_id"),
        "bundle_id": row.get("bundle_id"),
        "name": row.get("name"),
        "vault_ref": row.get("vault_ref"),
        "trigger": row.get("trigger"),
        "session_id": str(row.get("session_id")) if row.get("session_id") else None,
        "staged_apps": row.get("staged_apps") or [],
        "deployment_id": row.get("deployment_id"),
        "memory_store_id": row.get("memory_store_id"),
        "description": row.get("description"),
        "deployed_at": row.get("deployed_at") or row.get("created_at"),
    }
    return d


def _agent_to_sb(agent):
    """Convert a caller-shape agent dict to Supabase column names for upsert."""
    sb = {}
    if "agent_id" in agent:
        sb["id"] = agent["agent_id"]
    if "owner_user_id" in agent:
        sb["owner_id"] = agent["owner_user_id"]
    for key in ("external_user_id", "bundle_id", "name", "vault_ref", "trigger",
                "session_id", "deployment_id", "memory_store_id", "description"):
        if key in agent:
            sb[key] = agent[key]
    if "staged_apps" in agent:
        sb["staged_apps"] = list(agent.get("staged_apps") or [])
    return sb


def _run_to_caller(row):
    """Convert a Supabase agent_runs row to the dict shape callers expect."""
    return {
        "ran_at": row.get("started_at"),
        "status": row.get("status"),
        "summary": row.get("summary"),
        "details": row.get("details") or [],
        "run_session_id": row.get("run_session_id"),
        "drun_id": row.get("drun_id"),
    }


def _pending_to_caller(row):
    """Convert a Supabase pending_actions row to the dict shape callers expect."""
    return {
        "id": str(row.get("id")),
        "agent_id": str(row.get("agent_id")),
        "owner_user_id": str(row.get("owner_id")) if row.get("owner_id") else None,
        "run_session_id": row.get("run_session_id"),
        "app": row.get("app"),
        "kind": row.get("kind"),
        "summary": row.get("summary"),
        "payload": row.get("payload") or {},
        "content_hash": row.get("content_hash"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "processed_at": row.get("processed_at"),
    }


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
        json.dump(obj, f, default=str)
    os.replace(tmp, p)


# --- agents ----------------------------------------------------------------
def agent_upsert(root, agent):
    """agent: {agent_id, owner_user_id, external_user_id, bundle_id, name,
    vault_ref, trigger, session_id, ...}."""
    if supa.is_configured():
        admin = supa.get_admin()
        sb = _agent_to_sb(agent)
        admin.table("agents").upsert(sb, on_conflict="id").execute()
        return
    with _LOCK:
        d = _load(root, "agents.json")
        d[agent["agent_id"]] = {**agent, "deployed_at": _now_iso()}
        _save(root, "agents.json", d)


def agents_for_user(root, owner_user_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agents").select("*") \
            .eq("owner_id", owner_user_id) \
            .order("created_at", desc=True).execute()
        return [_agent_to_caller(row) for row in (r.data or [])]
    d = _load(root, "agents.json")
    return sorted((a for a in d.values() if a.get("owner_user_id") == owner_user_id),
                  key=lambda a: a.get("deployed_at") or "", reverse=True)


def agents_all(root):
    """Every deployed agent across all users (admin portal)."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agents").select("*") \
            .order("created_at", desc=True).execute()
        return [_agent_to_caller(row) for row in (r.data or [])]
    d = _load(root, "agents.json")
    return sorted(d.values(), key=lambda a: a.get("deployed_at") or "", reverse=True)


def agents_with_deployments(root):
    """Agents backed by a platform deployment (the sync loop's work list)."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agents").select("*") \
            .not_.is_("deployment_id", "null") \
            .order("created_at", desc=True).execute()
        return [_agent_to_caller(row) for row in (r.data or [])]
    d = _load(root, "agents.json")
    return sorted((a for a in d.values() if a.get("deployment_id")),
                  key=lambda a: a.get("deployed_at") or "", reverse=True)


def agent_set_deployment(root, agent_id, deployment_id):
    """Point one agent row at a new platform deployment."""
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("agents").update({"deployment_id": deployment_id}) \
            .eq("id", agent_id).execute()
        return
    with _LOCK:
        d = _load(root, "agents.json")
        if agent_id in d:
            d[agent_id]["deployment_id"] = deployment_id
            _save(root, "agents.json", d)


# --- schedules -------------------------------------------------------------
def schedule_set(root, agent_id, owner_user_id, cadence, paused=False):
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("schedules").upsert({
            "agent_id": agent_id,
            "owner_id": owner_user_id,
            "cadence": cadence,
            "paused": paused,
        }, on_conflict="agent_id").execute()
        return
    with _LOCK:
        d = _load(root, "schedules.json")
        prev = d.get(agent_id) or {}
        d[agent_id] = {"owner_user_id": owner_user_id, "cadence": cadence,
                       "paused": paused, "last_run_epoch": prev.get("last_run_epoch")}
        _save(root, "schedules.json", d)


def schedule_clear(root, agent_id):
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("schedules").delete().eq("agent_id", agent_id).execute()
        return
    with _LOCK:
        d = _load(root, "schedules.json")
        d.pop(agent_id, None)
        _save(root, "schedules.json", d)


def schedule_get(root, agent_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("schedules").select("cadence, paused, last_run_at") \
            .eq("agent_id", agent_id).maybe_single().execute()
        if not r.data:
            return None
        s = r.data
        return {"cadence": s["cadence"], "paused": s["paused"],
                "last_run_at": s.get("last_run_at")}
    s = _load(root, "schedules.json").get(agent_id)
    if not s:
        return None
    return {"cadence": s["cadence"], "paused": s.get("paused", False),
            "last_run_at": _iso(s.get("last_run_epoch"))}


def schedule_list_active(root):
    """Active (not paused) schedules joined with their agent row."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("schedules").select("*, agents(*)") \
            .eq("paused", False).execute()
        out = []
        for s in (r.data or []):
            agent_data = s.get("agents")
            if not agent_data:
                continue
            d = _agent_to_caller(agent_data)
            d["cadence"] = s["cadence"]
            lr = s.get("last_run_at")
            if lr:
                from datetime import datetime, timezone
                try:
                    dt = datetime.fromisoformat(lr.replace("Z", "+00:00"))
                    d["last_run_epoch"] = dt.timestamp()
                except (ValueError, TypeError):
                    d["last_run_epoch"] = None
            else:
                d["last_run_epoch"] = None
            out.append(d)
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
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("schedules").update({"last_run_at": _now_iso()}) \
            .eq("agent_id", agent_id).execute()
        return
    with _LOCK:
        d = _load(root, "schedules.json")
        if agent_id in d:
            d[agent_id]["last_run_epoch"] = time.time()
            _save(root, "schedules.json", d)


# --- runs ------------------------------------------------------------------
def run_add(root, rec):
    """rec: {agent_id, owner_user_id, status, summary, details, run_session_id, drun_id}."""
    if supa.is_configured():
        admin = supa.get_admin()
        row = {
            "agent_id": rec.get("agent_id"),
            "owner_id": rec.get("owner_user_id"),
            "status": rec.get("status"),
            "summary": rec.get("summary"),
            "details": rec.get("details") or [],
            "run_session_id": rec.get("run_session_id"),
            "drun_id": rec.get("drun_id"),
        }
        admin.table("agent_runs").insert(row).execute()
        return
    with _LOCK:
        p = os.path.join(_dir(root), f"runs_{_safe(rec['agent_id'])}.jsonl")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({**rec, "ran_at": _now_iso()}) + "\n")


def runs_for_agent(root, agent_id, limit=20):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agent_runs").select("*") \
            .eq("agent_id", agent_id) \
            .order("started_at", desc=True) \
            .limit(limit).execute()
        return [_run_to_caller(row) for row in (r.data or [])]
    p = os.path.join(_dir(root), f"runs_{_safe(agent_id)}.jsonl")
    try:
        with open(p, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
    return list(reversed(rows))[:limit]


def run_seen_drun_ids(root, agent_id):
    """The deployment_run ids already recorded for one agent (sync-loop dedupe)."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agent_runs").select("drun_id") \
            .eq("agent_id", agent_id) \
            .not_.is_("drun_id", "null").execute()
        return {row["drun_id"] for row in (r.data or [])}
    p = os.path.join(_dir(root), f"runs_{_safe(agent_id)}.jsonl")
    try:
        with open(p, encoding="utf-8") as f:
            return {json.loads(line).get("drun_id") for line in f if line.strip()} - {None}
    except FileNotFoundError:
        return set()


# --- pending (staged) actions — Spec 8 -------------------------------------
def pending_add(root, rec):
    """Deduped on (agent_id, content_hash) against pending/executed items.
    Returns True if inserted, False if deduped."""
    if supa.is_configured():
        admin = supa.get_admin()
        # Check for existing
        r = admin.table("pending_actions").select("id") \
            .eq("agent_id", rec.get("agent_id")) \
            .eq("content_hash", rec.get("content_hash")) \
            .in_("status", ["pending", "executed"]) \
            .limit(1).execute()
        if r.data:
            return False
        admin.table("pending_actions").insert({
            "agent_id": rec.get("agent_id"),
            "owner_id": rec.get("owner_user_id"),
            "run_session_id": rec.get("run_session_id"),
            "app": rec.get("app"),
            "kind": rec.get("kind"),
            "summary": rec.get("summary"),
            "payload": rec.get("payload") or {},
            "content_hash": rec.get("content_hash"),
        }).execute()
        return True
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
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("pending_actions").select("*") \
            .eq("owner_id", owner_user_id) \
            .eq("status", status) \
            .order("created_at", desc=True).execute()
        return [_pending_to_caller(row) for row in (r.data or [])]
    d = _load(root, "pending.json")
    return sorted((it for it in d.values()
                   if it.get("owner_user_id") == owner_user_id and it.get("status") == status),
                  key=lambda it: it.get("created_at") or "", reverse=True)


def pending_get(root, pending_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("pending_actions").select("*") \
            .eq("id", str(pending_id)).maybe_single().execute()
        return _pending_to_caller(r.data) if r.data else None
    return _load(root, "pending.json").get(str(pending_id))


def pending_set_status(root, pending_id, status):
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("pending_actions").update({
            "status": status, "processed_at": _now_iso()
        }).eq("id", str(pending_id)).execute()
        return
    with _LOCK:
        d = _load(root, "pending.json")
        it = d.get(str(pending_id))
        if it:
            it["status"] = status
            it["processed_at"] = _now_iso()
            _save(root, "pending.json", d)


def pending_count_for_user(root, owner_user_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("pending_actions").select("id", count="exact") \
            .eq("owner_id", owner_user_id) \
            .eq("status", "pending").execute()
        return r.count or 0
    d = _load(root, "pending.json")
    return sum(1 for it in d.values()
               if it.get("owner_user_id") == owner_user_id and it.get("status") == "pending")


# --- story review marks ------------------------------------------------------
def review_mark(root, agent_id, owner_user_id, run_key):
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("run_reviews").upsert({
            "agent_id": agent_id,
            "owner_id": owner_user_id,
            "run_key": run_key,
        }, on_conflict="agent_id,run_key").execute()
        return
    with _LOCK:
        d = _load(root, "run_reviews.json")
        d.setdefault(agent_id, {})[run_key] = _now_iso()
        _save(root, "run_reviews.json", d)


def reviewed_keys(root, agent_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("run_reviews").select("run_key") \
            .eq("agent_id", agent_id).execute()
        return {row["run_key"] for row in (r.data or [])}
    return set((_load(root, "run_reviews.json").get(agent_id) or {}).keys())


# --- agent direct messages (Instagram-style "My agents") --------------------
def message_add(root, rec):
    """rec: {agent_id, owner_user_id, sender ('user'|'agent'), text, read?}."""
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("agent_messages").insert({
            "agent_id": rec.get("agent_id"),
            "owner_id": rec.get("owner_user_id"),
            "sender": rec.get("sender"),
            "text": rec.get("text"),
            "read": bool(rec.get("read", False)),
        }).execute()
        return
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
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agent_messages").select("*") \
            .eq("owner_id", owner_user_id) \
            .eq("agent_id", agent_id) \
            .order("created_at", desc=True) \
            .order("id", desc=True) \
            .limit(limit).execute()
        rows = r.data or []
        out = []
        for row in reversed(rows):
            out.append({
                "id": str(row["id"]),
                "sender": row["sender"],
                "text": row["text"],
                "read": bool(row["read"]),
                "created_at": row["created_at"],
            })
        return out
    d = _load(root, "messages.json")
    mine = [m for m in d.values()
            if m.get("owner_user_id") == owner_user_id and m.get("agent_id") == agent_id]
    mine.sort(key=lambda m: (m.get("created_at") or "", int(m.get("id") or 0)))
    return mine[-limit:]


def messages_mark_read(root, owner_user_id, agent_id):
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("agent_messages").update({"read": True}) \
            .eq("owner_id", owner_user_id) \
            .eq("agent_id", agent_id) \
            .eq("sender", "agent").execute()
        return
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
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agent_messages").select("agent_id") \
            .eq("owner_id", owner_user_id) \
            .eq("sender", "agent") \
            .eq("read", False).execute()
        out = {}
        for row in (r.data or []):
            aid = str(row["agent_id"])
            out[aid] = out.get(aid, 0) + 1
        return out
    d = _load(root, "messages.json")
    out = {}
    for m in d.values():
        if (m.get("owner_user_id") == owner_user_id and m.get("sender") == "agent"
                and not m.get("read")):
            out[m["agent_id"]] = out.get(m["agent_id"], 0) + 1
    return out


# --- session-summary cache (activity view) ---------------------------------
def summary_get(root, session_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("session_summaries").select("summary, status") \
            .eq("session_id", session_id).maybe_single().execute()
        return {"summary": r.data["summary"], "status": r.data["status"]} if r.data else None
    return _load(root, "session_summaries.json").get(session_id)


def summary_set(root, session_id, summary, status):
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("session_summaries").upsert({
            "session_id": session_id,
            "summary": summary,
            "status": status,
        }, on_conflict="session_id").execute()
        return
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
