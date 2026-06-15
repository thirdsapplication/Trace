"""The Structured Interview gap detector (Phase 2).

Per approved bundle, ONE Sonnet call finds what the captured data could not reveal —
capture blind spots (Lens A) and undetermined decisions (Lens B) — and proposes a
few short, hard-triaged questions for the worker. The worker's one-line answers
become the AUTHORITATIVE ``user_answers`` input the Phase-1 config engine consumes.

The model writes the questions; deterministic code normalizes them (coerces enums,
drops anything the agent could discover live, ranks by cost-if-wrong, caps at 5).
``model_fn(system, user) -> str`` is injectable so the whole thing runs offline.
"""

from . import config
from .configgen import _default_model_fn, _parse_json_object, make_engine_inputs
from .interview_prompt import SYSTEM_PROMPT

DEFAULT_MODEL = config.DEFAULT_MODEL

_LENSES = ("capture_blind_spot", "undetermined_decision")
_COSTS = ("high", "medium", "low")
_COST_RANK = {"high": 0, "medium": 1, "low": 2}
_MAX_QUESTIONS = 5


def build_prompt_material(workflow_description, observed_events):
    """The gap detector's USER message: the workflow + observed events as JSON (also
    what the CLI ``--dry-run`` prints). No ``user_answers`` — they don't exist yet."""
    import json
    return json.dumps({"workflow_description": workflow_description,
                       "observed_events": observed_events},
                      ensure_ascii=False, indent=2)


def _truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"))


def _normalize_questions(raw):
    """Code-side safety net over the model's question list: keep well-formed items,
    coerce the enums, DROP anything the agent could resolve live at run time, rank by
    cost-if-wrong, cap at 5, and re-id q1..qN in final order."""
    kept = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        q = str(it.get("question", "") or "").strip()
        if not q:
            continue
        if _truthy(it.get("runtime_discoverable")):
            continue                                  # belt-and-suspenders vs the triage gate
        lens = it.get("lens") if it.get("lens") in _LENSES else "undetermined_decision"
        cost = it.get("cost_if_wrong") if it.get("cost_if_wrong") in _COSTS else "medium"
        suggested = [str(s).strip() for s in (it.get("suggested_answers") or [])
                     if str(s).strip()][:4]
        kept.append({
            "unknown": str(it.get("unknown", "") or "").strip(),
            "lens": lens,
            "why_it_matters": str(it.get("why_it_matters", "") or "").strip(),
            "question": q,
            "suggested_answers": suggested,
            "answer_type": "short_text",
            "cost_if_wrong": cost,
            "runtime_discoverable": False,
        })
    kept.sort(key=lambda x: _COST_RANK[x["cost_if_wrong"]])   # stable: high -> medium -> low
    kept = kept[:_MAX_QUESTIONS]
    for i, it in enumerate(kept):
        it["id"] = f"q{i + 1}"                        # ids reflect final display order
    return kept


def generate_questions(bundle, event_log, model=DEFAULT_MODEL, model_fn=None):
    """Detect gaps for one bundle. Returns ``{"questions": [...]}`` (possibly empty)."""
    workflow_description, observed_events, _ = make_engine_inputs(bundle, event_log)
    if model_fn is None:
        model_fn = _default_model_fn(model)
    user_msg = build_prompt_material(workflow_description, observed_events)
    model_out = _parse_json_object(model_fn(SYSTEM_PROMPT, user_msg))
    return {"questions": _normalize_questions(model_out.get("questions"))}
