# Trace Capture — browser extension (Chrome, Manifest V3)

The browser half of Engine 1's capture. It records the signals that live in the
page and would otherwise be invisible to the local keystroke/window process:

- **clicks** with the target element's `{role, label, value}`
- **focus targets** (`target` events) — what the worker focused/selected
- **in-browser clipboard** — `copy` / `cut` / `paste` with content and direction
- **scoped structured reads** — the *delta* of visible labels/values/dropdown
  options at meaningful anchors (`page_load`, `click`, `focus`)

All of it is event-anchored (never polled on a timer) and scoped to the unique
delta — never a full-page DOM dump. A full dump would just recreate the OCR
redundancy and cost v1 deliberately removed.

## Privacy

- `input[type=password]`, password-autocomplete fields, OTP/cc-number fields, and
  anything marked `data-trace-sensitive` are **never read** — not their value,
  not into structured reads, and clipboard events near them are dropped.
- Obvious secrets/PII are redacted client-side; the collector + `RawWriter`
  redact again as an enforced backstop.
- The toolbar button toggles **pause/resume** (badge shows `REC` / `❚❚`). While
  paused, nothing is captured or sent.

## Install (developer mode)

1. Start the collector so there is something to receive events:
   ```
   python -m trace.capture.collector --out captures/raw_events.jsonl --port 8137
   ```
2. Open `chrome://extensions`, enable **Developer mode**, click **Load
   unpacked**, and select this `extension/` directory.
3. The badge shows `REC`. Browse/work normally; events stream to the collector
   and merge into the same `raw_events.jsonl` the local process writes.

The collector endpoint is `http://127.0.0.1:8137/event`. To change the port,
edit `COLLECTOR_URL` in `background.js` and pass the matching `--port` to the
collector.
