"""Activity engine — turn an agent's real Managed Agents session logs into a plain
-language summary (via a small model) and extract any staged proposed actions.

The activity view is driven by GROUND TRUTH (the session's actual event stream fetched
with ``client.list_session_events``), not Trace's own run self-report — which was
unreliable. The model call is injected (``model_fn(system, user) -> str``) so this runs
offline against a stub; the real caller is a Haiku model built in AppState.
"""

import json

from . import runner

SUMMARY_SYSTEM = (
    "You summarize one run of an automated work agent for a non-technical business "
    "user. From the session transcript (the agent's own messages, the tools it called, "
    "and the results), write 2–4 short, plain-English sentences: what the agent looked "
    "at, what it found, and what it did or proposed to do. Be concrete (names, counts) "
    "but brief. No code and no raw tool names (say 'searched Gmail', not "
    "'gmail-find-email'). If it proposed actions for approval instead of doing them, "
    "say so plainly.")


def _compact(events):
    """A short, model-friendly transcript: drop token/lifecycle/thinking noise, keep the
    agent's messages, its tool calls (name + truncated input), results, and the ending."""
    out = []
    for e in events:
        t = (e.get("type") or "")
        tl = t.lower()
        if "span." in tl or "thinking" in tl or "status_running" in tl:
            continue
        if tl.endswith("user.message"):
            out.append(f"[trigger] {runner._first_line(e.get('text') or '', 200)}")
        elif "agent.message" in tl:
            txt = (e.get("text") or "").strip()
            if txt:
                out.append(f"[agent] {txt[:800]}")
        elif "mcp_tool_use" in tl or tl.endswith("tool_use"):
            name = e.get("name") or "tool"
            inp = json.dumps(e.get("input") or {}, ensure_ascii=False, default=str)[:200]
            out.append(f"[tool call] {name} {inp}")
        elif "tool_result" in tl:
            res = (e.get("text") or "")[:200]
            out.append(f"[tool result{' (error)' if e.get('is_error') else ''}] {res}")
        elif "idle" in tl or "completed" in tl or "ended" in tl or "failed" in tl:
            sr = e.get("stop_reason")
            out.append(f"[end] {sr or t}")
    return "\n".join(out)


def summarize_session(events, agent_name, model_fn):
    """A plain-language paragraph of what the agent did this session, or "" if nothing
    to say / the model call fails (best-effort — never raises)."""
    transcript = _compact(events)
    if not transcript.strip():
        return ""
    user = f"Agent: {agent_name or 'agent'}\n\nSession transcript:\n{transcript}"
    try:
        return (model_fn(SUMMARY_SYSTEM, user) or "").strip()
    except Exception:                                      # noqa: BLE001
        return ""


def extract_proposals(events):
    """The staged ``proposed_actions`` list from the session's final message, parsed into
    ``[{app, action, summary, details}]``; ``[]`` when none/unparseable."""
    proposals = runner._parse_proposals(runner._final_text(events))
    if not proposals:
        return []
    return [p for p in proposals if isinstance(p, dict)]


def extract_questions(events):
    """The agent's ``needs_input`` questions from the session's final message (Gap #3),
    parsed into ``[{question, context, options?}]``; ``[]`` when none/unparseable."""
    return runner._parse_needs_input(runner._final_text(events))


def is_terminal(events):
    return any(any(k in (e.get("type") or "").lower()
                   for k in ("idle", "completed", "ended", "failed"))
               for e in events)


def session_status(events):
    """``running`` | ``failed`` | ``needs_input`` | ``staged`` | ``completed`` derived
    from the events. A pause for input outranks staging — the human must answer first."""
    types = [(e.get("type") or "").lower() for e in events]
    if any(("status_failed" in t or t.endswith(".failed") or "session.failed" in t)
           for t in types):
        return "failed"
    if not is_terminal(events):
        return "running"
    if extract_questions(events):
        return "needs_input"
    if extract_proposals(events):
        return "staged"
    return "completed"
