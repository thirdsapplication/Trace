"""PyInstaller entry point for the bundled desktop node.

The Tauri shell launches this frozen binary as a sidecar. It is just the existing
``trace.ui.app`` server (capture local, pipeline proxied to the cloud) — the Tauri
shell passes HOST/PORT/TRACE_CLOUD_BACKEND/TRACE_SESSIONS_ROOT via the environment,
so there is no app-specific logic here beyond starting the server.

A watchdog thread monitors the parent process (the Tauri shell) and exits if it
dies, preventing orphaned sidecar processes from accumulating.
"""

import os
import sys
import threading


def _watch_parent():
    """Exit this process when the parent (Tauri shell) dies.

    On macOS/Linux, when the parent exits the PPID changes to 1 (launchd/init
    adopts the orphan). Poll every 2 seconds — cheap and reliable.

    Windows never re-parents an orphan: ``os.getppid()`` keeps returning the dead
    parent's id, so the check above would never fire and every quit would leave a
    node running. There the parent's process handle is waited on instead; it is
    signalled the moment the parent exits.
    """
    import time
    parent_pid = os.getppid()
    if sys.platform == "win32":
        _watch_parent_windows(parent_pid)
        return
    while True:
        time.sleep(2)
        if os.getppid() != parent_pid:
            os._exit(0)


def _watch_parent_windows(parent_pid):
    import ctypes
    from ctypes import wintypes
    SYNCHRONIZE, INFINITE, WAIT_OBJECT_0 = 0x00100000, 0xFFFFFFFF, 0
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not handle:
        os._exit(0)          # the parent is already gone
    if kernel32.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0:
        os._exit(0)


if __name__ == "__main__":
    # Start the watchdog before anything else so it's always running.
    t = threading.Thread(target=_watch_parent, daemon=True)
    t.start()

    # Test hook: simulate a slow start (Gatekeeper scan, cold disk) to exercise
    # the shell's readiness loop. Never set in production.
    delay = os.environ.get("TRACE_STARTUP_DELAY_S")
    if delay:
        import time
        try:
            time.sleep(float(delay))
        except ValueError:
            pass

    from trace.ui.app import main
    main()
