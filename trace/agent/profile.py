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
    """How many times the agent took a turn — which is how many times it re-sent its
    whole prefix, and therefore the unit cost is actually paid in.

    Counted from the agent's MESSAGES, not from usage payloads. Counting usage was the
    first attempt and it was wrong on the real platform: five production sessions each
    reported exactly one usage payload carrying the session's whole total, so every
    session read as a single turn — one of them a single turn spending 1.75 million
    tokens, which no context window would hold. The platform reports usage per session,
    not per turn.

    Falls back to one turn per tool result plus the final answer, for a stream that
    carries tool calls but no assistant text. Never below 1: a session that ran at all
    took at least one turn.
    """
    msgs = sum(1 for e in (events or [])
               if isinstance(e, dict) and "agent.message" in str(e.get("type") or "").lower())
    if msgs:
        return msgs
    results = sum(1 for e in (events or [])
                  if isinstance(e, dict) and "tool_result" in str(e.get("type") or "").lower())
    return max(results + 1, 1) if results else 1


def per_turn_usage_reported(events):
    """Whether the platform broke usage down by turn, or reported one session total.

    Load bearing for how the prefix is described: with a session total there is no first
    turn to read the prefix off, and a number labelled "prefix" that is really the whole
    session's input would tell the engine memory is a rounding error on every agent it
    ever sees."""
    return len(_usage_events(events)) > 1


def tokens_per_turn(events):
    """``[{turn, input_tokens, output_tokens}]``, oldest first — EMPTY when the platform
    reported one session-level total, because then there is no per-turn breakdown to
    give and inventing one from the aggregate would be a fabrication.

    Cache reads and cache creation count as input, the same way session_token_usage
    counts them, or a cached prefix would look free when it is merely cheaper.
    """
    if not per_turn_usage_reported(events):
        return []
    out = []
    for i, e in enumerate(_usage_events(events), 1):
        u = e["usage"]
        out.append({"turn": i,
                    "input_tokens": int(u.get("input_tokens") or 0)
                    + int(u.get("cache_read_input_tokens") or 0)
                    + int(u.get("cache_creation_input_tokens") or 0),
                    "output_tokens": int(u.get("output_tokens") or 0)})
    return out


def _total_input(events):
    """Every input token the session paid for, however the platform chose to report it."""
    total = 0
    for e in _usage_events(events):
        u = e["usage"]
        total += (int(u.get("input_tokens") or 0)
                  + int(u.get("cache_read_input_tokens") or 0)
                  + int(u.get("cache_creation_input_tokens") or 0))
    return total


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
    return {
        "session_id": session_id,
        # turns(), not len(tokens_per_turn): the latter is empty whenever the platform
        # reports one session-level usage total, which is always here.
        "turns": turns(events),
        "tokens_per_turn": tokens_per_turn(events),
        "tool_calls": [{"name": c["name"], "args": c["args"][:200],
                        "result_chars": c["result_chars"]} for c in calls],
        "repeated_calls": [{"name": r["name"], "args": r["args"][:200],
                            "count": r["count"], "result_chars": r["result_chars"]}
                           for r in repeated_calls(calls)],
        "documents_referenced": sorted(referenced_docs(events, doc_paths)),
    }


def prefix(events, memory_docs):
    """What the agent re-sends every turn, and how much of it the engine can reach.

    The honest version of this was hard-won. It used to report the first turn's input as
    ``prefix_tokens_measured``, which is right only if the platform breaks usage down by
    turn. It does not: it sends ONE payload per session carrying the session total. So
    the "prefix" was the whole session's input, and dividing a 10K memory store by a
    1.75M-token session said memory was 0.6% of cost -- on every agent, always. The one
    field built to tell the engine whether memory is worth touching told it no, every
    time, confidently and wrongly.

    So the per-turn prefix is reported only when it can actually be measured, and is None
    with a stated reason otherwise. What is always computable is better anyway:
    memory is re-sent on EVERY turn, so ``memory_reads_estimated`` is the store's size
    times the turn count, and its share of the session's total input is the question the
    engine is actually asking -- what fraction of everything this session paid for was
    memory being read again.
    """
    n_turns = turns(events)
    total_in = _total_input(events)
    chars = sum(len(str(v or "")) for v in (memory_docs or {}).values())
    est = int(chars / 4)
    reads = est * n_turns
    per_turn = tokens_per_turn(events)
    measured = per_turn[0]["input_tokens"] if per_turn else None
    share = round(min(reads / total_in, 1.0), 3) if total_in else None
    return {
        "turns": n_turns,
        "total_input_tokens": total_in or None,
        "memory_store_chars": chars,
        "memory_store_tokens_estimated": est,
        "memory_reads_estimated": reads,
        "memory_share_of_input": share,
        "prefix_tokens_measured": measured,
        "note": ("memory_share_of_input is the share of this session's total input that "
                 "was the memory store, counting it once per turn because it is re-sent "
                 "every turn. The memory figure is characters over four. "
                 + ("prefix_tokens_measured is the first turn's reported input."
                    if measured is not None else
                    "prefix_tokens_measured is unavailable: this platform reports one "
                    "usage total per session, not per turn, so there is no first turn to "
                    "read it from. The rest of the prefix is the system prompt and the "
                    "tool schemas, neither of which the engine may change.")),
    }


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
