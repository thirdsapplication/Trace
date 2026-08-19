"""Live Accessibility-capture probe — the hand-verification the test suite can't do.

The unit tests pin down what the capture logic DECIDES, against a fake adapter.
What they cannot touch is the part that only exists on a real Mac: whether macOS
granted the permission, whether the app in front of you exposes a useful
Accessibility tree, and whether the events that come out are worth anything. This
answers exactly that, in one command, without starting a session or touching the
UI.

    python -m trace.capture.probe               # 30 seconds, live
    python -m trace.capture.probe --seconds 60
    python -m trace.capture.probe --tree        # dump the focused window's tree once

Use it by running it, switching to the app you want to test — Shopify admin,
Safari, a native client — and working normally for the duration. Every line it
prints is one raw event it would have written to ``raw_events.jsonl``.

Reading the output:

- ``FOCUS`` lines are navigation context: which control you moved to.
- ``CHANGED`` lines are the ones that matter — a value you actually set. A
  workflow that is mostly button-pushing lives or dies on these.
- ``CLICK`` lines with ``target=None`` mean the element under the cursor could not
  be resolved: usually a canvas-drawn control, which Accessibility cannot see.
- No output at all, with the permission granted, means the app in front of you
  exposes nothing useful — worth knowing before building on it.
"""

import argparse
import tempfile
import threading
import time

from . import ax, sources
from .controller import read_raw_rows
from .raw_writer import RawWriter


def _fmt(event):
    """One raw event as a single readable line."""
    source, p = event["source"], event["payload"]
    if source == "target":
        kind = "CHANGED" if p.get("changed") else "FOCUS  "
        return (f'{kind} {p.get("role",""):<10} {p.get("label","")!r}'
                f' = {p.get("value","")!r}   [{p.get("window","")}]')
    if source == "click":
        t = p.get("target")
        where = (f'{t.get("role","")} {t.get("label","")!r}' if t else "target=None")
        return f'CLICK   {p.get("button","")} at ({p.get("x")},{p.get("y")})   {where}'
    if source == "structured_read":
        elements = p.get("elements", [])
        head = " | ".join(f'{e.get("label","")}={e.get("value","")}'
                          for e in elements[:4])
        return f'READ    {len(elements)} elements   {head}'
    return f'{source.upper():<7} {p}'


class Tee(RawWriter):
    """A real writer that also prints, so what you see is exactly what would be
    persisted — redaction included, since that happens inside write()."""

    def write(self, source, payload, ts=None, origin="local"):
        record = super().write(source, payload, ts=ts, origin=origin)
        if record is not None:
            print("  " + _fmt(record), flush=True)
        return record


def show_permissions():
    from ..ui import permissions
    checks = permissions.check_all()
    print(f"Platform: {checks['platform']}")
    for key in ("accessibility", "input_monitoring", "screen_recording"):
        c = checks[key]
        state = {True: "GRANTED", False: "DENIED", None: "unknown"}[c["granted"]]
        print(f"  {c['name']:<18} {state:<8} ({c['importance']})")
    if not checks["ready"]:
        print(f"\n  Blocking: {', '.join(checks['blocking'])}")
        print("  Grant it, then FULLY QUIT and relaunch the terminal running this.")
    return checks


def dump_tree():
    """One-shot dump of the focused window's element tree — the fastest way to see
    whether an app is worth capturing at all."""
    adapter = ax.AXAdapter()
    if not adapter.available():
        print("Accessibility not granted; nothing to dump.")
        return
    print(f'Focused window: {adapter.focused_window_title()!r}\n')
    elements = ax.walk(adapter, adapter.focused_window())
    if not elements:
        print("No readable elements. This app exposes no useful Accessibility tree.")
        return
    for e in elements:
        print(f'  {e["role"]:<12} {e["label"]!r} = {e["value"]!r}')
    print(f'\n{len(elements)} elements (cap is {ax.MAX_WALK_ELEMENTS}).')


def main():
    ap = argparse.ArgumentParser(description="Probe Accessibility capture live")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--tree", action="store_true",
                    help="dump the focused window's element tree and exit")
    args = ap.parse_args()

    checks = show_permissions()
    print()
    if args.tree:
        dump_tree()
        return

    adapter = ax.AXAdapter()
    if not adapter.available():
        print("Accessibility capture is OFF — nothing below would be captured.")
        return

    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tmp.close()
    writer = Tee(tmp.name, reset=True)
    stop = threading.Event()

    print(f"Recording for {args.seconds}s. Switch to the app you want to test and "
          "work normally.\n")
    threading.Thread(target=ax.ax_loop, args=(stop, writer, adapter),
                     daemon=True).start()
    listener = ax.click_listener(writer, adapter)
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    stop.set()
    if listener:
        listener.stop()
    time.sleep(0.6)                       # let the last poll land

    rows = read_raw_rows(tmp.name)
    health = sources.capture_health(sources.summarize_sources(rows), rows=rows)
    print(f"\n--- {health['total']} events ---")
    for src, n in sorted(health["by_source"].items()):
        print(f"  {src:<18} {n}")
    print(f"  semantic events    {health['semantic_count']}")
    changed = sum(1 for r in rows
                  if (r.get("payload") or {}).get("changed"))
    print(f"  values you set     {changed}")
    if health["message"]:
        print(f"\n  WARNING: {health['message']}")
    elif not changed:
        print("\n  No value changes captured. Fields and dropdowns you filled would "
              "not be recoverable from this session.")
    else:
        print("\n  Looks healthy.")
    print(f"\nRaw stream kept at: {tmp.name}")


if __name__ == "__main__":
    main()
