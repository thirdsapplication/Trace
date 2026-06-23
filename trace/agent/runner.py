"""Spec 4 — the headless agent run path.

Reused by the Trace-side scheduler and the in-app "Run now" button so a deployed
agent runs WITHOUT the manual Managed Agents console step. It composes the
verified platform-client methods (all MA calls stay in ``api_client``): create a
fresh session bound to the customer's vault, send the trigger turn, and collect
the event stream. The approval policy is baked into the agent envelope at create
time, so a run honors it — this never bypasses outbound-write approvals.

Pure orchestration over an injected ``client``, so it runs offline against a stub
(mirrors ``deploy.py``).
"""

import datetime
import hashlib
import json
import re

from . import config, deploy

# event-type markers (substring match against the MA event stream)
_FAIL_MARKERS = ("failed", "error", "cancel", "timeout")
_APPROVAL_STOP = ("requires_action", "requires_approval", "tool_confirmation")


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def schedule_due(cadence, last_run_epoch, now_epoch):
    """Whether a schedule should fire now, given its cadence dict and the epoch of
    its last run (or None). Current cadences: {"kind":"daily","time":"HH:MM"} |
    {"kind":"weekly","weekday":N,"time":"HH:MM"} (N: 0=Mon … 6=Sun), local time.
    {"kind":"interval","minutes":N} and {"kind":"hourly"} are legacy (no longer
    offered in the UI) but still honored so any pre-existing schedule keeps firing."""
    if not cadence:
        return False
    kind = cadence.get("kind")
    if kind in ("interval", "hourly"):
        period = 3600 if kind == "hourly" else max(1, int(cadence.get("minutes", 15))) * 60
        return last_run_epoch is None or (now_epoch - last_run_epoch) >= period
    if kind == "daily":
        try:
            hh, mm = (int(x) for x in str(cadence.get("time", "09:00")).split(":"))
        except Exception:
            hh, mm = 9, 0
        now_dt = datetime.datetime.fromtimestamp(now_epoch)
        scheduled = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now_dt < scheduled:
            return False
        return last_run_epoch is None or datetime.datetime.fromtimestamp(last_run_epoch) < scheduled
    if kind == "weekly":
        try:
            hh, mm = (int(x) for x in str(cadence.get("time", "09:00")).split(":"))
        except Exception:
            hh, mm = 9, 0
        try:
            weekday = int(cadence.get("weekday", 0)) % 7      # 0=Mon … 6=Sun
        except Exception:
            weekday = 0
        now_dt = datetime.datetime.fromtimestamp(now_epoch)
        at_time = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # the most recent occurrence of weekday@time at or before now
        anchor = at_time - datetime.timedelta(days=(now_dt.weekday() - weekday) % 7)
        if now_dt < anchor:                                    # today is the day but before the time
            anchor -= datetime.timedelta(days=7)
        return last_run_epoch is None or datetime.datetime.fromtimestamp(last_run_epoch) < anchor
    return False


# --- Spec 6: derive a Trace-native run outcome from the session event stream ---
# Status is read from what actually happened, NOT from a raw count of tool calls.
# An always-allow agent's tool calls EXECUTE (no tool_confirmation), so they are
# completed actions — never "awaiting approval". "Needs approval" is reported only
# when the session genuinely stopped for input (stop_reason=requires_action).

def _action_labels(events):
    """Short, plain labels for the tool actions the agent actually took, in order
    (an executed outbound call surfaces as an ``mcp_tool_use`` event)."""
    out = []
    for ev in events:
        t = (ev.get("type") or "").lower()
        if "tool_use" in t and "confirmation" not in t and "result" not in t:
            out.append((ev.get("name") or ev.get("text") or "action").strip()[:120] or "action")
    return out


def _last_text(events):
    for ev in reversed(events):
        text = (ev.get("text") or "").strip()
        if text:
            return text[:280]
    return ""


def _failed(events):
    return any(any(k in (ev.get("type") or "").lower() for k in _FAIL_MARKERS) for ev in events)


def _needs_approval(events):
    """True only if the session genuinely stopped waiting for approval."""
    return any(any(k in (ev.get("stop_reason") or "").lower() for k in _APPROVAL_STOP)
               for ev in events)


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _content_hash(agent_id, kind, payload):
    """Stable id for one proposed action (Spec 8 idempotency): identical
    agent+kind+payload hash the same, so the same staged item is never duplicated
    across repeated scheduled runs."""
    blob = json.dumps({"a": agent_id, "k": kind, "p": payload},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _pending_items(agent_id, held, run_session_id):
    """Turn the platform's held actions into Trace-side staged-action records,
    capturing each action's FULL input so Trace can review and later execute it."""
    items = []
    for h in held:
        payload = h.get("input") or {}
        kind = h.get("kind") or "write"
        items.append({"action_id": h.get("action_id"),
                      "kind": kind,
                      "summary": h.get("summary") or kind,
                      "payload": payload,
                      "content_hash": _content_hash(agent_id, kind, payload),
                      "run_session_id": run_session_id})
    return items


# --- Spec 8 prompt-level staging ------------------------------------------
def _final_text(events):
    """The agent's full final message (untruncated), where the proposed-actions
    block lives — unlike _last_text which truncates for summaries."""
    for ev in reversed(events):
        t = (ev.get("text") or "").strip()
        if t:
            return t
    return ""


def _first_line(text, n=160):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:n]


def _app_label(slug):
    return str(slug or "").replace("_", " ").title()


def _staging_trigger(base_trigger, staged_slugs):
    """Prepend the staging directive (naming the staged apps) to the run's trigger so
    the agent reads freely but proposes — not performs — outbound actions in those apps."""
    apps = ", ".join(_app_label(s) for s in staged_slugs) or "the staged apps"
    # plain replace (not str.format) — the directive contains literal JSON braces
    directive = config.STAGING_DIRECTIVE.replace("{apps}", apps)
    return f"{directive}\nTask: {base_trigger}"


def _parse_proposals(text):
    """Parse the agent's ``proposed_actions`` JSON array out of its final message.
    Returns a list (possibly empty) on success, or None if no array could be parsed
    (the caller then falls back so a proposal is never silently dropped)."""
    if not text:
        return None
    m = re.search(r"```(?:proposed_actions|json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    blob = m.group(1) if m else None
    if blob is None:                                   # no fence — try a bare top-level array
        m2 = re.search(r"(\[.*\])", text, re.DOTALL)
        blob = m2.group(1) if m2 else None
    if blob is None:
        return None
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def _proposals_to_pending(agent_id, proposals, run_session_id):
    items = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        details = p.get("details")
        if not isinstance(details, (dict, list)):
            details = {"description": str(details)} if details is not None else {}
        kind = str(p.get("action") or p.get("app") or "action")
        summary = str(p.get("summary") or kind)
        items.append({"action_id": None, "kind": kind, "summary": summary,
                      "app": p.get("app") or "", "payload": details,
                      "content_hash": _content_hash(agent_id, kind, details),
                      "run_session_id": run_session_id})
    return items


def run_once(client, agent):
    """Run one agent once. ``agent`` is a registry row carrying ``agent_id``,
    ``vault_ref``, and ``trigger``. Returns a run record dict
    ``{agent_id, status, summary, details, run_session_id}`` where ``status`` is
    ``completed`` | ``needs_approval`` | ``failed`` and ``details`` is the in-Trace
    list of actions. Never raises (a platform failure becomes a ``failed`` record).
    """
    agent_id = agent.get("agent_id")
    base_trigger = agent.get("trigger") or "on a new matching task"
    vault_ref = agent.get("vault_ref")
    staged = [s for s in (agent.get("staged_apps") or []) if s]
    trigger = _staging_trigger(base_trigger, staged) if staged else base_trigger
    try:
        environment_id = client.create_environment()
        run_session_id = client.create_session(
            agent_id, environment_id, resources=[], title="Trace scheduled run",
            vault_ids=[vault_ref] if vault_ref else None)
        events = client.send_trigger_and_collect(run_session_id, trigger)
    except Exception as e:                                  # noqa: BLE001
        return {"agent_id": agent_id, "status": "failed",
                "summary": f"Couldn't run: {type(e).__name__}", "details": [],
                "run_session_id": None}
    # Reading the result must NEVER raise (that previously surfaced as a bogus
    # "Couldn't start the run." with no pending actions). Degrade gracefully instead.
    try:
        return _build_run_record(agent_id, events, staged, run_session_id, client)
    except Exception as e:                                  # noqa: BLE001
        return {"agent_id": agent_id, "status": "failed",
                "summary": f"Ran, but couldn't read the result: {type(e).__name__}",
                "details": [], "run_session_id": run_session_id}


def _build_run_record(agent_id, events, staged, run_session_id, client):
    """Turn the collected event stream into a run record. For a STAGED agent, parse the
    agent's proposed actions out of its final message into pending approvals; otherwise
    report what executed (with a benign held-action fallback for the always_allow case)."""
    actions = _action_labels(events)
    if _failed(events):
        return {"agent_id": agent_id, "status": "failed",
                "summary": _last_text(events) or "The run stopped before finishing.",
                "details": actions, "run_session_id": run_session_id}
    if staged:
        final = _final_text(events)
        proposals = _parse_proposals(final)
        if proposals is None:                          # contract not followed — surface, don't drop
            proposals = ([{"app": "", "action": "review",
                           "summary": _first_line(final) or "Review the agent's proposal",
                           "details": {"description": final}}] if final.strip() else [])
        pending = _proposals_to_pending(agent_id, proposals, run_session_id)
        if pending:
            return {"agent_id": agent_id, "status": "needs_approval",
                    "summary": f"Staged {_plural(len(pending), 'action')} for you to approve.",
                    "details": [p["summary"] for p in pending], "pending": pending,
                    "run_session_id": run_session_id}
        return {"agent_id": agent_id, "status": "completed",
                "summary": _last_text(events) or "Completed with nothing to approve.",
                "details": actions, "run_session_id": run_session_id}
    if _needs_approval(events):                        # always_allow shouldn't hold, but be safe
        try:
            held = deploy.list_held(client, run_session_id)
        except Exception:
            held = []
        pending = _pending_items(agent_id, held, run_session_id)
        labels = [(p.get("summary") or p.get("kind") or "action") for p in pending] or actions
        return {"agent_id": agent_id, "status": "needs_approval",
                "summary": f"Staged {_plural(len(labels), 'action')} for you to approve.",
                "details": labels, "pending": pending, "run_session_id": run_session_id}
    n = len(actions)
    summary = _last_text(events) or (f"Completed {_plural(n, 'action')}." if n else "Completed with no actions needed.")
    return {"agent_id": agent_id, "status": "completed",
            "summary": summary, "details": actions, "run_session_id": run_session_id}


def execute_pending(client, agent, pending):
    """Perform one previously-staged action on the user's approval (Spec 8 "quick fresh
    task"). Runs the deployed agent on a fresh session with an explicit EXECUTE
    instruction that overrides staging (staging lives only in the scheduled-run trigger,
    not the agent's system prompt, so a plain execute trigger performs the action under
    the always_allow toolsets). Returns ``{status: executed|failed, summary,
    run_session_id}``. Never raises."""
    agent_id = agent.get("agent_id")
    vault_ref = agent.get("vault_ref")
    summary = pending.get("summary") or pending.get("kind") or "the action"
    payload = pending.get("payload") or {}
    instruction = (
        "You are approved to perform this ONE action now, for real. Do NOT stage, "
        "propose, or describe it — actually carry it out using your tools, then stop. "
        f"Action: {summary}. Details: {json.dumps(payload, ensure_ascii=False, default=str)}")
    try:
        environment_id = client.create_environment()
        run_session_id = client.create_session(
            agent_id, environment_id, resources=[], title="Trace approved action",
            vault_ids=[vault_ref] if vault_ref else None)
        events = client.send_trigger_and_collect(run_session_id, instruction)
    except Exception as e:                                  # noqa: BLE001
        return {"status": "failed", "summary": f"Couldn't perform it: {type(e).__name__}",
                "run_session_id": None}
    if _failed(events):
        return {"status": "failed",
                "summary": _last_text(events) or "The action stopped before finishing.",
                "run_session_id": run_session_id}
    return {"status": "executed",
            "summary": _last_text(events) or f"Performed {summary}.",
            "run_session_id": run_session_id}
