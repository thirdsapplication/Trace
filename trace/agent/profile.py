"""What the engine is shown about WHERE a session's tokens went.

The compacted transcript (activity._compact) truncates every tool result to 200
characters, so an engine reading it cannot tell a session that pulled 400KB from one that
pulled a paragraph, and cannot see that the same call was made four times with identical
arguments. Those are exactly the facts a change has to be built on, so they are measured
here from the raw events and handed over as numbers.

Everything in this module is computed in code. The engine receives it and is told not to
recompute it: a party that produces its own evidence can produce evidence that flatters it.
"""

import json

# A session this short did not have much to do. It is a PROXY for "often nothing waiting",
# which is the precondition an early-exit probe needs, and not a measurement of it -- an
# agent can also finish real work in three turns. Named for what it counts.
SHORT_SESSION_TURNS = 3
# Memory mounts at /mnt/memory/<store-name>/ and the agent reads it with ordinary file
# tools, so a read shows up as a tool call whose input carries the document's name.
MEMORY_MOUNT = "/mnt/memory"


def _usage_events(events):
    return [e for e in (events or [])
            if isinstance(e, dict) and isinstance(e.get("usage"), dict)]


def turns(events):
    """One turn per assistant message that reported usage. That is the unit the prefix is
    re-sent on, which is the unit cost is actually paid in."""
    return len(_usage_events(events))


def tokens_per_turn(events):
    """``[{turn, input_tokens, output_tokens}]``, oldest first. Cache reads and cache
    creation count as input, the same way session_token_usage counts them, or a cached
    prefix would look free when it is merely cheaper."""
    out = []
    for i, e in enumerate(_usage_events(events), 1):
        u = e["usage"]
        out.append({"turn": i,
                    "input_tokens": int(u.get("input_tokens") or 0)
                    + int(u.get("cache_read_input_tokens") or 0)
                    + int(u.get("cache_creation_input_tokens") or 0),
                    "output_tokens": int(u.get("output_tokens") or 0)})
    return out


def _is_tool_call(t):
    return "mcp_tool_use" in t or t.endswith("tool_use")


def tool_calls(events):
    """``[{name, args, result_chars}]`` in order. ``args`` is the call's input rendered
    with sorted keys so two calls that differ only in key order are recognised as the
    same call. ``result_chars`` is the size of the result that came back -- paid once when
    it arrives and again on every later turn, which is what makes a broad scan expensive
    long after the turn that ran it."""
    calls, pending = [], None
    for e in (events or []):
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "").lower()
        if _is_tool_call(t):
            if pending is not None:
                calls.append(pending)
            pending = {"name": e.get("name") or "tool",
                       "args": json.dumps(e.get("input") or {}, sort_keys=True,
                                          ensure_ascii=False, default=str),
                       "result_chars": 0}
        elif "tool_result" in t and pending is not None:
            pending["result_chars"] = len(str(e.get("text") or ""))
            calls.append(pending)
            pending = None
    if pending is not None:
        calls.append(pending)
    return calls


def repeated_calls(calls):
    """Calls made more than once with IDENTICAL arguments, most repeated first.

    Not "similar" calls: identical ones. A second identical call returns what the first
    already put in the conversation, so it pays for the round trip and then pays for the
    result again on every turn after. It is the cheapest thing in a profile to act on and
    invisible in a truncated transcript."""
    seen = {}
    for c in (calls or []):
        key = (c.get("name"), c.get("args"))
        row = seen.setdefault(key, {"name": c.get("name"), "args": c.get("args"),
                                    "count": 0, "result_chars": 0})
        row["count"] += 1
        row["result_chars"] = max(row["result_chars"], c.get("result_chars") or 0)
    return sorted([r for r in seen.values() if r["count"] > 1],
                  key=lambda r: (-r["count"], -r["result_chars"]))


def referenced_docs(events, doc_paths):
    """Which memory documents this session actually touched.

    Matched on the document's file name appearing in any tool call's arguments, because
    the store is mounted as a directory and read with file tools. Deliberately loose: a
    false POSITIVE marks a document as used and makes the engine less likely to prune it,
    which is the safe direction to be wrong in. A false negative would invite deleting a
    document the agent reads.
    """
    blob = "\n".join(str(c.get("args") or "") for c in tool_calls(events))
    blob += "\n" + "\n".join(str(e.get("text") or "") for e in (events or [])
                             if isinstance(e, dict)
                             and MEMORY_MOUNT in str(e.get("text") or ""))
    out = set()
    for path in (doc_paths or []):
        name = str(path).rsplit("/", 1)[-1]
        if name and name in blob:
            out.add(path)
    return out


def session_profile(events, doc_paths=None, session_id=None):
    """The per-session profile the engine reads instead of guessing from a transcript."""
    calls = tool_calls(events)
    per_turn = tokens_per_turn(events)
    return {
        "session_id": session_id,
        "turns": len(per_turn),
        "tokens_per_turn": per_turn,
        "tool_calls": [{"name": c["name"], "args": c["args"][:200],
                        "result_chars": c["result_chars"]} for c in calls],
        "repeated_calls": [{"name": r["name"], "args": r["args"][:200],
                            "count": r["count"], "result_chars": r["result_chars"]}
                           for r in repeated_calls(calls)],
        "documents_referenced": sorted(referenced_docs(events, doc_paths)),
    }


def prefix(events, memory_docs):
    """What the agent re-sends on every turn, and how much of it the engine can reach.

    ``prefix_tokens`` is MEASURED, not estimated: the first turn's input is the system
    prompt, the tool schemas, the memory store and the trigger, and the platform reported
    its size. Only the memory share is an estimate (characters over four), because that is
    the one part whose text is in hand.

    The remainder is not broken down further and says so. System prompt and tool schemas
    are both outside what the engine may change, so the number that matters is what share
    of the per-turn cost is memory -- if it is small, the largest saving available is not
    the engine's to make, and saying so is worth more than a marginal change.
    """
    per_turn = tokens_per_turn(events)
    measured = per_turn[0]["input_tokens"] if per_turn else None
    chars = sum(len(str(v or "")) for v in (memory_docs or {}).values())
    est = int(chars / 4)
    share = None
    if measured:
        share = round(min(est / measured, 1.0), 3)
    return {"prefix_tokens_measured": measured,
            "memory_store_chars": chars,
            "memory_store_tokens_estimated": est,
            "memory_share_of_prefix": share,
            "unattributed_tokens": None if measured is None else max(measured - est, 0),
            "note": ("prefix_tokens_measured is the first turn's reported input. The "
                     "memory figure is characters over four. The remainder is the system "
                     "prompt and the tool schemas, neither of which the engine may change.")}


def precondition_vector(profiles, memory_docs):
    """The countable facts a fleet record is matched on.

    Preconditions answer "does this agent have the problem this technique fixes", which is
    the question that decides whether a technique transfers. Agent similarity answers
    "are these two agents alike", which is only ever a proxy for it -- and one that fails
    both ways: two agents with three never-referenced documents each share a pruning
    result however different their businesses, and two agents on the same connectors do
    not share an early-exit result if only one of them has quiet sessions.

    The model also writes preconditions in prose, for the next engine to read. This is the
    half a matcher can filter on, because prose cannot be compared to numbers.
    """
    profiles = [p for p in (profiles or []) if isinstance(p, dict)]
    docs = dict(memory_docs or {})
    referenced = set()
    for p in profiles:
        referenced |= set(p.get("documents_referenced") or [])
    turn_counts = sorted(int(p.get("turns") or 0) for p in profiles)
    median = turn_counts[len(turn_counts) // 2] if turn_counts else 0
    tools = sorted({str(c.get("name") or "") for p in profiles
                    for c in (p.get("tool_calls") or []) if c.get("name")})
    n = len(profiles) or 1
    return {
        "store_kb": round(sum(len(str(v or "")) for v in docs.values()) / 1024.0, 1),
        "docs_total": len(docs),
        "docs_never_referenced": len([d for d in docs if d not in referenced]),
        "median_turns": median,
        "short_session_share": round(
            sum(1 for p in profiles
                if int(p.get("turns") or 0) <= SHORT_SESSION_TURNS) / n, 2),
        "repeated_call_share": round(
            sum(1 for p in profiles if p.get("repeated_calls")) / n, 2),
        "tools": tools,
    }
