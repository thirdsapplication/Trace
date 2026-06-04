#!/bin/bash
# Double-clickable launcher for the Trace Engine 1 test UI (macOS).
# Opens the local app in your browser. Close this window or Ctrl-C to stop.
cd "$(dirname "$0")" || exit 1

# Use a local virtualenv if one exists, else the system python3.
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

exec python3 -m trace.ui.app "$@"
