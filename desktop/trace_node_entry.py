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
    """
    import time
    parent_pid = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != parent_pid:
            os._exit(0)


if __name__ == "__main__":
    # Start the watchdog before anything else so it's always running.
    t = threading.Thread(target=_watch_parent, daemon=True)
    t.start()

    from trace.ui.app import main
    main()
