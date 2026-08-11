# Config Engine contracts (Phase 1)

The Config Engine turns one approved workflow bundle into a Claude Managed Agents
configuration **directly** — no intermediate build-spec markdown. A single model
call writes the instruction *content*; deterministic code stamps all structure and
builds a grounding record.

## Inputs (per approved bundle)
- **workflow_description** `{name, description, trigger, steps[], evidence_event_ids[]}`
  — from the reviewed bundle. Carries `user_description` (the worker's verbatim
  review-step correction of what the workflow really is — **AUTHORITATIVE** over the
  other fields and over event inference) only when the worker corrected it.
- **observed_events** `[{id, description}]` — one per `evidence_event_id`; `description`
  is the event log's plain `detail`.
- **user_answers** `[{q, question, answer}]` — the worker's one-line answers about
  things the data could not reveal. **May be empty. AUTHORITATIVE** over any inference
  from the events. (Filled by the Phase 2 interview; Phase 1 only consumes it.)

## Model output (content only) — `config.schema.json`
```json
{ "description": "...", "role": "...",
  "tools_and_artifacts": ["..."],
  "steps":         [{"text": "...", "source": {...}}],
  "working_rules": [{"text": "...", "source": {...}}],
  "requirements":  [{"text": "...", "source": {...}}] }
```
Each `source` is `{type:"events", ids:[...]}` | `{type:"discovery"}` |
`{type:"user_answer", q:"..."}` | `{type:"user_description"}` |
`{type:"user_test_feedback", note:"..."}`. The model never writes structural fields.

## Outputs (per approved bundle)
- **agent_&lt;id&gt;.test.yaml** — sandbox variant: `mcp_servers: []`, the agent toolset only.
- **agent_&lt;id&gt;.live.yaml** — live variant: one Pipedream MCP server + an approval-required
  `mcp_toolset`.
- **grounding_&lt;id&gt;.json** — `{bundle_id, instructions:[{key, text, source, observed}]}`,
  one entry per step/working-rule/requirement. `observed = (source.type == "events")`.
  Not part of any prompt; used for audit and by the grader.

## Assembled `system` order (deterministic)
role → `You work with:` (bulleted apps/artifacts) → `Steps:` (numbered) →
`How to do the work:` (working rules; the canonical "match existing examples",
anti-duplication, and anti-prompt-injection rules are each appended if the model
omits them) → `Requirements:`.

## Stamped structural constants (code, never the model)
`model: claude-sonnet-4-6`; test toolset `agent_toolset_20260401`; live adds the
Pipedream `mcp_toolset` with `permission_policy.type` = `always_ask`; `skills: []`;
`metadata: {trace_bundle_id, variant}`. The Pipedream URL and the permission enum are
flagged "confirm against current docs" in `config.py`.
