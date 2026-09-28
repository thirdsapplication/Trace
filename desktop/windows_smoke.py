"""Live Windows capture check — records a real session against Notepad.

The unit tests prove what capture DECIDES against fakes. This proves the parts
only a real Windows desktop has: that UI Automation answers, that the keyboard and
mouse hooks fire, and that a session through the same CaptureController the app
uses comes out with window, keystroke, click and target events.

    python desktop/windows_smoke.py

Runs in CI on a Windows runner (.github/workflows/windows-build.yml) and by hand on
any Windows PC. Exits non-zero, naming what is missing, if capture is not working.
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trace.capture import ax, sources, uia, win_window  # noqa: E402
from trace.capture.controller import CaptureController, read_raw_rows  # noqa: E402


def main():
    if sys.platform != "win32":
        print("windows_smoke: not Windows, nothing to check")
        return 0
    uia.make_dpi_aware()
    failures = []

    adapter = uia.UIAAdapter()
    print("UI Automation available:", adapter.available())
    if not adapter.available():
        failures.append("UI Automation unavailable")

    out = os.path.join(tempfile.mkdtemp(), "raw_events.jsonl")
    ctl = CaptureController(out, collector_port=0)
    ctl.start()
    print("capture started; accessibility:", ctl.status()["accessibility"])

    notepad = subprocess.Popen(["notepad.exe"])
    time.sleep(3)
    print("frontmost:", win_window.frontmost())

    from pynput.keyboard import Controller as Keys
    from pynput.mouse import Button, Controller as Mouse
    Keys().type("invoice 42 sent to bob@example.com")
    time.sleep(1)
    focused = ax.detail_of(adapter, adapter.focused_element())
    print("focused element:", focused)

    # Click inside Notepad's text area, then move focus away so the text field
    # flushes its final value the way it does on blur.
    window = adapter.focused_window()
    try:
        rect = window.CurrentBoundingRectangle
        Mouse().position = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        Mouse().click(Button.left)
    except Exception as e:
        print("could not click:", e)
    time.sleep(1.5)
    notepad.kill()
    time.sleep(1.5)
    ctl.stop()

    rows = read_raw_rows(out)
    counts = sources.summarize_sources(rows)
    print("events by source:", counts)
    for r in rows:
        if r["source"] in ("window", "click", "target"):
            print("  ", r["source"], r["payload"])

    for needed in ("window", "keystroke", "click", "target"):
        if not counts.get(needed):
            failures.append(f"no {needed} events")
    if focused is None or focused.get("role") != "textbox":
        failures.append(f"focused element in Notepad was {focused!r}, expected a textbox")
    titled = [r for r in rows if r["source"] == "window" and r["payload"].get("title")]
    if not titled:
        failures.append("window events carried no titles")
    typed = " ".join(r["payload"].get("text", "") for r in rows if r["source"] == "keystroke")
    if "invoice 42" not in typed:
        failures.append(f"typed text not captured (got {typed!r})")

    if failures:
        print("FAILED:", "; ".join(failures))
        return 1
    print("OK: Windows capture recorded a live session")
    return 0


if __name__ == "__main__":
    sys.exit(main())
