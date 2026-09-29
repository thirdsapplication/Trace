"""Capture diagnostics — what went wrong inside capture, and on what machine.

Every capture loop swallows its exceptions, on purpose: a capture thread must never
take a session down. The cost is that a failure is indistinguishable from a quiet
workday — "no clicks recorded" could mean nobody clicked, or that UI Automation
threw on every call. On a machine we cannot sit at (a Windows tester's PC) that
difference is the whole question.

So swallowed exceptions are ALSO noted here: counted by where they happened and
what they were, with the first message of each kind kept. ``report()`` bundles that
with the facts about the machine, and the desktop sends it with the recording.

It holds exception types and messages only — never an event payload, typed text or
a window title — so it is safe to send wherever the recording itself may go.
"""

import os
import platform
import sys
import threading
import traceback

MAX_KINDS = 40            # distinct (where, type) pairs kept; the rest are counted
MAX_MESSAGE = 300

_lock = threading.Lock()
_errors = {}              # (where, type) -> {"count", "message", "at"}
_dropped = 0
_facts = {}               # one-off facts noted during startup (e.g. adapter status)


def note(where, exc):
    """Record a swallowed exception. Cheap and never raises."""
    global _dropped
    try:
        key = (where, type(exc).__name__)
        with _lock:
            entry = _errors.get(key)
            if entry is not None:
                entry["count"] += 1
            elif len(_errors) < MAX_KINDS:
                frame = traceback.extract_tb(exc.__traceback__)[-1:] if exc.__traceback__ else []
                at = f"{os.path.basename(frame[0].filename)}:{frame[0].lineno}" if frame else ""
                _errors[key] = {"count": 1, "message": str(exc)[:MAX_MESSAGE], "at": at}
            else:
                _dropped += 1
    except Exception:
        pass


def fact(key, value):
    """Record a fact about this capture run (adapter availability, why not, ...)."""
    with _lock:
        _facts[key] = value


def reset():
    global _dropped
    with _lock:
        _errors.clear()
        _facts.clear()
        _dropped = 0


def errors():
    with _lock:
        out = [{"where": w, "type": t, **v} for (w, t), v in _errors.items()]
        return sorted(out, key=lambda e: -e["count"]), _dropped


def machine():
    """Facts about the machine that change how capture behaves."""
    info = {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    if sys.platform == "win32":
        info.update(_windows_facts())
    return info


def _windows_facts():
    out = {}
    try:
        import ctypes
        # Display scaling: clicks on a scaled display are where coordinate bugs live.
        dpi = ctypes.windll.user32.GetDpiForSystem()
        out["display_scale_percent"] = round(dpi * 100 / 96)
    except Exception as e:
        out["display_scale_percent"] = f"unknown ({type(e).__name__})"
    try:
        import ctypes
        # A capture process running as Administrator can read elevated apps; one that
        # is not, cannot. Worth knowing either way.
        out["capture_elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass
    return out


def report(capture_status=None, capture_health=None, log_tail=None):
    """The whole diagnostics bundle for one recording."""
    errs, dropped = errors()
    with _lock:
        facts = dict(_facts)
    return {
        "machine": machine(),
        "facts": facts,
        "capture_status": capture_status or {},
        "capture_health": capture_health or {},
        "errors": errs,
        "errors_dropped": dropped,
        "log_tail": log_tail or [],
    }


def log_tail(path, max_lines=150):
    """The end of the node's log, filtered to lines about the app itself. The log
    holds startup lines, capture status and tracebacks — never captured content —
    but it is still filtered to what diagnosis needs."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-2000:]
    except OSError:
        return []
    keep = ("[shell", "[capture", "Traceback", "Error", "error", "Exception",
            "  File ", "WARN", "FATAL", "health", "spawn", "navigate")
    picked = [ln.rstrip("\n")[:400] for ln in lines if any(k in ln for k in keep)]
    return picked[-max_lines:]
