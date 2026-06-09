# Trace Stage 3 — Review step + Artifact spec contracts

```
discovery.json --(review/edit in Test UI)--> reviewed_bundles.json --(artifact engine)--> spec_<id>.md
```

## Input A: `reviewed_bundles.json` (FROZEN CONTRACT — Review → Artifact)
Produced by the review step; the **only** workflow input the Artifact Engine reads
(never raw `discovery.json`). Carries both a `bundles` and an `anti_bundles` list
because reclassification moves items between them. Full shape in
`reviewed_bundles.schema.json`. Per item:

- `classification` — `bundle` | `anti_bundle` (the current call; overrides discovery).
- edited intent — `name`, `description`, `trigger`, `steps` (bundles).
- `disposition` (bundles) — `approve_for_build` | `reject` (only approved bundles are built).
- `human_checkpoints` — `[{step_index, note}]` (judgment embedded in an automatable flow).
- `included_example_event_ids` — examples NOT flagged unrepresentative (verbatim, never edited).
- `added_acceptance_criteria` — free-text criteria the user added.
- `preferred_output` — optional desired message/document template/tone.
- `evidence_event_ids` — original provenance.
- `reclassified_from` + `original_reason_for_judgment` — if reclassified anti→bundle,
  the judgment reason is carried into the spec as context.

## Input B: `event_log.json`
Read-only, only to fetch the **verbatim** text of cited examples. Examples are
copied field-for-field; never paraphrased.

## Input C: `environment_<id>.json` (Review 2 — FROZEN CONTRACT, one per approved bundle)
Gathered between Review 1 and the engine: the real work products the workflow reads
and writes (uploaded by the user — Trace never logs into live accounts) plus short
answers for facts no artifact contains. Full shape in `environment.schema.json`.

```json
{
  "bundle_id": "<id>",
  "artifacts": [
    { "role": "input | output | reference", "kind": "pdf | spreadsheet | doc | …",
      "derived_from_event_ids": ["<events that justified the request>"],
      "label_shown_to_user": "<the ask>", "uploaded": true,
      "file_ref": "<path in the dated session folder>",
      "extracted": { "<kind-specific structure: headers/rows/tabs/format or text/fields>": "…" } }
  ],
  "answers": [ { "question": "<derived/minimal>", "answer": "<user free text>" } ],
  "status": "complete | incomplete"
}
```

- **Requests are derived from the bundle's own event evidence** (any action that
  reads/writes an external artifact), grouped so repeated instances collapse to one
  real example — never a per-workflow-type list.
- **Extraction is keyed on artifact kind**, not workflow: spreadsheet → headers, a
  few rows, tab names, detected format; doc/pdf → text + field layout. Kinds are
  extensible. Extracted text is redacted at write; the raw file is retained, not
  inlined blindly.
- `file_ref` lives in the dated session folder (same redaction/retention posture).
- `status` lets the UI block or let the user **skip** — a skipped/incomplete record
  yields a spec flagged `environment-incomplete` rather than silently degraded.

## Output: `spec_<id>.md` (FROZEN TEMPLATE)
One markdown file per `approve_for_build` bundle, self-contained for pasting into
Claude Code / Cursor. Anti-bundles instead get a `spec_<id>_manual.md`
"handle manually — here's why" note (no build spec). Section structure:

```
# Build spec: <name>
## 1. Intent
  <plain-language authority for what the agent should do, from the edited intent +
   preferred output; reclassified items include the carried-over judgment context>
## 2. Observed examples
  <real captured instances, VERBATIM, each marked *representative* or *illustrative*;
   illustrative = contradicts the intent (divergence rule) and is context only>
## 3. Acceptance criteria
  - <model-derived, example-supported, concrete/checkable where possible>
  - <every user added_acceptance_criteria, verbatim, marked (user-specified)>
  - <one "pause and request human input" criterion per human checkpoint>
```

### Guarantees (deterministic, not left to the model)
- Observed examples are inserted **verbatim** from the event log — never invented or
  paraphrased — minus any the user excluded.
- **Every** user-added criterion and **every** checkpoint criterion appears exactly.
- **Divergence rule:** when the edited intent contradicts an example, intent +
  criteria win and the example is marked *illustrative of context, not a target*.
- **Environment folding (Review 2):** when `environment_<id>.json` is present, the
  uploaded artifacts **lead** the Observed Examples (real columns/fields/format/rows)
  and event-log instances become supplementary; Acceptance Criteria gain
  deterministic real-schema assertions (correct field parsing, output mapping, file
  format) plus each answered question. A spec that embeds real files carries a
  sensitivity header; a missing/incomplete record flags it `environment-incomplete`
  but still generates (never hard-blocks).

The model call (one per approved bundle; default `claude-sonnet-4-6`, injectable)
produces only the intent prose, the example-supported derived criteria, and the
per-example representative/illustrative assessment; `artifact.py` assembles the
markdown around those so the guarantees above always hold. `--dry-run` renders the
prompt + bundle + verbatim examples with no model call.

The Artifact Engine prompt is domain-neutral; the only domain content in a spec is
the user's own event log and edits.
