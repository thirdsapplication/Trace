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


def augment_document_opens(event_log, raw, persistence_seconds=DEFAULT_OPEN_PERSISTENCE_SECONDS):
    """Return ``event_log`` plus synthesized open events for sustained, otherwise
    unrepresented window focuses. ``raw`` is the ordered raw-event list (each
    ``{id, ts, source, payload}``). Pure function; does not mutate ``event_log``
    in place beyond appending new events to a copy.

    The caller is responsible for re-sequencing ids afterwards.
    """
    windows = [(i, r) for i, r in enumerate(raw) if r["source"] == "window"]
    if not windows:
        return list(event_log)

    last_ts = raw[-1]["ts"]
    # An existing event "represents" a window if it is *about* that window
    # (same app + title) — not merely cites it as incidental evidence.
    represented = {(e.get("app", ""), e.get("window", "")) for e in event_log}

    out = list(event_log)
    seen_keys = set()  # one synthesized open per distinct (app, title)

    for pos, (idx, w) in enumerate(windows):
        app = w["payload"].get("app", "")
        title = w["payload"].get("title", "")
        key = (app, title)

        # dwell = until the next focus change (or end of session for the last one)
        next_ts = windows[pos + 1][1]["ts"] if pos + 1 < len(windows) else last_ts
        dwell = _dwell_seconds(w["ts"], next_ts)
        if dwell is None or dwell < persistence_seconds:
            continue  # brief flicker / sub-threshold oscillation -> no event
        if key in represented or key in seen_keys:
            continue  # already surfaced (e.g. a browser-doc navigation) -> no dup

        seen_keys.add(key)
        out.append({
            "id": "",  # assigned by resequence
            "ts_start": w["ts"],
            "ts_end": next_ts,
            "app": app,
            "window": title,
            "action": OPEN_ACTION,
            # grounded only in the window title; contents are never invented
            "detail": f'Opened/viewed "{title}".' if title else f"Opened/viewed {app}.",
            "evidence": [w["id"]],
            "confidence": "medium",
        })

    return out
