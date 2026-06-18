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

from . import deploy

# event-type markers (substring match against the MA event stream)
_FAIL_MARKERS = ("failed", "error", "cancel", "timeout")
_APPROVAL_STOP = ("requires_action", "requires_approval", "tool_confirmation")


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def schedule_due(cadence, last_run_epoch, now_epoch):
    """Whether a schedule should fire now, given its cadence dict and the epoch of
    its last run (or None). Cadences: {"kind":"interval","minutes":N} |
    {"kind":"hourly"} | {"kind":"daily","time":"HH:MM"} (local time)."""
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


def run_once(client, agent):
    """Run one agent once. ``agent`` is a registry row carrying ``agent_id``,
    ``vault_ref``, and ``trigger``. Returns a run record dict
    ``{agent_id, status, summary, details, run_session_id}`` where ``status`` is
    ``completed`` | ``needs_approval`` | ``failed`` and ``details`` is the in-Trace
    list of actions. Never raises (a platform failure becomes a ``failed`` record).
    """
    agent_id = agent.get("agent_id")
    trigger = agent.get("trigger") or "on a new matching task"
    vault_ref = agent.get("vault_ref")
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

    actions = _action_labels(events)
    if _failed(events):
        return {"agent_id": agent_id, "status": "failed",
                "summary": _last_text(events) or "The run stopped before finishing.",
                "details": actions, "run_session_id": run_session_id}
    if _needs_approval(events):
        # genuinely paused for approval — count the unresolved requests, not all actions
        try:
            pending = deploy.list_held(client, run_session_id)
        except Exception:
            pending = []
        labels = [(p.get("summary") or p.get("kind") or "action") for p in pending] or actions
        n = len(labels)
        return {"agent_id": agent_id, "status": "needs_approval",
                "summary": f"Waiting for you to approve {_plural(n, 'action')}.",
                "details": labels, "run_session_id": run_session_id}
    # completed: actions executed (the normal always-allow case)
    n = len(actions)
    summary = _last_text(events) or (f"Completed {_plural(n, 'action')}." if n else "Completed with no actions needed.")
    return {"agent_id": agent_id, "status": "completed",
            "summary": summary, "details": actions, "run_session_id": run_session_id}
