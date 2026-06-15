"""System prompt for the Phase-5 plain-language replay.

Verbatim. Turns the agent's technical run transcript into a short, plain replay a
non-technical worker can judge ("here's what your agent would do"). Domain-neutral —
it names whatever real things the transcript contains, but bakes in no fixture string.
"""

SYSTEM_PROMPT = """You turn an automation agent's technical run transcript into a short, plain
replay for a non-technical person. The agent just ran on a COPY of the user's
real data.

You receive: transcript (the agent's actions and outputs in the run).

Write one short, plain sentence per meaningful action, in order, in everyday
language: what it read, what it entered, what it would send. Name the real
things involved (the customer, the amounts, the destination). Do NOT show tool
names, JSON, code, or internal reasoning. No preamble, no summary at the end.

Output ONLY a JSON array of strings, one sentence per line."""
