"""Reconstruction: raw_events.jsonl  ->  event_log.json.

Implements handoff Section 5b end to end:
  1. load and order the raw stream (chunking.load_raw)
  2. chunk chronologically with overlap + a deterministic running summary
  3. send each chunk to the model with the Section 6 prompt, JSON only
  4. parse, validate against the schema, stitch chunk outputs, renumber ids
  5. flag (not drop) typed values that look fragmented after stitching

Determinism: a fixed model and a deterministic running summary (built from
already-emitted events, not a second LLM call) so the same raw input yields a
materially identical log — Engine 2's recurrence detection depends on it. Fable 5.1
rejects sampling parameters, so temperature 0 is no longer part of that recipe.

The Anthropic call is injected as ``model_fn`` so the pipeline is testable without
network/keys; the default builds a real ``claude-fable-5-1`` client.
"""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from . import augment, chunking, validate
from .prompt import SYSTEM_PROMPT
from trace.agent import model_call

DEFAULT_MODEL = "claude-fable-5-1"

# Output ceiling per chunk. A 120-event chunk can legitimately become 60-100 events
# of ~120 tokens each, and Fable's thinking is billed against the same ceiling. The
# old cap of 8000 was set for a model that did not think and a prompt that asked for
# fewer, coarser events; under the v2 prompt it was hit routinely, the cut-off JSON
# parsed as an empty chunk, and a full day reconstructed to nothing. Streaming is what
# lets the cap be this large — above ~16K a non-streaming request hangs rather than
# failing (see model_call).
MAX_TOKENS = 32000

# Chunks sent to the model at once. Each chunk is ~75s of Fable, so one after another a
# 28-minute recording (9 chunks) took ~20 minutes — past the UI's wait, which is how a
# real tester's recording never reached discovery. Concurrency costs nothing extra (the
# same chunks are sent) and is bounded by the account's rate limits, shared by everyone
# reconstructing at once. 1 restores the sequential path exactly.
WORKERS = max(1, int(os.environ.get("TRACE_RECON_WORKERS", "8") or 8))

# The SDK's own backoff on 429/529. Concurrent chunks make a rate-limit answer likelier,
# and one chunk failing fails the whole reconstruction, so it gets more than the default 2.
MAX_RETRIES = 5


def _default_model_fn(model):
    """Build the reconstruction caller. Imported lazily so the module loads
    without the anthropic package / API key (e.g. in tests)."""
    import anthropic

    client = anthropic.Anthropic(max_retries=MAX_RETRIES)

    def call(system, user):
        return model_call.create_text_streaming(client, model, system, user,
                                                max_tokens=MAX_TOKENS)

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


def _reply_was_readable(text):
    """True when the reply parsed as JSON at all (a fenced ``[]`` is readable and empty;
    prose, a refusal, or cut-off JSON is not). Tells a lost chunk from an empty one."""
    if not text:
        return False
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    try:
        json.loads(t)
        return True
    except json.JSONDecodeError:
        return False


def _build_user_content(summary, rendered_lines):
    return (
        "SESSION SO FAR (already logged before this chunk; do not re-emit):\n"
        f"{summary}\n\n"
        "RAW CAPTURE STREAM (this chunk, in order):\n"
        + "\n".join(rendered_lines)
    )


def _run_chunk(model_fn, summary, ch, i, total, report):
    """One chunk through the model. Returns its (unvalidated) events; a reply that is
    not a JSON list is recorded on ``report`` as a lost chunk."""
    rendered = [chunking.render_event(ev) for ev in ch]
    raw_text = model_fn(SYSTEM_PROMPT, _build_user_content(summary, rendered))
    events = _parse_json_array(raw_text)
    if not events and (raw_text or "").strip() and not _reply_was_readable(raw_text):
        # A reply that parsed to nothing is a lost chunk, not an empty one. It used
        # to vanish without a trace; the report is where a person looks.
        report.error(f"chunk {i}/{total}: the model's reply was not a JSON event list "
                     f"({len(raw_text)} chars) — that chunk's events were lost")
    return events


def _run_sequential(chunks, model_fn, raw_by_id, report, progress_cb):
    """Each chunk sees the model's own reading of the chunks before it."""
    total = len(chunks)
    outputs = []
    running = []  # accumulated *validated* events, for the running summary only
    for i, ch in enumerate(chunks, start=1):
        events = _run_chunk(model_fn, chunking.summarize(running), ch, i, total, report)
        outputs.append(events)
        # maintain a provisional validated view for the next chunk's summary
        for ev in events:
            cleaned = validate.validate_event(ev, raw_by_id, validate.ValidationReport())
            if cleaned:
                running.append(cleaned)
        if progress_cb:
            progress_cb(i, total)
    return outputs


def _run_concurrent(chunks, raw, model_fn, report, progress_cb, workers):
    """All chunks at once, ``workers`` at a time. Outputs come back in chunk order —
    ``stitch`` keeps the first of two duplicates, so order is what makes this as
    deterministic as the sequential path. The first chunk to fail fails the whole
    reconstruction, as it does sequentially; chunks not yet started are cancelled."""
    total = len(chunks)
    position = {r["id"]: n for n, r in enumerate(raw)}
    summaries = [chunking.summarize_raw(raw[:position[ch[0]["id"]]]) for ch in chunks]
    lock = threading.Lock()
    done = [0]

    def one(i):
        events = _run_chunk(model_fn, summaries[i - 1], chunks[i - 1], i, total, report)
        if progress_cb:
            with lock:
                done[0] += 1
                progress_cb(done[0], total)
        return events

    pool = ThreadPoolExecutor(max_workers=min(workers, total))
    try:
        futures = [pool.submit(one, i) for i in range(1, total + 1)]
        return [f.result() for f in futures]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def reconstruct(raw_path, model=DEFAULT_MODEL, chunk_size=120, overlap=20,
                model_fn=None, progress_cb=None,
                open_persistence_seconds=augment.DEFAULT_OPEN_PERSISTENCE_SECONDS,
                workers=None):
    """Reconstruct an event log from a raw stream.

    Returns ``(event_log, report)`` where ``event_log`` is a list of event dicts
    matching the frozen contract and ``report`` is a ``ValidationReport``.

    ``progress_cb`` is an optional ``callable(done, total)`` invoked once before
    processing (``0, total``) and after each chunk. It is a pure observation hook
    for UIs/progress bars and does not affect reconstruction logic.

    ``open_persistence_seconds`` is the dwell threshold for surfacing a sustained
    window focus as an "opened/viewed" action (symmetric across browser/native
    apps; see ``augment``).

    ``workers`` is how many chunks go to the model at once (default ``WORKERS``). With
    more than one, each chunk's running summary is built from the raw events before it
    rather than from the previous chunk's output, and ``progress_cb`` counts chunks as
    they finish, not in order.
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

    workers = WORKERS if workers is None else max(1, workers)
    if workers == 1 or total == 1:
        chunk_outputs = _run_sequential(chunks, model_fn, raw_by_id, report, progress_cb)
    else:
        chunk_outputs = _run_concurrent(chunks, raw, model_fn, report, progress_cb, workers)

    event_log = validate.stitch(chunk_outputs, raw_by_id, report)
    # Symmetric document-open detection: surface sustained, otherwise-unrepresented
    # document opens (browser or native) the model did not already represent.
    event_log = augment.augment_document_opens(
        event_log, raw, persistence_seconds=open_persistence_seconds)
    event_log = validate.resequence(event_log)
    # Stable assembly of one logical record fragmented across many keystrokes:
    # coalesce contiguous same-window data-entry fragments (no intervening action
    # of a different kind) into one event, then re-sequence.
    event_log = validate.coalesce_contiguous(event_log, raw_by_id)
    event_log = validate.resequence(event_log)
    return event_log, report
