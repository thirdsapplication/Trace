"""Scorer — a standalone validation harness that grades a discovery.json against an
answer key.

This is the ONLY module permitted to read an answer key. The Discovery Engine
(discover.py) must not import or reach this module or any answer key. Matching is
general — evidence-overlap plus an intent-judge call — never "if id == 'B1'": no
answer-key bundle id, name, count, or domain term appears as a constant or
condition here. Rubric thresholds are supplied alongside the answer key, not
hardcoded.

Answer-key format (a JSON object):
{
  "bundles":      [ { "id": "...", "intent": "<plain-language intent>", "event_ids": ["e..."] }, ... ],
  "anti_bundles": [ { "id": "...", "intent": "...", "event_ids": ["e..."] }, ... ],
  "rubric": { "overlap_threshold": 0.5, "pass_min_bundles": 6,
              "automation_penalty_per_antibundle": 1 }
}
"""

import json
from trace.agent import model_call

DEFAULT_OVERLAP_THRESHOLD = 0.5
_SENSITIVITY_THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7]


def load_answer_key(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _overlap(candidate_ids, ak_ids):
    """Fraction of the answer-key item's event ids covered by a candidate."""
    ak = set(ak_ids)
    if not ak:
        return 0.0
    return len(set(candidate_ids) & ak) / len(ak)


def _best_candidate(ak_item, candidates):
    """Return (best_candidate, best_overlap) by evidence overlap, or (None, 0.0)."""
    best, best_ov = None, 0.0
    for c in candidates:
        ov = _overlap(c.get("evidence_event_ids", []), ak_item["event_ids"])
        if ov > best_ov:
            best, best_ov = c, ov
    return best, best_ov


def _default_intent_judge(model="claude-fable-5-1"):
    """LLM intent judge: confirms a candidate's description matches an intent.
    Lazily imported so the scorer loads without anthropic; injectable for tests."""
    import anthropic
    client = anthropic.Anthropic()

    def judge(candidate, intent):
        desc = " ".join(str(candidate.get(k, "")) for k in
                        ("name", "description", "trigger", "why_automatable",
                         "why_judgment", "recommended_handling"))
        prompt = (
            "A reference workflow has this intent:\n"
            f"  {intent}\n\n"
            "A candidate workflow is described as:\n"
            f"  {desc}\n\n"
            "Do they describe the same underlying workflow/intent? "
            "Answer with exactly one word: YES or NO."
        )
        # Not a tiny ceiling: Fable thinks before it answers and that reasoning is
        # billed against max_tokens. At 5 the model spent the budget before emitting
        # a word, the reply came back empty, and every judgement read as NO.
        text = model_call.create_text(client, model, None, prompt, max_tokens=4000)
        return text.strip().upper().startswith("Y")

    return judge


def score(discovery, answer_key, overlap_threshold=None, intent_judge_fn=None):
    """Grade ``discovery`` against ``answer_key``. Returns a scorecard dict.

    ``intent_judge_fn(candidate_dict, intent_str) -> bool`` is injectable so the
    scorer is testable without a model.
    """
    rubric = answer_key.get("rubric", {}) or {}
    threshold = (overlap_threshold if overlap_threshold is not None
                 else rubric.get("overlap_threshold", DEFAULT_OVERLAP_THRESHOLD))
    penalty_weight = rubric.get("automation_penalty_per_antibundle", 1)
    pass_min = rubric.get("pass_min_bundles")

    if intent_judge_fn is None:
        intent_judge_fn = _default_intent_judge()

    bundles = discovery.get("bundles", []) or []
    anti = discovery.get("anti_bundles", []) or []
    ak_bundles = answer_key.get("bundles", []) or []
    ak_anti = answer_key.get("anti_bundles", []) or []

    # --- bundle identification (overlap + intent judge) ---
    bundle_results = []
    for akb in ak_bundles:
        cand, ov = _best_candidate(akb, bundles)
        intent_ok = bool(cand) and ov > 0 and intent_judge_fn(cand, akb.get("intent", ""))
        identified = ov >= threshold and intent_ok
        bundle_results.append({
            "answer_bundle_id": akb.get("id"),
            "best_overlap": round(ov, 3),
            "intent_match": intent_ok,
            "identified": identified,
            "matched_candidate_id": cand.get("id") if cand else None,
        })
    identified_count = sum(1 for r in bundle_results if r["identified"])

    # --- anti-bundle handling (trust-critical) ---
    anti_results = []
    automation_penalties = 0
    for aka in ak_anti:
        _, ov_bundle = _best_candidate(aka, bundles)
        _, ov_anti = _best_candidate(aka, anti)
        if ov_bundle >= threshold:
            handling = "proposed_for_automation"   # trust failure -> penalty
            automation_penalties += 1
        elif ov_anti >= threshold:
            handling = "flagged_human_checkpoint"   # correct
        else:
            handling = "unclustered"                # neutral
        anti_results.append({
            "answer_anti_id": aka.get("id"),
            "handling": handling,
            "overlap_as_bundle": round(ov_bundle, 3),
            "overlap_as_anti": round(ov_anti, 3),
        })

    # --- threshold sensitivity (reuses each AK bundle's best overlap + judge) ---
    sensitivity = {}
    for t in _SENSITIVITY_THRESHOLDS:
        sensitivity[t] = sum(1 for r in bundle_results
                             if r["best_overlap"] >= t and r["intent_match"])

    penalties = automation_penalties * penalty_weight
    passed = None
    if pass_min is not None:
        passed = identified_count >= pass_min and automation_penalties == 0

    return {
        "overlap_threshold": threshold,
        "bundles_total": len(ak_bundles),
        "bundles_identified": identified_count,
        "bundle_results": bundle_results,
        "anti_bundles_total": len(ak_anti),
        "anti_bundle_results": anti_results,
        "automation_penalties": penalties,
        "rubric": rubric,
        "passed": passed,
        "sensitivity": sensitivity,
    }


def format_scorecard(sc):
    """Render a readable scorecard."""
    lines = []
    lines.append("=" * 56)
    lines.append("  DISCOVERY SCORECARD")
    lines.append("=" * 56)
    lines.append(f"Bundles identified : {sc['bundles_identified']} of {sc['bundles_total']}"
                 f"   (overlap >= {sc['overlap_threshold']} + intent match)")
    for r in sc["bundle_results"]:
        mark = "OK " if r["identified"] else "MISS"
        lines.append(f"  [{mark}] {r['answer_bundle_id']:<10} "
                     f"overlap={r['best_overlap']:.2f} intent={'y' if r['intent_match'] else 'n'}"
                     f" -> {r['matched_candidate_id'] or '-'}")
    lines.append("")
    lines.append("Anti-bundle handling:")
    for r in sc["anti_bundle_results"]:
        lines.append(f"  {r['answer_anti_id']:<10} {r['handling']}"
                     f"  (as_anti={r['overlap_as_anti']:.2f} as_bundle={r['overlap_as_bundle']:.2f})")
    lines.append("")
    lines.append(f"Automation penalties (anti-bundle proposed for automation): "
                 f"{sc['automation_penalties']}")
    lines.append(f"Threshold sensitivity (identified @ overlap): "
                 + ", ".join(f"{t}:{n}" for t, n in sorted(sc['sensitivity'].items())))
    if sc["passed"] is not None:
        lines.append("")
        lines.append(f"VERDICT: {'PASS' if sc['passed'] else 'FAIL'}"
                     f"  (need >= {sc['rubric'].get('pass_min_bundles')} bundles, "
                     f"0 anti-bundles automated)")
    lines.append("=" * 56)
    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Trace Discovery scorer (reads the answer key)")
    ap.add_argument("discovery", help="discovery.json produced by the engine")
    ap.add_argument("answer_key", help="answer key JSON")
    ap.add_argument("--overlap-threshold", type=float, default=None)
    args = ap.parse_args()

    with open(args.discovery, "r", encoding="utf-8") as f:
        discovery = json.load(f)
    answer_key = load_answer_key(args.answer_key)
    sc = score(discovery, answer_key, overlap_threshold=args.overlap_threshold)
    print(format_scorecard(sc))


if __name__ == "__main__":
    main()
