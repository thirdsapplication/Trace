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

## Browser-capture path — how events reach the raw stream (diagnosis)

The path from a click in the page to a row in `raw_events.jsonl` has four links.
If browser-sourced events are missing from a session (the local process records
window/keystroke/clipboard but there are zero clicks/targets/structured-reads),
the break is in one of these:

1. **Capture & send (content script → worker).** `content.js` attaches
   capture-phase listeners for click / focus / copy-cut-paste and emits a
   scoped structured-read on page-load/click/focus, sending each to the
   background worker. *(Implemented — not a stub.)*

2. **Deliver (worker → collector).** `background.js` POSTs to the collector.
   **This was the failure.** Under Manifest V3 the background is a *service
   worker* terminated after ~30s idle; the previous design buffered events in a
   module-global array and flushed on `setInterval`, which neither keeps the
   worker alive nor survives termination — so sporadic clicks/reads were dropped
   before they were ever sent. Fixed by POSTing **on message receipt** (an async
   `sendResponse` keeps the worker alive until the `fetch` settles) and using
   `chrome.alarms` (which survive termination) only to retry events that failed
   while the collector was down.

3. **Persist (collector → raw stream).** The collector accepts the
   browser-only sources on `POST /event` and writes them through the shared,
   redacting `RawWriter` in the same `{ts, source, payload}` schema as local
   events, so reconstruction consumes them identically. *(Working.)*

4. **Observe (health/handshake).** The worker pings `POST /hello` on
   startup/resume and periodically; the collector exposes `GET /health` with
   `connected` (heartbeat seen) and `detected` (events actually arrived). The
   Test UI's browser-capture status and the session summary read these.

The setup step (enabling the extension and pointing it at the running collector)
is the operator's; the four links above are the code path. The Item-3 warning is
unchanged — it fires when browser-only sources are absent and stays quiet once
real browser events flow.
