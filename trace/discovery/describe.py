"""Describe Engine: a typed workflow description -> discovery.json.

The skip-recording path. The worker describes their workflow in broad terms; ONE
model call turns that description into the same discovery contract the Discovery
Engine produces from an event log, so everything downstream — review, the
mandatory questions, build, connect, deploy — runs unchanged.

Mirrors the Discovery Engine's conventions: injectable ``model_fn`` (offline
tests), tolerant JSON parsing, and schema validation/repair via ``validate``.
Validation runs with an EMPTY valid-id set, so any evidence ids the model
hallucinates are stripped (there is no event log; empty-evidence bundles are
kept, only flagged). No events are ever fabricated — the session's event log
stays ``[]`` so nothing downstream is falsely marked observed.
"""

from . import validate
from .describe_prompt import SYSTEM_PROMPT
from .discover import DEFAULT_MODEL, _default_model_fn, _parse_json_object


def _build_user_content(description):
    return (
        "Here is the worker's description of their recurring workflow, in their "
        "own words:\n\n"
        "DESCRIPTION:\n" + description.strip() +
        "\n\nInterpret it per your instructions. Output ONLY the JSON object."
    )


def describe_workflows(description_text, model=DEFAULT_MODEL, model_fn=None):
    """Turn a worker's typed description into ``(discovery, report)``.

    Raises ``ValueError`` on a blank description and ``RuntimeError`` when the
    model output yields no workflow at all (the caller surfaces both as a job
    error with a retry, never an empty review screen).
    """
    description = str(description_text or "").strip()
    if not description:
        raise ValueError("empty_description")

    report = validate.DiscoveryReport()
    if model_fn is None:
        model_fn = _default_model_fn(model)

    raw_text = model_fn(SYSTEM_PROMPT, _build_user_content(description))
    obj = _parse_json_object(raw_text)
    discovery = validate.validate_discovery(obj, set(), report)
    if not discovery["bundles"] and not discovery["anti_bundles"]:
        raise RuntimeError(
            "couldn't find a workflow in that description — try naming the "
            "trigger, the steps, and the apps you use")
    return discovery, report
