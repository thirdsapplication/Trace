# Trace Engine 1 — Schemas (the Engine 1 ↔ Engine 2 contract)

Engine 1 has two artifacts. The first is internal to Engine 1; the second is the
**frozen contract** that Engine 2 (Discovery) consumes.

```
raw_events.jsonl   (internal to Engine 1: capture -> reconstruction)
event_log.json     (CONTRACT: Engine 1 -> Engine 2)
```

The reconstruction step is the only thing allowed to read `raw_events.jsonl`.
Everything downstream reads `event_log.json` and nothing else.

---

## 1. `raw_events.jsonl` — the raw capture stream

One JSON object per line, append-only, written by every capturer (local process
and browser extension via the collector) into one shared file. Lines are written
in arrival order; the reconstruction step sorts by `ts` before processing, so
strict on-disk ordering is not required.

```json
{ "ts": "ISO-8601", "source": "keystroke|window|click|target|clipboard|structured_read", "payload": { } }
```

`ts` is an ISO-8601 timestamp with millisecond precision and a timezone offset.

### Per-source `payload`

| source            | payload shape |
|-------------------|---------------|
| `window`          | `{ "app": "...", "title": "..." }` |
| `keystroke`       | `{ "text": "...", "window": "...", "ts_end": "ISO-8601" }` |
| `click`           | `{ "x": 0, "y": 0, "button": "left\|right\|middle", "target": { "role": "...", "label": "...", "value": "..." } \| null }` |
| `target`          | `{ "role": "...", "label": "...", "value": "..." }` (a focus/selection target reported on its own, e.g. focus change) |
| `clipboard`       | `{ "op": "copy\|cut\|paste", "content": "..." }` |
| `structured_read` | `{ "url": "...", "anchor": "page_load\|click\|focus", "elements": [ { "role": "...", "label": "...", "value": "..." } ] }` |

Notes:
- `keystroke` events are **coalesced segments**, never individual keys.
  Backspaces are already applied; `[ENTER]` / `[TAB]` are marked inline; modifier
  keys are dropped. The buffer is flushed (and a segment emitted) on every window
  change, which is what produces the *fragmented* values the reconstruction layer
  stitches back together.
- `structured_read.elements` is the **scoped delta only** — labels/values/options
  on the active page that are not already captured by keystrokes or the window
  title. It is never a full-page DOM dump (that would recreate the OCR redundancy
  v1 deliberately removed).
- All text-bearing fields (`keystroke.text`, `clipboard.content`,
  `structured_read.*.value`, `click.target.value`, …) pass through capture-time
  redaction before they are written. Secrets/PII are replaced with a redaction
  token (default `[REDACTED]`) so they never hit disk.

---

## 2. `event_log.json` — the reconstructed event log (FROZEN CONTRACT)

A JSON array of event objects, in chronological order. This is the stable
interface Engine 2 consumes. **Do not change this shape without versioning it.**

```json
{
  "id": "e001",
  "ts_start": "ISO-8601",
  "ts_end": "ISO-8601",
  "app": "...",
  "window": "...",
  "action": "snake_case_type",
  "detail": "human-readable, fully grounded in evidence",
  "evidence": ["r0007", "r0008"],
  "confidence": "high | medium | low"
}
```

| field      | type        | meaning |
|------------|-------------|---------|
| `id`       | string      | `eNNN`, globally sequential across the whole day after stitching. |
| `ts_start` | string      | ISO-8601 start of the action (from the earliest supporting raw event). |
| `ts_end`   | string      | ISO-8601 end of the action (from the latest supporting raw event). |
| `app`      | string      | Application the action happened in (from the window spine). |
| `window`   | string      | Window/document/email/sheet title the action happened in. |
| `action`   | string      | `snake_case` type from a small controlled vocabulary (see below). |
| `detail`   | string      | Human-readable description, **fully grounded** in the evidence. Preserves concrete observed values exactly (line items, totals, prices, tracking #s, recipients, message text, dropdown selections). |
| `evidence` | string[]    | Raw-event ids (`rNNNN`, the 1-based index assigned to each line of `raw_events.jsonl` at load time) that support this event. |
| `confidence` | string    | `high` \| `medium` \| `low`. Ambiguous signals (stray keystrokes, find-bar queries, shortcuts) are recorded with low confidence rather than guessed. |

### Why these constraints exist (Engine 2 depends on them)
- **Normalized `action` vocabulary.** Engine 2 finds workflows by detecting action
  *subsequences that recur* within a day (it needs ~≥4 recurrences to surface a
  bundle). If the same real task produces different `action` strings on different
  runs, recurrence detection can't tell instance #3 and instance #11 are the same
  workflow. Keep `action` to a small, stable, controlled set.
- **Stable `detail` formatting + preserved values.** Concrete values become the
  "observed examples" Engine 3 builds specs from, and stable phrasing keeps
  recurring instances comparable.
- **Judgment legibility.** Where an action obviously involves a human judgment
  call (credit decision, complaint handling, custom pricing), that should be
  legible in the event, not flattened away — Engine 2 has to separate automatable
  work from judgment-dependent work.
- **Faithfulness.** `detail` never invents content not present in `evidence`. An
  opened-but-uncaptured document is recorded as "opened X", not summarized.

### Controlled `action` vocabulary (v1, small-but-fixed)
Starting set; expand only as real sessions demand (see Open Decision in README).
Domain-neutral on purpose — no business-specific verbs.

```
read_email, reply_email, send_email, compose_email,
open_document, view_pdf, read_document,
enter_data, update_data, copy_value, paste_value,
search, navigate, select_option, click_control,
switch_window, unknown
```

`unknown` is the explicit escape hatch for ambiguous signals (paired with
`confidence: low`) so the model never has to guess a meaning.
