"""CLI for the Discovery Engine:  event_log.json -> discovery.json.

    python -m trace.discovery.cli captures/.../event_log.json -o discovery.json

Requires ANTHROPIC_API_KEY for the real run. ``--dry-run`` renders the exact
prompt + the rendered event log that would be sent to the model, with no call and
no cost. The engine never reads an answer key; grade output with
``python -m trace.discovery.scorer`` separately.
"""

import argparse
import json
import sys

from .discover import DEFAULT_MODEL, discover, load_event_log, render_event_log
from .prompt import SYSTEM_PROMPT


def main():
    ap = argparse.ArgumentParser(description="Trace Discovery Engine (Stage 2)")
    ap.add_argument("event_log", help="path to event_log.json")
    ap.add_argument("-o", "--out", default="discovery.json", help="output discovery.json path")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="render the prompt + event log only; do not call the model")
    args = ap.parse_args()

    if args.dry_run:
        events = load_event_log(args.event_log)
        print(f"# {len(events)} events -> ONE discovery call (no chunking)\n")
        print("=== SYSTEM PROMPT ===")
        print(SYSTEM_PROMPT)
        print("\n=== RENDERED EVENT LOG (model input) ===")
        print(render_event_log(events))
        return

    discovery, report = discover(args.event_log, model=args.model)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(discovery, f, indent=2, ensure_ascii=False)
    n_b, n_a = len(discovery["bundles"]), len(discovery["anti_bundles"])
    print(f"Wrote {n_b} bundle(s) + {n_a} anti-bundle(s) -> {args.out}", file=sys.stderr)
    print("Validation: " + str(report), file=sys.stderr)


if __name__ == "__main__":
    main()
