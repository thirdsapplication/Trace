"""Trace Engine 1 — capture component.

Independent capturers feeding one shared, append-only raw stream
(``raw_events.jsonl``) in the normalized ``{ts, source, payload}`` schema:

- local macOS process (``capture.py``): keystrokes, window changes, clipboard
- browser extension -> collector (``collector.py``): clicks, targets, in-browser
  clipboard, scoped structured reads

All writes go through ``RawWriter``, which redacts secrets/PII before anything
touches disk.
"""

from .raw_writer import RawWriter, now_iso  # noqa: F401
