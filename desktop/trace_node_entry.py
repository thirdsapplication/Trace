"""PyInstaller entry point for the bundled desktop node.

The Tauri shell launches this frozen binary as a sidecar. It is just the existing
``trace.ui.app`` server (capture local, pipeline proxied to the cloud) — the Tauri
shell passes HOST/PORT/TRACE_CLOUD_BACKEND/TRACE_SESSIONS_ROOT via the environment,
so there is no app-specific logic here beyond starting the server.
"""

from trace.ui.app import main

if __name__ == "__main__":
    main()
