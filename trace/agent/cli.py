"""CLI for Stage 4 — compile approved bundles into agent envelopes (offline).

    python -m trace.agent.cli reviewed_bundles.json event_log.json --specs-dir <dir> -o <out>

Writes ``agent_<id>.test.yaml`` and ``agent_<id>.live.yaml`` per approved bundle,
reading each bundle's ``spec_<id>.md`` from ``--specs-dir``. The sandbox test run
(which needs the API) is driven from the Test UI or a later gate, not this CLI.
"""

import argparse
import json
import os
import sys

from ..artifact import review
from ..artifact.artifact import _spec_filename
from . import compiler


def _safe(s):
    return "".join(c if c.isalnum() else "_" for c in str(s))


def main():
    ap = argparse.ArgumentParser(description="Trace Stage 4 — Agent Compiler")
    ap.add_argument("reviewed", help="reviewed_bundles.json")
    ap.add_argument("event_log", help="event_log.json")
    ap.add_argument("--specs-dir", required=True, help="dir containing spec_<id>.md files")
    ap.add_argument("-o", "--out", default="agents", help="output dir for agent envelopes")
    ap.add_argument("--model", default=compiler.config.DEFAULT_MODEL)
    args = ap.parse_args()

    with open(args.reviewed) as f:
        reviewed = json.load(f)
    with open(args.event_log) as f:
        event_log = json.load(f)
    report = review.ReviewReport()
    reviewed = review.validate_reviewed(reviewed, set(review.event_index(event_log)), report)
    os.makedirs(args.out, exist_ok=True)

    n = 0
    for b in reviewed["bundles"]:
        if b.get("disposition") != "approve_for_build":
            continue
        spec_path = os.path.join(args.specs_dir, _spec_filename(b["id"], True))
        if not os.path.exists(spec_path):
            print(f"  skip {b['id']}: no spec at {spec_path}", file=sys.stderr)
            continue
        with open(spec_path, encoding="utf-8") as f:
            spec_md = f.read()
        compiled = compiler.compile_agent(b, spec_md, event_log, model=args.model)
        for mode in ("test", "live"):
            with open(os.path.join(args.out, f"agent_{_safe(b['id'])}.{mode}.yaml"), "w",
                      encoding="utf-8") as f:
                f.write(compiled[mode]["yaml"])
        n += 1
        if compiled["live"]["unmapped_apps"]:
            print(f"  {b['id']}: unmapped apps -> {compiled['live']['unmapped_apps']}", file=sys.stderr)
    print(f"Compiled {n} bundle(s) -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
