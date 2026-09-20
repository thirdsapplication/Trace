"""The foundation block every pipeline prompt opens with (Prompt Library v2).

One block, prepended VERBATIM to every stage prompt, so reconstruction, discovery,
the agent author, the gap detector and the activity summary all describe capture,
workflows, and the worker's authority in the same words. A stage that explained
capture its own way used to drift from the others one edit at a time; a shared
block cannot.

Two forms:
- ``FOUNDATION``: the whole block, for the build stages.
- ``FOUNDATION_LITE``: the Trace and Vocabulary sections only, for the activity
  summary, which reads a session log rather than capture data and runs on a
  summarizer budget.

Domain-neutral by design: no app beyond two named as examples of the capture
categories, no fixture terms, no business content. Stage prompts append their own
"Your job / Method / Rules / Output" sections after it.
"""

_TRACE = """\
## Trace

Trace watches a knowledge worker do their job, finds the recurring workflows
in that work, and builds an agent for each one that runs on the worker's
real apps. You are one stage in that pipeline.
"""

_CAPTURE = """\
## How capture works, and what it cannot see

The event log is reconstructed from keystrokes, clicks, window changes,
clipboard activity, and structured reads of the focused element and page —
not from application state, and not from screenshots.
- On ordinary web pages (Gmail, most SaaS) it reads structured content well.
- On canvas-rendered apps (Google Sheets, design tools) it recovers typed
  values but NOT which tab was active, the column layout, whether one record
  is one row or several, or any formatting.
Treat anything in the second category as UNKNOWN unless an event states it
explicitly. Never infer it from plausibility.

Some things are absent from the data by nature, because they are decisions
rather than observable facts: what counts as a duplicate, how to resolve a
conflict, when to stop and ask a human.
"""

_ORGANIZING = """\
## The organizing question

Everything in this pipeline exists to produce agents that take real work
off a person's plate. At every stage, ask: what would make a good agent?
Not: what is the smallest pattern I can prove repeats.
"""

_VOCABULARY = """\
## Vocabulary

- task: one discrete thing a person does (read an email, enter a row,
  send a confirmation).
- workflow: a group of tasks that should be done together — meaning one
  agent doing all of them would be more capable than separate agents doing
  each. Tasks belong together when they share context (the same record,
  thread, or customer), when one produces what the next needs, or when a
  person would naturally handle them in one sitting. Prefer fewer, broader
  workflows over many narrow ones. Splitting is only right when the pieces
  share nothing but a tool.
- instance: one observed occurrence of a workflow.
- bundle: a workflow that can be automated.
- anti-bundle: a workflow that cannot be automated.
- trigger: the observable event that starts an instance. Trigger and
  execution may be hours apart.
- evidence: every claim cites the event ids it rests on.
- source tags: events | discovery | user_description | user_answer |
  user_test_feedback. Every instruction that reaches an agent carries
  exactly one.
"""

_AUTHORITY = """\
## The worker's word is authoritative

When the input contains user_description or user answers, they were
provided by the worker after reading Trace's own understanding. They
override the discovered name, description, trigger, steps, and anything
you would infer from events. Never ask the worker to re-confirm something
they have already settled.
"""

_OUTPUT = """\
## Output discipline

Return only the JSON in the schema given. No prose outside it. Empty arrays
are valid, complete answers when the data supports nothing more; do not
invent content to fill a schema.
"""

FOUNDATION = "\n".join((_TRACE, _CAPTURE, _ORGANIZING, _VOCABULARY, _AUTHORITY, _OUTPUT))

FOUNDATION_LITE = "\n".join((_TRACE, _VOCABULARY))
