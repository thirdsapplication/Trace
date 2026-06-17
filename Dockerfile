# Trace cloud backend (Phase 1).
#
# Builds the existing stdlib http.server backend from a clean clone and runs it
# as an always-on web service. The same image runs locally (docker run) and on
# Render; behavior is switched only by environment variables — never by code.
#
# The server reads HOST and PORT from the environment (Render injects $PORT), so
# it binds 0.0.0.0:$PORT in the cloud and 127.0.0.1:8765 by default locally.
# pynput/keyring are gated to macOS in requirements.txt, so they are simply
# skipped on this Linux image; capture is local-only and not run here.

FROM python:3.12-slim

# No .pyc files, unbuffered logs (so Render's log stream is live).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so the layer caches across source-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Then the application source.
COPY . .

# Default local port; Render overrides via $PORT. Documentation only — the bind
# is driven by the HOST/PORT env reads in trace/ui/app.py.
EXPOSE 8765

# Production start: the real web server, not a dev shim. HOST/PORT come from env.
CMD ["python", "-m", "trace.ui.app"]
