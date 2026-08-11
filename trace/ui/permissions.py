"""macOS permission + browser-capture status checks (handoff §4.1).

Capture needs three macOS permissions that tripped up earlier manual runs:
- **Accessibility** — legacy window-title path (the ``osascript`` spine).
- **Input Monitoring** — to read keystrokes.
- **Screen Recording** — to read window TITLES. Since window capture moved to the
  Quartz CGWindowList API, ``kCGWindowName`` is only populated for a process that
  holds Screen Recording; without it the API still returns every window, just with
  an empty name. That failure is silent: events keep flowing with ``title: ""``,
  and because ``window_loop`` only emits on change, a whole session inside one
  browser collapses to a single event.

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
SCREEN_RECORDING_FIX = (
    "Grant in System Settings → Privacy & Security → Screen Recording, then add/enable "
    "the app running Trace and FULLY QUIT AND RELAUNCH it — macOS does not apply this "
    "permission to an already-running process. Without it window titles are blank, so "
    "Trace cannot tell which app or page you were working in."
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


def check_screen_recording():
    """Screen Recording (window titles via kCGWindowName). Uses the preflight call so
    this never triggers the system prompt as a side effect of a status check."""
    if not _is_macos():
        return {"name": "Screen Recording", "granted": None,
                "detail": "Not macOS — capture is macOS-only.", "fix": SCREEN_RECORDING_FIX}
    try:
        from Quartz import CGPreflightScreenCaptureAccess
        granted = bool(CGPreflightScreenCaptureAccess())
        return {"name": "Screen Recording", "granted": granted,
                "detail": "CGPreflightScreenCaptureAccess",
                "fix": None if granted else SCREEN_RECORDING_FIX}
    except Exception:
        pass
    # Fallback: a non-empty title from the poller means kCGWindowName is readable,
    # which is only true when the permission is held.
    try:
        from ..capture.window import frontmost
        cur = frontmost()
        ok = bool(cur and cur[1])
        return {"name": "Screen Recording", "granted": True if ok else None,
                "detail": "probed active-window title",
                "fix": None if ok else SCREEN_RECORDING_FIX}
    except Exception as e:
        return {"name": "Screen Recording", "granted": None,
                "detail": f"could not auto-detect ({e})", "fix": SCREEN_RECORDING_FIX}


def check_all():
    return {
        "platform": sys.platform,
        "accessibility": check_accessibility(),
        "input_monitoring": check_input_monitoring(),
        "screen_recording": check_screen_recording(),
    }
