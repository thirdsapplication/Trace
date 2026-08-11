"""Trace local capture process (macOS).

The local half of Engine 1's capture spine: keystrokes + window changes +
outside-browser clipboard, written to the shared ``raw_events.jsonl`` in the
normalized ``{ts, source, payload}`` schema with redaction applied at write time.

The browser half (clicks, element targets, in-browser clipboard, scoped
structured reads) is captured by the Chrome extension and POSTed to the collector
(``trace.capture.collector``), which appends to the same file. Run both processes
together for a full session; run just this one for a keystroke+window+clipboard
capture.

Privacy controls (Section 5a):
- All text is redacted at write time (``redaction.py``).
- Native password fields are a documented v1 gap (no OS-accessibility hook);
  browser password fields are dropped by the extension.
- Obvious pause/resume + stop:  Ctrl-Shift-P toggles pause, Ctrl-C stops.

Requires: ``pip install pynput`` and macOS Input Monitoring permission for the
terminal app. ``osascript`` / ``pbpaste`` are stock macOS.

Run:
    python -m trace.capture.capture --out captures/raw_events.jsonl
"""

import argparse
import threading
import time

from pynput import keyboard

from .clipboard import clipboard_loop
from .keystrokes import KeystrokeCoalescer
from .raw_writer import RawWriter, now_iso
from .window import window_loop


def _key_to_repr(key):
    """Normalize a pynput key event to the string form the coalescer expects."""
    try:
        if key.char is not None:
            return key.char
    except AttributeError:
        pass
    return str(key)


def main():
    ap = argparse.ArgumentParser(description="Trace local capture (macOS)")
    ap.add_argument("--out", default="captures/raw_events.jsonl",
                    help="shared raw_events.jsonl path")
    ap.add_argument("--append", action="store_true",
                    help="append to an existing stream instead of resetting it")
    ap.add_argument("--no-clipboard", action="store_true",
                    help="disable the outside-browser clipboard poll")
    args = ap.parse_args()

    writer = RawWriter(args.out, reset=not args.append)
    coalescer = KeystrokeCoalescer(writer)
    stop = threading.Event()

    # Window poll (also drives keystroke-segment flushing on navigation).
    threading.Thread(target=window_loop, args=(stop, writer, coalescer),
                     daemon=True).start()
    if not args.no_clipboard:
        threading.Thread(target=clipboard_loop, args=(stop, writer),
                         daemon=True).start()

    # Hotkey state for pause toggle (Ctrl-Shift-P).
    pressed = set()

    def on_press(key):
        pressed.add(key)
        ctrl = (keyboard.Key.ctrl in pressed or keyboard.Key.ctrl_l in pressed
                or keyboard.Key.ctrl_r in pressed)
        shift = (keyboard.Key.shift in pressed or keyboard.Key.shift_l in pressed
                 or keyboard.Key.shift_r in pressed)
        is_p = getattr(key, "char", None) in ("p", "P", "\x10")
        if ctrl and shift and is_p:
            if writer.paused:
                writer.resume()
                print("[capture] RESUMED")
            else:
                coalescer.flush(ts_end=now_iso())  # don't carry buffer across a pause
                writer.pause()
                print("[capture] PAUSED — keystrokes/window/clipboard not recorded")
            return
        # ordinary key -> coalesce (suppressed while paused at the writer level)
        if not writer.paused:
            coalescer.on_key(_key_to_repr(key), now_iso())

    def on_release(key):
        pressed.discard(key)
        coalescer.on_key_release(_key_to_repr(key))

    print("Trace local capture running: keystrokes + window changes + clipboard.")
    print("Ctrl-Shift-P pauses/resumes. Ctrl-C stops. Work normally.")
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
        coalescer.flush(ts_end=now_iso())
        listener.stop()
        print(f"\nStopped. Raw stream: {args.out}")


if __name__ == "__main__":
    main()
