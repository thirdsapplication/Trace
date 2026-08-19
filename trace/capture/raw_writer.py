"""Shared append-only writer for the raw capture stream.

Every capturer — the local macOS process *and* the browser extension (via the
collector) — writes normalized ``{ts, source, payload}`` events into one shared
``raw_events.jsonl`` through this writer. Two concerns are handled here so no
caller can bypass them:

1. **Redaction.** Every payload passes through ``redaction.redact_payload`` before
   it is serialized, so secrets/PII never hit disk.
2. **Concurrency.** The local process and the collector are separate OS processes
   appending to the same file. We take an advisory ``flock`` per write (plus an
   in-process lock) so interleaved appends never corrupt a line.

The on-disk order is arrival order; reconstruction sorts by ``ts`` before
processing, so we do not attempt to globally order across processes here.
"""

import datetime
import json
import os
import threading

try:
    import fcntl  # POSIX only; present on macOS and Linux.
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from . import redaction

_VALID_SOURCES = {
    "keystroke", "window", "click", "target", "clipboard", "structured_read",
}

# Which capturer produced an event. Not the same question as the source type:
# ``target`` and ``structured_read`` used to imply "browser extension", but the
# Accessibility capturer produces them for native apps too, so the origin has to
# be recorded rather than inferred.
LOCAL, BROWSER = "local", "browser"
VALID_ORIGINS = {LOCAL, BROWSER}
DEFAULT_ORIGIN = LOCAL


def now_iso():
    """ISO-8601 timestamp with millisecond precision and local tz offset."""
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


class RawWriter:
    """Append-only, redacting, process-safe writer to ``raw_events.jsonl``."""

    def __init__(self, path, reset=False):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._paused = False
        if reset:
            open(self.path, "w").close()

    # --- pause / resume ---------------------------------------------------
    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    @property
    def paused(self):
        return self._paused

    # --- write ------------------------------------------------------------
    def write(self, source, payload, ts=None, origin=DEFAULT_ORIGIN):
        """Redact and append one raw event. Returns the written record, or None
        if capture is paused or the source is unknown.

        ``origin`` records WHICH capturer produced the event. Three sources
        (``click``, ``target``, ``structured_read``) can now come from either the
        browser extension or the local Accessibility capturer, so the source type
        alone no longer identifies the capturer — which the health check and any
        future debugging both need. Stamped after redaction: it is a fixed
        vocabulary word, never user text, so there is nothing in it to redact.
        """
        if self._paused:
            return None
        if source not in _VALID_SOURCES:
            raise ValueError(f"unknown raw event source: {source!r}")
        if origin not in VALID_ORIGINS:
            raise ValueError(f"unknown capture origin: {origin!r}")

        payload = redaction.redact_payload(source, payload or {})
        if isinstance(payload, dict):
            payload = dict(payload, origin=origin)
        record = {
            "ts": ts or now_iso(),
            "source": source,
            "payload": payload,
        }
        line = json.dumps(record, ensure_ascii=False)

        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        f.write(line + "\n")
                        f.flush()
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover
                    f.write(line + "\n")
                    f.flush()
        return record
