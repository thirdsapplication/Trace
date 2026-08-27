"""Discovery Engine: event_log.json -> discovery.json.

Reads a reconstructed event log and proposes the automatable workflows inside it,
split into bundles (automatable) and anti-bundles (judgment-required), with
supporting evidence ids — the discovery.json contract consumed downstream.

Design, mirroring the reconstruction engine's conventions:
- the whole day is reasoned over in ONE model call (no chunking) so workflows
  whose trigger and execution are separated by hours — linked only by an artifact —
  remain visible;
- a more capable default model than reconstruction (Claude Sonnet 4.6), kept
  injectable/configurable via a single default so the tier swaps trivially;
- the Anthropic call is injected as ``model_fn`` so the engine is testable without
  a key/network;
- tolerant JSON parsing + schema validation/repair of obvious format slips.

The engine sees ONLY the event log and its domain-neutral instructions. It never
sees raw capture data and never sees any answer key.
"""

import json

from . import validate
from .prompt import SYSTEM_PROMPT
from trace.agent import model_call

# Single, one-line default model tier (Open decision §11: trivially swappable).
DEFAULT_MODEL = "claude-sonnet-4-6"


def load_event_log(path):
    """Load event_log.json (a JSON array of event objects)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "events" in data:
        data = data["events"]
    return data if isinstance(data, list) else []


def _trunc(s, n=500):
    s = "" if s is None else str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_event_log(events):
    """Render the event log as a compact chronological list for the model, one line
    per event including id, time, app/window, action, detail, and confidence (the
    model needs the id to cite evidence and the confidence to weight)."""
    lines = []
    for e in events:
        if not isinstance(e, dict):
            continue
        when = e.get("ts_start", "") or ""
        app = e.get("app", "") or ""
        window = e.get("window", "") or ""
        loc = f"{app} / {window}".strip(" /")
        lines.append(
            f'{e.get("id", "?")} [{when}] ({loc}) {e.get("action", "")}'
            f' [{e.get("confidence", "")}]: {_trunc(e.get("detail", ""))}'
        )
    return "\n".join(lines)


def _build_user_content(events):
    return (
        "Here is the full event log for one person's workday "
        f"({len(events)} events), in chronological order.\n\n"
        "EVENT LOG:\n" + render_event_log(events) +
        "\n\nDiscover the workflows per your instructions. Output ONLY the JSON object."
    )


def _default_model_fn(model):
    """Build a Sonnet caller. Imported lazily so the module loads without the
    anthropic package / API key (e.g. in tests)."""
    import anthropic

    client = anthropic.Anthropic()

    def call(system, user):
        return model_call.create_text(client, model, system, user, max_tokens=16000)

    return call


def _parse_json_object(text):
    """Parse the model's JSON-object output. Defensive against stray markdown
    fences / prose even though the prompt forbids them."""
    import re
    if not text:
        return {}
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            data = json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def discover(event_log_path, model=DEFAULT_MODEL, model_fn=None):
    """Run discovery on an event log. Returns ``(discovery, report)`` where
    ``discovery`` is a schema-valid dict and ``report`` is a ``DiscoveryReport``.
    """
    events = load_event_log(event_log_path)
    report = validate.DiscoveryReport()
    valid_ids = {e.get("id") for e in events if isinstance(e, dict) and e.get("id")}

    if not events:
        return validate.empty_discovery(), report

    if model_fn is None:
        model_fn = _default_model_fn(model)

    raw_text = model_fn(SYSTEM_PROMPT, _build_user_content(events))
    obj = _parse_json_object(raw_text)
    discovery = validate.validate_discovery(obj, valid_ids, report)
    return discovery, report
