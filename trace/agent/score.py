"""Performance scoring — how many tokens an agent spends per unit of work that arrived.

The self-improvement engine needs ONE number it can compare between cycles, or it cannot
tell whether anything it did helped. Raw tokens per session will not do: a heavy week and
a light week differ by more than any memory change ever will, and the loop would spend its
life chasing the workload.

    D = items_in_scope + novel_items + (input_kb / KB_DIVISOR)     floored at D_FLOOR
    P = total_tokens / D                                           lower is better

D IS BUILT ONLY FROM WHAT ARRIVED. Nothing the agent chose to do enters it. An earlier
draft counted tool calls, which is self-defeating: cutting tool calls cuts tokens and D
together, the ratio holds flat, and the loop goes blind at exactly the moment it does its
best work.

THE WEIGHTS ARE ARBITRARY AND FROZEN. P never has to be correct in absolute terms, only
comparable between cycles of the SAME agent. Change a weight and every prior log entry
becomes uncomparable and the loop loses its history — which is why they live in config.py
as constants rather than in the prompt as prose, and why they are not tuned.

Scores are NOT comparable across agents. Different workloads, different baselines,
different difficulty profiles. When a peer's number crosses in an improvement log, the
SIGN is evidence and the magnitude is context (see learn.py).
"""

import json

from . import activity, config, model_call
from .configgen import _parse_json_object
from .score_prompt import SCORER_SYSTEM

DEFAULT_MODEL = config.SCORE_MODEL


def _default_model_fn(model=DEFAULT_MODEL):
    import anthropic
    client = anthropic.Anthropic(timeout=config.SCORE_REQUEST_TIMEOUT,
                                 max_retries=config.SCORE_RETRIES)

    def call(system, user):
        # temperature=None: the 5-family models reject sampling parameters with a 400.
        return model_call.create_text(client, model, system, user,
                                      max_tokens=config.SCORE_MAX_TOKENS,
                                      temperature=None)

    return call


def input_kb(events):
    """Kilobytes of EXTERNAL material a session had to read, measured from the raw events.

    Measured here rather than judged by the scorer, and that distinction is load bearing.
    The scorer sees a COMPACTED transcript: activity._compact truncates every tool result
    to 200 characters and every agent message to 800, and the whole transcript is then
    clipped again before it is sent. A session that read a 400KB document and one that read
    a 2KB email look nearly identical by then, so asking the model to estimate input size
    would systematically under-count exactly the heavy sessions the term exists to account
    for — deflating D and inflating P on the sessions that matter most.

    Counts tool RESULTS and the trigger text: material that arrived. Excludes the agent's
    own messages and its tool-call arguments, which are things it chose to write.
    """
    total = 0
    for e in (events or []):
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "").lower()
        if "tool_result" in t or t.endswith("user.message"):
            total += len(str(e.get("text") or ""))
    return round(total / 1024.0, 1)


def difficulty(items_in_scope, novel_items, kb):
    """D, floored. Never raises on junk input — a bad count reads as an easy session
    rather than an exception, and the floor keeps P finite."""
    try:
        d = float(items_in_scope or 0) + float(novel_items or 0) \
            + (float(kb or 0) / config.SCORE_KB_DIVISOR)
    except (TypeError, ValueError):
        d = 0.0
    return max(d, config.SCORE_D_FLOOR)


def performance(total_tokens, d):
    """P for one session: tokens per unit of difficulty. Lower is better."""
    try:
        return float(total_tokens or 0) / float(d or config.SCORE_D_FLOOR)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def build_payload(session_records):
    """What the scorer sees: transcripts and the measured input size. No log, no pending
    change, no memory store, no mention that an improvement loop exists."""
    return {"sessions": [{"session_id": r.get("session_id"),
                          "input_kb": r.get("input_kb"),
                          "transcript": r.get("transcript")}
                         for r in (session_records or [])]}


def score_batch(session_records, model_fn):
    """ONE scoring call. Returns ``{session_id: {"items_in_scope", "novel_items",
    "note"}}``, or None when the call or the parse failed. Never raises.

    Returning None means the cycle has no measurement and must not judge anything: better
    to skip a verdict than to grade a change against a number nobody produced.
    """
    if not session_records:
        return None
    user = json.dumps(build_payload(session_records), ensure_ascii=False, default=str)
    try:
        reply = model_fn(SCORER_SYSTEM, user)
    except Exception as e:                                 # noqa: BLE001 — best-effort, loudly
        print(f"[score] scoring call failed: {type(e).__name__}: {str(e)[:200]}",
              flush=True)
        return None
    obj = _parse_json_object(reply) or {}
    rows = obj.get("sessions")
    if not isinstance(rows, list) or not rows:
        print(f"[score] scoring reply did not parse as JSON with sessions. "
              f"Tail: {str(reply or '').rstrip()[-160:]!r}", flush=True)
        return None
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("session_id") or "")
        if not sid:
            continue
        out[sid] = {"items_in_scope": row.get("items_in_scope") or 0,
                    "novel_items": row.get("novel_items") or 0,
                    "note": str(row.get("note") or "")}
    return out or None


def measure(session_records, model_fn):
    """The whole measurement: score the batch, then compute D, P and the mean IN CODE.

    Returns ``{"sessions": [...], "mean_performance": float}`` or None. A session the
    scorer did not return is dropped from the mean rather than defaulted — a made-up
    difficulty is worse than a smaller sample.
    """
    judged = score_batch(session_records, model_fn)
    if not judged:
        return None
    rows = []
    for rec in (session_records or []):
        sid = str(rec.get("session_id") or "")
        j = judged.get(sid)
        if not j:
            print(f"[score] no judgement returned for session {sid} — dropped from the "
                  f"mean", flush=True)
            continue
        kb = rec.get("input_kb")
        d = difficulty(j["items_in_scope"], j["novel_items"], kb)
        tokens = ((rec.get("tokens") or {}).get("total_tokens")) or 0
        rows.append({"session_id": sid,
                     "items_in_scope": j["items_in_scope"],
                     "novel_items": j["novel_items"],
                     "input_kb": kb,
                     "difficulty": round(d, 1),
                     "total_tokens": tokens,
                     "performance": round(performance(tokens, d), 1),
                     "note": j["note"]})
    if not rows:
        return None
    mean = sum(r["performance"] for r in rows) / len(rows)
    return {"sessions": rows, "mean_performance": round(mean, 1)}
