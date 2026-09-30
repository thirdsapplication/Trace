"""CLI for reconstruction:  raw_events.jsonl -> event_log.json.

    python -m trace.reconstruct.cli captures/raw_events.jsonl -o captures/event_log.json

Requires ANTHROPIC_API_KEY in the environment for the real run. Use
``--dry-run`` to render the chunked prompts without calling the model (handy for
inspecting what Haiku will see, with no cost).
"""

import argparse
import json
import sys

from . import chunking
from .prompt import SYSTEM_PROMPT
from .reconstruct import DEFAULT_MODEL, reconstruct


def main():
    ap = argparse.ArgumentParser(description="Trace reconstruction (Engine 1b)")
    ap.add_argument("raw", help="path to raw_events.jsonl")
    ap.add_argument("-o", "--out", default="captures/event_log.json",
                    help="output event_log.json path")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--chunk-size", type=int, default=120)
    ap.add_argument("--overlap", type=int, default=20)
    ap.add_argument("--workers", type=int, default=None,
                    help="chunks sent at once (default TRACE_RECON_WORKERS or 8; 1 = sequential)")
    ap.add_argument("--dry-run", action="store_true",
                    help="render chunked prompts only; do not call the model")
    args = ap.parse_args()

    if args.dry_run:
        raw = chunking.load_raw(args.raw)
        chunks = chunking.chunk(raw, args.chunk_size, args.overlap)
        print(f"# {len(raw)} raw events -> {len(chunks)} chunk(s)\n")
        print("=== SYSTEM PROMPT ===")
        print(SYSTEM_PROMPT)
        for i, ch in enumerate(chunks, 1):
            print(f"\n=== CHUNK {i} ({len(ch)} events) ===")
            for ev in ch:
                print(chunking.render_event(ev))
        return

    event_log, report = reconstruct(
        args.raw, model=args.model,
        chunk_size=args.chunk_size, overlap=args.overlap, workers=args.workers,
    )
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(event_log, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(event_log)} events -> {args.out}", file=sys.stderr)
    print("Validation: " + str(report), file=sys.stderr)


if __name__ == "__main__":
    main()
