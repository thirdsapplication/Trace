"""macOS permission + browser-capture status checks (handoff §4.1).

Capture needs two macOS permissions that tripped up earlier manual runs:
- **Accessibility** — to read active-window titles (the ``osascript`` spine).
- **Input Monitoring** — to read keystrokes.

We detect them best-effort via pyobjc if present, and degrade gracefully to
``granted: None`` ("can't auto-detect") with fix-it instructions otherwise. These
checks never raise and are safe to call on any platform.
"""

import sys


def _is_macos():
    return sys.platform == "darwin"


ACCESSIBILITY_FIX = (
    "Grant in System Settings → Privacy & Security → Accessibility, then add/enable "
    "the app running Trace (e.g. your Terminal or the Trace launcher)."
)
INPUT_MONITORING_FIX = (
    "Grant in System Settings → Privacy & Security → Input Monitoring, then add/enable "
    "the app running Trace. You may need to quit and relaunch it afterward."
)


def check_accessibility():
    """Accessibility (window titles). Uses AXIsProcessTrusted via pyobjc when
    available; otherwise falls back to probing the active-window poller."""
    if not _is_macos():
        return {"name": "Accessibility", "granted": None,
                "detail": "Not macOS — capture is macOS-only.", "fix": ACCESSIBILITY_FIX}
    try:
        from ApplicationServices import AXIsProcessTrusted
        granted = bool(AXIsProcessTrusted())
        return {"name": "Accessibility", "granted": granted,
                "detail": "AXIsProcessTrusted", "fix": None if granted else ACCESSIBILITY_FIX}
    except Exception:
        pass
    # Fallback: if the window poller can read a title, accessibility is effectively on.
    try:
        from ..capture.window import frontmost
        ok = frontmost() is not None
        return {"name": "Accessibility", "granted": ok if ok else None,
                "detail": "probed active-window poller",
                "fix": None if ok else ACCESSIBILITY_FIX}
    except Exception as e:
        return {"name": "Accessibility", "granted": None,
                "detail": f"could not auto-detect ({e})", "fix": ACCESSIBILITY_FIX}


def check_input_monitoring():
    """Input Monitoring (keystrokes). Uses IOHIDCheckAccess via pyobjc/Quartz when
    available (0 = granted, 1 = denied, 2 = unknown); otherwise not auto-detectable."""
    if not _is_macos():
        return {"name": "Input Monitoring", "granted": None,
                "detail": "Not macOS — capture is macOS-only.", "fix": INPUT_MONITORING_FIX}
    try:
        from Quartz import IOHIDCheckAccess, kIOHIDRequestTypeListenEvent
        rc = IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)
        if rc == 0:
            return {"name": "Input Monitoring", "granted": True,
                    "detail": "IOHIDCheckAccess=granted", "fix": None}
        if rc == 1:
            return {"name": "Input Monitoring", "granted": False,
                    "detail": "IOHIDCheckAccess=denied", "fix": INPUT_MONITORING_FIX}
        return {"name": "Input Monitoring", "granted": None,
                "detail": "IOHIDCheckAccess=unknown (not yet requested)", "fix": INPUT_MONITORING_FIX}
    except Exception:
        return {"name": "Input Monitoring", "granted": None,
                "detail": "could not auto-detect (install pyobjc for a definitive check)",
                "fix": INPUT_MONITORING_FIX}


def check_all():
    return {
        "platform": sys.platform,
        "accessibility": check_accessibility(),
        "input_monitoring": check_input_monitoring(),
    }
