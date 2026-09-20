"""CLI for the Gap Detector (Prompt Library v2).

    python -m trace.agent.interview_cli reviewed_bundles.json event_log.json \
        -o <session_dir> [--model M] [--dry-run]

Per approved bundle: runs the Agent Author's draft pass, writes ``author_<id>.json``
(the draft whose unknowns the questions came from), then seeds ``interview_<id>.json``
(questions only; answers are collected in the UI). ``--dry-run`` prints both system
prompts and each bundle's exact prompt material with NO model call (free).
"""

import argparse
import json
import os
import sys

from ..artifact import review
from . import configgen, interview
from .interview_prompt import SYSTEM_PROMPT


def _safe(s):
    return "".join(c if c.isalnum() else "_" for c in str(s))


def _approved(reviewed_path, event_log_path):
    with open(reviewed_path, encoding="utf-8") as f:
        reviewed = json.load(f)
    with open(event_log_path, encoding="utf-8") as f:
        event_log = json.load(f)
    report = review.ReviewReport()
    reviewed = review.validate_reviewed(reviewed, set(review.event_index(event_log)), report)
    approved = [b for b in reviewed["bundles"] if b.get("disposition") == "approve_for_build"]
    return approved, event_log


def _dry_run(reviewed_path, event_log_path):
    """Both prompts of the questions step: the Agent Author drafts the agent (its
    unknowns are what the detector asks about), then the Gap Detector. With no model
    call there is no draft, so the detector's material shows an empty one."""
    approved, event_log = _approved(reviewed_path, event_log_path)
    print("===== AGENT AUTHOR SYSTEM PROMPT (draft pass) =====")
    print(configgen.SYSTEM_PROMPT)
    print("\n===== GAP DETECTOR SYSTEM PROMPT =====")
    print(SYSTEM_PROMPT)
    for b in approved:
        wf, observed, _ = configgen.make_engine_inputs(b, event_log)
        print(f"\n===== BUNDLE {b['id']} (author material) =====")
        print(configgen.build_prompt_material(wf, observed, []))
        print(f"\n===== BUNDLE {b['id']} (gap detector material, empty draft) =====")
        print(interview.build_prompt_material(wf, observed, None))


def main():
    ap = argparse.ArgumentParser(description="Trace Phase 2 — Structured Interview (gap detector)")
    ap.add_argument("reviewed", help="reviewed_bundles.json")
    ap.add_argument("event_log", help="event_log.json")
    ap.add_argument("-o", "--out", default=".", help="output dir for interview_<id>.json")
    ap.add_argument("--model", default=interview.DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt + per-bundle material; no model call")
    args = ap.parse_args()

    if args.dry_run:
        _dry_run(args.reviewed, args.event_log)
        return

    approved, event_log = _approved(args.reviewed, args.event_log)
    os.makedirs(args.out, exist_ok=True)
    n = 0
    model_fn = configgen._default_model_fn(args.model)
    for b in approved:
        draft = configgen.draft_agent(b, event_log, model_fn)
        with open(os.path.join(args.out, f"author_{_safe(b['id'])}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(draft, f, indent=2, ensure_ascii=False)
        result = interview.generate_questions(b, event_log, model_fn=model_fn, draft=draft)
        questions = result["questions"]
        record = {"bundle_id": b["id"], "questions": questions, "answers": [],
                  "unresolved": [q["id"] for q in questions],
                  "status": "complete" if not questions else "unstarted"}
        with open(os.path.join(args.out, f"interview_{_safe(b['id'])}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        n += 1
    print(f"Generated questions for {n} bundle(s) -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
