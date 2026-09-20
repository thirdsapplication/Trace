"""The Discovery prompt (Prompt Library v2, stage 2): event_log.json -> discovery.json.

Groups tasks into workflows using the foundation's broad definition and classifies
each as bundle or anti-bundle. It is and must remain free of business-, industry-,
or fixture-specific content: no business names, no industry vocabulary, no example
drawn from any validation fixture, no answer-key-derived counts or types.

Reconciled against the codebase:
- The output keeps every field of the frozen discovery.json contract that the
  Review UI, the reviewed-bundles seed and the Scorer already read (``id, name,
  description, trigger, steps[], frequency, evidence_event_ids, why_automatable,
  rationale, confidence`` on bundles; ``why_judgment, recommended_handling`` on
  anti-bundles). Steps stay a list of strings, with a branch condition written
  into the step it governs. The library's new fields — ``apps``,
  ``grouping_rationale``, ``unknowns`` and the anti-bundle ``reason`` — were added
  to the validator, which carries them through when present.
- The anti-bundle ``reason.why`` also lands in ``why_judgment``, which is the
  field the Review UI renders as the classification rationale.
- The event log carries an optional ``target`` field (``"unknown"`` when capture
  could not see where a typed value went) and a confidence per event; Method
  step 5 reads both.
- The connectable-app list is NOT hardcoded here: ``system_prompt()`` injects it
  at call time from the same table the connection layer uses
  (``config.CONNECT_APP_TABLE``), so the two never drift.
"""

from trace.agent.foundation import FOUNDATION

APPS_PLACEHOLDER = "{{CONNECTABLE_APPS}}"

_STAGE = """\
## Your job

You receive event_log: one worker's session as an ordered list of tasks.
Find the workflows in it, decide which are bundles and which are
anti-bundles, and return each with its evidence. This is where the
organizing question matters most: you are not looking for patterns, you
are proposing agents. Each bundle you return should be a job a person
would be glad to hand off whole.

## Apps an agent can connect to

{{CONNECTABLE_APPS}}

An agent can only act in these apps. A workflow whose tasks require any
other app is an anti-bundle, regardless of how mechanical it is.

## Method

1. Read the whole log before grouping anything. Note the apps, the record
   types (orders, invoices, tickets), and the people involved.
2. Find recurring outcomes — a row entered, a confirmation sent, a file
   filed — and trace each back to its trigger. Triggers and execution may
   be hours apart and interleaved with other work.
3. Group tasks into workflows using the vocabulary definition. Test each
   candidate boundary both ways:
   - Too narrow? If two candidates share a record, a thread, or a
     customer, or one feeds the other, merge them.
   - Too broad? If two candidates share nothing but a tool, split them.
   A conditional branch inside a workflow is one workflow with a branch,
   never two workflows.
4. For each workflow, decide bundle or anti-bundle. First check apps: if
   any task happens in an app outside the connectable list, the workflow
   is an anti-bundle — name the app. Then walk the tasks and ask of each:
   could an agent do this with the data it would have at run time? If a
   task needs a judgment the data cannot supply — weighing a customer
   relationship, deciding an exception, reading tone — the workflow is an
   anti-bundle. Name that task. If the only obstacle is a fact the agent
   could ask for or read live, it is a bundle.
5. Carry forward what capture could not see. Where a workflow's tasks have
   target "unknown" or low confidence, list those as unknowns on the
   workflow so later stages ask rather than assume.
6. Anything that occurred once, or is clearly ad hoc, is not a workflow.
   Leave it out; do not force it into a group.

## Rules

- Every workflow cites the event ids of at least two instances. One
  occurrence is not recurrence.
- Never split a workflow to make its parts easier to automate. If the
  whole is an anti-bundle, say so.
- An unsupported app makes the whole workflow an anti-bundle. Do not split
  the workflow to carve out the supported part.
- Never merge two workflows because they use the same app.
- Name workflows by outcome ("Enter vendor POs into ERP"), not by tool
  ("Sheets work").
- Steps are what the worker does, in order, in plain language, with the
  branch condition stated inside the step where one exists ("If the
  order is marked rush, ...").
- The reason for an anti-bundle names the specific task or app, not the
  workflow.
- The log is machine-reconstructed and imperfect. Duplicate or overlapping
  events that describe one action count as ONE instance. Conflicting
  values for what is plausibly one thing are an unknown to flag, never a
  choice to make silently. Down-weight low-confidence events; do not
  anchor a workflow on them. Never invent content the log does not show.
- Evidence is exhaustive: every event that belongs to a workflow is cited,
  and no event that does not.

## Output

{ "bundles": [
  { "id": "B1",
    "name": "outcome-named",
    "description": "two or three sentences: trigger, what happens, outcome",
    "trigger": "the observable start of an instance",
    "steps": ["step 1", "step 2, with its branch condition stated inside it"],
    "frequency": "how often it occurred during the day",
    "apps": ["Gmail", "Google Sheets"],
    "evidence_event_ids": ["e410", "e419", "..."],
    "why_automatable": "why the steps reduce to a rule the agent can follow; where a human checkpoint remains",
    "rationale": "one plain-language sentence, no jargon, for the worker: why this is automatable",
    "grouping_rationale": "one line: why these tasks belong to one agent",
    "unknowns": [ { "what": "destination tab in the destination sheet",
                    "evidence": ["e412"] } ],
    "confidence": "high | medium | low" }
],
  "anti_bundles": [
  { "id": "A1",
    "name": "outcome-named",
    "description": "what the work is on the surface",
    "apps": ["..."],
    "evidence_event_ids": ["..."],
    "reason": { "type": "unsupported_app | judgment",
                "app": "the unsupported app, or null",
                "step": "the 1-based step number that needs judgment, or null",
                "why": "one line naming the specific task or app" },
    "why_judgment": "the same one line, for the review screen",
    "recommended_handling": "what a human should do / where the checkpoint sits",
    "rationale": "one plain-language sentence, no jargon, for the worker: why this needs a person",
    "confidence": "high | medium | low" }
],
  "unclustered_or_noise_event_ids": ["..."],
  "notes": "anything ambiguous, conflicts you flagged, and any judgment calls you made while grouping" }

Example (from an ops coordinator's day):
- A vendor emails a PO → the coordinator reads it, enters one row per PO in
  the ERP sheet, replies to confirm. Three instances, hours apart,
  interleaved with other work. → one bundle, three steps, unknowns:
  rows-per-order.
- A customer emails asking for a discount on a late order → the coordinator
  checks history, decides, replies. → anti-bundle; reason.type = judgment,
  reason.step = the decide step.
"""

# The template: the foundation block, then this stage's sections, with the
# connectable-app list still to be filled in.
SYSTEM_PROMPT = FOUNDATION + "\n" + _STAGE


def connectable_app_names():
    """The apps an agent can act in, as human names — the ``label`` column of the
    connection layer's own table, deduplicated and sorted, so the prompt and the
    connect step can never disagree about what is connectable."""
    from trace.agent import config
    labels = {v["label"] for v in config.CONNECT_APP_TABLE.values()}
    return sorted(labels)


def system_prompt(apps=None):
    """The Discovery system prompt with the connectable-app list filled in, one app
    per line. ``apps`` defaults to ``connectable_app_names()``; tests pass their own."""
    names = list(apps) if apps is not None else connectable_app_names()
    block = "\n".join(f"- {a}" for a in names) if names else "- (none configured)"
    return SYSTEM_PROMPT.replace(APPS_PLACEHOLDER, block)
