"""Raw-event source registry + capture observability (Item 3).

A single place that records, per raw source type, which capture *origin(s)* can
produce it. This metadata — not a hardcoded check tuned to any run's counts — is
what the fail-loud heuristic keys off, so adding a new source later means
registering its origin here, never editing the summary/warning logic.

Origins:
- ``local``   : the local macOS process — window focus, keystrokes, clipboard, and
                (via the Accessibility API) focused elements, resolved clicks and
                structured reads in *native* apps
- ``browser`` : the Chrome extension via the collector — clicks, focused-element
                targets, in-page structured reads; clipboard can come from both

Note what changed when Accessibility capture landed: ``click``, ``target`` and
``structured_read`` are no longer browser-only. The source type therefore no
longer identifies the capturer, which is why every event now records its origin
(``raw_writer.write(..., origin=)``) and why the health check below asks "did we
capture what the actions MEANT?" rather than "was the extension connected?".
"""

SOURCE_ORIGINS = {
    "window": {"local"},
    "keystroke": {"local"},
    "clipboard": {"local", "browser"},
    "click": {"local", "browser"},
    "target": {"local", "browser"},
    "structured_read": {"local", "browser"},
}

# The sources that carry what an action MEANT — which control was pressed, which
# field was filled, what the screen showed. Without at least one of these a
# session is window titles and loose keystrokes: enough to see that work happened,
# never enough to rebuild it as a workflow. This set is the thing worth alerting
# on, and it is deliberately independent of which capturer supplied it.
SEMANTIC_SOURCES = {"click", "target", "structured_read"}


def semantic_sources():
    """Source types that carry the meaning of an action (see SEMANTIC_SOURCES)."""
    return set(SEMANTIC_SOURCES)


def browser_only_sources():
    """Source types that *only* the browser extension can produce.

    Empty since Accessibility capture landed — kept because the emptiness is the
    point: any caller still branching on this is asking a question that no longer
    has a meaningful answer, and should ask ``semantic_sources()`` instead.
    """
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


def summarize_origins(rows):
    """Count raw events by recorded origin.

    Events captured before origin stamping (or by a capturer that omits it) count
    under ``"unknown"`` rather than being silently attributed to either capturer —
    an old session must not read as proof that a capturer was live.
    """
    counts = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        origin = (r.get("payload") or {}).get("origin") or "unknown"
        counts[origin] = counts.get(origin, 0) + 1
    return counts


def capture_health(counts, min_semantic_events=1, rows=None, min_browser_events=None):
    """Summarize a completed session and flag two silent failures.

    1. No semantic capture: local events present but fewer than
       ``min_semantic_events`` events of the semantic source types. Neither the
       browser extension nor the Accessibility capturer was producing, so the
       session records that work happened without recording what it was.
    2. Blank window titles: ``window`` events present but none carries a title.
       ``kCGWindowName`` is only populated for a process holding Screen Recording,
       so without it the poller keeps emitting events that say nothing about where
       the work happened — and since it only emits on CHANGE, a whole session inside
       one browser collapses to a single blank event. Needs ``rows`` to detect.
       (Accessibility capture reads the title via ``kAXTitleAttribute`` instead,
       which needs no Screen Recording — so this only fires when neither path
       supplied a title.)

    ``min_browser_events`` is the former name of ``min_semantic_events``, accepted
    so existing callers keep working. Thresholds are configurable, not tuned to any
    run's specific numbers.
    """
    if min_browser_events is not None:
        min_semantic_events = min_browser_events

    semantic = semantic_sources()
    local_sources = local_capable_sources()

    semantic_count = sum(counts.get(s, 0) for s in semantic)
    local_count = sum(counts.get(s, 0) for s in local_sources)
    by_origin = summarize_origins(rows)

    messages = []
    if local_count > 0 and semantic_count < min_semantic_events:
        messages.append("Nothing captured what your actions meant this session: "
                        f"{local_count} event(s) recorded but {semantic_count} "
                        "carrying a clicked control, a filled field or page content. "
                        "Check that the Chrome extension is connected, or that Trace "
                        "has Accessibility permission for native apps.")

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
                            "work happened in. Grant Screen Recording or Accessibility "
                            "to the Trace app in System Settings → Privacy & Security, "
                            "then fully quit and relaunch it.")

    return {
        "by_source": counts,
        "by_origin": by_origin,
        "total": sum(counts.values()),
        "local_count": local_count,
        "semantic_count": semantic_count,
        "semantic_sources": sorted(semantic),
        # Retained under the old names so existing readers keep working; both now
        # describe semantic capture regardless of which capturer supplied it.
        "browser_only_count": semantic_count,
        "browser_only_sources": sorted(semantic),
        "window_count": window_count,
        "titled_window_count": titled_window_count,
        "warning": bool(messages),
        "message": " ".join(messages) or None,
    }
