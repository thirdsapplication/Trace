"""Trace — Engine 1: the Event Log Engine.

Capture (``trace.capture``) produces a lean, normalized raw signal stream;
reconstruction (``trace.reconstruct``) turns it into the ``event_log.json``
contract consumed by Engine 2 (Discovery).
"""

# Phase 1 (cloud backend): load a git-ignored ``.env`` once, at package import,
# so a local run needs no extra setup and every entry point (UI + the
# reconstruct/discovery/agent CLIs) sees the vars *before* submodules read them
# at import time (e.g. agent.config). In the cloud, Render supplies the same
# names as real environment variables and there is no .env — same code, two
# sources. Optional dependency: a missing python-dotenv (or .env) is a no-op,
# and a real environment variable always wins over the file.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(override=False)
except Exception:
    pass

__version__ = "1.0.0"
