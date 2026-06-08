"""Trace — Stage 2: the Discovery Engine.

Reads a frozen ``event_log.json`` (the Event Log Engine's output) and proposes the
automatable workflows inside it as ``discovery.json``: bundles (automatable),
anti-bundles (judgment-required, human-checkpoint), and unclustered noise — each
candidate citing evidence event ids. One domain-neutral model call over the whole
day (no chunking).

The engine sees only the event log; it never sees raw capture data or any answer
key. The Scorer (``scorer.py``) is the only module that reads an answer key.
"""

from .discover import discover, DEFAULT_MODEL  # noqa: F401
