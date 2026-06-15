"""Phase 3 — the Sandbox Grader (walled off from the engine, like the discovery Scorer).

Reads ONLY the agent's acceptance criteria and a rendered sandbox transcript, and
shares no prompt text or code with the config engine / gate. One model call grades
all criteria at once, returning a per-criterion verdict
(pass | fail | not_testable_here | inconclusive) plus an overall.

Walled off on purpose: this module imports only ``re``, ``json``, and (lazily)
``anthropic`` — never ``gate``, ``configgen``, ``deployer`` or ``compiler`` — so the
thing being graded can never reach into the grader.
"""

import json
import re

# The grader's OWN prompt — verbatim Phase 3 spec; not imported from anywhere the
# engine touches.
GRADER_SYSTEM = """You are the Trace Sandbox Grader. An automation agent has just run in a sealed
sandbox: it had NO real connections, could not touch any real data, and when
it 'sent' or 'wrote' something it only emitted text describing the action. You
judge whether it performed the workflow correctly, by its acceptance criteria.

You receive:
- acceptance_criteria: the agent's own checklist of must-hold conditions.
- transcript: the full record of what the agent did and emitted in the run.

Judge BEHAVIORALLY. Base every verdict on what the agent actually did and
emitted — the values it extracted, the actions it took, the text it produced.
Do NOT pass a criterion merely because the agent restated it: the criteria are
also written into the agent's own instructions, so repeating them proves
nothing.

This is a sandbox with no real environment. For any criterion that requires
the real environment to judge (e.g. whether output matched a real file's
columns/format/layout), mark it 'not_testable_here' rather than guessing.
If the transcript is incomplete or you cannot find evidence for a criterion
that should be testable, mark it 'inconclusive'.

Output ONLY this JSON:
{ "per_criterion": [
    {"criterion":"...","verdict":"pass|fail|not_testable_here|inconclusive",
     "reason":"one line citing the transcript"} ],
  "overall":"pass|fail",            // pass only if no criterion failed
  "notes":"brief" }"""

_VERDICTS = ("pass", "fail", "not_testable_here", "inconclusive")


def criteria_from_grounding(grounding, keys=("req_",)):
    """The agent's acceptance criteria — its must-hold conditions. By default the
    requirements (grounding instructions whose key starts with ``req_``); ``keys`` can
    widen this (e.g. add ``step_``) without touching the grader's contract."""
    out = []
    for inst in (grounding or {}).get("instructions", []) or []:
        key = str(inst.get("key", ""))
        text = str(inst.get("text", "") or "").strip()
        if text and any(key.startswith(k) for k in keys):
            out.append(text)
    return out


def _parse_grade(text):
    """Defensive parse of the grader's JSON (mirrors configgen._parse_json_object):
    strip code fences, then json.loads, then an outermost ``{...}`` slice. Returns a
    dict, or ``{}`` when nothing parses."""
    if not text:
        return {}
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        s, e = t.find("{"), t.rfind("}")
        if s == -1 or e <= s:
            return {}
        try:
            d = json.loads(t[s:e + 1])
            return d if isinstance(d, dict) else {}
        except json.JSONDecodeError:
            return {}


def _normalize_per_criterion(raw):
    out = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        v = str(it.get("verdict", "")).strip().lower()
        if v not in _VERDICTS:
            v = "inconclusive"                 # coerce anything unexpected to non-blocking
        out.append({"criterion": str(it.get("criterion", "") or "").strip(),
                    "verdict": v,
                    "reason": str(it.get("reason", "") or "").strip()[:400]})
    return out


def _default_judge(model="claude-sonnet-4-6"):
    import anthropic
    client = anthropic.Anthropic()

    def judge(acceptance_criteria, transcript):
        user = json.dumps({"acceptance_criteria": list(acceptance_criteria or []),
                           "transcript": transcript or ""}, ensure_ascii=False)
        resp = client.messages.create(model=model, max_tokens=1500, temperature=0,
                                      system=GRADER_SYSTEM,
                                      messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    return judge


def grade(acceptance_criteria, transcript, judge_fn=None):
    """Grade a sandbox run. ``judge_fn(acceptance_criteria, transcript) -> raw_text`` is
    injectable for offline tests. Returns ``{per_criterion, overall, notes}``.

    Safety nets (never trust the model blindly): ``overall`` is recomputed to ``fail``
    if ANY criterion failed; a wholly unparseable grader response is treated as
    ``overall='fail'`` (a broken grader must never silently advance an agent).
    ``not_testable_here`` and ``inconclusive`` are NON-blocking."""
    if judge_fn is None:
        judge_fn = _default_judge()
    parsed = _parse_grade(judge_fn(acceptance_criteria, transcript))

    if "per_criterion" not in parsed:
        return {"per_criterion": [], "overall": "fail",
                "notes": "grader output unparseable"}

    per_criterion = _normalize_per_criterion(parsed.get("per_criterion"))
    any_fail = any(r["verdict"] == "fail" for r in per_criterion)
    overall = "fail" if any_fail else "pass"        # deterministic; ignore model's overall
    return {"per_criterion": per_criterion, "overall": overall,
            "notes": str(parsed.get("notes", "") or "").strip()[:400]}
