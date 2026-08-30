"""The score: mean total tokens per valid session. Lower is better.

Nothing is normalised and nothing is divided. An earlier version measured
``tokens / difficulty``, where difficulty was a scorer model's item count plus a share of
the input size, so that a heavy group and a light one could be compared. Seven real
sessions spanning one to seven work items settled it:

    items   1        1        2        3        4        5        7
    tokens  531,972  595,839  521,524  523,007  455,709  604,397  534,060

Total tokens barely move with the size of the job. The cost is almost entirely fixed
per-session overhead, so dividing by difficulty did not normalise the workload out, it
divided it back IN: the same seven sessions vary 9.3% by tokens and 31% once normalised,
and two groups of the same agent with nothing changed between them scored 3.6x apart.

The bar a metric needs is set by its own noise, so that difference decides which loop can
learn at all: about 10% on raw tokens at seven sessions a group, against about 34%
normalised. Almost no real change clears 34%.

Raw tokens also needs no guard clauses, because it already contains everything the guards
protected. A larger prefix raises it. More turns raise it. More narration raises it. There
is no way to make the agent more expensive that the score cannot see.

What it does NOT see is work not done, which is why the validity gate (gate.py) and the
prompt's hard rules carry that weight instead.
"""

from . import config


def mean_tokens(session_records):
    """Mean total tokens over the records given, or None when there are none.

    Callers pass the VALID sessions only — gate.py has already removed rejected, failed,
    stalled and unmetered ones. None means "this group scored nothing", which is a state
    the loop must be able to be in: better no verdict than one against a number nobody
    produced.
    """
    totals = []
    for rec in (session_records or []):
        if not isinstance(rec, dict):
            continue
        t = ((rec.get("tokens") or {}).get("total_tokens"))
        try:
            totals.append(float(t))
        except (TypeError, ValueError):
            continue
    if not totals:
        return None
    return round(sum(totals) / len(totals), 1)


def input_kb(events):
    """Kilobytes of EXTERNAL material a session had to read, from the raw events.

    No longer part of the score. It survives because the session profile reports it: an
    agent reading 400KB and one reading 2KB have different problems, and the compacted
    transcript the engine sees truncates every tool result to 200 characters, so the
    engine cannot tell them apart without being told.

    Counts tool RESULTS and the trigger text: material that arrived. Excludes the agent's
    own messages and its tool-call arguments, which are things it chose to write.
    """
    total = 0
    for e in (events or []):
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "").lower()
        if "tool_result" in t or t.endswith("user.message"):
            total += len(str(e.get("text") or ""))
    return round(total / 1024.0, 1)
