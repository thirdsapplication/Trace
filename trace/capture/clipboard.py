"""Clipboard capturer (macOS, outside-browser).

Closes the copy-paste blind spot for native apps. Polls the macOS pasteboard via
``pbpaste``; when the contents change, emits a ``clipboard`` raw event.

Limitation (documented): polling the system pasteboard can observe *that the
clipboard changed* (a copy or cut happened somewhere) but cannot by itself tell
copy from cut, nor observe paste. So local pasteboard changes are recorded as
``op: "copy"``. Precise ``copy``/``cut``/``paste`` ops with correct direction come
from the browser extension, which hooks the actual clipboard events in-page. This
local poll exists so a native-app copy is not completely invisible.
"""

import subprocess

from .raw_writer import now_iso


def read_pasteboard():
    """Return current pasteboard text, or ``None`` if unavailable."""
    try:
        return subprocess.check_output(
            ["pbpaste"], stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        return None


def clipboard_loop(stop, writer, interval=0.5):
    """Poll the pasteboard and emit a clipboard event on change."""
    last = read_pasteboard()
    while not stop.is_set():
        cur = read_pasteboard()
        if cur is not None and cur != last and cur.strip():
            writer.write("clipboard", {"op": "copy", "content": cur}, ts=now_iso())
            last = cur
        elif cur is not None:
            last = cur
        stop.wait(interval)
