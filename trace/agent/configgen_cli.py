"""CLI for the Config Engine (Phase 1) — reviewed bundles -> agent configs.

    python -m trace.agent.configgen_cli reviewed_bundles.json event_log.json \
        -o configs/ [--answers-dir DIR] [--model M] [--dry-run]

Writes ``agent_<id>.live.yaml`` and ``grounding_<id>.json`` per approved bundle.
``--dry-run`` prints the system prompt and each bundle's exact prompt material with
NO model call (free).
"""

import argparse
import json
import os
import sys

from ..artifact import review
from . import configgen
from .config_prompt import SYSTEM_PROMPT


def _safe(s):
    return "".join(c if c.isalnum() else "_" for c in str(s))


def _dry_run(reviewed_path, event_log_path, answers_dir):
    with open(reviewed_path, encoding="utf-8") as f:
        reviewed = json.load(f)
    with open(event_log_path, encoding="utf-8") as f:
        event_log = json.load(f)
    report = review.ReviewReport()
    reviewed = review.validate_reviewed(reviewed, set(review.event_index(event_log)), report)
    if answers_dir is None:
        answers_dir = os.path.dirname(os.path.abspath(reviewed_path))

    print("===== SYSTEM PROMPT =====")
    print(SYSTEM_PROMPT)
    for b in reviewed["bundles"]:
        if b.get("disposition") != "approve_for_build":
            continue
        wf, observed, _ = configgen.make_engine_inputs(b, event_log)
        answers = configgen._load_answers(answers_dir, b["id"])
        print(f"\n===== BUNDLE {b['id']} =====")
        print(configgen.build_prompt_material(wf, observed, answers))


def main():
    ap = argparse.ArgumentParser(description="Trace Phase 1 — Config Engine")
    ap.add_argument("reviewed", help="reviewed_bundles.json")
    ap.add_argument("event_log", help="event_log.json")
    ap.add_argument("-o", "--out", default="configs", help="output dir for configs + grounding")
    ap.add_argument("--answers-dir", default=None,
                    help="dir of interview_<id>.json answer files (default: reviewed file's dir)")
    ap.add_argument("--model", default=configgen.DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt + per-bundle material; no model call")
    args = ap.parse_args()

    if args.dry_run:
        _dry_run(args.reviewed, args.event_log, args.answers_dir)
        return

    results, report = configgen.generate_configs(
        args.reviewed, args.event_log, model=args.model, answers_dir=args.answers_dir)
    os.makedirs(args.out, exist_ok=True)
    n = 0
    for r in results:
        if not r["built"]:
            continue
        bid = _safe(r["id"])
        with open(os.path.join(args.out, f"agent_{bid}.live.yaml"), "w", encoding="utf-8") as f:
            f.write(r["live_yaml"])
        with open(os.path.join(args.out, f"grounding_{bid}.json"), "w", encoding="utf-8") as f:
            json.dump(r["grounding"], f, indent=2, ensure_ascii=False)
        n += 1
    print(f"Generated {n} config(s) -> {args.out}", file=sys.stderr)
    print(f"Validation: {report}", file=sys.stderr)


if __name__ == "__main__":
    main()
