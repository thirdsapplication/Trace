# Trace — Engine 1: The Event Log Engine (v1)

Trace observes a knowledge worker and discovers the automatable tasks for them.
It is three engines run in sequence over one observed workday:

```
Raw capture stream
   │
   ▼
Stage 1 — Event Log Engine     ← THIS REPO  (capture -> event_log.json)
   │   hands off event_log.json  (the frozen contract)
   ▼
Stage 2 — Discovery Engine     ← THIS REPO  (event_log.json -> discovery.json)
   │   hands off discovery.json (bundles + anti-bundles + evidence)
   ▼
Review / edit (Test UI)        ← THIS REPO  (discovery.json -> reviewed_bundles.json)
   │   human corrects intent / reclassifies / sets disposition
   ▼
Stage 3 — Artifact Engine      ← THIS REPO  (reviewed_bundles.json -> spec_<id>.md bundle)
   │   one self-contained buildable spec per approved workflow
   ▼
Stage 4 — Agent Compiler + Test Deployer  ← THIS REPO
   │   spec -> agent_<id>.{test,live}.yaml; sandbox-test the test agent + grade it
   ▼
Live deployment of the tested agent  (separate, later handoff — out of scope here)
```

This repository implements the **whole MVP spine**: **Stage 1 (Event Log)**,
**Stage 2 (Discovery)**, a lightweight **review/edit** step, and **Stage 3
(Artifact Engine)**. Stage 1 captures a lean, privacy-preserving signal stream and
reconstructs it (Haiku) into `event_log.json`; Stage 2 (`trace/discovery/`)
proposes workflows — **bundles** (automatable) and **anti-bundles**
(judgment-required) — in one Sonnet call over the whole day; the operator lightly
corrects them in the review surface (`reviewed_bundles.json`); Stage 3
(`trace/artifact/`) turns each approved bundle into a paste-ready Artifact C
markdown spec. See [Stage 2](#stage-2--discovery-engine) and
[Stage 3](#stage-3--review--edit--artifact-engine).

Engine 1 runs over **one full workday** — capture runs continuously, and the
result is a single `event_log.json` for that day. The recurrence Engine 2 looks
for happens *within* a single day (order intake a dozen times, ship confirmations
many times, etc.), so one day's log already contains the repetition discovery
needs.

## Quickstart — launch and test

Full **capture** requires macOS (keystrokes/window/clipboard use mac APIs).
**Reconstruction, dry-run, and the test suite** run on any OS. Below: the main
path (the local test UI), then the pure-CLI path, then a no-cost/no-Mac smoke
test.

### 0. Get the code

```bash
git clone https://github.com/thirdsapplication/Trace.git
cd Trace
git checkout claude/determined-dijkstra-WvqW0
```

Already have a `Trace` folder? Don't re-clone — update it instead:

```bash
cd Trace
git remote -v        # confirm it points at github.com/thirdsapplication/Trace
git fetch origin
git checkout claude/determined-dijkstra-WvqW0
git pull origin claude/determined-dijkstra-WvqW0
```

Verify you're set up:

```bash
git branch --show-current     # -> claude/determined-dijkstra-WvqW0
ls trace/ui/app.py            # -> prints the path if the UI is present
```

### 1. One-time setup (virtualenv + deps)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On macOS this installs `anthropic`, `pynput`, and `keyring`. On other OSes the
mac-only ones are skipped automatically.

### 2. Launch the test UI (main path)

```bash
python -m trace.ui.app
```

Expected output:

```
Trace test UI: http://127.0.0.1:8765/
  sessions  -> /…/Trace/captures/sessions
  collector -> 127.0.0.1:8137 (browser extension target)
Ctrl-C to stop.
```

The browser opens to `http://127.0.0.1:8765/` (add `--no-open` to suppress).
Leave the terminal running; **Ctrl-C stops the whole app.** Then, in the browser:

1. **Settings → API key:** paste your `sk-ant-…` key, **Save**, then **Test key** → expect "valid ✓".
2. **Settings → permissions:** grant the two macOS permissions (step 3), then click **refresh** → both flip to ✓.
3. **Capture:** **Start capture** (pill turns green, timer ticks, event count rises), do your scripted session, optionally **Pause/Resume**, then **Stop**.
4. **Reconstruct:** **Preview (dry run)** first (free — shows exactly what Haiku gets), then **Reconstruct** (paid; watch the progress bar).
5. **Results:** read `event_log.json`; switch tabs to **review flags** and **raw stream**; **copy/export**. Past runs are under **Session history**.

macOS alternative to the command: double-click **`launch_ui.command`** at the repo root.

### 3. macOS permissions (required before capture records anything)

Grant these to the app that runs the command (your Terminal/iTerm, or the launcher):

- **System Settings → Privacy & Security → Input Monitoring** → enable your terminal.
- **System Settings → Privacy & Security → Accessibility** → enable your terminal.

Then **quit and relaunch the terminal** (macOS applies these only on restart) and re-run step 2.

### 4. (Optional) Browser signals — clicks, in-browser clipboard, structured reads

The UI already runs the collector on port **8137**, so just install the extension
(Chrome/Chromium only):

1. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → select `trace/extension/`.
2. The badge shows **REC**; during capture the UI's browser-capture status flips to "✓ extension sending".

Don't start a separate collector when using the UI — it already owns port 8137.

### 5. Alternative — pure CLI path (no UI)

Three terminals from the repo root, venv activated:

```bash
# Terminal A — browser-event collector (only if using the extension)
python -m trace.capture.collector --out captures/raw_events.jsonl --port 8137
```
```bash
# Terminal B — local capture (keystrokes + window + clipboard)
python -m trace.capture.capture --out captures/raw_events.jsonl --append
#   Ctrl-Shift-P pauses/resumes · Ctrl-C stops
```
```bash
# Terminal C — reconstruct after stopping capture
export ANTHROPIC_API_KEY=sk-ant-...
python -m trace.reconstruct.cli captures/raw_events.jsonl -o captures/event_log.json
cat captures/event_log.json
```

### 6. Anywhere smoke test (no key, no Mac, no cost)

```bash
# full automated suite (218 tests)
python -m unittest discover -s trace/tests -t . -p "test_*.py"

# preview the chunked prompt on the bundled sample (no model call)
python -m trace.reconstruct.cli trace/samples/sample_raw_events.jsonl --dry-run --chunk-size 8 --overlap 2
```

Expect `Ran 218 tests … OK`, and a printed system prompt + 3 rendered chunks.

**Useful flags:**
- UI: `python -m trace.ui.app --port 8765 --collector-port 8137 --sessions-root captures/sessions --no-open`
- Reconstruct: `--model claude-haiku-4-5 --chunk-size 120 --overlap 20 --dry-run`

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
  discovery/               Engine 2 — event_log.json -> discovery.json
    discover.py            ONE Sonnet call over the whole day (no chunking)
    prompt.py              the domain-neutral discovery prompt
    validate.py            discovery.json schema validation/repair
    scorer.py              gate harness (the ONLY code that reads an answer key)
    cli.py                 entry point + --dry-run
    discovery.schema.json, DISCOVERY_SCHEMA.md
    fixtures/              marcy + recruiter event logs + answer keys (test data)
  artifact/                Stage 3 — reviewed_bundles.json (+ environment) -> spec_<id>.md
    review.py              reviewed_bundles contract (seed/validate/fetch examples)
    environment.py         Review 2: derive artifact requests + questions, status
    extract.py             per-kind extractors (csv/xlsx/doc/pdf), stdlib only
    artifact.py            one model call/bundle + deterministic markdown assembly
    prompt.py              the domain-neutral spec-writing prompt
    cli.py                 entry point + --dry-run
    reviewed_bundles.schema.json, environment.schema.json, ARTIFACT_SCHEMA.md
  agent/                   Stage 4 — spec -> Managed Agents agent + sandbox test
    compiler.py            wrap spec into agent_<id>.{test,live}.yaml (pure, no network)
    trigger.py             trigger messages from the bundle's observed instances
    grader.py              grade a run vs the spec's own criteria (walled off)
    deployer.py            full sandbox test run (upload/mount/trigger/grade/cleanup)
    api_client.py          the ONE beta-pinned Managed Agents/Files API client
    config.py              app→MCP table, framing templates, defaults (editable)
    agent_envelope.schema.json, test_report.schema.json
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

Structural rules in the stitch/finalize step keep the log faithful and stable:

- **Boundary-straddle de-duplication.** An action whose evidence crosses a chunk
  seam is reconstructed truncated in the earlier chunk and complete in the later
  one. The merge recognizes them as the same action by structural sameness
  (window context + action type) plus overlapping *material* evidence
  (keystrokes/clicks/clipboard — never shared navigation context), and keeps the
  most complete instance. Two genuinely distinct-but-similar actions cite disjoint
  material spans and are both preserved (no over-merge).
- **Contiguous-entry coalescing.** A single logical record fragmented across many
  keystrokes in one window (with no intervening action of a different kind) is
  assembled into one `enter_data` event — consistently, regardless of fragment
  count or stream position — restoring stable reconstruction of fragmented canvas
  typing. Distinct actions separated by a different action (or window) are never
  coalesced.
- **Symmetric document-open detection.** A focus that persists beyond a brief
  flicker and is *not already represented* by another action over its rows is
  recorded once as an `open_document` action — the *same* rule and the *same*
  single vocabulary for browser tabs and separate native apps (no app allowlist,
  no file-type branching). A window the worker immediately acts in is represented
  by that action, so no separate open is emitted; blank/anonymous titles and
  sub-threshold oscillation produce nothing. Contents are never fabricated — only
  the window-title identifier is recorded.

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

The suite (218 tests) covers redaction, keystroke coalescing, the raw writer,
chunking, schema validation/normalization, fragment-stitch flagging, boundary
de-duplication, contiguous-entry coalescing, document-open detection/coverage,
the browser-capture collector path, the full reconstruction pipeline with a mock
model, the discovery engine + validator + scorer + fixture gate, the Stage 3
review contract (seed/validate/reclassification routing/checkpoint mapping), the
Artifact Engine (verbatim example fetching, criteria assembly, divergence marking),
Review 2 environment capture (evidence-derived requests across two domains, per-kind
extraction, the courier/annotator model — authoritative instruction, raw file always
carried, non-load-bearing extraction, criteria targeting the user-designated
location, completeness off uploads+instructions), self-contained spec bundles
(byte-identical artifact export, spec-relative paths, move-and-resolve, mandatory
preflight, filename-collision safety, on two domains), Stage 4 (envelope compile
test/live with verbatim spec content + path rewrite + connector derivation + safe
permission defaults across two domains, trigger generation, the walled-off grader's
pass/fail/not-testable + gate-can-fail, the deployer's upload/mount/zero-coaching/
retention via a stub API client),
plus the test-UI backends for every stage (key store, sessions, capture lifecycle,
reconstruct/discovery/review/environment/spec/agent guards) — so it runs without an
API key, `pynput`, or a browser.

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
  timer and event count, and Pause/Resume. On Stop it shows a **by-source-type
  count summary** and a **non-blocking warning** if the session has local events
  but none of the browser-only source types (clicks/targets/structured reads) —
  i.e. the extension likely wasn't connected. The summary is derived generically
  from source-origin metadata (`trace/capture/sources.py`), so a new source type
  needs no code change, and the threshold is configurable.
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

## Stage 2 — Discovery Engine

`trace/discovery/` reads a frozen `event_log.json` and proposes the automatable
workflows inside it as `discovery.json`: **bundles** (automatable), **anti-bundles**
(judgment-required — surfaced as a human-checkpoint, **never** proposed for full
automation), and unclustered noise, each candidate citing `evidence_event_ids`.
It reasons over the **whole day in one Sonnet call (no chunking)** so a workflow
whose trigger and execution are hours apart — linked only by an artifact — stays
visible. The contract is `trace/discovery/DISCOVERY_SCHEMA.md`.

```bash
# render the prompt + whole event log that would be sent (free, no model call)
python -m trace.discovery.cli trace/discovery/fixtures/marcy_event_log.json --dry-run

# run discovery (needs ANTHROPIC_API_KEY; default model claude-sonnet-4-6)
export ANTHROPIC_API_KEY=sk-ant-...
python -m trace.discovery.cli path/to/event_log.json -o discovery.json
```

**The gate (Scorer).** `scorer.py` is a *separate* harness and the **only** code
that reads an answer key — the engine can't reach it. It grades a `discovery.json`
by **evidence-overlap + an intent-judge call** (never by recognizing bundle
ids/names), reports "X of N bundles identified", anti-bundle handling, automation
penalties, threshold sensitivity, and a verdict against rubric thresholds supplied
*alongside* the answer key:

```bash
python -m trace.discovery.scorer discovery.json trace/discovery/fixtures/marcy_answer_key.json
```

**From the Test UI.** The whole pipeline (capture → reconstruct → discover) plus
the gate score runs from the same local window: a **Run discovery** action with a
free **dry-run preview**, a `discovery.json` results tab, a **Load fixture**
control (run discovery on the bundled Marcy/recruiter logs), and a **Run gate
score** button that shows the scorecard when an answer key is present.

**Anti-overfitting (highest-risk here).** The engine never sees an answer key; the
prompt and engine code carry no business-, domain-, or answer-key-specific names,
counts, or ids. Generality is proven on a second, structurally-different fixture (a
recruiter's day) with its own answer key. The bundled fixtures are *test data* read
only by the scorer/tests — never by the engine.

> Note: the model-requiring steps (real discovery + the intent-judge) need an API
> key; the gate's empirical pass is run by the operator. The harness itself — schema
> validation, the scorer's overlap/handling/verdict logic, dry-run rendering, and
> the UI wiring — is covered by tests that run with a stub, no key, and no network.

## Stage 3 — review / edit + Artifact Engine

Discovery is good but not perfect, so a **lightweight review step** sits between
discovery and spec generation: the operator corrects each workflow at the cheapest
point, before it becomes a spec. It is editing, not workflow-building.

**Review surface (in the Test UI).** Loads a `discovery.json` and shows each
bundle with its intent fields, evidence, and the **verbatim observed examples**
(pulled from `event_log.json` by evidence id). Allowed edits, and only these:
reword name/description/trigger/steps; **reclassify** between bundle and
anti-bundle in either direction; set a bundle's **disposition**
(`approve_for_build` | `reject`); flag any step as a **human checkpoint** with a
note; **include/exclude** observed examples (never edit their text); add/edit/remove
**acceptance criteria**; set an optional **preferred output**. Saving writes the
frozen `reviewed_bundles.json` (contract in `trace/artifact/ARTIFACT_SCHEMA.md`) —
the only workflow input the Artifact Engine reads.

**Artifact Engine (`trace/artifact/`).** For each item classified as a `bundle`
with disposition `approve_for_build`, one model call (default `claude-sonnet-4-6`,
injectable) produces the intent prose, example-supported acceptance criteria, and a
representative/illustrative assessment of each example; deterministic code then
assembles the three-layer **Artifact C** markdown spec so that the verbatim
examples, every user-added criterion, and a "pause and request human input"
criterion per checkpoint are guaranteed to appear exactly. Anti-bundles emit a
short "handle manually — here's why" note instead; `reject` emits nothing.

**Self-contained spec bundles.** The deliverable is `spec_<id>.md` **plus a sibling
`spec_<id>_files/` directory** holding a byte-identical copy of every artifact
attached in Review 2. All references inside the spec are **spec-relative**
(`./spec_<id>_files/<file>`) — no path points into Trace's session storage — so the
bundle stays valid when moved to any machine. Every spec opens with a **mandatory
preflight** instructing the coding agent to open every attached file, echo back the
structure relevant to each authoritative instruction, and **STOP rather than infer**
if a file can't be opened or conflicts with its instruction. This closes the
delivery gap where a spec referenced files that only existed inside Trace.

```bash
# render the prompt + bundle + verbatim examples per approved bundle (free)
python -m trace.artifact.cli reviewed_bundles.json event_log.json --dry-run

# generate one spec_<id>.md per approved bundle (needs ANTHROPIC_API_KEY)
python -m trace.artifact.cli reviewed_bundles.json event_log.json -o specs/
```

**Divergence rule.** When the edited intent contradicts an observed example
(because the user corrected a habit or generalized past a one-off), **intent and
acceptance criteria win**, and the contradicted example is marked *illustrative of
context, not a target* so the coding agent doesn't build to it.

**Review 2 — environment capture (courier + annotator).** Between review and spec
generation, the system gathers the environment specifics the event log can't
contain — the real work products the workflow reads/writes. For each approved bundle
it **derives, from that bundle's own event evidence** (any action that reads/writes
an external artifact), which files to request — tagged input/output/reference with
the justifying event ids. The user **uploads** the real files (Trace never touches
live accounts) and, next to each, types a one-line **authoritative instruction**
("use the Open_Orders tab; match its columns"). Review 2 is a *courier + annotator*,
not the expert on the files:

- the **user instruction is authoritative** — nothing auto-derived overrides it;
- the **raw file always travels** to the spec, so the downstream coding agent reads
  it live;
- **auto-extraction is best-effort and non-load-bearing** — per-kind extractors
  (`extract.py`, stdlib-only) still run as a sanity aid, but a failed/empty/wrong
  extraction **never blocks**, never overrides the instruction, and is **never the
  basis for an assertion** — it shows only as a clearly-labeled "auto-read,
  unverified" note.

This is written to `environment_<id>.json` (contract in `ARTIFACT_SCHEMA.md`). The
engine folds it in: each uploaded artifact's **raw file + instruction lead the
Observed Examples**; **Acceptance Criteria target the user-designated location and
are checked live** against the real file (never against an auto-extracted schema).
**Completeness keys off uploads + instructions**, not extraction. A spec embedding
real files carries a sensitivity header; a record missing a required upload or
instruction flags the spec `environment-incomplete` but still generates (never
hard-blocks).

```bash
# fold real uploaded artifacts into the spec (env records live beside reviewed_bundles)
python -m trace.artifact.cli reviewed_bundles.json event_log.json --env-dir <dir> -o specs/
```

**From the Test UI.** The full spine runs from one window: capture → reconstruct →
discover → **review/edit/reclassify** → **environment capture** (upload files,
answer derived questions) → **generate specs** (free dry-run) → view each
`spec_<id>.md` as markdown in the results tabs, with copy/export.

## Stage 4 — Agent Compiler + Test Deployer (Claude Managed Agents)

`trace/agent/` turns an approved bundle into a **tested agent** on the Managed
Agents platform (beta `managed-agents-2026-04-01`). Two components:

**Agent Compiler** (pure, no network). Wraps `spec_<id>.md` into a Managed Agents
definition — the spec's substantive content (preflight, intent, examples, criteria)
travels **verbatim** into the `system` field; only mechanical adaptations are made
(prepend a framing paragraph, rewrite artifact paths to the session **mount** path,
drop coding-agent wrapper lines, reword the preflight trigger). It emits both
`agent_<id>.test.yaml` and `agent_<id>.live.yaml`:
- **test**: no external connections; outbound actions become text; no MCP servers.
- **live**: MCP servers derived from the bundle's **observed apps** via an editable
  `app → MCP` table (apps with no row are surfaced as a `# unmapped` comment, never
  guessed); every write capability defaults to an **approval-required** policy.
  *Emitted and schema-validated only — never deployed by this stage.*

**Test Deployer** (API orchestration). Through the single beta-pinned
`api_client.py`: uploads the bundle's Review 2 files, creates the agent + a sandbox
cloud environment, mounts the files, sends **only** a trigger generated from the
bundle's observed instances (zero coaching — no steering messages), captures the
event stream, downloads outputs, and **deletes the session and files** (nothing
persists). A walled-off **grader** (own prompt, like the discovery Scorer) judges
the run against the **spec's own acceptance criteria** → `test_report_<id>.json`,
per-criterion pass / fail / **not-testable-in-sandbox** (criteria needing live
services are never silently passed). A failing criterion or a stall blocks the
deploy recommendation — the gate can fail.

```bash
# compile the agent envelopes for approved bundles (offline, no API)
python -m trace.agent.cli reviewed_bundles.json event_log.json --specs-dir <specs> -o <out>
```

**From the Test UI.** A **Compile & test agents** action runs Compiler → Test
Deployer and shows the per-bundle verdict; the `agents` results tab shows both
envelopes and the test report. The whole of Stage 4 is offline-testable (the API
client is injected/stubbed); real platform calls happen only in the explicit gate
run. Live deployment (OAuth, real MCP connections, scheduling, the refinement loop)
is a separate, later handoff.

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
