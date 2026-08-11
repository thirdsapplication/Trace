"""Raw-event source registry + capture observability (Item 3).

A single place that records, per raw source type, which capture *origin(s)* can
produce it. This metadata — not a hardcoded check tuned to any run's counts — is
what the fail-loud heuristic keys off, so adding a new source later means
registering its origin here, never editing the summary/warning logic.

Origins:
- ``local``   : the local macOS process (window focus, keystrokes, clipboard)
- ``browser`` : the Chrome extension via the collector (clicks, focused-element
                targets, in-page structured reads); clipboard can come from both
"""

SOURCE_ORIGINS = {
    "window": {"local"},
    "keystroke": {"local"},
    "clipboard": {"local", "browser"},
    "click": {"browser"},
    "target": {"browser"},
    "structured_read": {"browser"},
}


def browser_only_sources():
    """Source types that *only* the browser extension can produce."""
    return {s for s, origins in SOURCE_ORIGINS.items() if origins == {"browser"}}


def local_capable_sources():
    """Source types the local capture process can produce."""
    return {s for s, origins in SOURCE_ORIGINS.items() if "local" in origins}


def summarize_sources(rows):
    """Count raw events by source type. Generic over whatever sources appear —
    including any not yet in the registry (counted under their own name) — so new
    source types need no change here."""
    counts = {}
    for r in rows:
        src = r.get("source", "unknown") if isinstance(r, dict) else "unknown"
        counts[src] = counts.get(src, 0) + 1
    return counts


def capture_health(counts, min_browser_events=1, rows=None):
    """Summarize a completed session and flag (non-blocking) two silent failures.

    1. Browser capture inactive: local events present but fewer than
       ``min_browser_events`` of the browser-only source types.
    2. Blank window titles: ``window`` events present but none carries a title.
       ``kCGWindowName`` is only populated for a process holding Screen Recording,
       so without it the poller keeps emitting events that say nothing about where
       the work happened — and since it only emits on CHANGE, a whole session inside
       one browser collapses to a single blank event. Needs ``rows`` to detect.

    Thresholds are configurable, not tuned to any run's specific numbers.
    """
    browser_only = browser_only_sources()
    local_sources = local_capable_sources()

    browser_only_count = sum(counts.get(s, 0) for s in browser_only)
    local_count = sum(counts.get(s, 0) for s in local_sources)

    messages = []
    if local_count > 0 and browser_only_count < min_browser_events:
        messages.append("Browser capture may not have been active this session: "
                        f"{local_count} local event(s) but {browser_only_count} "
                        "browser-only event(s). Check that the Chrome extension is "
                        "installed and connected to the collector.")

    window_count = titled_window_count = 0
    if rows:
        for r in rows:
            if not isinstance(r, dict) or r.get("source") != "window":
                continue
            window_count += 1
            if str((r.get("payload") or {}).get("title") or "").strip():
                titled_window_count += 1
        if window_count and not titled_window_count:
            messages.append(f"Window titles are blank ({window_count} window event(s), "
                            "none titled), so Trace cannot tell which app or page the "
                            "work happened in. Grant Screen Recording to the Trace app "
                            "in System Settings → Privacy & Security, then fully quit "
                            "and relaunch it.")

    return {
        "by_source": counts,
        "total": sum(counts.values()),
        "local_count": local_count,
        "browser_only_count": browser_only_count,
        "browser_only_sources": sorted(browser_only),
        "window_count": window_count,
        "titled_window_count": titled_window_count,
        "warning": bool(messages),
        "message": " ".join(messages) or None,
    }
