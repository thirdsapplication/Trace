# Trace — Engine 1: The Event Log Engine (v1)

Trace observes a knowledge worker and discovers the automatable tasks for them.
It is three engines run in sequence over one observed workday:

```
Raw capture stream
   │
   ▼
Engine 1 — Event Log Engine   ← THIS REPO
   │   hands off event_log.json  (the contract with Engine 2)
   ▼
Engine 2 — Discovery Engine    (later) — finds workflows that recur within the day
   │
   ▼
Engine 3 — Artifact Engine     (later) — turns each workflow into a buildable spec
```

This repository implements **Engine 1 only**. Its job: capture a lean,
privacy-preserving signal stream of a real browser-work session, then use Haiku to
reconstruct it into a clean, ordered `event_log.json` faithful enough that
Engine 2 can mine it for recurring workflows.

Engine 1 runs over **one full workday** — capture runs continuously, and the
result is a single `event_log.json` for that day. The recurrence Engine 2 looks
for happens *within* a single day (order intake a dozen times, ship confirmations
many times, etc.), so one day's log already contains the repetition discovery
needs.

## Architecture

```
trace/
  capture/                 Engine 1a — capture -> raw_events.jsonl
    capture.py             local macOS process: keystrokes + window + clipboard
    controller.py          programmatic start/stop/pause/resume/status (for the UI)
    keystrokes.py          coalesces keys into typed segments (Appendix A logic)
    window.py              active-window spine (osascript poll)
    clipboard.py           outside-browser pasteboard poll
    collector.py           localhost endpoint; merges browser-extension events
    raw_writer.py          shared, redacting, flock-protected append-only writer
    redaction.py           capture-time secret/PII redaction (first-class privacy)
  extension/               Engine 1a — Chrome MV3 extension (browser signals)
    manifest.json, background.js, content.js
  reconstruct/             Engine 1b — raw_events.jsonl -> event_log.json
    reconstruct.py         orchestration: chunk -> Haiku -> validate -> stitch
    prompt.py              the Haiku perception prompt (domain-neutral)
    chunking.py            load/order/id/render + chronological chunking
    validate.py            schema validation, action normalization, fragment checks
    cli.py                 command-line entry point
  ui/                      local test harness (drives the engine from a window)
    app.py                 stdlib HTTP backend (local-only); the API
    keystore.py            secure API-key storage (Keychain / 0600 file)
    permissions.py         macOS permission + browser-capture status checks
    sessions.py            dated capture+reconstruct session storage
    static/                single-page front end (index.html, app.js, style.css)
  schema/                  the frozen contract
    SCHEMA.md              human-readable schema + why each constraint exists
    raw_event.schema.json, event_log.schema.json
  samples/                 a domain-neutral sample raw stream (NOT a test slice)
  tests/                   unittest suite (no API key / pynput required)
launch_ui.command          double-click launcher for the test UI (macOS)
```

Capture is built as **independent capturers feeding one shared append-only
stream** (`raw_events.jsonl`, normalized `{ts, source, payload}`), because the
signals live in different places: keystrokes/window/clipboard run as a local
macOS process; clicks, element targets, in-browser clipboard, and scoped
structured reads come from the browser extension via the localhost collector.

## Why this capture shape (no screenshots / no OCR)

v1 capture is lightweight signals only — **no pixels, no OCR**. In prior testing,
OCR was ~95% of data volume and cost but added almost nothing to action
structure: window titles + keystrokes reconstructed the same action sequence on
their own at ~1/20th the token cost, with no hallucinations. OCR's only unique
contribution was read-only document content, which v1 forgoes (browser-side
read-only content is instead recovered by scoped structured reads).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # anthropic; pynput only on macOS
export ANTHROPIC_API_KEY=sk-ant-...       # for reconstruction (see "Anthropic API key" below)
```

## Running Engine 1 end to end

### 1. Capture a session → `raw_events.jsonl`

On the worker's **macOS** machine, run the local process and the collector
together, and load the extension so browser signals merge into the same stream:

```bash
# terminal 1 — browser-event collector (writes the shared stream)
python -m trace.capture.collector --out captures/raw_events.jsonl --port 8137

# terminal 2 — local keystroke + window + clipboard capture (appends to it)
python -m trace.capture.capture --out captures/raw_events.jsonl --append
```

Then load `trace/extension/` as an unpacked Chrome extension (see
`extension/README.md`). The extension badge shows `REC`; click it to pause.

> **Browser support:** the extension currently works on **Chrome / Chromium-based
> browsers only** (Chrome, Edge, Brave, Arc, Opera). It is a Manifest V3 extension
> built on the `chrome.*` APIs and does **not** run on Safari yet — Safari uses a
> different extension model (a native-app-wrapped Web Extension, the `browser.*`
> namespace, and native messaging instead of a localhost `fetch`). A Safari port
> would reuse the `content.js` capture logic but needs a new shell; see the open
> items below. Note the **local capture process works regardless of browser** — a
> Safari user still gets keystrokes, window titles, and clipboard; without the
> extension they only lose the additive browser stack (click targets, in-browser
> clipboard direction, scoped structured reads).

- The local process needs macOS **Input Monitoring** permission.
- `Ctrl-Shift-P` pauses/resumes the local capture; `Ctrl-C` stops it.
- All text is redacted before it hits disk; password fields are never read.

### 2. Reconstruct → `event_log.json`

```bash
python -m trace.reconstruct.cli captures/raw_events.jsonl -o captures/event_log.json
```

Reconstruction loads and orders the stream, chunks it chronologically with a
small overlap and a running summary (so boundary-spanning actions aren't split),
sends each chunk to `claude-haiku-4-5` at **temperature 0** with the perception
prompt, then validates, stitches, and renumbers the result. Determinism matters:
the same raw input yields a materially identical log, because Engine 2's
recurrence detection depends on it.

Inspect exactly what Haiku will see, with **no API cost**:

```bash
python -m trace.reconstruct.cli captures/raw_events.jsonl --dry-run --chunk-size 8 --overlap 2
```

A domain-neutral example stream is in `trace/samples/sample_raw_events.jsonl`.

#### Anthropic API key (the Haiku step)

The reconstruct step is the only part that calls a model — `claude-haiku-4-5` via
the Anthropic API — so it needs an **Anthropic API key**. (Capture, `--dry-run`,
and the test suite need no key and make no API calls.)

The key is **not** entered interactively or stored in the repo. Under the hood
`reconstruct.py` constructs `anthropic.Anthropic()`, which by default reads the
key from the **`ANTHROPIC_API_KEY` environment variable** — so that is where the
user supplies it:

```bash
# get a key from https://console.anthropic.com/  (starts with sk-ant-...)
export ANTHROPIC_API_KEY=sk-ant-...        # current shell, or add to ~/.zshrc
python -m trace.reconstruct.cli captures/raw_events.jsonl -o captures/event_log.json
```

Prefer not to export it globally? Set it for the single command, or keep it in a
git-ignored `.env` and source it:

```bash
ANTHROPIC_API_KEY=sk-ant-... python -m trace.reconstruct.cli captures/raw_events.jsonl -o out.json
# or:  echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env && set -a && source .env && set +a
```

`.env` is already in `.gitignore`, so the key never gets committed. If the key is
missing or invalid, the Anthropic client raises an authentication error at the
first chunk — capture and `--dry-run` are unaffected.

### 3. Run the tests

```bash
python -m unittest discover -s trace/tests -t . -p "test_*.py"
```

The suite (69 tests) covers redaction, keystroke coalescing, the raw writer,
chunking, schema validation/normalization, fragment-stitch flagging, the full
reconstruction pipeline with a mock model, plus the test-UI backend (key store,
sessions, capture lifecycle, reconstruct guards) — so it runs without an API key,
`pynput`, or a browser.

## Local test UI (run the whole engine from a window)

For the operator validating the engine, there's a **local web UI** that drives
everything above without the terminal: enter/test the API key, start/stop/pause a
capture, run reconstruction (or a free dry-run preview) with progress, and inspect
the resulting JSON. It's a thin controller over the *unchanged* engine.

```bash
python -m trace.ui.app            # opens http://127.0.0.1:8765/ in your browser
# or on macOS, double-click launch_ui.command
```

It is **local-only** (binds 127.0.0.1, no accounts, no remote anything — capture
needs OS permissions and local files). What it does:

- **Settings** — masked API-key field with a **Test key** button (one tiny call to
  confirm it works), a macOS **permissions** checklist (Accessibility + Input
  Monitoring), and **browser-capture** status from the collector.
- **Capture** — one Start/Stop button, a live capturing indicator with elapsed
  timer and event count, and Pause/Resume.
- **Reconstruct** — an explicit Reconstruct button (with per-chunk progress) and a
  free **Preview (dry run)**; guarded against empty/short recordings and a missing
  key.
- **Results** — the engine's `event_log.json` shown as pretty JSON, alongside the
  **review-flags** report and the **raw stream**, with copy/export; plus **session
  history** to reopen past runs (and re-reconstruct to check run-to-run stability).

The key is stored in the **macOS Keychain** (via `keyring`) when available, else a
`0600` `~/.trace/config.json`, and is never returned over the API, logged, or
written into results. The UI adds two small, additive seams to the engine — a
`CaptureController` (`start/stop/pause/resume/status`) and an optional
`progress_cb` on `reconstruct()` — but the capture/reconstruction/validation logic
is unchanged, so what you test through the UI is the real engine.

## The contract: `event_log.json`

`event_log.json` is the **frozen interface** Engine 2 consumes. Full definition
and rationale live in [`schema/SCHEMA.md`](trace/schema/SCHEMA.md). Key points:

- A JSON array of events, each `{id, ts_start, ts_end, app, window, action,
  detail, evidence, confidence}`.
- `action` is drawn from a **small controlled vocabulary**, so every instance of
  the same real task yields a comparable action subsequence — that comparability
  is what lets Engine 2 recognize a workflow recurred. Unstable vocab or detail
  formatting breaks recurrence detection.
- `detail` is **fully grounded** in `evidence` (cited raw-event ids) and preserves
  concrete observed values exactly (line items, totals, prices, tracking numbers,
  recipients, message text, dropdown selections) — these become Engine 3's
  "observed examples."
- Human-judgment steps are kept legible in `detail` rather than flattened, so
  Engine 2 can separate automatable work from judgment-dependent work.

## Faithfulness over richness

Reconstruction **never invents content it cannot see**. If the worker opened a
PDF whose contents were never captured, the event is "opened that PDF" — not a
fabricated summary. Thinner-but-honest logs are the intended failure mode.

## Privacy (first-class, from the first commit)

- Keystrokes inside password fields are never recorded (the extension drops
  `input[type=password]` and equivalents).
- Obvious secrets/PII (card numbers via Luhn, SSNs, API keys, bearer tokens, JWTs,
  private keys, `password=`/`secret=` assignments) are redacted **at capture
  time**, in both the extension and the enforced `RawWriter` backstop, so they
  never reach disk. Email addresses are kept (recipients are needed signal).
- An obvious pause/stop: the extension toolbar toggle and the local
  `Ctrl-Shift-P` / `Ctrl-C`.

## Known limitations (v1) — documented, not solved

These are accepted consequences of dropping OCR/screenshots:

1. **Native-app document content is not captured.** Structured read is
   browser-only, so a PDF opened in a native viewer (e.g. Acrobat) appears as
   "opened X.pdf" with no contents. Future paths: open such docs in-browser
   (DOM-readable), OS-accessibility capture on native apps, or selective OCR on
   read-heavy anchors only.
2. **Canvas-app visual state is invisible.** A conditional-formatting color (e.g.
   a cell that turns red in Google Sheets) has no DOM element and no pixels are
   captured, so "this cell turned red" cannot be recovered. Reporting it would be
   a hallucination; correctly *not* reporting it is the right behavior.
3. **Native-app password fields.** The browser extension protects browser
   password fields; native-app password fields have no OS-accessibility hook in
   v1, so the local keystroke capturer relies on pattern redaction there. Pause
   capture for native logins.
4. **Intra-document context that doesn't change the window title** (e.g.
   switching tabs inside one spreadsheet) won't fire a window event; we rely on
   clicks / structured-read anchors to disambiguate where possible.

## Open decisions (flagged for the team, not blocking)

- **Native-app content:** accept the v1 gap as written, or require target
  workflows keep documents in-browser so structured read can see them?
- **Action vocabulary:** v1 ships a *small fixed* vocabulary (in `prompt.py` /
  `SCHEMA.md`) and normalizes synonyms in `validate.py`, per the recommendation
  to "start fixed-but-small, expand as real sessions demand." An open vocab would
  be faster to start but riskier for Engine 2's recurrence detection.

## A note on the validation slice (Appendix B)

The 16-action "Marcy" slice is a **held-out fidelity exam**, scored by a human —
not a loop to optimize against. There are **no business-specific strings**
anywhere in this code or the prompt (no customer/sheet/app/SKU names from any
slice); generalizing meaning from the raw signals is the model's job. The sample
stream in `trace/samples/` is generic and unrelated to the slice. A correct v1
should land ~15/16 on that slice, missing only the conditional-formatting red
cell (limitation #2), with the copy-paste-to-another-tab step now visible thanks
to clipboard capture.
