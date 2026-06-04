"""Localhost collector for browser-extension events.

The browser extension (clicks, element targets, in-browser clipboard, scoped
structured reads) POSTs normalized raw events to this localhost endpoint, which
merges them into the *same* ``raw_events.jsonl`` the local macOS process writes to
(via the shared, redacting, flock-protected ``RawWriter``).

Endpoints
---------
``POST /event``   body = one normalized event ``{source, payload, ts?}`` OR a
                  batch ``{"events": [ ... ]}``. Returns ``{"written": N}``.
``GET  /health``  liveness probe -> ``{"ok": true}``.

Only the browser-originated sources are accepted here (``click``, ``target``,
``clipboard``, ``structured_read``); ``keystroke``/``window`` come from the local
process. The writer redacts every payload regardless of origin, so a misbehaving
extension still cannot land secrets on disk.

Run standalone:
    python -m trace.capture.collector --out captures/raw_events.jsonl --port 8137
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .raw_writer import RawWriter

# CORS: a Chrome extension service worker / content script posts here from a
# different origin, so allow it explicitly.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

_BROWSER_SOURCES = {"click", "target", "clipboard", "structured_read"}


class CollectorStats:
    """Lightweight, thread-safe-enough counters so the control surface (§6) can
    report whether the browser extension is connected and when events last
    arrived. Optional; passing ``None`` to ``make_handler`` preserves the original
    behavior exactly."""

    def __init__(self):
        self.browser_event_count = 0
        self.last_event_ts = None  # ISO-8601 string of the last accepted browser event

    def record(self, ts):
        self.browser_event_count += 1
        self.last_event_ts = ts

    def snapshot(self):
        return {
            "detected": self.browser_event_count > 0,
            "browser_event_count": self.browser_event_count,
            "last_event_ts": self.last_event_ts,
        }


def make_handler(writer, stats=None):
    class Handler(BaseHTTPRequestHandler):
        # quiet the default logging noise
        def log_message(self, *args):  # noqa: D401
            pass

        def _send(self, code, body):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in _CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self):
            self.send_response(204)
            for k, v in _CORS_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                body = {"ok": True, "paused": writer.paused}
                if stats is not None:
                    body.update(stats.snapshot())
                self._send(200, body)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/event":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid json"})
                return

            events = body.get("events") if isinstance(body, dict) and "events" in body else [body]
            written = 0
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                source = ev.get("source")
                if source not in _BROWSER_SOURCES:
                    continue
                rec = writer.write(source, ev.get("payload", {}), ts=ev.get("ts"))
                written += 1
                if stats is not None and rec is not None:
                    stats.record(rec["ts"])
            self._send(200, {"written": written})

    return Handler


def serve(out_path, port=8137):
    writer = RawWriter(out_path, reset=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(writer))
    print(f"Trace collector listening on http://127.0.0.1:{port}  ->  {out_path}")
    print("Browser extension should POST normalized events to /event. Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nCollector stopped.")
        httpd.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Trace browser-event collector")
    ap.add_argument("--out", default="captures/raw_events.jsonl",
                    help="shared raw_events.jsonl path (default: captures/raw_events.jsonl)")
    ap.add_argument("--port", type=int, default=8137)
    args = ap.parse_args()
    serve(args.out, args.port)


if __name__ == "__main__":
    main()
