"""Activity engine — turn an agent's real Managed Agents session logs into the account
the worker reads (via a small model call) and extract any staged proposed actions.

The activity view is driven by GROUND TRUTH (the session's actual event stream fetched
with ``client.list_session_events``), not Trace's own run self-report — which was
unreliable. The model call is injected (``model_fn(system, user) -> str``) so this runs
offline against a stub; the real caller is built in AppState.

Prompt Library v2: the summary prompt opens with the foundation's Trace + Vocabulary
sections only (it reads a session log, not capture data, and runs on a summarizer
budget) and asks for a structured account — headline, actions taken, what needs the
worker, problems, status. ``summarize_session`` renders that JSON into the one plain
paragraph the activity view already shows and the summary store already holds, so the
view and the approve -> execute flow are untouched: proposals and questions are still
extracted from the session's structured blocks by code (``extract_proposals`` /
``extract_questions``), never from the model's account of them. A reply that is not
JSON is used as-is, which is also the fallback for an older cached summary.
"""

import json

from . import runner
from .configgen import _parse_json_object
from .foundation import FOUNDATION_LITE

_STAGE = """\
## Your job

You receive the session log of one agent run. Write the account of it the
worker will read: what the agent did, what it is waiting on, and anything
that went wrong. The worker is not technical and did not watch the run.
They want to know whether their work got done and whether they need to
do anything.

The log is a compacted transcript, one line per event: [trigger] the
message that started the run, [agent] what the agent said, [tool call] a
tool it called with its input, [tool result] what came back (marked
"(error)" when the call failed), and [end] how the run stopped. On a
STAGED run the agent does not act: its final message carries a
proposed_actions block listing what it would have done, for the worker
to approve. On a run that stopped to ask, the final message carries a
needs_input block with its questions.

## Method

1. Read the whole log. Separate what the agent actually did (a tool call
   that returned success) from what it proposed, tried, or said it would
   do.
2. Write the headline: one sentence, what got done, in the worker's own
   record names ("Entered 3 orders into the tracking sheet and sent 3
   confirmations").
3. List actions taken: one line each, with the record it touched. Only
   completed actions. Staged proposals are not actions.
4. List what needs the worker: staged proposals awaiting approval, halts
   where the agent stopped and asked, and anything it could not do. Say
   what the worker has to decide, not what the agent was thinking.
5. Note problems plainly: errors, retries, a run that ended early. If
   the agent skipped something because it was already done, say so —
   that is correct behaviour, not a problem.

## Rules

- Report only what the log shows. Never infer that an action succeeded
  from the agent saying it did; a tool result is the evidence.
- Never invent a record name, count, or recipient. If the log is unclear,
  say "one or more".
- Plain words. No tool names (say "searched the inbox", not the tool's
  name), no session ids, no pipeline vocabulary.
- Short. The worker reads this in a list of runs; the headline should
  usually be enough.

## Output

{ "headline": "one sentence",
  "actions": [ { "text": "Added order 4471 (three items) as one row to the Orders tab", "ref": "tool event id or null" } ],
  "needs_you": [ { "type": "proposal | question | blocked",
                   "text": "Reply to the customer confirming order 4480 — waiting for your approval",
                   "ref": "proposal id, event id, or null" } ],
  "problems": [ { "text": "...", "ref": "event id or null" } ],
  "status": "done | waiting | partial | failed" }
"""

SUMMARY_SYSTEM = FOUNDATION_LITE + "\n" + _STAGE


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


def _lines(items):
    out = []
    for it in items or []:
        text = str((it.get("text") if isinstance(it, dict) else it) or "").strip()
        if text:
            out.append(text.rstrip("."))
    return out


def render_summary(account):
    """The structured account as the one plain paragraph the activity view shows:
    the headline, then what got done, what needs the worker, and any problems, each
    only when non-empty. Returns "" for an account with nothing in it."""
    if not isinstance(account, dict):
        return ""
    parts = []
    headline = str(account.get("headline") or "").strip()
    if headline:
        parts.append(headline if headline.endswith((".", "!", "?")) else headline + ".")
    for label, key in (("Done", "actions"), ("Needs you", "needs_you"),
                       ("Problems", "problems")):
        items = _lines(account.get(key))
        if items:
            parts.append(f"{label}: " + "; ".join(items) + ".")
    return " ".join(parts)


def summarize_session(events, agent_name, model_fn, mode=None):
    """A plain-language paragraph of what the agent did this session, or "" if nothing
    to say / the model call fails (best-effort — never raises). ``mode`` names the run
    ("live" or "staged") when the caller knows it, so a staged run's proposals are
    reported as waiting on the worker rather than as things done."""
    transcript = _compact(events)
    if not transcript.strip():
        return ""
    head = f"Agent: {agent_name or 'agent'}"
    if mode:
        head += f"\nMode: {mode}"
    user = f"{head}\n\nSession transcript:\n{transcript}"
    try:
        raw = (model_fn(SUMMARY_SYSTEM, user) or "").strip()
    except Exception:                                      # noqa: BLE001
        return ""
    account = _parse_json_object(raw)
    if account:
        rendered = render_summary(account)
        if rendered:
            return rendered
    return raw                                             # a plain-text reply is used as-is


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


def session_token_usage(events):
    """Total token cost of one session: ``{input_tokens, output_tokens,
    total_tokens, estimated}``. Sums the ``usage`` payloads the api_client
    preserved on each event (cache reads/creation count as input). When the
    platform reported no usage at all, falls back to a chars/4 estimate over the
    events' text and says so (``estimated=True``) — the learning payload always
    carries a cost figure per session."""
    inp = out = 0
    found = False
    for e in events:
        u = e.get("usage") if isinstance(e, dict) else None
        if not isinstance(u, dict):
            continue
        found = True
        inp += int(u.get("input_tokens") or 0) \
            + int(u.get("cache_read_input_tokens") or 0) \
            + int(u.get("cache_creation_input_tokens") or 0)
        out += int(u.get("output_tokens") or 0)
    if not found:
        total = max(1, sum(len(e.get("text") or "") for e in events if isinstance(e, dict)) // 4)
        return {"input_tokens": 0, "output_tokens": 0,
                "total_tokens": total, "estimated": True}
    return {"input_tokens": inp, "output_tokens": out,
            "total_tokens": inp + out, "estimated": False}
