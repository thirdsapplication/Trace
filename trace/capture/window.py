"""Active-window capturer (macOS) — the capture "spine".

Polls the frontmost app + window title via AppleScript (``osascript``), as in the
Appendix A prototype, and emits a normalized ``window`` raw event
``{app, title}`` only when it changes. The window stream is the cheap, reliable
carrier of document/app/email identity (subjects, filenames, sheet names).

On each change we (1) flush the keystroke buffer so the just-finished typed
segment is attributed to the window it was typed in, then (2) emit the window
event, then (3) point the coalescer at the new window.
"""

import subprocess

from .raw_writer import now_iso

_FRONTMOST_SCRIPT = (
    'tell application "System Events"\n'
    ' set frontApp to name of first process whose frontmost is true\n'
    ' set winTitle to ""\n'
    ' try\n'
    '  tell process frontApp to set winTitle to name of front window\n'
    ' end try\n'
    ' return frontApp & " | " & winTitle\n'
    'end tell'
)


def frontmost():
    """Return ``(app, title)`` for the active window, or ``None`` on failure."""
    try:
        raw = subprocess.check_output(
            ["osascript", "-e", _FRONTMOST_SCRIPT],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return None
    if not raw:
        return None
    app, _, title = raw.partition(" | ")
    return app.strip(), title.strip()


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
