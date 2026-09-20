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
- `user_description` + `understanding` — the review step's approve/correct outcome.
  `understanding` is `confirmed` | `corrected` | `""` (unresolved). When `corrected`,
  `user_description` holds the worker's own VERBATIM description of the workflow and
  is AUTHORITATIVE over the discovered name/description/trigger/steps downstream.

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
      "label_shown_to_user": "<the ask>",
      "user_instruction": "<AUTHORITATIVE free text: what this file is / how to use it; may name a tab or section>",
      "uploaded": true,
      "file_ref": "<path in the dated session folder — ALWAYS present>",
      "extraction_status": "ok | partial | failed",
      "extracted": "<best-effort, may be null/empty; NEVER authoritative>" }
  ],
  "answers": [ { "question": "<derived/minimal>", "answer": "<user free text>" } ],
  "status": "complete | incomplete"
}
```

Review 2 is a **courier + annotator**, not the expert on the user's files:

- **The user instruction is authoritative.** Each artifact carries a free-text
  `user_instruction` (what it is, how to use it, which tab/section applies). Nothing
  auto-derived may override it.
- **The raw file always travels** (`file_ref` always present). The downstream coding
  agent reads it live; the pipeline never depends on Trace having parsed it.
- **Auto-extraction is best-effort and non-load-bearing.** It still runs (keyed on
  artifact *kind*, not workflow) as a sanity aid, but its **failure never blocks**
  (`extraction_status: failed` is normal), it **never overrides an instruction**, and
  it is **never the sole basis for an assertion** — it appears in the spec only as a
  clearly-labeled, unverified aid.
- **Requests are derived from the bundle's own event evidence** (any action that
  reads/writes an external artifact), grouped so repeated instances collapse to one
  real example — never a per-workflow-type list.
- `file_ref` lives in the dated session folder (same redaction/retention posture).
- **`status: complete` keys off uploads + instructions**, independent of extraction.
  `environment-incomplete` is reserved for a missing required upload or instruction.

## Output: the spec bundle — `spec_<id>.md` + `spec_<id>_files/` (FROZEN TEMPLATE)
The deliverable is a **self-contained bundle**: the markdown spec **plus** a sibling
`spec_<id>_files/` directory holding a byte-identical copy of **every** artifact
attached to the bundle in Review 2 (P1). Every artifact reference in the spec is
**spec-relative** (`./spec_<id>_files/<file>`) — no path points into Trace's session
storage (P2) — so the spec and its `_files/` directory remain valid when moved
together to any machine. Filename collisions are disambiguated deterministically
(P4). Anti-bundles instead get a `spec_<id>_manual.md` note (no build spec).

Every spec opens with a **mandatory preflight** (P3), generated generically from the
bundle's artifacts and their authoritative instructions, instructing the coding
agent — before writing any code — to open every attached file, echo back the
structure relevant to each instruction, confirm its understanding, and **STOP and
report (not infer)** if any file can't be opened or conflicts with its instruction.
A bundle with zero attached artifacts still exports a valid spec (the preflight says
none are attached). Section structure:

```
# Build spec: <name>
## 0. Preflight — read the attached files first (lists every ./spec_<id>_files/… +
   its authoritative instruction; stop-on-failure directive)
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
- **Environment folding (Review 2, courier model):** when `environment_<id>.json` is
  present, each uploaded artifact's **raw file reference + authoritative instruction**
  lead the Observed Examples; auto-extraction appears only as an "auto-read,
  unverified" aid; event-log instances are supplementary. Acceptance Criteria target
  the **user-designated artifact/location** and are checked **live** against the real
  file (`"…use the «file» exactly as instructed: '<instruction>', reading it live…"`)
  — they never assert against an auto-extracted schema/location. If extraction
  contradicts the instruction, the extracted detail is dropped from criteria. A spec
  embedding real files carries a sensitivity header; a record missing a required
  upload or instruction flags it `environment-incomplete` but still generates.

The model call (one per approved bundle; default `claude-fable-5-1`, injectable)
produces only the intent prose, the example-supported derived criteria, and the
per-example representative/illustrative assessment; `artifact.py` assembles the
markdown around those so the guarantees above always hold. `--dry-run` renders the
prompt + bundle + verbatim examples with no model call.

The Artifact Engine prompt is domain-neutral; the only domain content in a spec is
the user's own event log and edits.
