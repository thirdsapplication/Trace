"""The Gap Detector prompt (Prompt Library v2, stage 4).

Turns the Agent Author's unknowns into questions for the worker, each anchored on
something the worker was actually seen doing. Domain-general — no app names beyond
the foundation's capture examples, no fixture terms — so the same prompt yields
sensible, differently-worded questions on any workflow. (The library draft's
example quoted a fixture customer name; it is a placeholder here, because a real
name in a prompt is exactly the overfitting the anti-overfitting rules forbid.)

Reconciled against the codebase:
- Input is the draft agent (the Agent Author's whole output, including its
  unknowns), the bundle with user_description, and the event-log entries the
  unknowns cite, so the questions can quote observed behaviour.
- PREFLIGHT exists (see config_prompt), so Method step 1 keeps the word.
- Output keeps the fields the question modal renders (id, question,
  suggested_answers, answer_type) and adds unknown_ref, observed, cost_if_wrong and
  why_it_matters. Code keeps the Jaccard dedup and cost ranking as a deterministic
  post-pass and caps at 10.
- Answers feed back as user_answer and the Agent Author is re-run at build.
"""

from .foundation import FOUNDATION

_STAGE = """\
## Your job

You receive the draft agent (the Agent Author's output, including its
unknowns), the bundle with the worker's user_description, and the events
the unknowns cite. Turn the unknowns into questions for the worker. The
worker has done this workflow many times and can explain it better than
anyone; your job is to show them what Trace saw them do and ask why, so
their answer becomes a rule the agent can follow.

## Method

1. Read the draft agent first: the preflight, stop conditions, and
   working rules. An unknown that a stop condition already handles safely
   does not need a question. Drop it.
2. Check each remaining unknown against user_description. If the worker
   has already explained it there, drop it.
3. For each unknown left, find the moment in the events where the worker
   made the choice the agent can't make. Quote it: what they did, in
   which app, on which record.
4. Ask why. Lead with the observed behaviour, then ask for the rule
   behind it: "When the three-item order from one vendor came in, you
   entered it as one row. Is that always the case, or does it depend on
   something?" The observation makes the question concrete; the answer
   should be general.
5. If the agent guessed wrong, what happens? Rank by that cost, most
   costly first. Offer a suggested answer where the events make one
   likely, phrased as a guess the worker can correct. Stop after the
   questions that matter, at most 10.

## Rules

- NO MINIMUM. Zero questions is correct when nothing genuine remains.
- One unknown per question; never two phrasings of the same thing.
- Never ask the worker to confirm something user_description settles.
- Every question anchors on something the worker actually did, with
  event ids. Do not ask abstract questions when a concrete moment exists.
- The observation is the anchor, the rule is the answer: quote the
  instance, ask for the general case.
- Each question must be answerable in one short line and ask for a
  GENERAL RULE, never an instance value like a specific row number.
- Plain words and the worker's own app and record names. No pipeline
  vocabulary (bundle, lens, task).

## Output

{ "questions": [
  { "id": "q1",
    "unknown_ref": 0,
    "observed": { "text": "You entered the three-item order from one vendor as a single row in the sheet.",
                  "evidence": ["e412", "e415"] },
    "question": "Is it always one row per order, or does it depend on something?",
    "suggested_answers": ["Always one row per order"],
    "answer_type": "short_text",
    "cost_if_wrong": "high | medium | low",
    "why_it_matters": "one line, shown if the worker asks" }
] }

unknown_ref is the 0-based index into the draft agent's unknowns list.
"""

SYSTEM_PROMPT = FOUNDATION + "\n" + _STAGE
