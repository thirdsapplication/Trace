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

from .raw_writer import now_iso


def frontmost():
    """Return ``(app, title)`` for the active window, or ``None`` on failure."""
    try:
        from AppKit import NSWorkspace
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )

        ws = NSWorkspace.sharedWorkspace()
        active = ws.frontmostApplication()
        if active is None:
            return None

        app_name = active.localizedName() or ""
        pid = active.processIdentifier()

        # Find the frontmost normal-layer window for this app.
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
        title = ""
        for w in windows:
            if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", -1) == 0:
                title = w.get("kCGWindowName") or ""
                if title:
                    break  # take the first named window

        return (app_name, title)
    except Exception:
        return None


def window_loop(stop, writer, coalescer, interval=0.5):
    """Poll the active window and log changes until ``stop`` is set."""
    last = None
    while not stop.is_set():
        cur = frontmost()
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
