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

The model call (one per approved bundle; default `claude-sonnet-4-6`, injectable)
produces only the intent prose, the example-supported derived criteria, and the
per-example representative/illustrative assessment; `artifact.py` assembles the
markdown around those so the guarantees above always hold. `--dry-run` renders the
prompt + bundle + verbatim examples with no model call.

The Artifact Engine prompt is domain-neutral; the only domain content in a spec is
the user's own event log and edits.
