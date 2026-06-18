# Trace Stage 2 — Discovery schema (the Discovery → Artifact C contract)

The Discovery Engine consumes the frozen `event_log.json` and emits `discovery.json`.

```
event_log.json   (Stage 1 output, read-only input here)
discovery.json   (CONTRACT: Discovery -> Artifact C)
```

## Input: `event_log.json`
The frozen event-log contract (see `trace/schema/SCHEMA.md`). Each event:
`id, ts_start, ts_end, app, window, action, detail, evidence, confidence`. The
engine renders it as a compact chronological list (id, time, app/window, action,
confidence, detail) and reasons over the **whole day in one call** — no chunking —
so a workflow whose trigger and execution are hours apart, linked only by an
artifact, stays visible. The engine consumes the log as-is; it does not repair it.

## Output: `discovery.json` (FROZEN CONTRACT)

```json
{
  "bundles": [
    {
      "id": "B1",
      "name": "<short imperative name>",
      "description": "<what the workflow does>",
      "trigger": "<what kicks it off>",
      "steps": ["<ordered steps>"],
      "frequency": "<how often it recurred in this log>",
      "evidence_event_ids": ["e..."],
      "why_automatable": "<why software could do it; note human-checkpoint exceptions>",
      "rationale": "<one plain-language sentence for the end user: why this is automatable>",
      "confidence": "high|medium|low"
    }
  ],
  "anti_bundles": [
    {
      "id": "A1",
      "name": "<short name>",
      "description": "<the surface workflow>",
      "why_judgment": "<why the core step needs a human every time>",
      "recommended_handling": "<what a human should do / where the checkpoint sits>",
      "rationale": "<one plain-language sentence for the end user: why this needs a person>",
      "evidence_event_ids": ["e..."],
      "confidence": "high|medium|low"
    }
  ],
  "unclustered_or_noise_event_ids": ["e..."],
  "notes": "<brief reasoning, ambiguities, conflicts flagged>"
}
```

### Rules baked into the contract
- **Bundles are automatable**; **anti-bundles require per-instance human judgment**
  and are **never** proposed for full automation — they are surfaced as a
  human-checkpoint. Getting this wrong is worse than missing a bundle.
- **Every candidate cites `evidence_event_ids`** — required for grading and for the
  downstream Artifact C stage.
- A single workflow with a **conditional branch** stays one bundle (not split).
- A workflow spanning **several apps** stays one bundle (not fragmented); two
  distinct workflows sharing a tool stay separate (not merged).
- Candidate ids (`B1`, `A1`, …) are engine-assigned for internal reference and
  carry **no** relationship to any answer key.
- Imperfect-log handling: duplicate/overlapping events collapse to one instance;
  conflicting values are flagged as exceptions, not resolved by guessing; missing
  content is never invented; low-confidence events are down-weighted.

## The Scorer (separate harness)
`scorer.py` grades a `discovery.json` against an answer key by **evidence-overlap +
an intent-judge call** — never by recognizing specific bundle ids/names. It is the
**only** code that reads an answer key; the engine cannot reach it. Rubric
thresholds (overlap threshold, pass bar) are supplied **alongside the answer key**,
not hardcoded in the scorer.
