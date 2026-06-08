"""Symmetric document-open detection (Defect 2).

The LLM reliably emits an action when a document is opened *in the browser* (it is
in focus and content follows), but it drops documents opened in a *separate native
application* — the window appears in the raw stream, yet no "opened/viewed X"
action is produced, even when focus persists. The treatment should not be
asymmetric by application.

This pass closes that gap deterministically and symmetrically: it scans the
ordered raw stream for focus/window transitions to a distinct window that
*persists beyond a brief flicker* and, where no existing event already represents
that window, synthesizes a grounded "opened/viewed «window»" action. It is driven
purely by persistence/duration on window transitions — no app allowlist, no file
extensions, no app-name branching — so browser-rendered and native-application
documents go through the same rule. Contents are never fabricated: the action
records only that the window was opened, plus the window-title identifier.
"""

from datetime import datetime

# Default dwell a window must hold focus to count as a sustained open (seconds).
# General threshold, not tuned to any particular run; configurable by the caller.
DEFAULT_OPEN_PERSISTENCE_SECONDS = 3.0

# Controlled-vocabulary action for an open/consult (domain-neutral).
OPEN_ACTION = "open_document"


def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _dwell_seconds(this_ts, next_ts):
    a, b = _parse(this_ts), _parse(next_ts)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _evidence_index_set(ev, idx_of):
    return {idx_of[r] for r in ev.get("evidence", []) if r in idx_of}


def _represents_focus(ev, idx_of, raw, lo, hi):
    """True when ``ev`` already represents the window focused over raw rows
    ``[lo, hi)`` — i.e. an action that happened *in* this window.

    Structural test, robust to model-normalized titles/app strings (no string
    matching): ``ev`` represents the focus iff it cites at least one row inside the
    span AND every *content* (non-window) row it cites lies inside the span. A
    navigation cites the focus's window row with no content (vacuously inside). A
    data-entry/compose/reply in this window cites content rows inside the span. An
    action anchored elsewhere that merely cites this window row incidentally has
    content rows *outside* the span, so it does NOT represent this focus."""
    ev_idxs = _evidence_index_set(ev, idx_of)
    if not ev_idxs:
        return False
    in_span = any(lo <= k < hi for k in ev_idxs)
    if not in_span:
        return False
    content_idxs = [k for k in ev_idxs if raw[k]["source"] != "window"]
    return all(lo <= k < hi for k in content_idxs)


def augment_document_opens(event_log, raw, persistence_seconds=DEFAULT_OPEN_PERSISTENCE_SECONDS):
    """Return ``event_log`` plus synthesized open events for window focuses that are
    **sustained** and **not already represented** by another action over that
    focus's rows. ``raw`` is the ordered raw-event list (each
    ``{id, ts, source, payload}``). Pure function; appends to a copy.

    A standalone open is emitted only when, during a window's focus span, no other
    action represents it (so a window the worker immediately acts in is covered by
    that action, not double-counted), the focus persists beyond the flicker
    threshold, and the title is not blank/anonymous. One open per distinct window.
    The caller re-sequences ids afterwards.
    """
    window_positions = [k for k, r in enumerate(raw) if r["source"] == "window"]
    if not window_positions:
        return list(event_log)

    idx_of = {r["id"]: k for k, r in enumerate(raw)}
    out = list(event_log)
    seen_keys = set()  # one synthesized open per distinct (app, title)

    for n, i in enumerate(window_positions):
        w = raw[i]
        app = w["payload"].get("app", "")
        title = w["payload"].get("title", "")

        if not (title or "").strip():
            continue  # blank/anonymous window title -> not a document, suppress

        # focus span = [i, j): from this window event up to the next focus change
        j = window_positions[n + 1] if n + 1 < len(window_positions) else len(raw)
        dwell = _dwell_seconds(w["ts"], raw[j]["ts"] if j < len(raw) else raw[-1]["ts"])
        if dwell is None or dwell < persistence_seconds:
            continue  # brief flicker / sub-threshold oscillation -> no event

        key = (app, title)
        if key in seen_keys:
            continue
        # suppress if any existing action already represents this focus
        if any(_represents_focus(e, idx_of, raw, i, j) for e in event_log):
            continue

        seen_keys.add(key)
        out.append({
            "id": "",  # assigned by resequence
            "ts_start": w["ts"],
            "ts_end": raw[j]["ts"] if j < len(raw) else w["ts"],
            "app": app,
            "window": title,
            "action": OPEN_ACTION,
            # grounded only in the window title; contents are never invented
            "detail": f'Opened/viewed "{title}".',
            "evidence": [w["id"]],
            "confidence": "medium",
        })

    return out
