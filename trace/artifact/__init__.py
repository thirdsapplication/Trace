"""Trace — Stage 3: the review step contract + the Artifact Engine.

``review.py`` defines the ``reviewed_bundles.json`` contract (seeded from
``discovery.json`` and edited by the operator in the Test UI). ``artifact.py``
consumes the reviewed bundles (plus ``event_log.json`` for verbatim examples) and
emits one Artifact C markdown spec per approved bundle, with a short manual-handling
note for anti-bundles.

The engine never reaches the Scorer or any answer key.
"""

from .artifact import generate_specs, DEFAULT_MODEL  # noqa: F401
