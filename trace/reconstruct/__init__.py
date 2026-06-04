"""Trace Engine 1 — reconstruction component.

Reads a session's ``raw_events.jsonl`` and reconstructs it with Haiku into a
clean, ordered ``event_log.json`` matching the frozen Engine 2 contract.
"""

from .reconstruct import reconstruct, DEFAULT_MODEL  # noqa: F401
