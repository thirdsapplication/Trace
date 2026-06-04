"""Keystroke coalescing — adapted from the Appendix A prototype.

The proven logic: buffer characters for the current segment, apply backspaces,
mark ``[ENTER]`` / ``[TAB]`` inline, drop other modifier/special keys, and flush
the buffer on every window change. Flushing on window change is exactly what
produces the *fragmented* typed values seen in testing (the worker flips between
a source doc and a destination mid-entry); stitching those back together is the
reconstruction layer's job, not ours.

This module differs from the prototype in two ways required by the v1 schema:
- it emits a normalized ``keystroke`` raw event ``{text, window, ts_end}`` per
  segment (not a flat ``[TYPED] ...`` string), and
- text passes through redaction in ``RawWriter`` before it is written.
"""

import threading

from .raw_writer import now_iso

# pynput key reprs -> the inline markers we keep.
SPECIAL = {
    "Key.space": " ",
    "Key.enter": " [ENTER] ",
    "Key.tab": " [TAB] ",
}


class KeystrokeCoalescer:
    """Coalesces individual key events into typed segments and emits one
    ``keystroke`` raw event per segment via the shared writer.

    Thread-safe: ``on_key`` (keyboard listener thread) and ``flush`` (window-poll
    thread, on a window change) can race, so both take an internal lock.
    """

    def __init__(self, writer):
        self._writer = writer
        self._buf = []
        self._window = ""          # window title the current segment is being typed into
        self._seg_start_ts = None  # arrival ts of the first key in the buffer
        self._lock = threading.Lock()

    def set_window(self, window_title):
        """Record which window subsequent keystrokes are typed into. Called by the
        window poller; the actual flush happens via ``flush()`` so the caller can
        order it relative to the window event."""
        with self._lock:
            self._window = window_title

    def on_key(self, key_repr, ts):
        """Feed one key. ``key_repr`` is the pynput-style string
        (e.g. ``'a'``, ``'Key.space'``, ``'Key.backspace'``)."""
        with self._lock:
            if not self._buf:
                self._seg_start_ts = ts
            if key_repr == "Key.backspace":
                if self._buf:
                    self._buf.pop()
            elif key_repr in SPECIAL:
                self._buf.append(SPECIAL[key_repr])
            elif key_repr.startswith("Key."):
                pass  # ignore other modifier/special keys
            else:
                self._buf.append(key_repr)

    def flush(self, ts_end=None):
        """Emit the buffered segment (if any) as a ``keystroke`` raw event and
        clear the buffer. Called on window change and at shutdown."""
        with self._lock:
            text = "".join(self._buf).strip()
            window = self._window
            start = self._seg_start_ts
            self._buf.clear()
            self._seg_start_ts = None
        if not text:
            return None
        return self._writer.write(
            "keystroke",
            {"text": text, "window": window, "ts_end": ts_end or now_iso()},
            ts=start,
        )
