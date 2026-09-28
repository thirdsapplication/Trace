"""Active-window capturer (macOS) — the capture "spine".

Polls the frontmost app + window title and emits a normalized ``window`` raw
event ``{app, title}`` only when it changes. The window stream is the cheap,
reliable carrier of document/app/email identity (subjects, filenames, sheet
names).

Uses the Quartz CGWindowList API + NSWorkspace for reliable window title
access on macOS Sequoia (15+). The older AppleScript/System Events approach
broke in Sequoia — ``name of front window`` returns empty for most apps.
Quartz works with Screen Recording permission.

On each change we (1) flush the keystroke buffer so the just-finished typed
segment is attributed to the window it was typed in, then (2) emit the window
event, then (3) point the coalescer at the new window.
"""

import sys

from .raw_writer import now_iso


def frontmost_pid():
    """PID of the frontmost application, or None.

    Split out because Accessibility capture needs the PID specifically: the
    system-wide AX element (``AXUIElementCreateSystemWide``) returns
    kAXErrorCannotComplete in this process, exactly as NSWorkspace silently
    stalls here — both want a run loop the capture sidecar does not pump. Reading
    the PID from the same CGWindowList call that already works and creating a
    per-application AX element from it is the path that does work. Kept beside
    ``frontmost`` so the two never disagree about which app is in front.
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )

        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        ) or []
        for w in windows:
            if w.get("kCGWindowLayer", -1) == 0 and w.get("kCGWindowOwnerName"):
                return w.get("kCGWindowOwnerPID")
    except Exception:
        pass
    return None


def frontmost(title_fallback=None):
    """Return ``(app, title)`` for the active window, or ``None`` on failure.

    ``title_fallback`` is an optional zero-arg callable consulted only when Quartz
    yields a blank title — in practice ``AXAdapter.focused_window_title``, which
    reads the same title under the Accessibility grant. That is what demotes Screen
    Recording from load-bearing to optional: without the fallback a missing Screen
    Recording permission silently collapses a whole session to one blank event.

    Reads BOTH values from CGWindowList rather than asking NSWorkspace which app is
    active. NSWorkspace.frontmostApplication() refreshes off the AppKit run loop, and
    the capture sidecar is a headless process Tauri spawns with no run loop pumping —
    there it returns whatever was frontmost when the process started and never updates
    again. Symptom: one window event per session, permanently naming the Trace app,
    while the worker switches between Chrome, Docs and Sheets (verified 2026-08-11 by
    driving app switches against a live capture — 3 switches, 0 events).

    CGWindowList's on-screen list is ordered front-to-back, so the first layer-0 window
    IS the frontmost one. ``kCGWindowOwnerName`` needs no permission; ``kCGWindowName``
    (the title) is only populated with Screen Recording, so a blank title here means
    that permission is missing — the app name still tracks correctly either way.
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )

        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
        if not windows:
            return None

        front = None                          # (owner_pid, app_name) of the top window
        for w in windows:
            if w.get("kCGWindowLayer", -1) != 0:
                continue                      # menu bar, dock, overlays
            app_name = w.get("kCGWindowOwnerName") or ""
            if app_name:
                front = (w.get("kCGWindowOwnerPID"), app_name)
                break
        if front is None:
            return None

        # The frontmost window itself is often unnamed (Chrome does this), so take the
        # first NAMED window belonging to the same app — matching the old behavior.
        pid, app_name = front
        for w in windows:
            if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer", -1) != 0:
                continue
            title = w.get("kCGWindowName") or ""
            if title:
                return (app_name, title)
        return (app_name, _fallback_title(title_fallback))
    except Exception:
        return None


def _fallback_title(title_fallback):
    """Ask the fallback for a title, tolerating any failure — a broken fallback
    must degrade to today's blank title, never take the window poller down."""
    if title_fallback is None:
        return ""
    try:
        return title_fallback() or ""
    except Exception:
        return ""


def window_loop(stop, writer, coalescer, interval=0.5, title_fallback=None):
    """Poll the active window and log changes until ``stop`` is set."""
    last = None
    while not stop.is_set():
        cur = frontmost(title_fallback)
        if cur and cur != last:
            ts = now_iso()
            # 1. close out the typed segment in the *previous* window.
            coalescer.flush(ts_end=ts)
            # 2. record the navigation.
            app, title = cur
            writer.write("window", {"app": app, "title": title}, ts=ts)
            # 3. attribute subsequent keystrokes to the new window.
            coalescer.set_window(title)
            last = cur
        stop.wait(interval)


# Windows reads the same two facts from the window manager instead of Quartz; the
# poll loop above is shared. Rebinding here means everything that imports these
# names from this module — window_loop, ax.AXAdapter, permissions — gets the right
# one without a platform check of its own.
if sys.platform == "win32":
    from .win_window import frontmost, frontmost_pid  # noqa: E402,F811
