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

# Modifiers that make a character key a SHORTCUT rather than typed text. Without
# this, Cmd+C / Cmd+V arrive as the bare chars 'c' and 'v' and land in the buffer,
# so copy-paste-heavy work records as `cvcvcvcv` — which reads to discovery as
# garbled input and has made it refuse to automate an otherwise clean workflow.
# Shift is deliberately absent: it produces ordinary uppercase characters.
SHORTCUT_MODIFIERS = {
    "Key.cmd", "Key.cmd_l", "Key.cmd_r",
    "Key.ctrl", "Key.ctrl_l", "Key.ctrl_r",
    "Key.alt", "Key.alt_l", "Key.alt_r", "Key.alt_gr",
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
        self._mods = set()         # shortcut modifiers currently held down
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
            if key_repr in SHORTCUT_MODIFIERS:
                self._mods.add(key_repr)
                return
            # A character typed while cmd/ctrl/alt is held is a shortcut (Cmd+C,
            # Cmd+V, Cmd+Tab), not text the worker entered. Drop it rather than
            # letting it pollute the segment.
            #
            # Except under AltGr. On European keyboards AltGr is how @, €, { and \
            # are typed, and Windows reports it as Ctrl+Alt — so without this an
            # email address typed on a German or French layout loses its @.
            if (self._mods and not key_repr.startswith("Key.")
                    and "Key.alt_gr" not in self._mods):
                return
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

    def on_key_release(self, key_repr):
        """Release half of shortcut-modifier tracking. Listeners that register only
        ``on_press`` still work — a held modifier is simply never cleared, so wire
        this up wherever ``on_key`` is wired."""
        with self._lock:
            self._mods.discard(key_repr)

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
