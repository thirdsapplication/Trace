# Trace — UI Redesign Handoff

**Audience:** the designer/agent writing the new UI spec ("Claude Design").
**Purpose:** a complete, current description of the Trace system and its existing UI so a redesign
spec can be written. Treat the "current UI" as a starting point to improve, not a thing to reproduce.

> **Currency note.** The system is now **live-only**. Config generation produces **only**
> `agent_<id>.live.yaml`. The old **sandbox logic gate** (Phase 3), the **dry-run preview** feature
> (Phase 5), and the **test agent variant** were all removed. Some code comments / README lines still
> mention a test YAML or a gate — ignore them; this document reflects the real current state.

---

## 1. What Trace is

Trace is a **closed-loop automation builder**. It observes a knowledge worker for one workday,
discovers their repeating workflows from lightweight capture signals, then **builds and deploys a
Claude agent per workflow** — each running on Claude **Managed Agents** and connected to the worker's
real apps (Gmail, Google Sheets, …) through a single **Pipedream** connection. Every outbound
write/send the agent attempts is **held for one-tap human approval**; reads proceed.

### Two audiences (currently merged into one screen)
- **Operator** — technical; runs the whole pipeline, manages API keys / Pipedream settings, reviews
  discovered workflows, reads raw JSON/YAML.
- **Worker** — non-technical; the observed person. They need only three things: **connect their apps**,
  **approve/reject the agent's outbound actions**, and **see what the agent did**.

The current UI is an operator/developer "test harness" (its literal title is
"Trace · Engine 1 Test Harness"). **Open design question:** keep one operator console, or split a clean
worker-facing surface (connect + approve + activity) from the operator console?

---

## 2. The pipeline (current, live-only)

Linear; each stage's output feeds the next. Model + cost matter for UX (paid steps deserve clear
"this costs money / this is free" affordances).

| # | Stage | Input → Output | Model | Cost | Notes |
|---|-------|----------------|-------|------|-------|
| 1 | **Capture** | worker activity → `raw_events.jsonl` | — | Free | macOS local capture + optional Chrome extension; redacted at ingest; no screenshots |
| 2 | **Reconstruct** | raw → `event_log.json` | `claude-fable-5-1` | **Paid** | Deterministic, chunked, has a progress bar; "Preview (dry run · free)" shows the exact prompt with no API call |
| 3 | **Discovery** | event_log → `discovery.json` (**bundles** + **anti-bundles**) | `claude-fable-5-1` (one whole-day call) | **Paid** | Free prompt preview; plus a fixture loader + "Run gate score" (offline accuracy check vs an answer key — the *discovery scorer*, unrelated to the removed sandbox gate) |
| 4 | **Review & edit** | discovery → `reviewed_bundles.json` | — | Free | Operator edits/reclassifies/approves; sets `disposition = approve_for_build | reject` |
| 5 | **Questions** (Agent Author draft → Gap Detector) | reviewed bundle → `author_<id>.json` + `interview_<id>.json` | `claude-fable-5-1` ×2 | **Paid** | The Agent Author drafts the agent and lists its unknowns; the Gap Detector turns those into up to 10 questions, each anchored on what the worker was seen doing ("We saw: …") (what capture couldn't see / decisions it couldn't make), and **none at all** when the workflow has no real unknowns; answers are authoritative |
| 6 | **Build agents (Agent Author)** | bundle + answers → `agent_<id>.live.yaml` + `grounding_<id>.json` | `claude-fable-5-1` | **Paid** | **Live-only.** Writes files locally — does **NOT** touch the Managed Agents console |
| 7 | **Connect your apps** | app list + worker email → `connection.json` | — | Free orchestration (real Pipedream/vault calls) | Per-app OAuth tap + "Verify"; identity-mismatch flagged & made unusable |
| 8 | **Go live (Deploy)** | live YAML + connection → live agent | — | Paid (Managed Agents) | Creates the agent on the platform, injects the vault, holds outbound actions for approval |

**Removed — do not design for these:** sandbox **logic gate** (old Phase 3) and the **dry-run preview**
feature (old Phase 5). The pipeline is now **config → connect → deploy**.

**Dormant/legacy in the tree (NOT in the live flow):** `trace/artifact/` markdown specs,
`compiler.py`/`deployer.py` manual-eval path, the `generate-specs`/`specs` backend endpoints. Present
but not part of the live journey — ignore for the redesign.

---

## 3. Domain vocabulary

- **event_log** — the frozen, normalized list of what the worker did (the contract output of Reconstruct).
- **bundle** — a discovered **automatable** workflow (trigger + steps).
- **anti-bundle** — a discovered workflow that **needs human judgment**; never automated; surfaced as a caution.
- **grounding** — a record footnoting every instruction in an agent to its source (event ids / a worker answer / a discovery rule). Traceability.
- **interview / unresolved** — gap questions; answered ones become authoritative, unanswered ones stay "unresolved."
- **Connect / Pipedream** — the layer holding the worker's app OAuth grants.
- **vault** — a Managed Agents secret container, one per customer, injected into the agent's session.
- **external_user_id** — the per-customer isolation key (`trace-<session>`); scopes the connection, vault, and per-app URLs.
- **mcp_server / mcp_toolset** — how the live agent reaches an app (one per app); every toolset is **approval-required**.
- **held action / approval** — an outbound write/send the agent proposes; **held** until the worker approves (kind + summary shown).
- **environment (development/production)** — Pipedream routing level; **defaults to production**; dev and prod are fully isolated.

---

## 4. Current UI, screen-by-screen (top → bottom)

A **single long vertical page** (dark theme), one `<section class="panel">` per stage, plus Results and
Session History. **No router, no SPA framework** — vanilla HTML/CSS/JS served by a stdlib HTTP server;
async work is **polled** (~700–800 ms) and reported in small color-coded status lines. **No modals**
(uses native `confirm()`, `alert()`, `window.open()`).

**Header** — title "Trace · Engine 1 Test Harness" + a **status pill**: `idle · capturing · paused · reconstructing · results`.

1. **Settings** — (a) *Anthropic API key*: password input, Save, Test key. (b) *Pipedream Connect*:
   project id, client id, client secret, **environment dropdown (production default)**, Save. (c) *macOS
   permissions* checklist (+refresh) and *Browser capture* status.
2. **Preflight** — "Run preflight" → checklist: Anthropic key valid, Managed Agents access, Pipedream
   token mints + project reachable. Each item is `✓ / ✕ / – (not configured)` + a detail string.
3. **Capture** — Start/Stop (primary; turns red when active), Pause/Resume, live timer (mm:ss), event
   counter, session id, summary on stop.
4. **Reconstruct** — "Preview (dry run · free)" + "Reconstruct"; animated **progress bar** ("part X of Y").
5. **Discovery** — "Run discovery" + free preview; **fixture selector + "Load fixture"** + "Run gate
   score" (renders a scorecard).
6. **Review & edit** — "Load discovery for review" → editable **bundle cards** (green border = bundle,
   yellow = anti-bundle): name, classification, disposition, description, trigger, why-judgment,
   handling, **per-step rows** (text + "checkpoint" toggle + note), **observed-example include/exclude
   checkboxes (verbatim text)**, acceptance-criteria + preferred-output textareas. "Save reviewed bundles."
7. **Interview** — "Generate questions" + dry run → **per-bundle Q&A cards**: question + lens +
   "cost if wrong" + why-it-matters, one-line answer input, clickable **suggestion chips**, "save answers"
   per bundle; per-bundle status (unstarted/incomplete/complete; "N skipped").
8. **Build agents** — **bundle checkboxes** (all checked; "built" badge if already built), "Build
   selected", and **"Clean up platform"** (native confirm → deletes Trace-created sessions/agents and
   sweeps orphaned agents by workflow name).
9. **Connect your apps** — worker email input + "Confirm email"; **per-app cards** with "Connect"
   (opens Google OAuth via `window.open`) and "Verify connection" (polls); status is
   `not connected / connected ✓ (email) / connected — FLAGGED ⚠ not used`.
10. **Go live** — start-mode dropdown (`a new matching event (trigger)` | `a schedule` → reveals a cron
    input), "Deploy live"; **per-bundle deploy cards** showing live status and, crucially, **held
    outbound actions** each with inline **Approve / Reject**.
11. **Results** — tabbed raw viewer: `event_log.json`, `discovery.json`, `reviewed_bundles`,
    `agents (yaml + grounding)`, `review flags`, `raw stream`, `recon dry-run`, `discovery dry-run`;
    plus **copy / export**. (Developer-oriented JSON/YAML dumps.)
12. **Session history** — table of past sessions (id, created, raw/events/reconstructed/discovered
    indicators, "open").

**Stage gating:** later sections' buttons are **disabled until prerequisites exist** (Interview/Build/
Connect/Deploy enable only after "Save reviewed bundles"; Reconstruct after Capture; etc.). There is
**no explicit "you are here" stepper** beyond the single pill.

**Visual primitives:** dark palette (`--accent #4f8cff` blue, `--ok #3ec46d` green, `--warn #e0a92e`
yellow, `--err #e0533e` red, `--muted` gray); buttons `.primary`/`.secondary`/`.link`; status text
`.ok/.warn/.err/.muted`; cards `.review-item.bundle|.anti`; monospace `.json-view`.

---

## 5. Backend / data contract the UI is bound to

A redesign can keep or evolve this; either way it must surface every state below.

**Async job model:** each long action is `POST …/{action}` → `202 {state:"running"}`, then the UI
**polls** `GET …/{action}/status`. Status dicts (render-able fields):
- `recon_jobs`: `{state, current, total, error, flags, errors}` (the only one with real progress)
- `discover_jobs`: `{state, error, bundles, anti_bundles}`
- `interview_jobs`: `{state, error, generated}`
- `build_jobs`: `{state, error, generated, errors:{bundle_id:detail}}`
- `connection_jobs`: `{state, error, slug, connect_url, verified, connected, usable, flag}`
- `deploy_jobs`: `{state, error, deployed, errors:{bundle_id:detail}}`

**Key read endpoints (what fills each panel):** `/preflight`, `/api/sessions` (history),
`/api/sessions/{sid}` (artifact availability), `…/event_log`, `…/discovery`, `…/reviewed-bundles`,
`…/review-data` (editor seed + verbatim examples), `…/interview-data`, `…/connection-data` (per-app
verified/usable/flag), `…/build-bundles` (`{id,name,built}`), `…/deploy-bundles`
(`{id,name,status,start,held_actions}`), `…/agents` (`{bundle_id, live_yaml, grounding, deploy}`).

**Per-session artifacts on disk:** `meta.json` (timestamps: created/stopped/reconstructed/discovered/
reviewed/interview/build/deploy_at), `raw_events.jsonl`, `event_log.json`, `validation_report.json`,
`discovery.json`, `reviewed_bundles.json`, `connection.json`, and in `specs/`: `agent_<id>.live.yaml`,
`grounding_<id>.json`, `interview_<id>.json`, `deploy_<id>.json`.

**Behavioral facts that matter for UX:**
- **Build is local-only** — writes YAML/grounding, never changes the Managed Agents console. The console
  changes only at **Deploy**. (This has confused operators; make "local artifact" vs "live on platform"
  unambiguous.)
- **Deploy** reuses an agent by deterministic name via **delete + recreate**, so redeploy reflects the
  latest build (and may briefly end a running session).
- **Connect is two-step** (Connect tap → Verify poll); identity is verified against the confirmed email;
  mismatches are flagged and the app marked unusable.

---

## 6. Integration & safety model (what the worker actually touches)

- A live agent reaches each app through a **per-app Pipedream MCP server URL**
  (`remote.mcp.pipedream.net/{external_user_id}/{app}?projectId=…&environment=…&toolMode=tools-only`),
  authenticated by Trace's project-scoped token stored in the customer's **vault**,
  **injected at session create**. **Every URL piece is load-bearing** (all verified live,
  Aug 2026): the path routes to the customer; `projectId` + `environment` are what let the
  server resolve their **connected account** — without them, tools still *list* but every
  call answers "connect your account" while all dashboards look green; `toolMode=tools-only`
  selects real structured tool schemas instead of the default one-string "instruction"
  wrapper. **Never pin an MCP version segment**: the old `/v3` query-param surface served a
  reduced, stale tool subset — for Google Sheets, a read/append toolset with **no update
  tool**, which stranded a live workflow in Aug 2026. The public per-app catalog
  (mcp.pipedream.com) is the registry, *not* a promise of what a given server URL serves —
  deploy now runs a `tools/list` preflight and records each app's served toolset on the
  deploy record, warning when a sheets toolset can append but not update, plus a **binding
  check** that fails the deploy when a baked server URL has no matching vault credential.
  Note the full catalog also includes destructive tools (delete/clear); staging remains
  prompt-level.
- **Every outbound write/send is held** (`always_ask`) — the agent goes idle awaiting a
  `tool_confirmation`; the held action's **kind + summary** is surfaced for one-tap **Approve/Reject**.
  **Reads proceed.** This is the single most important worker-facing safety surface.
- **Per-customer isolation** via vault + `external_user_id`.
- **Cleanup is explicit, never automatic** (a button); now sweeps orphaned agents by workflow name.
- **Production prerequisites that surface as errors** (design clear, actionable error/empty states):
  1. **Pipedream "Connect component API" org entitlement** — must be enabled per-workspace
     (support-gated); without it, tool *execution* 403s even though listing works.
  2. **Custom Google OAuth client** required for Gmail/Sheets in **production**.
  3. **dev vs prod isolation** — accounts connected in one environment are invisible to the other (the
     environment default is now `production`).

---

## 7. Known pain points (redesign targets)

1. **Operator and worker are merged** into one long developer console; worker-critical surfaces
   (connect, approve, activity) are buried among capture/reconstruct/discovery internals.
2. **No clear progress model** — a 12-panel scroll with disabled buttons; the only "where am I" cue is a
   single pill. No stepper, per-stage completion state, or "what's next."
3. **Developer-oriented output** — results are raw JSON/YAML dumps; nothing renders "here's what this
   agent will do" in plain language.
4. **Weak async feedback** — most long operations show only a text status line (only Reconstruct has a
   real progress bar); everything is polled.
5. **Jargon-heavy** — "Engine 1 Test Harness," "bundles/anti-bundles," "grounding," "gate score," "dry
   run" (now overloaded — it means *free prompt preview*), "dispositions." Needs plainer vocabulary,
   especially on worker surfaces.
6. **OAuth/connect flow is clunky** — `window.open` + a manual "Verify" + polling, with a separate
   "Confirm email" step; failure modes (flagged identity, wrong environment, missing entitlement) aren't
   explained inline.
7. **Held-action approvals are under-designed** — tiny inline `Approve/Reject` links on a deploy card,
   yet this is the core trust/safety moment.
8. **No empty-state / modal polish** — native `confirm()`/`alert()`; many panels are blank until acted on.

---

## 8. States to cover (so the spec is complete)

For each interactive surface define **empty / disabled (prerequisite missing) / running / done / error**,
plus these domain-specific states:
- **Capture:** idle, recording (timer+count), paused, stopped (+health/warning), short-recording confirm.
- **Reconstruct:** preview (free), running (progress %), done (N flags/repairs), no-key, empty-recording.
- **Discovery/Review:** N bundles / M anti-bundles; per-bundle bundle-vs-anti, approve/reject; verbatim
  example include/exclude.
- **Interview:** per-bundle unstarted/incomplete/complete, skipped count, suggestion chips.
- **Build:** built badge, per-bundle build error, **local-only (not on platform yet)**.
- **Connect:** not connected / connecting (window open) / connected-verified / **flagged (identity
  mismatch, unusable)** / wrong-environment.
- **Deploy:** not-eligible (needs built config + usable connection), deploying, live, errored, and **held
  action awaiting approval / approved / rejected**.
- **Cross-cutting:** no API key, Pipedream not configured, preflight failures, the three production
  prerequisites above.

---

## 9. Constraints to preserve

- **Stage ordering + artifact-gating** (later steps require earlier artifacts) is core to correctness —
  keep the dependency even if you change its presentation.
- **Approval-gated outbound actions** must remain a deliberate, legible, per-action human decision.
- **Per-customer isolation** (vault + external_user_id) and **explicit, never-automatic platform cleanup**.
- **Local-first / no mandatory account creation for the worker** (they only do the OAuth tap).
- **Current tech baseline:** stdlib HTTP backend + vanilla HTML/CSS/JS, polling job model, localhost test
  harness. A redesign *may* propose a framework / SSE / websockets (Managed Agents itself streams session
  events, so real-time activity is feasible), but should call that out explicitly as a change.

---

## 10. Highest-leverage opportunities for the new design

1. **Split surfaces by audience:** a clean **worker view** (connect apps → watch the agent → approve/reject
   → "what it did") vs an **operator console** (the pipeline).
2. **A first-class "agent activity + approvals" surface** — the held-action approve/reject is the product's
   trust moment; make it the centerpiece, with plain-language summaries of each proposed action (reads vs
   writes).
3. **A real pipeline progress model** (stepper with per-stage status, cost indicators on paid steps,
   "what's next").
4. **Human-readable agent summaries** instead of YAML/grounding dumps (keep raw views behind an
   "advanced/inspect" affordance).
5. **A guided connect flow** explaining OAuth, environment, identity verification, and the production
   prerequisites inline, with actionable error states.
6. **Replace native confirm/alert + polling-text** with proper in-app states, progress, and real-time updates.

---

## Appendix A — Source files (for the designer's reference)

- UI: `trace/ui/static/index.html`, `trace/ui/static/app.js`, `trace/ui/static/style.css`
- Backend/API + orchestration: `trace/ui/app.py`; per-session artifacts: `trace/ui/sessions.py`
- Pipeline engines: `trace/capture/`, `trace/reconstruct/`, `trace/discovery/`, `trace/agent/`
  (`configgen.py`, `interview.py`, `connect.py`, `connect_client.py`, `deploy.py`, `api_client.py`,
  `config.py`)
- Platform config/constants: `trace/agent/config.py` (e.g. `PIPEDREAM_MCP_BASE =
  https://remote.mcp.pipedream.net`, `LIVE_PERMISSION_POLICY = always_ask`, model ids)
