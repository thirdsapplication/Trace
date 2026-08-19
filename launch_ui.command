#!/bin/bash
# Double-clickable launcher for the Trace Engine 1 test UI (macOS).
# Opens the local app in your browser. Close this window or Ctrl-C to stop.
cd "$(dirname "$0")" || exit 1

# Use a local virtualenv if one exists, else fall through to the search below.
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Pick an interpreter that can actually CAPTURE, not merely one that can serve.
#
# Almost any python3 runs the UI — it is stdlib plus lazily-imported cloud deps —
# so the app starts fine and looks healthy. Capture is the part with real
# dependencies (pynput for keys and clicks, pyobjc for windows and Accessibility),
# and CaptureController imports them lazily inside start(). A Mac commonly has
# several pythons (system, python.org, Homebrew, conda) where only one of them has
# these installed. Pick the wrong one and nothing complains until you press Start
# recording, minutes in, and get "No module named 'pynput'".
#
# So: probe for capture-capability up front, and if nothing has it, say so plainly
# at launch instead of letting it surface as a mid-session failure.
PY=""
for cand in "${TRACE_PYTHON:-}" python3 python; do
  [ -n "$cand" ] || continue
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c "import pynput, Quartz" >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done

if [ -z "$PY" ]; then
  PY="${TRACE_PYTHON:-python3}"
  command -v "$PY" >/dev/null 2>&1 || PY="python"
  echo "WARNING: no Python on this machine has the capture dependencies."
  echo "         The UI will start and everything except recording will work;"
  echo "         'Start recording' will fail with No module named 'pynput'."
  echo
  echo "         Fix with:  $PY -m pip install -r requirements.txt"
  echo "         Or point at a different interpreter:  TRACE_PYTHON=/path/to/python $0"
  echo
fi

exec "$PY" -m trace.ui.app "$@"
