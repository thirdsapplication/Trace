"""Live Windows capture check — records a real session against Notepad.

The unit tests prove what capture DECIDES against fakes. This proves the parts
only a real Windows desktop has: that UI Automation answers, that the keyboard and
mouse hooks fire, and that a session through the same CaptureController the app
uses comes out with window, keystroke, click and target events.

    python desktop/windows_smoke.py            # Notepad
    python desktop/windows_smoke.py --chrome   # a web form in Chrome, no extension

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


FORM = """<!doctype html><title>Trace smoke form</title>
<label for=cust>Customer name</label> <input id=cust>
<label for=st>Status</label>
<select id=st><option>Open</option><option>Paid</option></select>
<button id=save onclick="document.title='Saved'">Save order</button>"""

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find(adapter, root, role, label, depth=25):
    """First element under ``root`` with this role whose label contains ``label``."""
    frontier = [root]
    for _ in range(depth):
        nxt = []
        for node in frontier:
            for child in adapter.children(node):
                d = ax.detail_of(adapter, child)
                if d and d["role"] == role and label.lower() in d["label"].lower():
                    return child
                nxt.append(child)
        frontier = nxt
    return None


def click_center(element):
    from pynput.mouse import Button, Controller as Mouse
    rect = element.CurrentBoundingRectangle
    Mouse().position = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    time.sleep(0.2)
    Mouse().click(Button.left)


def chrome_check():
    """Chrome without the extension: does UI Automation see into the page? This is
    what a tester who never installs the extension gets — named fields and buttons,
    or nothing but window titles."""
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if not chrome:
        print("FAILED: Chrome not installed")
        return 1
    uia.make_dpi_aware()
    tmp = tempfile.mkdtemp()
    page = os.path.join(tmp, "form.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(FORM)
    out = os.path.join(tmp, "raw_events.jsonl")
    adapter = uia.UIAAdapter()
    ctl = CaptureController(out, collector_port=0)
    ctl.start()
    proc = subprocess.Popen([chrome, "--new-window", "--no-first-run",
                             "--no-default-browser-check", f"--user-data-dir={tmp}\\profile",
                             "file:///" + page.replace("\\", "/")])
    failures = []
    try:
        time.sleep(8)                     # first launch of a fresh profile is slow
        root = adapter.content_root()
        print("content root:", ax.detail_of(adapter, root))
        field = find(adapter, root, "textbox", "Customer name")
        button = find(adapter, root, "button", "Save order")
        print("found field:", field is not None, " found button:", button is not None)
        if field is None or button is None:
            failures.append("Chrome page content not visible to UI Automation")
        else:
            from pynput.keyboard import Controller as Keys
            click_center(field)
            time.sleep(0.5)
            Keys().type("Acme Foods")
            time.sleep(0.5)
            click_center(button)
            time.sleep(2)
    finally:
        ctl.stop()
        proc.kill()

    rows = read_raw_rows(out)
    print("events by source:", sources.summarize_sources(rows))
    for r in rows:
        if r["source"] in ("click", "target", "structured_read"):
            p = r["payload"]
            print("  ", r["source"], {k: p.get(k) for k in ("role", "label", "value", "url", "target")})
    clicks = [r["payload"].get("target") or {} for r in rows if r["source"] == "click"]
    if not any("save order" in (t.get("label") or "").lower() for t in clicks):
        failures.append("the click on 'Save order' was not resolved to the button")
    targets = [r["payload"] for r in rows if r["source"] == "target"]
    if not any("customer name" in (t.get("label") or "").lower() for t in targets):
        failures.append("focus on the 'Customer name' field was not recorded")
    reads = [r["payload"] for r in rows if r["source"] == "structured_read"]
    if not any((p.get("url") or "").startswith("file:") for p in reads):
        failures.append("no page read carried the page URL")
    if failures:
        print("FAILED:", "; ".join(failures))
        return 1
    print("OK: Chrome page content recorded without the extension")
    return 0


def main():
    if sys.platform != "win32":
        print("windows_smoke: not Windows, nothing to check")
        return 0
    if "--chrome" in sys.argv:
        return chrome_check()
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
