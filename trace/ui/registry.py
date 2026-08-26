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
        "owner_email": row.get("owner_email"),
        "description": row.get("description"),
        "deployed_at": row.get("deployed_at") or row.get("created_at"),
        # Without this the column is written and never read back: agent_color() falls
        # through to the deterministic default every time, so a user's chosen colour has
        # no effect anywhere in Supabase mode. File-mode returns the raw stored dict, so
        # it kept working and the tests never caught it.
        "color": row.get("color"),
        # Same omission as color above, with worse consequences: agent_set_consent WRITES
        # these (008 added the columns, NULL = inherit) and nothing read them back, so
        # agent_lesson_scopes always fell through to the account default. A per-agent
        # override could be set, stored, shown — and never actually applied.
        "lesson_contribute_scope": row.get("lesson_contribute_scope"),
        "lesson_consume_scope": row.get("lesson_consume_scope"),
    }
    return d


def _agent_to_sb(agent):
    """Convert a caller-shape agent dict to Supabase column names for upsert."""
    sb = {}
    if "agent_id" in agent:
        sb["id"] = agent["agent_id"]
    if "owner_user_id" in agent:
        sb["owner_id"] = agent["owner_user_id"]
    # An allow-list silently drops whatever is not on it, and status/deployed_at were
    # not — so in Supabase mode a deploy wrote its deployment_id and nothing about
    # having deployed. Every agent read back as status 'draft' with a null deployed_at
    # forever, which is why "which of this customer's agents are actually live?" had no
    # answer. File mode stamps deployed_at on its own line below and so looked fine,
    # which is exactly how this survived the suite.
    for key in ("external_user_id", "bundle_id", "name", "vault_ref", "trigger",
                "session_id", "deployment_id", "memory_store_id", "owner_email",
                "description", "status", "deployed_at"):
        if key in agent:
            sb[key] = agent[key]
    if "staged_apps" in agent:
        sb["staged_apps"] = list(agent.get("staged_apps") or [])
    return sb


def _run_to_caller(row):
    """Convert a Supabase agent_runs row to the dict shape callers expect."""
    return {
        "ran_at": row.get("started_at"),
        # Both ends, not just the start. agent_runs has recorded ended_at all along and
        # this mapping quietly dropped it, so run DURATION — the figure frame 21's
        # "takes ~9 min" needs, and the only honest basis for any time claim on the
        # profile — was unavailable to every caller despite sitting in the row.
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "status": row.get("status"),
        "summary": row.get("summary"),
        "details": row.get("details") or [],
        "run_session_id": row.get("run_session_id"),
        "drun_id": row.get("drun_id"),
        "kind": row.get("kind"),
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
        "rationale": row.get("rationale") or "",
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


def agents_visible_to(root, owner_user_id, team_owner_ids=None):
    """Agents this user may SEE: their own, plus their teammates', read-only.

    Deliberately a separate function from ``agents_for_user`` rather than a widened
    one. agents_for_user IS the ownership check — a dozen mutating endpoints are
    written as ``next(a for a in agents_for_user(...) if a.agent_id == x)`` and 404
    when it misses. Widening it to include teammates would hand every one of those
    endpoints write access to other people's agents, silently, with no line of code
    appearing to change permissions. So visibility gets its own door.

    A teammate's agent comes back with ``shared_from`` (whose it is) and
    ``can_edit: False``. Those are for rendering; they are NOT the security boundary,
    which remains that mutations resolve through agents_for_user and cannot see these
    rows at all.
    """
    mine = agents_for_user(root, owner_user_id)
    mates = [o for o in (team_owner_ids or []) if o and o != owner_user_id]
    if not mates:
        return mine
    seen = {a.get("agent_id") for a in mine}
    out = list(mine)
    for owner in mates:
        for a in agents_for_user(root, owner):
            if a.get("agent_id") in seen:
                continue
            seen.add(a.get("agent_id"))
            out.append({**a, "shared_from": a.get("owner_email") or owner,
                        "can_edit": False})
    return out


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


def set_staged_apps(root, agent_id, staged_apps):
    """Update which apps are staged (held for approval) for one agent. Staging is
    prompt-level and read fresh each run (see runner.run_once), so this takes effect on
    the next run — no redeploy. ``staged_apps`` is a list of app slugs (may be empty =
    fully autonomous)."""
    slugs = [s for s in (staged_apps or []) if s]
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("agents").update({"staged_apps": slugs}) \
            .eq("id", agent_id).execute()
        return
    with _LOCK:
        d = _load(root, "agents.json")
        if agent_id in d:
            d[agent_id]["staged_apps"] = slugs
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
            .eq("agent_id", agent_id).maybe_single().execute() or supa.NO_ROW
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
    """rec: {agent_id, owner_user_id, status, summary, details, run_session_id, drun_id,
    kind}. kind="approval" marks a run whose session EXECUTED already-approved actions
    (rather than proposing new ones) — the activity view must not mine those sessions
    for proposals."""
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
        if rec.get("kind"):                                # column arrives with 019
            row["kind"] = rec["kind"]
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
    """Deduped on (agent_id, content_hash) against ANY existing item: once a proposal
    has been surfaced, it never re-stages, whatever its outcome — the source session's
    transcript is re-read on every activity view, and a rejected or failed decision
    must not resurrect from it. Returns True if inserted, False if deduped."""
    if supa.is_configured():
        admin = supa.get_admin()
        # Check for existing
        r = admin.table("pending_actions").select("id") \
            .eq("agent_id", rec.get("agent_id")) \
            .eq("content_hash", rec.get("content_hash")) \
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
            "rationale": rec.get("rationale") or "",
            "payload": rec.get("payload") or {},
            "content_hash": rec.get("content_hash"),
        }).execute()
        return True
    with _LOCK:
        d = _load(root, "pending.json")
        for it in d.values():
            if (it.get("agent_id") == rec.get("agent_id")
                    and it.get("content_hash") == rec.get("content_hash")):
                return False
        nid = str(max((int(k) for k in d), default=0) + 1)
        d[nid] = {**rec, "id": nid, "status": "pending",
                  "created_at": _now_iso(), "processed_at": None}
        _save(root, "pending.json", d)
        return True


def pending_for_user(root, owner_user_id, status="pending"):
    """status=None returns every status — the activity view renders decided cards
    (executed/rejected/failed) alongside pending ones, not just the live queue."""
    if supa.is_configured():
        admin = supa.get_admin()
        q = admin.table("pending_actions").select("*") \
            .eq("owner_id", owner_user_id)
        if status:
            q = q.eq("status", status)
        r = q.order("created_at", desc=True).execute()
        return [_pending_to_caller(row) for row in (r.data or [])]
    d = _load(root, "pending.json")
    return sorted((it for it in d.values()
                   if it.get("owner_user_id") == owner_user_id
                   and (status is None or it.get("status") == status)),
                  key=lambda it: it.get("created_at") or "", reverse=True)


def pending_get(root, pending_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("pending_actions").select("*") \
            .eq("id", str(pending_id)).maybe_single().execute() or supa.NO_ROW
        return _pending_to_caller(r.data) if r.data else None
    return _load(root, "pending.json").get(str(pending_id))


def pending_claim(root, pending_id, owner_user_id, to_status="executing"):
    """Atomically move one of this owner's rows from "pending" to ``to_status`` and
    return it, or None if it wasn't claimable (already claimed, already decided, or
    not theirs). The status predicate makes the claim exclusive — of two racing
    approvals exactly one gets the row, so the same action can't be sent twice.
    ``to_status="queued"`` claims into the approval queue (the send happens later,
    when the agent's worker reaches it) without starting the stale-executing clock."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("pending_actions").update({
            "status": to_status, "processed_at": _now_iso()
        }).eq("id", str(pending_id)) \
            .eq("owner_id", owner_user_id) \
            .eq("status", "pending").execute()
        return _pending_to_caller(r.data[0]) if r.data else None
    with _LOCK:
        d = _load(root, "pending.json")
        it = d.get(str(pending_id))
        if (not it or it.get("owner_user_id") != owner_user_id
                or it.get("status") != "pending"):
            return None
        it["status"] = to_status
        it["processed_at"] = _now_iso()
        _save(root, "pending.json", d)
        return dict(it)


# The human's reason for rejecting, kept inside the payload JSON under a reserved key.
# There is no column for it, and `rationale` is the AGENT's reason for proposing — a
# different thing by a different author. This avoids a migration for one string.
REJECT_REASON_KEY = "_reject_reason"


def pending_set_status(root, pending_id, status, reason=None):
    """Set a pending action's status, optionally recording WHY it was rejected.

    The proposal card promises "Reject asks one line: why?" and nothing ever captured
    one, so a rejection told the agent an action was unwanted and never what was wrong
    with it — which is the half that could have changed the next proposal.
    """
    reason = str(reason or "").strip()[:500]
    if supa.is_configured():
        admin = supa.get_admin()
        fields = {"status": status, "processed_at": _now_iso()}
        if reason:
            cur = pending_get(root, pending_id) or {}
            fields["payload"] = dict(cur.get("payload") or {},
                                     **{REJECT_REASON_KEY: reason})
        admin.table("pending_actions").update(fields).eq("id", str(pending_id)).execute()
        return
    with _LOCK:
        d = _load(root, "pending.json")
        it = d.get(str(pending_id))
        if it:
            it["status"] = status
            it["processed_at"] = _now_iso()
            if reason:
                it["payload"] = dict(it.get("payload") or {},
                                     **{REJECT_REASON_KEY: reason})
            _save(root, "pending.json", d)


def pending_release_queued(root):
    """Boot recovery: a "queued" row belongs to an in-memory approval queue that no
    longer exists after a restart, and its action was provably never sent — release
    it back to "pending" so the approve button honestly reappears. Never touches
    "executing" rows (outcome unknown; the stale reaper owns those — an auto-requeue
    could double-send)."""
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("pending_actions").update({
            "status": "pending", "processed_at": None
        }).eq("status", "queued").execute()
        return
    with _LOCK:
        d = _load(root, "pending.json")
        changed = False
        for it in d.values():
            if it.get("status") == "queued":
                it["status"], it["processed_at"], changed = "pending", None, True
        if changed:
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


# --- agent decisions (Gap #3: uncertainty questions the agent paused on) -----
def _decision_to_caller(row):
    """Convert a Supabase agent_decisions row to the dict shape callers expect."""
    return {
        "id": str(row.get("id")),
        "agent_id": str(row.get("agent_id")),
        "owner_user_id": str(row.get("owner_id")) if row.get("owner_id") else None,
        "run_session_id": row.get("run_session_id"),
        "question": row.get("question"),
        "answer": row.get("answer"),
        "context": row.get("context") or {},
        "tags": row.get("tags") or {},
        "reusable": bool(row.get("reusable")),
        "generalized_lesson": row.get("generalized_lesson"),
        "share_scope": row.get("share_scope") or "owner",
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "answered_at": row.get("answered_at"),
    }


def decision_add(root, rec):
    """Persist one question the agent paused on. Deduped on
    (agent_id, run_session_id, question) against pending/answered rows so re-reading a
    session never re-stages the same question. Returns True if inserted, else False."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agent_decisions").select("id") \
            .eq("agent_id", rec.get("agent_id")) \
            .eq("run_session_id", rec.get("run_session_id")) \
            .eq("question", rec.get("question")) \
            .in_("status", ["pending", "answered"]) \
            .limit(1).execute()
        if r.data:
            return False
        admin.table("agent_decisions").insert({
            "agent_id": rec.get("agent_id"),
            "owner_id": rec.get("owner_user_id"),
            "run_session_id": rec.get("run_session_id"),
            "question": rec.get("question"),
            "context": rec.get("context") or {},
            "tags": rec.get("tags") or {},
            "share_scope": rec.get("share_scope") or "owner",
        }).execute()
        return True
    with _LOCK:
        d = _load(root, "decisions.json")
        for it in d.values():
            if (it.get("agent_id") == rec.get("agent_id")
                    and it.get("run_session_id") == rec.get("run_session_id")
                    and it.get("question") == rec.get("question")
                    and it.get("status") in ("pending", "answered")):
                return False
        nid = str(max((int(k) for k in d), default=0) + 1)
        d[nid] = {**rec, "id": nid, "answer": None, "reusable": False,
                  "generalized_lesson": None, "share_scope": rec.get("share_scope") or "owner",
                  "status": "pending", "created_at": _now_iso(), "answered_at": None}
        _save(root, "decisions.json", d)
        return True


def decisions_for_user(root, owner_user_id, status=None):
    if supa.is_configured():
        admin = supa.get_admin()
        q = admin.table("agent_decisions").select("*").eq("owner_id", owner_user_id)
        if status:
            q = q.eq("status", status)
        r = q.order("created_at", desc=True).execute()
        return [_decision_to_caller(row) for row in (r.data or [])]
    d = _load(root, "decisions.json")
    out = [it for it in d.values()
           if it.get("owner_user_id") == owner_user_id
           and (status is None or it.get("status") == status)]
    return sorted(out, key=lambda it: it.get("created_at") or "", reverse=True)


def decision_get(root, decision_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agent_decisions").select("*") \
            .eq("id", str(decision_id)).maybe_single().execute() or supa.NO_ROW
        return _decision_to_caller(r.data) if r.data else None
    return _load(root, "decisions.json").get(str(decision_id))


def decision_answer(root, decision_id, answer):
    """Record the human's answer and flip the decision to ``answered``."""
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("agent_decisions").update({
            "answer": answer, "status": "answered", "answered_at": _now_iso()
        }).eq("id", str(decision_id)).execute()
        return
    with _LOCK:
        d = _load(root, "decisions.json")
        it = d.get(str(decision_id))
        if it:
            it["answer"] = answer
            it["status"] = "answered"
            it["answered_at"] = _now_iso()
            _save(root, "decisions.json", d)


def decision_set_status(root, decision_id, status):
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("agent_decisions").update({"status": status}) \
            .eq("id", str(decision_id)).execute()
        return
    with _LOCK:
        d = _load(root, "decisions.json")
        it = d.get(str(decision_id))
        if it:
            it["status"] = status
            _save(root, "decisions.json", d)


def decision_set_provenance(root, decision_id, source_apps=None, source_team_id=None):
    """Record WHERE a lesson came from: the apps its source agent touches, and the org.

    Applicability is currently decided by cosine between two agent descriptions, which
    compares how agents were WRITTEN rather than what they do. Shared apps are a fact —
    an agent either touches Shopify or it does not — and a rule learned in an app is
    likelier to hold for someone else in that app than for someone whose description
    merely reads alike. The org is here so corroboration can count distinct
    ORGANISATIONS later; two people at one company are one observation, not two.

    Best-effort and never raises: this is provenance, not correctness, and a failure
    here must not cost the lesson itself.
    """
    fields = {}
    if source_apps is not None:
        fields["source_apps"] = list(source_apps)
    if source_team_id is not None:
        fields["source_team_id"] = source_team_id
    if not fields:
        return
    try:
        if supa.is_configured():
            supa.get_admin().table("agent_decisions").update(fields) \
                .eq("id", str(decision_id)).execute()
            return
        with _LOCK:
            d = _load(root, "decisions.json")
            if str(decision_id) in d:
                d[str(decision_id)].update(fields)
                _save(root, "decisions.json", d)
    except Exception:                                      # noqa: BLE001
        pass


def decisions_source_apps(root, decision_ids):
    """``{decision_id: [app slugs]}`` for the given decisions — the apps each lesson was
    learned in. Missing or unrecorded comes back absent, and callers must treat that as
    "unknown", never as "no apps": every row written before migration 020 is unrecorded,
    and dropping those would silently disable sharing for the entire existing corpus."""
    ids = [str(i) for i in (decision_ids or []) if i]
    if not ids:
        return {}
    try:
        if supa.is_configured():
            r = supa.get_admin().table("agent_decisions") \
                .select("id,source_apps").in_("id", ids).execute()
            return {str(row["id"]): list(row.get("source_apps") or [])
                    for row in (r.data or []) if row.get("source_apps")}
        d = _load(root, "decisions.json")
        return {i: list(d[i]["source_apps"]) for i in ids
                if i in d and d[i].get("source_apps")}
    except Exception:                                      # noqa: BLE001
        return {}


def decision_set_generalization(root, decision_id, reusable, lesson):
    """Record the distilled reusable lesson (Gap #3 B3). ``reusable`` gates whether the
    lesson is eligible for fleet retrieval; ``lesson`` is the de-identified rule text."""
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("agent_decisions").update({
            "reusable": bool(reusable), "generalized_lesson": lesson
        }).eq("id", str(decision_id)).execute()
        return
    with _LOCK:
        d = _load(root, "decisions.json")
        it = d.get(str(decision_id))
        if it:
            it["reusable"] = bool(reusable)
            it["generalized_lesson"] = lesson
            _save(root, "decisions.json", d)


def decision_set_global_verified(root, decision_id, verified):
    """Gate whether a lesson may be retrieved by OTHER users (B5d): set only when the
    scrub-verify pass passed AND the source consented to share globally."""
    if supa.is_configured():
        supa.get_admin().table("agent_decisions").update(
            {"global_verified": bool(verified)}).eq("id", str(decision_id)).execute()
        return
    with _LOCK:
        d = _load(root, "decisions.json")
        it = d.get(str(decision_id))
        if it:
            it["global_verified"] = bool(verified)
            _save(root, "decisions.json", d)


def decision_embedding_add(root, decision_id, embedding):
    """Store the lesson's embedding for similarity search. Supabase writes the
    service-role-only decision_embeddings table (upsert-by-decision so a re-answer replaces
    it); file-mode keeps a decision_embeddings.json keyed by decision id."""
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("decision_embeddings").delete() \
            .eq("decision_id", str(decision_id)).execute()
        admin.table("decision_embeddings").insert({
            "decision_id": str(decision_id), "embedding": embedding}).execute()
        return
    with _LOCK:
        d = _load(root, "decision_embeddings.json")
        d[str(decision_id)] = embedding
        _save(root, "decision_embeddings.json", d)


def decision_embedding_get(root, decision_id):
    """The stored embedding for one decision (file-mode helper / tests), or None."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("decision_embeddings").select("embedding") \
            .eq("decision_id", str(decision_id)).maybe_single().execute() or supa.NO_ROW
        return (r.data or {}).get("embedding") if r.data else None
    return _load(root, "decision_embeddings.json").get(str(decision_id))


def _cosine(a, b):
    """Cosine similarity of two equal-length vectors; 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def match_decisions(root, query_embedding, owner_user_id, k=3, min_similarity=0.5):
    """Gap #3 B4 retrieval: up to ``k`` de-identified reusable lessons most similar to
    ``query_embedding``, scoped to the owner (T0/T1). Returns lesson strings, best first.
    Supabase: the match_decisions RPC (cosine over pgvector). File-mode: Python cosine over
    decision_embeddings.json. Never raises — returns [] on any error."""
    if not query_embedding:
        return []
    try:
        if supa.is_configured():
            admin = supa.get_admin()
            r = admin.rpc("match_decisions", {
                "query_embedding": query_embedding, "owner": owner_user_id,
                "match_count": k, "min_similarity": min_similarity}).execute()
            return [row.get("generalized_lesson") for row in (r.data or [])
                    if row.get("generalized_lesson")]
        embs = _load(root, "decision_embeddings.json")
        decs = _load(root, "decisions.json")
        scored = []
        for did, emb in embs.items():
            dec = decs.get(did)
            if not dec or not dec.get("reusable") or not dec.get("generalized_lesson"):
                continue
            if dec.get("owner_user_id") != owner_user_id:
                continue
            sim = _cosine(query_embedding, emb)
            if sim >= min_similarity:
                scored.append((sim, dec["generalized_lesson"]))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [lesson for _, lesson in scored[:k]]
    except Exception:                                      # noqa: BLE001 — retrieval is best-effort
        return []


def _lesson_hit(row):
    return {"decision_id": str(row.get("decision_id")), "lesson": row.get("generalized_lesson"),
            "source_owner_id": str(row.get("owner_id")) if row.get("owner_id") else None,
            "source_agent_id": str(row.get("agent_id")) if row.get("agent_id") else None,
            "similarity": row.get("similarity"), "tier": row.get("tier")}


def retrieve_lessons(root, situation_embedding, agent, agent_kind_embedding=None,
                     consume_scope="global", k=5, min_situation=0.5,
                     min_cross_situation=0.65, min_agent_kind=0.6,
                     team_owners=None, min_team_situation=0.58):
    """Phase B (B5b) double-gated retrieval. Reusable lessons whose SITUATION matches the
    query AND — for cross-owner lessons — whose SOURCE AGENT is similar-kind to ``agent``.
    ``consume_scope`` gates breadth: 'owner' = own only; 'global' = all owners; 'team' is
    treated as own-only until team plumbing lands. Each hit is
    ``{decision_id, lesson, source_owner_id, source_agent_id, similarity, tier}`` with tier
    'own' (same owner, no kind gate, lower situation bar) or 'cross' (different owner,
    kind-gated, higher bar). Best-effort — returns [] on any error."""
    if not situation_embedding:
        return []
    owner = agent.get("owner_user_id")
    allow_cross = consume_scope == "global"
    # Teammates' lessons are in scope at 'team' AND at 'global' — 'global' is the wider
    # setting, so it cannot exclude the narrower one. Empty list when the user has no
    # team, which is byte-for-byte the old behaviour.
    mates = [m for m in (team_owners or []) if m and m != owner] \
        if consume_scope in ("team", "global") else []
    try:
        if supa.is_configured():
            r = supa.get_admin().rpc("match_lessons", {
                "situation_embedding": situation_embedding, "owner": owner,
                "agent_kind_embedding": agent_kind_embedding, "allow_cross": allow_cross,
                "min_situation": min_situation, "min_cross_situation": min_cross_situation,
                "min_agent_kind": min_agent_kind, "match_count": k,
                # 018's parameters. Both default in SQL, so an unapplied 018 just ignores
                # them rather than erroring — adopt-early is safe.
                "team_owners": mates or None,
                "min_team_situation": min_team_situation}).execute()
            return [_lesson_hit(row) for row in (r.data or [])]
        embs = _load(root, "decision_embeddings.json")
        decs = _load(root, "decisions.json")
        aembs = _load(root, "agent_embeddings.json")
        hits = []
        for did, sit_emb in embs.items():
            dec = decs.get(did)
            if not dec or not dec.get("reusable") or not dec.get("generalized_lesson"):
                continue
            src_owner = dec.get("owner_user_id")
            is_own = src_owner == owner
            # This branch mirrors match_lessons by hand, so the team tier has to be added
            # in BOTH places or the offline path and Supabase disagree about who can see
            # what — which is the worst kind of divergence to have in a sharing gate.
            is_mate = not is_own and src_owner in mates
            if not is_own and not is_mate and not allow_cross:
                continue                                   # scope gate
            sit = _cosine(situation_embedding, sit_emb)
            if is_own:
                if sit < min_situation:
                    continue
            elif is_mate:
                # 018's team tier: no global_verified (sharing with colleagues must not
                # require publishing to strangers), no kind gate (the context is already
                # shared), and a bar between own and cross. The source must still have
                # chosen to contribute at team scope or wider.
                if sit < min_team_situation:
                    continue
                src_scopes = agent_lesson_scopes(root, {"owner_user_id": src_owner})
                if src_scopes["contribute_scope"] not in ("team", "global"):
                    continue
            else:                                          # cross-owner: higher bar + gates
                if not dec.get("global_verified"):
                    continue                               # scrub-verified + source consented only
                if sit < min_cross_situation:
                    continue
                src_kind = aembs.get(str(dec.get("agent_id")))
                if not agent_kind_embedding or not src_kind:
                    continue                               # can't verify kind -> don't leak
                if _cosine(agent_kind_embedding, src_kind) < min_agent_kind:
                    continue
            hits.append({"decision_id": did, "lesson": dec["generalized_lesson"],
                         "source_owner_id": src_owner, "source_agent_id": dec.get("agent_id"),
                         "similarity": sit,
                         "tier": "own" if is_own else ("team" if is_mate else "cross")})
        hits.sort(key=lambda h: h["similarity"], reverse=True)
        return hits[:k]
    except Exception:                                      # noqa: BLE001
        return []


# --- Phase B (B5): consent, promotions, provenance, agent-kind embeddings -----
_VALID_SCOPES = ("owner", "team", "global")


def user_consent_get(root, uid):
    """Per-user lesson consent (account-wide default). Opt-out posture -> defaults 'global'.
    Returns ``{contribute_scope, consume_scope}``."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("profiles") \
            .select("lesson_contribute_scope, lesson_consume_scope") \
            .eq("id", uid).maybe_single().execute() or supa.NO_ROW
        d = r.data or {}
    else:
        d = _load(root, "consents.json").get(str(uid)) or {}
    return {"contribute_scope": d.get("lesson_contribute_scope") or d.get("contribute_scope") or "global",
            "consume_scope": d.get("lesson_consume_scope") or d.get("consume_scope") or "global"}


# --- first-run state (PALZA_REDESIGN §3) -------------------------------------
def user_flags_get(root, uid):
    """``{terms_accepted_at, first_run_completed_at}`` for one account.

    Reads defensively: if migration 015 hasn't been applied the columns don't exist and
    PostgREST errors, which must not break sign-in — an account then simply looks like a
    first run, which is the safe direction.
    """
    if supa.is_configured():
        try:
            r = supa.get_admin().table("profiles") \
                .select("terms_accepted_at, first_run_completed_at") \
                .eq("id", uid).maybe_single().execute() or supa.NO_ROW
            d = r.data or {}
        except Exception:                                  # noqa: BLE001 — pre-migration
            d = {}
    else:
        d = _load(root, "user_flags.json").get(str(uid)) or {}
    return {"terms_accepted_at": d.get("terms_accepted_at"),
            "first_run_completed_at": d.get("first_run_completed_at")}


def user_flags_set(root, uid, **fields):
    """Stamp first-run fields. Accepts terms_accepted_at / first_run_completed_at."""
    allowed = {k: v for k, v in fields.items()
               if k in ("terms_accepted_at", "first_run_completed_at")}
    if not allowed:
        return
    if supa.is_configured():
        try:
            supa.get_admin().table("profiles").update(allowed).eq("id", uid).execute()
        except Exception:                                  # noqa: BLE001 — pre-migration
            pass
        return
    with _LOCK:
        d = _load(root, "user_flags.json")
        d.setdefault(str(uid), {}).update(allowed)
        _save(root, "user_flags.json", d)


def user_profile_get(root, uid):
    """Frame 18's Account pane: the human name and organisation, both from 017.

    Read defensively like the other profile reads — an unapplied migration must not
    break Settings, it should just show blanks.
    """
    if supa.is_configured():
        try:
            r = supa.get_admin().table("profiles").select("display_name, org_name") \
                .eq("id", uid or "").maybe_single().execute() or supa.NO_ROW
            d = r.data or {}
        except Exception:                                  # noqa: BLE001 — pre-migration
            d = {}
    else:
        d = (_load(root, "user_profile.json").get(str(uid)) or {})
    return {"display_name": d.get("display_name") or "", "org_name": d.get("org_name") or ""}


def user_profile_set(root, uid, display_name=None, org_name=None):
    """Set either field. Empty string clears it; None leaves it alone."""
    fields = {}
    if display_name is not None:
        fields["display_name"] = str(display_name).strip()[:120] or None
    if org_name is not None:
        fields["org_name"] = str(org_name).strip()[:120] or None
    if not fields:
        return user_profile_get(root, uid)
    if supa.is_configured():
        try:
            supa.get_admin().table("profiles").update(fields).eq("id", uid).execute()
        except Exception:                                  # noqa: BLE001 — pre-migration
            pass
        return user_profile_get(root, uid)
    with _LOCK:
        d = _load(root, "user_profile.json")
        d.setdefault(str(uid), {}).update({k: v for k, v in fields.items()})
        _save(root, "user_profile.json", d)
    return user_profile_get(root, uid)


_NOTIFY_DEFAULTS = {"notify_morning_brief": True, "notify_agent_blocked": True,
                    "notify_weekly_digest": False}


def user_notify_prefs(root, uid):
    """Frame 18's three notification toggles for one account.

    Read defensively, the same way user_flags_get is: migration 017 may not be applied,
    in which case the columns don't exist and PostgREST errors. That must not break a
    run path, so an unreadable preference falls back to the default — and the defaults
    match the frame, where the two that help you act are on and the digest is off.
    """
    if supa.is_configured():
        try:
            cols = ", ".join(_NOTIFY_DEFAULTS)
            r = supa.get_admin().table("profiles").select(cols) \
                .eq("id", uid or "").maybe_single().execute() or supa.NO_ROW
            d = r.data or {}
        except Exception:                                  # noqa: BLE001 — pre-migration
            d = {}
    else:
        d = (_load(root, "user_notify.json").get(str(uid)) or {})
    return {k: (d.get(k) if isinstance(d.get(k), bool) else v)
            for k, v in _NOTIFY_DEFAULTS.items()}


def user_notify_set(root, uid, **fields):
    """Set any of the three toggles. Unknown keys and non-booleans are ignored."""
    allowed = {k: bool(v) for k, v in fields.items()
               if k in _NOTIFY_DEFAULTS and isinstance(v, bool)}
    if not allowed:
        return user_notify_prefs(root, uid)
    if supa.is_configured():
        try:
            supa.get_admin().table("profiles").update(allowed).eq("id", uid).execute()
        except Exception:                                  # noqa: BLE001 — pre-migration
            pass
        return user_notify_prefs(root, uid)
    with _LOCK:
        d = _load(root, "user_notify.json")
        d.setdefault(str(uid), {}).update(allowed)
        _save(root, "user_notify.json", d)
    return user_notify_prefs(root, uid)


def user_consent_set(root, uid, contribute_scope=None, consume_scope=None):
    """Update a user's account-wide consent defaults (validated to owner|team|global)."""
    fields = {}
    if contribute_scope in _VALID_SCOPES:
        fields["lesson_contribute_scope"] = contribute_scope
    if consume_scope in _VALID_SCOPES:
        fields["lesson_consume_scope"] = consume_scope
    if not fields:
        return
    if supa.is_configured():
        supa.get_admin().table("profiles").update(fields).eq("id", uid).execute()
        return
    with _LOCK:
        d = _load(root, "consents.json")
        cur = d.get(str(uid)) or {}
        if "lesson_contribute_scope" in fields:
            cur["contribute_scope"] = fields["lesson_contribute_scope"]
        if "lesson_consume_scope" in fields:
            cur["consume_scope"] = fields["lesson_consume_scope"]
        d[str(uid)] = cur
        _save(root, "consents.json", d)


def agent_lesson_scopes(root, agent):
    """Effective (contribute, consume) scopes for an agent: its own override, else the
    owner's account default."""
    dflt = user_consent_get(root, agent.get("owner_user_id"))
    return {"contribute_scope": agent.get("lesson_contribute_scope") or dflt["contribute_scope"],
            "consume_scope": agent.get("lesson_consume_scope") or dflt["consume_scope"]}


def agent_set_consent(root, agent_id, contribute_scope=None, consume_scope=None):
    """Set or clear an agent's per-field consent OVERRIDE. A valid scope overrides the
    account default; 'inherit' (or any invalid value) clears the override (NULL = inherit);
    None means 'leave this field unchanged'."""
    updates = {}
    for col, val in (("lesson_contribute_scope", contribute_scope),
                     ("lesson_consume_scope", consume_scope)):
        if val is None:
            continue                                       # not provided -> unchanged
        updates[col] = val if val in _VALID_SCOPES else None   # 'inherit'/invalid -> clear
    if not updates:
        return
    if supa.is_configured():
        supa.get_admin().table("agents").update(updates).eq("id", agent_id).execute()
        return
    with _LOCK:
        d = _load(root, "agents.json")
        if agent_id in d:
            d[agent_id].update(updates)
            _save(root, "agents.json", d)


def _promo_key(agent_id, decision_id):
    return f"{agent_id}:{decision_id}"


def lesson_promotion_get(root, agent_id, decision_id):
    """The promotion record for one (agent, cross-user lesson), or None."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("lesson_promotions").select("*") \
            .eq("agent_id", agent_id).eq("decision_id", decision_id).maybe_single().execute() or supa.NO_ROW
        return r.data or None
    return _load(root, "promotions.json").get(_promo_key(agent_id, decision_id))


def lesson_promotion_upsert(root, agent_id, owner_id, decision_id, status="provisional",
                            lesson=None):
    """Create the promotion row (provisional on first sight) if absent; returns the record.
    ``lesson`` snapshots the de-identified text so the confirm card renders without a
    cross-owner read."""
    existing = lesson_promotion_get(root, agent_id, decision_id)
    if existing:
        return existing
    if supa.is_configured():
        supa.get_admin().table("lesson_promotions").insert({
            "agent_id": agent_id, "owner_id": owner_id, "decision_id": decision_id,
            "lesson": lesson, "status": status}).execute()
        return lesson_promotion_get(root, agent_id, decision_id)
    with _LOCK:
        d = _load(root, "promotions.json")
        rec = {"agent_id": agent_id, "owner_id": owner_id, "decision_id": decision_id,
               "lesson": lesson, "status": status, "created_at": _now_iso(),
               "confirmed_at": None}
        d[_promo_key(agent_id, decision_id)] = rec
        _save(root, "promotions.json", d)
        return rec


def lesson_promotions_for_user(root, owner_id, status="provisional"):
    """All promotions for a user in a given status (default: provisional = awaiting a
    confirm), newest first. Used to surface 'confirm this lesson?' cards."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("lesson_promotions").select("*") \
            .eq("owner_id", owner_id).eq("status", status) \
            .order("created_at", desc=True).execute()
        return r.data or []
    d = _load(root, "promotions.json")
    out = [rec for rec in d.values()
           if rec.get("owner_id") == owner_id and rec.get("status") == status]
    return sorted(out, key=lambda rec: rec.get("created_at") or "", reverse=True)


def lesson_promotion_set_status(root, agent_id, decision_id, status):
    """Promote (confirmed) or revoke (retired) a cross-user lesson for one agent."""
    confirmed_at = _now_iso() if status == "confirmed" else None
    if supa.is_configured():
        supa.get_admin().table("lesson_promotions").update({
            "status": status, "confirmed_at": confirmed_at
        }).eq("agent_id", agent_id).eq("decision_id", decision_id).execute()
        return
    with _LOCK:
        d = _load(root, "promotions.json")
        rec = d.get(_promo_key(agent_id, decision_id))
        if rec:
            rec["status"] = status
            if confirmed_at:
                rec["confirmed_at"] = confirmed_at
            _save(root, "promotions.json", d)


def lesson_confirmed_user_count(root, decision_id):
    """Distinct users who have CONFIRMED this lesson — the fleet-default signal (>=5 -> global)."""
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("lesson_promotions").select("owner_id") \
            .eq("decision_id", decision_id).eq("status", "confirmed").execute()
        return len({row.get("owner_id") for row in (r.data or [])})
    d = _load(root, "promotions.json")
    return len({rec.get("owner_id") for rec in d.values()
                if rec.get("decision_id") == decision_id and rec.get("status") == "confirmed"})


def lesson_application_add(root, agent_id, owner_id, decision_id, run_session_id):
    """Provenance: record that a lesson influenced a run (best-effort, never raises)."""
    try:
        if supa.is_configured():
            supa.get_admin().table("lesson_applications").insert({
                "agent_id": agent_id, "owner_id": owner_id,
                "decision_id": decision_id, "run_session_id": run_session_id}).execute()
            return
        with _LOCK:
            d = _load(root, "applications.json")
            nid = str(max((int(k) for k in d), default=0) + 1)
            d[nid] = {"agent_id": agent_id, "owner_id": owner_id, "decision_id": decision_id,
                      "run_session_id": run_session_id, "applied_at": _now_iso()}
            _save(root, "applications.json", d)
    except Exception:                                      # noqa: BLE001
        pass


def agent_embedding_upsert(root, agent_id, embedding):
    """Store/refresh an agent's kind embedding (for the retrieval double-gate)."""
    if supa.is_configured():
        admin = supa.get_admin()
        admin.table("agent_embeddings").upsert(
            {"agent_id": agent_id, "embedding": embedding}, on_conflict="agent_id").execute()
        return
    with _LOCK:
        d = _load(root, "agent_embeddings.json")
        d[str(agent_id)] = embedding
        _save(root, "agent_embeddings.json", d)


def agent_embedding_get(root, agent_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("agent_embeddings").select("embedding") \
            .eq("agent_id", agent_id).maybe_single().execute() or supa.NO_ROW
        return (r.data or {}).get("embedding") if r.data else None
    return _load(root, "agent_embeddings.json").get(str(agent_id))


# --- agent group chat (color-coded groups of agents) -------------------------
_AGENT_COLORS = ["#F26A1B", "#2D7FF9", "#12B886", "#E8590C", "#7048E8",
                 "#E64980", "#1098AD", "#F59F00", "#4263EB", "#0CA678"]


def agent_color(agent):
    """An agent's bubble color: its stored override, else a deterministic default derived
    from its id (stable across runs, distinct-ish across an owner's agents)."""
    c = (agent or {}).get("color")
    if c:
        return c
    aid = str((agent or {}).get("agent_id") or (agent or {}).get("id") or "")
    return _AGENT_COLORS[sum(bytearray(aid.encode())) % len(_AGENT_COLORS)]


def agent_set_color(root, agent_id, color):
    if supa.is_configured():
        supa.get_admin().table("agents").update({"color": color}).eq("id", agent_id).execute()
        return
    with _LOCK:
        d = _load(root, "agents.json")
        if agent_id in d:
            d[agent_id]["color"] = color
            _save(root, "agents.json", d)


def agent_set_name(root, agent_id, name):
    """Rename an agent. Display only: the memory store is keyed on the user + the
    PLATFORM workflow name (config.memory_store_name), which this rename never
    touches, so a rename never orphans what the agent has learned."""
    if supa.is_configured():
        supa.get_admin().table("agents").update({"name": name}).eq("id", agent_id).execute()
        return
    with _LOCK:
        d = _load(root, "agents.json")
        if agent_id in d:
            d[agent_id]["name"] = name
            _save(root, "agents.json", d)


def _group_to_caller(row):
    return {"id": str(row.get("id")),
            "owner_user_id": str(row.get("owner_id")) if row.get("owner_id") else None,
            "name": row.get("name"),
            "member_agent_ids": [str(m.get("agent_id")) for m in (row.get("chat_group_members") or [])],
            "created_at": row.get("created_at")}


def group_create(root, owner_user_id, name, agent_ids=None):
    ids = list(dict.fromkeys(a for a in (agent_ids or []) if a))
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("chat_groups").insert({"owner_id": owner_user_id, "name": name}).execute()
        gid = r.data[0]["id"]
        for aid in ids:
            admin.table("chat_group_members").insert({"group_id": gid, "agent_id": aid}).execute()
        return group_get(root, gid)
    with _LOCK:
        d = _load(root, "groups.json")
        gid = str(max((int(k) for k in d), default=0) + 1)
        d[gid] = {"id": gid, "owner_user_id": owner_user_id, "name": name,
                  "member_agent_ids": ids, "created_at": _now_iso()}
        _save(root, "groups.json", d)
        return d[gid]


def groups_for_user(root, owner_user_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("chat_groups").select("*, chat_group_members(agent_id)") \
            .eq("owner_id", owner_user_id).order("created_at").execute()
        return [_group_to_caller(row) for row in (r.data or [])]
    d = _load(root, "groups.json")
    return sorted((g for g in d.values() if g.get("owner_user_id") == owner_user_id),
                  key=lambda g: g.get("created_at") or "")


def group_get(root, group_id):
    if supa.is_configured():
        admin = supa.get_admin()
        r = admin.table("chat_groups").select("*, chat_group_members(agent_id)") \
            .eq("id", str(group_id)).maybe_single().execute() or supa.NO_ROW
        return _group_to_caller(r.data) if r.data else None
    return _load(root, "groups.json").get(str(group_id))


def group_update(root, group_id, name=None, agent_ids=None):
    """Rename and/or replace the member set of a group."""
    if supa.is_configured():
        admin = supa.get_admin()
        if name is not None:
            admin.table("chat_groups").update({"name": name}).eq("id", str(group_id)).execute()
        if agent_ids is not None:
            ids = list(dict.fromkeys(a for a in agent_ids if a))
            admin.table("chat_group_members").delete().eq("group_id", str(group_id)).execute()
            for aid in ids:
                admin.table("chat_group_members").insert({"group_id": str(group_id), "agent_id": aid}).execute()
        return group_get(root, group_id)
    with _LOCK:
        d = _load(root, "groups.json")
        g = d.get(str(group_id))
        if g:
            if name is not None:
                g["name"] = name
            if agent_ids is not None:
                g["member_agent_ids"] = list(dict.fromkeys(a for a in agent_ids if a))
            _save(root, "groups.json", d)
        return g


def group_delete(root, group_id):
    if supa.is_configured():
        supa.get_admin().table("chat_groups").delete().eq("id", str(group_id)).execute()
        return
    with _LOCK:
        d = _load(root, "groups.json")
        d.pop(str(group_id), None)
        _save(root, "groups.json", d)


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


# --- batched reads -----------------------------------------------------------
# home_data and the agents dashboard used to call reviewed_keys / runs_for_agent /
# schedule_get once PER AGENT — three round trips each, so the home screen cost 3N
# queries and got slower with every agent deployed. These fetch the same data for all
# of an owner's agents in one query apiece. Same return shapes, keyed by agent_id.

def reviewed_keys_many(root, agent_ids):
    ids = [a for a in (agent_ids or []) if a]
    if not ids:
        return {}
    if supa.is_configured():
        r = supa.get_admin().table("run_reviews").select("agent_id, run_key") \
            .in_("agent_id", ids).execute()
        out = {a: set() for a in ids}
        for row in (r.data or []):
            out.setdefault(str(row.get("agent_id")), set()).add(row.get("run_key"))
        return out
    return {a: reviewed_keys(root, a) for a in ids}


def runs_for_agents(root, agent_ids, limit=20):
    ids = [a for a in (agent_ids or []) if a]
    if not ids:
        return {}
    if supa.is_configured():
        # One query for every agent's runs, newest first, then grouped and truncated in
        # Python — PostgREST has no per-group limit. The ceiling keeps a chatty agent
        # from starving the others out of the result set.
        r = supa.get_admin().table("agent_runs").select("*") \
            .in_("agent_id", ids) \
            .order("started_at", desc=True) \
            .limit(limit * len(ids)).execute()
        out = {a: [] for a in ids}
        for row in (r.data or []):
            bucket = out.setdefault(str(row.get("agent_id")), [])
            if len(bucket) < limit:
                bucket.append(_run_to_caller(row))
        return out
    return {a: runs_for_agent(root, a, limit) for a in ids}


def schedules_for_agents(root, agent_ids):
    ids = [a for a in (agent_ids or []) if a]
    if not ids:
        return {}
    if supa.is_configured():
        r = supa.get_admin().table("schedules") \
            .select("agent_id, cadence, paused, last_run_at") \
            .in_("agent_id", ids).execute()
        out = {a: None for a in ids}
        for row in (r.data or []):
            out[str(row.get("agent_id"))] = {"cadence": row.get("cadence"),
                                             "paused": row.get("paused"),
                                             "last_run_at": row.get("last_run_at")}
        return out
    return {a: schedule_get(root, a) for a in ids}


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
            .eq("session_id", session_id).maybe_single().execute() or supa.NO_ROW
        return {"summary": r.data["summary"], "status": r.data["status"]} if r.data else None
    return _load(root, "session_summaries.json").get(session_id)


def week_stats(root, owner_user_id, agent_ids, since_iso):
    """Frame 14's strip: runs, and actions you approved, over a window.

    Two counts, two queries. The frame shows a third — "about 2h 10m of busywork
    handled" — and nothing measures run duration, so it is not here: two true numbers
    beat three where one is guessed.

    "Approved" counts EXECUTED and FAILED together, because both were approved by a
    human; only the outcome differed. Counting executed alone would quietly undercount
    the thing the number is actually about.
    """
    ids = [a for a in (agent_ids or []) if a]
    out = {"runs": 0, "approved": 0}
    _APPROVED = ("executed", "failed")
    if supa.is_configured():
        try:
            if ids:
                r = supa.get_admin().table("agent_runs").select("id", count="exact") \
                    .in_("agent_id", ids).gte("started_at", since_iso).execute()
                out["runs"] = r.count or 0
            p = supa.get_admin().table("pending_actions").select("id", count="exact") \
                .eq("owner_id", owner_user_id).in_("status", list(_APPROVED)) \
                .gte("processed_at", since_iso).execute()
            out["approved"] = p.count or 0
        except Exception:                                  # noqa: BLE001 — Home must paint
            pass
        return out
    # File mode keeps runs as one JSONL per agent (runs_<agent>.jsonl), not a single
    # dict — reuse runs_for_agent rather than re-deriving the layout here and getting it
    # wrong. The limit is generous: this is a 7-day count, not a feed.
    for aid in ids:
        for r in runs_for_agent(root, aid, 500):
            if str(r.get("ran_at") or "") >= since_iso:
                out["runs"] += 1
    for it in _load(root, "pending.json").values():
        if it.get("owner_user_id") == owner_user_id and it.get("status") in _APPROVED \
                and str(it.get("processed_at") or "") >= since_iso:
            out["approved"] += 1
    return out


def summaries_many(root, session_ids):
    """Every run summary in ONE query, keyed by session id.

    home_data called summary_get inside its per-run loop — one round trip per run, per
    agent. runs_for_agents already caps at 20 runs each, so five agents cost up to a
    hundred sequential queries before Home could paint, and it got worse with every
    agent deployed. The three neighbouring reads were batched for exactly this reason;
    this one was missed.
    """
    ids = [s for s in (session_ids or []) if s]
    if not ids:
        return {}
    if supa.is_configured():
        out = {}
        # Chunked: a session id list grows with runs × agents, and PostgREST puts the
        # whole `in_` set in the URL.
        for i in range(0, len(ids), 100):
            r = supa.get_admin().table("session_summaries") \
                .select("session_id, summary, status") \
                .in_("session_id", ids[i:i + 100]).execute()
            for row in (r.data or []):
                out[str(row.get("session_id"))] = {"summary": row.get("summary"),
                                                   "status": row.get("status")}
        return out
    store = _load(root, "session_summaries.json")
    return {s: store[s] for s in ids if s in store}


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
