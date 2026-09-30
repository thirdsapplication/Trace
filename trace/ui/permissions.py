"""Capture permission + browser-capture status checks (handoff §4.1).

macOS needs three grants, described below. Windows needs none of them; the checks
report that plainly rather than "can't detect", so nothing on Windows nags.

Capture uses three macOS permissions, and which of them matter changed when
Accessibility capture landed:

- **Accessibility** — now the important one. It reads the focused element, its
  label and value, and the element under a click, which is what makes a native-app
  or non-Chrome session legible at all. It ALSO supplies the window title via
  ``kAXTitleAttribute``. Without it, work outside Chrome records as window titles
  and loose keystrokes.
- **Input Monitoring** — to read keystrokes and mouse clicks. Unchanged.
- **Screen Recording** — reads window TITLES via ``kCGWindowName``, which is only
  populated for a process that holds it. Its absence used to be catastrophic and
  silent: events kept flowing with ``title: ""``, and since ``window_loop`` only
  emits on change, a whole session inside one browser collapsed to a single blank
  event. Now that Accessibility supplies the same title, this is a **fallback**
  rather than a requirement — it only bites when neither permission is granted.

``REQUIRED`` below is the machine-readable version of that, so the UI ranks these
by consequence instead of listing three equal-looking switches.

We detect them best-effort via pyobjc if present, and degrade gracefully to
``granted: None`` ("can't auto-detect") with fix-it instructions otherwise. These
checks never raise and are safe to call on any platform.
"""

import sys

# Permission key -> whether capture is meaningfully degraded without it.
# "required": semantic capture does not happen at all.
# "recommended": there is a working fallback, but a narrower one.
REQUIRED = {
    "accessibility": "required",
    "input_monitoring": "required",
    "screen_recording": "recommended",
}


def _is_macos():
    return sys.platform == "darwin"


def _is_windows():
    return sys.platform == "win32"


# Windows has no counterpart to these three grants: UI Automation, keyboard/mouse
# hooks and window titles are open to any ordinary process. The one real limit is
# that a normal process cannot read an app running as Administrator.
WINDOWS_NOTE = ("Windows does not require this permission. Apps you run as "
                "Administrator can't be read, so run them normally while recording.")


def _windows_check(name):
    return {"name": name, "granted": True, "detail": WINDOWS_NOTE, "fix": None}


# The pane names moved with the OS: macOS 27 renamed Accessibility to "Device Control
# and Data Access" and took it out of Privacy & Security; Tahoe (26) renamed Screen
# Recording to "Screen & System Audio Recording". The fix-it text names both so a
# person on either release can find the switch. The underlying TCC service (and the
# API each check calls) is unchanged.
ACCESSIBILITY_FIX = (
    "Grant in System Settings → Privacy & Security → Accessibility (on macOS 27 it is "
    "called Device Control and Data Access — search System Settings for it), then "
    "add/enable the Palza app. If Palza already shows as enabled, remove it with the "
    "minus button, add it again and relaunch — an updated app keeps its old, stale "
    "entry. Without it Palza can see that you worked in an app but not which buttons "
    "you pressed or which fields you filled, so workflows outside Chrome cannot be "
    "rebuilt."
)
INPUT_MONITORING_FIX = (
    "Grant in System Settings → Privacy & Security → Input Monitoring, then add/enable "
    "the Palza app and quit and relaunch it. macOS never prompts for this one on its "
    "own, and an entry left over from an earlier version of the app silently stops "
    "working — remove and re-add it if it already shows as enabled."
)
SCREEN_RECORDING_FIX = (
    "Grant in System Settings → Privacy & Security → Screen Recording (called Screen & "
    "System Audio Recording on macOS 26 and later), then add/enable the Palza app and "
    "FULLY QUIT AND RELAUNCH it — macOS does not apply this permission to an "
    "already-running process. Palza reads window titles from Accessibility when this "
    "is off, so grant it only if titles still come back blank."
)


def request_accessibility():
    """Trigger the system's Accessibility prompt and report whether it is granted.

    ``AXIsProcessTrustedWithOptions`` with the prompt option is the only way to
    make macOS show the grant dialog; ``AXIsProcessTrusted`` alone silently returns
    False forever, which is why the permission has sat unrequested. Safe to call
    repeatedly — macOS shows the dialog at most once per process.
    """
    if _is_windows():
        return True
    if not _is_macos():
        return False
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt,
        )
        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
    except Exception:
        return False


def check_accessibility():
    """Accessibility (element labels/values, click targets, window titles). Uses
    AXIsProcessTrusted via pyobjc when available; otherwise falls back to probing
    the active-window poller."""
    if _is_windows():
        from ..capture.uia import UIAAdapter
        if UIAAdapter().available():
            return _windows_check("Accessibility")
        return {"name": "Accessibility", "granted": None,
                "detail": "UI Automation could not be reached on this machine.",
                "fix": None}
    if not _is_macos():
        return {"name": "Accessibility", "granted": None,
                "detail": "Capture runs on macOS and Windows.", "fix": ACCESSIBILITY_FIX}
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
    if _is_windows():
        return _windows_check("Input Monitoring")
    if not _is_macos():
        return {"name": "Input Monitoring", "granted": None,
                "detail": "Capture runs on macOS and Windows.", "fix": INPUT_MONITORING_FIX}
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
    if _is_windows():
        return _windows_check("Screen Recording")
    if not _is_macos():
        return {"name": "Screen Recording", "granted": None,
                "detail": "Capture runs on macOS and Windows.", "fix": SCREEN_RECORDING_FIX}
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
    """All three checks plus a single verdict the UI can gate a recording on.

    ``ready`` is False only when a *required* permission is known-denied. An
    undetectable permission (``granted: None``) does not block — a check we cannot
    run is not evidence of a problem, and refusing to record on that basis would
    be worse than recording and warning afterwards.
    """
    checks = {
        "accessibility": check_accessibility(),
        "input_monitoring": check_input_monitoring(),
        "screen_recording": check_screen_recording(),
    }
    for key, check in checks.items():
        check["importance"] = REQUIRED.get(key, "recommended")
    blocking = [c["name"] for k, c in checks.items()
                if REQUIRED.get(k) == "required" and c["granted"] is False]
    return {
        "platform": sys.platform,
        "ready": not blocking,
        "blocking": blocking,
        **checks,
    }
