"""The Gap Detector (Prompt Library v2, stage 4): the draft agent's unknowns -> questions.

Per approved bundle, ONE call turns the unknowns the Agent Author could not settle
into questions for the worker, each anchored on a moment the worker was actually seen
making the choice the agent cannot make. The worker's one-line answers become the
AUTHORITATIVE ``user_answers`` the Agent Author folds in at build.

The detector reads the DRAFT AGENT — the Agent Author's whole content record,
unknowns included — so an unknown a stop condition already handles safely, or that
the worker's own description already settles, is dropped rather than asked. When no
draft is supplied the detector runs the author itself first.

Questions are generated AS NEEDED: a workflow with no real unknowns yields none, and
that is a complete interview, not a failure. Nothing pads the list toward a minimum.

The model writes the questions; deterministic code normalizes them (coerces enums,
drops anything the agent could discover live, drops near-duplicates, ranks by
cost-if-wrong, caps at 10). ``model_fn(system, user) -> str`` is injectable so the
whole thing runs offline.
"""

import json
import re

from . import config
from .configgen import (_default_model_fn, _parse_json_object, make_engine_inputs,
                        draft_agent)
from .interview_prompt import SYSTEM_PROMPT

DEFAULT_MODEL = config.DEFAULT_MODEL

_LENSES = ("capture_blind_spot", "undetermined_decision")
_COSTS = ("high", "medium", "low")
_COST_RANK = {"high": 0, "medium": 1, "low": 2}
_MAX_QUESTIONS = 10

# Content words to ignore when comparing two questions for near-duplication, so the
# overlap test reflects the substance of the question, not its scaffolding.
_QUESTION_STOPWORDS = frozenset((
    "the", "a", "an", "to", "of", "in", "on", "is", "it", "do", "does", "did", "for",
    "and", "or", "be", "should", "would", "will", "what", "which", "when", "how", "who",
    "where", "that", "this", "its", "into", "are", "you", "your", "i", "my", "we", "us",
    "new", "one", "each", "any", "from", "as", "with", "at", "by", "if", "then", "else",
))
_DUP_THRESHOLD = 0.7        # token-set Jaccard at/above which two questions are duplicates


def _draft_for_prompt(draft):
    """The draft agent as the model sees it: the content sections and the unknowns,
    without the platform envelope. ``None`` becomes an empty draft."""
    d = draft or {}
    return {k: d.get(k, [] if k not in ("name", "description", "role") else "")
            for k in ("name", "description", "role", "apps", "preflight", "steps",
                      "working_rules", "acceptance_criteria", "stop_conditions", "unknowns")}


def build_prompt_material(workflow_description, observed_events, draft=None):
    """The gap detector's USER message: the draft agent (unknowns included), the
    workflow with the worker's user_description, and the observed events the unknowns
    can be anchored on, as one JSON object (also what the CLI ``--dry-run`` prints). No
    ``user_answers`` — they don't exist yet."""
    return json.dumps({"draft_agent": _draft_for_prompt(draft),
                       "workflow_description": workflow_description,
                       "observed_events": observed_events},
                      ensure_ascii=False, indent=2)


def _truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"))


def _question_tokens(text):
    """Content tokens of a question, lower-cased, scaffolding words removed — the basis
    for the near-duplicate test."""
    return frozenset(t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
                     if t not in _QUESTION_STOPWORDS)


def _dedup_questions(items):
    """Drop near-duplicates (token-set Jaccard >= _DUP_THRESHOLD) so a higher cap can
    never pad the set with trivially reworded questions. Input is already cost-sorted, so
    the first occurrence — the higher-cost one — is the keeper."""
    out, kept_tokens = [], []
    for it in items:
        toks = _question_tokens(it["question"])
        dup = any(prev and toks and len(toks & prev) / len(toks | prev) >= _DUP_THRESHOLD
                  for prev in kept_tokens)
        if not dup:
            out.append(it)
            kept_tokens.append(toks)
    return out


def _unknown_ref(v, n_unknowns):
    """A 0-based index into the draft's unknowns, or None."""
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < n_unknowns else None


def _observed(raw):
    """The quoted moment a question anchors on: ``{text, evidence}``."""
    if isinstance(raw, dict):
        text = str(raw.get("text") or "").strip()
        ev = [str(e).strip() for e in (raw.get("evidence") or []) if str(e).strip()]
    else:
        text, ev = str(raw or "").strip(), []
    return {"text": text, "evidence": ev}


def _normalize_questions(raw, unknowns=None):
    """Code-side safety net over the model's question list: keep well-formed items,
    coerce the enums, link each to the draft unknown it came from, DROP anything the
    agent could resolve live at run time, rank by cost-if-wrong, DROP near-duplicates,
    cap at 10, and re-id q1..qN in final order.

    Every question keeps the fields both UIs render (``id, question,
    suggested_answers, answer_type, why_it_matters, lens, cost_if_wrong``) and gains
    ``unknown_ref`` + ``observed`` so the modal can show the worker what they did."""
    unknowns = list(unknowns or [])
    kept = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        q = str(it.get("question", "") or "").strip()
        if not q:
            continue
        if _truthy(it.get("runtime_discoverable")):
            continue                                  # belt-and-suspenders vs the triage gate
        ref = _unknown_ref(it.get("unknown_ref"), len(unknowns))
        src = unknowns[ref] if ref is not None else {}
        lens = it.get("lens") if it.get("lens") in _LENSES else (
            src.get("lens") if src.get("lens") in _LENSES else "undetermined_decision")
        cost = it.get("cost_if_wrong") if it.get("cost_if_wrong") in _COSTS else "medium"
        suggested = [str(s).strip() for s in (it.get("suggested_answers") or [])
                     if str(s).strip()][:4]
        kept.append({
            "unknown_ref": ref,
            "unknown": str(it.get("unknown", "") or src.get("what", "") or "").strip(),
            "lens": lens,
            "observed": _observed(it.get("observed")),
            "why_it_matters": str(it.get("why_it_matters", "")
                                  or src.get("why_it_matters", "") or "").strip(),
            "question": q,
            "suggested_answers": suggested,
            "answer_type": "short_text",
            "cost_if_wrong": cost,
            "runtime_discoverable": False,
        })
    kept.sort(key=lambda x: _COST_RANK[x["cost_if_wrong"]])   # stable: high -> medium -> low
    kept = _dedup_questions(kept)[:_MAX_QUESTIONS]            # quality gate before the cap
    for i, it in enumerate(kept):
        it["id"] = f"q{i + 1}"                        # ids reflect final display order
    return kept


def generate_questions(bundle, event_log, model=DEFAULT_MODEL, model_fn=None,
                       draft=None, author_model_fn=None):
    """Detect gaps for one bundle. Returns ``{"questions": [...]}``, which may legitimately
    be EMPTY.

    ``draft`` is the Agent Author's content record for this bundle (see
    ``configgen.draft_agent``). When it is not supplied the author runs here first,
    on ``author_model_fn`` if given, else on ``model_fn`` — the detector cannot ask
    about unknowns nobody has listed.

    An empty result used to be back-filled with three canned fallback questions, because
    the interview was treated as mandatory back when capture was weak enough that a
    workflow with no detected gaps almost certainly had undetected ones. Capture is good
    enough now that "nothing worth asking" is a real answer, and the padding was actively
    harmful: the gap-detector prompt tells the model not to pad, and code then padded on
    its behalf.

    Downstream already handles zero questions as a complete interview
    (``ui.app._interview_record``), so the build gate is not blocked by an empty round.
    """
    workflow_description, observed_events, _ = make_engine_inputs(bundle, event_log)
    if model_fn is None:
        model_fn = _default_model_fn(model)
    if draft is None:
        draft = draft_agent(bundle, event_log, author_model_fn or model_fn)
    user_msg = build_prompt_material(workflow_description, observed_events, draft)
    model_out = _parse_json_object(model_fn(SYSTEM_PROMPT, user_msg))
    return {"questions": _normalize_questions(model_out.get("questions"),
                                              (draft or {}).get("unknowns"))}
