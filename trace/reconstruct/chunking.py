"""Load, order, id, render, and chunk the raw stream for reconstruction.

A full workday is a large raw stream, so reconstruction processes it in
chronological chunks with a small overlap and a short running summary carried
across boundaries (handoff Section 5b) — so an action that spans a chunk boundary
is not split. The output is still one coherent day-long log.

Each raw line is assigned a stable id ``rNNNN`` (1-based index in time order) at
load time; that id is what events cite in their ``evidence`` array.
"""

import json


def load_raw(path):
    """Read ``raw_events.jsonl``, sort by ``ts``, and assign 1-based ids.

    Returns a list of dicts ``{id, ts, source, payload}``."""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("ts", ""))
    out = []
    for i, r in enumerate(rows, start=1):
        out.append({
            "id": f"r{i:04d}",
            "ts": r.get("ts", ""),
            "source": r.get("source", ""),
            "payload": r.get("payload", {}) or {},
        })
    return out


def _trunc(s, n=400):
    s = "" if s is None else str(s)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_event(ev):
    """Render one raw event to a single compact line for the model, prefixed with
    its id and timestamp so the model can cite it in ``evidence``."""
    src = ev["source"]
    p = ev["payload"]
    head = f'[{ev["id"]} @ {ev["ts"]}]'

    if src == "window":
        return f'{head} WINDOW app="{_trunc(p.get("app"), 80)}" title="{_trunc(p.get("title"), 160)}"'
    if src == "keystroke":
        return f'{head} TYPED in "{_trunc(p.get("window"), 100)}": "{_trunc(p.get("text"))}"'
    if src == "click":
        t = p.get("target") or {}
        tgt = f'role={t.get("role","")} label="{_trunc(t.get("label"),80)}" value="{_trunc(t.get("value"),120)}"' if t else "target=unknown"
        return f'{head} CLICK {p.get("button","left")} ({p.get("x")},{p.get("y")}) {tgt}'
    if src == "target":
        return f'{head} FOCUS role={p.get("role","")} label="{_trunc(p.get("label"),80)}" value="{_trunc(p.get("value"),120)}"'
    if src == "clipboard":
        return f'{head} CLIPBOARD {p.get("op","")} "{_trunc(p.get("content"))}"'
    if src == "structured_read":
        elems = p.get("elements", []) or []
        parts = []
        for el in elems[:40]:
            parts.append(f'{el.get("role","")}:{_trunc(el.get("label"),60)}={_trunc(el.get("value"),80)}')
        body = " | ".join(parts)
        return f'{head} READ anchor={p.get("anchor","")} url={_trunc(p.get("url"),120)} [{body}]'
    return f'{head} {src} {_trunc(json.dumps(p), 200)}'


def chunk(raw, chunk_size=120, overlap=20):
    """Split the ordered raw events into chunks of ``chunk_size`` with ``overlap``
    trailing events repeated at the start of the next chunk.

    Returns a list of chunks, each a list of raw-event dicts. The overlap lets the
    model see boundary-spanning actions in both chunks; the stitcher dedupes.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    overlap = max(0, min(overlap, chunk_size - 1))
    if len(raw) <= chunk_size:
        return [raw] if raw else []
    chunks = []
    start = 0
    step = chunk_size - overlap
    while start < len(raw):
        chunks.append(raw[start:start + chunk_size])
        if start + chunk_size >= len(raw):
            break
        start += step
    return chunks


def summarize(events, max_items=6):
    """Build a short, deterministic running summary from already-emitted event log
    objects, to carry across a chunk boundary. Deterministic (no LLM call) so it
    does not perturb temperature-0 reproducibility."""
    if not events:
        return "(none yet)"
    tail = events[-max_items:]
    lines = []
    for e in tail:
        lines.append(f'- {e.get("action","?")} in "{e.get("window","")}": {_trunc(e.get("detail"), 160)}')
    return "\n".join(lines)


def summarize_raw(raw_before, max_items=6):
    """The running summary when chunks run concurrently: the last raw lines BEFORE the
    chunk, rendered, since the previous chunk's reconstructed events do not exist yet.
    Still deterministic. It carries less than ``summarize`` (what happened, not what the
    model made of it); the overlap already puts the preceding 20 raw events in the
    chunk itself, and the stitcher dedupes what both chunks then emit.

    The ``[rNNNN @ ts]`` head is dropped: these lines are context, not this chunk's
    evidence, and an id in view is an id the model can cite for an action it would
    then log a second time."""
    if not raw_before:
        return "(none yet)"
    return "\n".join("- (raw, before this chunk) " + render_event(ev).split("] ", 1)[-1]
                     for ev in raw_before[-max_items:])
