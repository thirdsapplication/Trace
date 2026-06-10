"""CLI for the Artifact Engine:  reviewed_bundles.json + event_log.json -> specs.

    python -m trace.artifact.cli reviewed_bundles.json event_log.json -o specs/

Writes one ``spec_<id>.md`` per approved bundle (and a ``spec_<id>_manual.md`` note
per anti-bundle). ``--dry-run`` renders, for each approved bundle, the prompt + the
bundle + the verbatim examples that would be sent — no model call, no cost.
"""

import argparse
import os
import sys

from . import environment, review
from .artifact import (DEFAULT_MODEL, build_prompt_material, environment_filename,
                       generate_specs, load_json, write_spec_bundle)
from .prompt import SYSTEM_PROMPT


def main():
    ap = argparse.ArgumentParser(description="Trace Artifact Engine (Stage 3)")
    ap.add_argument("reviewed", help="path to reviewed_bundles.json")
    ap.add_argument("event_log", help="path to event_log.json")
    ap.add_argument("-o", "--out", default="specs", help="output directory for spec files")
    ap.add_argument("--env-dir", default=None,
                    help="directory of environment_<id>.json records (default: reviewed file's dir)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="render the prompt + bundle + verbatim examples; no model call")
    args = ap.parse_args()

    if args.dry_run:
        reviewed = load_json(args.reviewed)
        event_log = load_json(args.event_log)
        rep = review.ReviewReport()
        reviewed = review.validate_reviewed(reviewed, set(review.event_index(event_log)), rep)
        print("=== SYSTEM PROMPT ===")
        print(SYSTEM_PROMPT)
        approved = [b for b in reviewed["bundles"] if b.get("disposition") == "approve_for_build"]
        env_dir = args.env_dir or os.path.dirname(os.path.abspath(args.reviewed))
        print(f"\n# {len(approved)} approved bundle(s) -> one model call each\n")
        for b in approved:
            examples = review.fetch_examples(event_log, b["included_example_event_ids"])
            ep = os.path.join(env_dir, environment_filename(b["id"]))
            env_rec = environment.validate_environment(load_json(ep)) if os.path.exists(ep) else None
            print(f"===== BUNDLE {b['id']} =====")
            print(build_prompt_material(b, examples, env_rec))
            print()
        return

    results, report = generate_specs(args.reviewed, args.event_log, model=args.model,
                                     env_dir=args.env_dir)
    os.makedirs(args.out, exist_ok=True)
    written = 0
    for r in results:
        if r.get("markdown") is None:
            continue
        missing = write_spec_bundle(args.out, r)   # spec_<id>.md + spec_<id>_files/
        if missing:
            print(f"  warning: {r['id']}: could not copy {missing} (preflight will catch it)",
                  file=sys.stderr)
        written += 1
    built = sum(1 for r in results if r["built"])
    print(f"Wrote {written} self-contained spec bundle(s) ({built} build spec(s)) -> {args.out}",
          file=sys.stderr)
    print("Validation: " + str(report), file=sys.stderr)


if __name__ == "__main__":
    main()
