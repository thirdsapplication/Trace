"""Programmatic control surface for capture (handoff §6).

The existing local capture (``capture.py``) is driven by a global hotkey and
Ctrl-C, which a UI can't call. This module adds ``start() / stop() / pause() /
resume() / status()`` around the *same* capture building blocks — the keystroke
coalescer, the window poller, the clipboard poller, and the browser-event
collector — so a front end can drive the real engine unchanged. No capture logic
lives here; this is composition + lifecycle only.

``pynput`` is imported lazily inside ``start()`` so this module (and the UI
backend that imports it) loads on any platform; capture itself is macOS-only.
"""

import threading
from http.server import ThreadingHTTPServer

import json

from . import sources
from .clipboard import clipboard_loop
from .collector import CollectorStats, make_handler
from .keystrokes import KeystrokeCoalescer
from .raw_writer import RawWriter, now_iso
from .window import window_loop


def read_raw_rows(path):
    """Parse the raw stream into a list of dicts (one per line)."""
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return rows


def count_events(path):
    """Event count = number of non-blank lines in the raw stream (handoff §6
    explicitly allows counting lines in the raw file)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0


def _key_to_repr(key):
    """Normalize a pynput key event to the string the coalescer expects."""
    try:
        if key.char is not None:
            return key.char
    except AttributeError:
        pass
    return str(key)


class CaptureController:
    """Drives a single capture session writing to ``out_path``.

    One controller == one session. Create a fresh controller per session so the
    raw stream, collector stats, and timers are isolated.
    """

    def __init__(self, out_path, collector_port=8137, enable_clipboard=True):
        self.out_path = out_path
        self.collector_port = collector_port
        self.enable_clipboard = enable_clipboard

        self._writer = None
        self._coalescer = None
        self._stop = None
        self._listener = None
        self._httpd = None
        self._stats = CollectorStats()
        self._started_at = None
        self._running = False
        self._lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------
    def start(self):
        """Begin capture: keystrokes + window + clipboard locally, and the
        browser-event collector. Raises RuntimeError if already running, or
        ImportError on a platform without pynput."""
        with self._lock:
            if self._running:
                raise RuntimeError("capture already running")

            from pynput import keyboard  # lazy: macOS-only dependency

            self._writer = RawWriter(self.out_path, reset=True)
            self._coalescer = KeystrokeCoalescer(self._writer)
            self._stop = threading.Event()

            # window poll (also flushes keystroke segments on navigation)
            threading.Thread(target=window_loop,
                             args=(self._stop, self._writer, self._coalescer),
                             daemon=True).start()
            if self.enable_clipboard:
                threading.Thread(target=clipboard_loop,
                                 args=(self._stop, self._writer),
                                 daemon=True).start()

            # keystroke listener
            def on_press(key):
                if not self._writer.paused:
                    self._coalescer.on_key(_key_to_repr(key), now_iso())

            self._listener = keyboard.Listener(on_press=on_press)
            self._listener.start()

            # browser-event collector, in-process, on its own thread
            class _ReuseServer(ThreadingHTTPServer):
                allow_reuse_address = True
            try:
                self._httpd = _ReuseServer(
                    ("127.0.0.1", self.collector_port),
                    make_handler(self._writer, self._stats),
                )
            except OSError:
                # Port busy (stale process or TIME_WAIT) — pick a free one
                self._httpd = _ReuseServer(
                    ("127.0.0.1", 0),
                    make_handler(self._writer, self._stats),
                )
                self.collector_port = self._httpd.server_address[1]
            threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

            self._started_at = now_iso()
            self._running = True

    def pause(self):
        """Stop writing without ending the session (reuses the engine's pause).
        Flushes the in-flight typed segment so it isn't carried across the pause."""
        with self._lock:
            if not self._running or self._writer is None:
                raise RuntimeError("capture not running")
            self._coalescer.flush(ts_end=now_iso())
            self._writer.pause()

    def resume(self):
        with self._lock:
            if not self._running or self._writer is None:
                raise RuntimeError("capture not running")
            self._writer.resume()

    def stop(self):
        """End the session cleanly: stop pollers, flush the last segment, stop the
        listener, and shut the collector down. Idempotent."""
        with self._lock:
            if not self._running:
                return
            if self._stop:
                self._stop.set()
            if self._coalescer:
                self._coalescer.flush(ts_end=now_iso())
            if self._listener:
                self._listener.stop()
            if self._httpd:
                self._httpd.shutdown()
                self._httpd.server_close()
            self._running = False

    # --- introspection ----------------------------------------------------
    @property
    def running(self):
        return self._running

    def status(self):
        """Snapshot for the UI: running-state, pause-state, elapsed time, event
        count, and browser-capture status."""
        paused = bool(self._writer.paused) if self._writer else False
        elapsed = None
        if self._started_at:
            from datetime import datetime
            try:
                elapsed = (datetime.now().astimezone()
                           - datetime.fromisoformat(self._started_at)).total_seconds()
            except ValueError:
                elapsed = None
        return {
            "running": self._running,
            "paused": paused,
            "started_at": self._started_at,
            "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
            "event_count": count_events(self.out_path),
            "out_path": self.out_path,
            "browser": self._stats.snapshot(),
        }

    def summary(self, min_browser_events=1):
        """Completed-session observability (Item 3): raw-event counts by source
        type plus a non-blocking warning if browser-only sources are absent or the
        window titles came back blank (see sources.capture_health)."""
        rows = read_raw_rows(self.out_path)
        return sources.capture_health(sources.summarize_sources(rows),
                                      min_browser_events=min_browser_events,
                                      rows=rows)
