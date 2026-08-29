"""Validity gate — which sessions count as a MEASUREMENT of the agent's efficiency.

The self-improvement engine's only objective is spending fewer tokens per session. That
objective is dangerous on its own: the cheapest way to spend fewer tokens is to do less
work, or to do it worse. Nothing in this module measures quality. What it does instead is
refuse to let bad work count as a cheap success — a session a human had to reject, a run
that failed or stalled, a session whose token figure was never reported, is not evidence
about efficiency and is dropped before any score is computed.

Everything here is a DATABASE FACT and everything here runs BEFORE any model call. A gate
that is a judgement drifts toward whatever answer the loop wants, and the loop wants to
pass.

Excluded sessions are not discarded — they go to the analyst as EVIDENCE. A proposal a
human refused is the clearest signal in a batch about what the memory store is missing,
and a fact that stops a rejection also stops the reasoning the agent burns getting there.
They simply never enter a number.

NARROWER THAN DESIGNED, on purpose and on record. The design called for excluding sessions
a human EDITED or CORRECTED as well as rejected. Trace records neither: approval is binary
(app.py's decision handler writes 'approved' or 'rejected' and nothing else) and there is
no correction path at all. So this catches the loud third of human intervention and misses
the quiet two thirds. The predicates are a list so an ``edited`` condition drops in the day
that event exists, without reworking anything around it.
"""

from . import activity

# Ordered: the first matching reason is the one reported, so the most specific and most
# actionable comes first. A rejected proposal tells the analyst something; "no usage
# reported" tells it only that the platform was quiet.
REJECTED = "rejected"
FAILED = "failed"
NEEDS_INPUT = "needs_input"
NO_USAGE = "no_usage_reported"

EXCLUSION_REASONS = (REJECTED, FAILED, NEEDS_INPUT, NO_USAGE)


def rejected_sessions(pending):
    """Session ids with at least one REJECTED proposal, from pending_actions rows.

    One rejection is enough to disqualify the session: the human intervened, so whatever
    the agent spent getting there is not the cost of work done right."""
    out = set()
    for item in (pending or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") != "rejected":
            continue
        sid = item.get("run_session_id")
        if sid:
            out.add(str(sid))
    return out


def exclusion_reason(run, events, rejected_ids):
    """Why this session is not a measurement, or None when it is one.

    ``run`` is an agent_runs row in caller shape, ``events`` its session event list.
    Never raises: an unreadable session is excluded, not an exception — the gate runs on
    every cycle and must not be the thing that ends one.
    """
    try:
        sid = str((run or {}).get("run_session_id") or "")
        if sid and sid in (rejected_ids or set()):
            return REJECTED
        status = str((run or {}).get("status") or "")
        if status == "failed":
            return FAILED
        if status == "needs_input":
            return NEEDS_INPUT
        # needs_approval is NOT an exclusion. An agent that staged an action and waited is
        # an agent doing exactly what it was told; only a human REFUSING one is evidence
        # that something went wrong.
        usage = activity.session_token_usage(events or [])
        if usage.get("estimated"):
            # A character-count estimate is not a measurement. Comparing an estimated
            # batch against a measured baseline would move the number for a reason that
            # has nothing to do with the agent.
            return NO_USAGE
        return None
    except Exception:                                      # noqa: BLE001 — exclude, never raise
        return NO_USAGE


def partition(pairs, pending):
    """Split ``[(run, events)]`` into what may be measured and what may not.

    Returns ``{"valid": [(run, events)], "excluded": [(run, events, reason)],
    "valid_rate": float}``. ``valid_rate`` is valid over total, 0.0 for an empty batch.

    The rate is not decoration. It is a PRECONDITION on keeping a change: excluding
    damaged sessions hides damage rather than catching it, so a change that makes the
    agent sloppier draws rejections, those sessions leave the pool, and the survivors can
    show a better score from a change that only did harm. The excluded ones are also
    systematically the high-novelty, high-token sessions, so removing them lowers the mean
    on its own.
    """
    rejected_ids = rejected_sessions(pending)
    valid, excluded = [], []
    for run, events in (pairs or []):
        reason = exclusion_reason(run, events, rejected_ids)
        if reason:
            excluded.append((run, events, reason))
        else:
            valid.append((run, events))
    total = len(valid) + len(excluded)
    return {"valid": valid, "excluded": excluded,
            "valid_rate": (len(valid) / total) if total else 0.0}
