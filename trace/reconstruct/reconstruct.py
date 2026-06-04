"""Reconstruction: raw_events.jsonl  ->  event_log.json.

Implements handoff Section 5b end to end:
  1. load and order the raw stream (chunking.load_raw)
  2. chunk chronologically with overlap + a deterministic running summary
  3. send each chunk to Haiku with the Section 6 prompt, temperature 0, JSON only
  4. parse, validate against the schema, stitch chunk outputs, renumber ids
  5. flag (not drop) typed values that look fragmented after stitching

Determinism: temperature 0, a fixed model, and a deterministic running summary
(built from already-emitted events, not a second LLM call) so the same raw input
yields a materially identical log — Engine 2's recurrence detection depends on it.

The Anthropic call is injected as ``model_fn`` so the pipeline is testable without
network/keys; the default builds a real ``claude-haiku-4-5`` client.
"""

import json
import re

from . import chunking, validate
from .prompt import SYSTEM_PROMPT

DEFAULT_MODEL = "claude-haiku-4-5"


def _default_model_fn(model):
    """Build a temperature-0 Haiku caller. Imported lazily so the module loads
    without the anthropic package / API key (e.g. in tests)."""
    import anthropic

    client = anthropic.Anthropic()

    def call(system, user):
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )

    return call


def _parse_json_array(text):
    """Parse the model's JSON-array output. Defensive against stray markdown
    fences/prose even though the prompt forbids them."""
    if not text:
        return []
    t = text.strip()
    # strip ```json ... ``` fences if present
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        # last resort: grab the outermost [...] span
        start, end = t.find("["), t.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        # tolerate {"events": [...]}
        data = data.get("events", [])
    return data if isinstance(data, list) else []


def _build_user_content(summary, rendered_lines):
    return (
        "SESSION SO FAR (already logged before this chunk; do not re-emit):\n"
        f"{summary}\n\n"
        "RAW CAPTURE STREAM (this chunk, in order):\n"
        + "\n".join(rendered_lines)
    )


def reconstruct(raw_path, model=DEFAULT_MODEL, chunk_size=120, overlap=20,
                model_fn=None, progress_cb=None):
    """Reconstruct an event log from a raw stream.

    Returns ``(event_log, report)`` where ``event_log`` is a list of event dicts
    matching the frozen contract and ``report`` is a ``ValidationReport``.

    ``progress_cb`` is an optional ``callable(done, total)`` invoked once before
    processing (``0, total``) and after each chunk. It is a pure observation hook
    for UIs/progress bars and does not affect reconstruction logic.
    """
    raw = chunking.load_raw(raw_path)
    raw_by_id = {r["id"]: r for r in raw}
    report = validate.ValidationReport()
    if not raw:
        return [], report

    if model_fn is None:
        model_fn = _default_model_fn(model)

    chunks = chunking.chunk(raw, chunk_size=chunk_size, overlap=overlap)
    total = len(chunks)
    if progress_cb:
        progress_cb(0, total)

    chunk_outputs = []
    running = []  # accumulated *validated* events, for the running summary only
    for i, ch in enumerate(chunks, start=1):
        summary = chunking.summarize(running)
        rendered = [chunking.render_event(ev) for ev in ch]
        user = _build_user_content(summary, rendered)
        raw_text = model_fn(SYSTEM_PROMPT, user)
        events = _parse_json_array(raw_text)
        chunk_outputs.append(events)
        # maintain a provisional validated view for the next chunk's summary
        for ev in events:
            cleaned = validate.validate_event(ev, raw_by_id, validate.ValidationReport())
            if cleaned:
                running.append(cleaned)
        if progress_cb:
            progress_cb(i, total)

    event_log = validate.stitch(chunk_outputs, raw_by_id, report)
    return event_log, report
