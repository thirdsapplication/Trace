"""Local test-harness backend for Engine 1 (handoff §7).

A small stdlib HTTP server (no framework, no new deps — consistent with the
collector) that drives the *existing* engine: it manages capture via the §6
control surface, runs the existing reconstruction pipeline (real + dry-run), and
serves the three engine artifacts as JSON for inspection. The front end is a
single static page served from ``ui/static/``.

LOCAL ONLY (handoff §3): binds 127.0.0.1, no accounts, no remote anything.

Seams kept injectable so the whole backend is testable without a key, pynput, or
network: ``AppState.reconstruct_fn`` and ``AppState.controller_factory``.
"""

import argparse
import json
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..capture.controller import CaptureController
from ..reconstruct import chunking
from ..reconstruct.prompt import SYSTEM_PROMPT
from ..reconstruct.reconstruct import reconstruct as _real_reconstruct
from . import keystore, permissions, sessions

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
SHORT_RECORDING_THRESHOLD = 5  # events; below this, warn before a paid run

_CONTENT_TYPES = {".html": "text/html", ".js": "application/javascript",
                  ".css": "text/css", ".json": "application/json"}


class AppState:
    def __init__(self, sessions_root, collector_port=8137,
                 reconstruct_fn=None, controller_factory=None, key_store=None):
        self.sessions_root = sessions_root
        self.collector_port = collector_port
        self.reconstruct_fn = reconstruct_fn or _real_reconstruct
        self.controller_factory = controller_factory or (
            lambda out: CaptureController(out, collector_port=collector_port))
        self.keystore = key_store or keystore
        self.controller = None       # current CaptureController (one session at a time)
        self.current_sid = None
        self.recon_jobs = {}         # sid -> {state, current, total, error, flags, errors}
        self.lock = threading.Lock()
        os.makedirs(sessions_root, exist_ok=True)

    # --- reconstruction job ----------------------------------------------
    def start_reconstruction(self, sid):
        raw_path = sessions.path(self.sessions_root, sid, sessions.RAW)
        job = {"state": "running", "current": 0, "total": 0, "error": None,
               "flags": 0, "errors": 0}
        self.recon_jobs[sid] = job

        def run():
            try:
                key = self.keystore.get_key()
                if not key:
                    raise RuntimeError("no_api_key")
                # Supply the key exactly as the engine expects: via the process env.
                os.environ["ANTHROPIC_API_KEY"] = key

                def cb(done, total):
                    job["current"], job["total"] = done, total

                log, report = self.reconstruct_fn(raw_path, progress_cb=cb)
                with open(sessions.path(self.sessions_root, sid, sessions.EVENT_LOG),
                          "w", encoding="utf-8") as f:
                    json.dump(log, f, indent=2, ensure_ascii=False)
                with open(sessions.path(self.sessions_root, sid, sessions.VALIDATION),
                          "w", encoding="utf-8") as f:
                    json.dump({"errors": report.errors, "flags": report.flags},
                              f, indent=2, ensure_ascii=False)
                sessions.update_meta(self.sessions_root, sid,
                                     reconstructed_at=sessions._now(),
                                     event_count=len(log))
                job.update(state="done", flags=len(report.flags),
                           errors=len(report.errors))
            except Exception as e:  # surface real detail (handoff §4.5)
                job.update(state="error", error=str(e))

        threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------
def make_handler(state):
    root = state.sessions_root

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # --- io helpers ---
        def _json(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _text(self, code, body, ctype):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        # --- routing ---
        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p == "/" or p == "/index.html":
                return self._serve_static("index.html")
            if p.startswith("/static/"):
                return self._serve_static(p[len("/static/"):])
            if p == "/api/status":
                return self._status()
            if p == "/api/settings":
                return self._json(200, state.keystore.info())
            if p == "/api/permissions":
                return self._json(200, permissions.check_all())
            if p == "/api/sessions":
                return self._json(200, {"sessions": sessions.list_sessions(root)})
            m = re.match(r"^/api/sessions/([^/]+)(/.*)?$", p)
            if m:
                return self._session_get(m.group(1), m.group(2) or "")
            return self._json(404, {"error": "not_found"})

        def do_POST(self):
            p = self.path.split("?", 1)[0]
            body = self._body()
            if p == "/api/settings/key":
                return self._save_key(body)
            if p == "/api/settings/test-key":
                return self._json(200, _test_key(state))
            if p == "/api/capture/start":
                return self._capture_start()
            if p == "/api/capture/stop":
                return self._capture_stop()
            if p == "/api/capture/pause":
                return self._capture_ctl("pause")
            if p == "/api/capture/resume":
                return self._capture_ctl("resume")
            m = re.match(r"^/api/sessions/([^/]+)/(dry-run|reconstruct)$", p)
            if m:
                sid, action = m.group(1), m.group(2)
                if action == "dry-run":
                    return self._dry_run(sid, body)
                return self._reconstruct(sid, body)
            return self._json(404, {"error": "not_found"})

        def do_DELETE(self):
            if self.path.split("?", 1)[0] == "/api/settings/key":
                state.keystore.clear_key()
                return self._json(200, {"saved": False})
            return self._json(404, {"error": "not_found"})

        # --- static ---
        def _serve_static(self, rel):
            rel = rel.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                return self._json(404, {"error": "not_found"})
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as f:
                self._text(200, f.read(), _CONTENT_TYPES.get(ext, "application/octet-stream"))

        # --- settings ---
        def _save_key(self, body):
            key = (body or {}).get("key", "")
            try:
                backend = state.keystore.save_key(key)
            except ValueError:
                return self._json(400, {"error": "empty_key"})
            return self._json(200, {**state.keystore.info(), "backend": backend})

        # --- capture ---
        def _status(self):
            with state.lock:
                if state.controller and state.controller.running:
                    s = state.controller.status()
                    s["session_id"] = state.current_sid
                    s["state"] = "paused" if s["paused"] else "capturing"
                else:
                    s = {"running": False, "state": "idle", "session_id": None}
            return self._json(200, s)

        def _capture_start(self):
            with state.lock:
                if state.controller and state.controller.running:
                    return self._json(409, {"error": "already_running",
                                            "session_id": state.current_sid})
                sid = sessions.create_session(root)
                out = sessions.path(root, sid, sessions.RAW)
                controller = state.controller_factory(out)
                try:
                    controller.start()
                except Exception as e:
                    return self._json(500, {"error": "start_failed", "detail": str(e),
                                            "session_id": sid})
                state.controller = controller
                state.current_sid = sid
                s = controller.status()
                s["session_id"] = sid
                return self._json(200, s)

        def _capture_stop(self):
            with state.lock:
                if not (state.controller and state.controller.running):
                    return self._json(409, {"error": "not_running"})
                sid = state.current_sid
                state.controller.stop()
                count = sessions.artifacts(root, sid)["raw_event_count"]
                sessions.update_meta(root, sid, stopped_at=sessions._now(),
                                     raw_event_count=count)
                state.controller = None
                state.current_sid = None
            return self._json(200, {"session_id": sid, "raw_event_count": count})

        def _capture_ctl(self, action):
            with state.lock:
                if not (state.controller and state.controller.running):
                    return self._json(409, {"error": "not_running"})
                try:
                    getattr(state.controller, action)()
                except RuntimeError as e:
                    return self._json(409, {"error": str(e)})
                s = state.controller.status()
                s["session_id"] = state.current_sid
            return self._json(200, s)

        # --- session reads ---
        def _session_get(self, sid, sub):
            if sub in ("", "/"):
                meta = sessions.read_meta(root, sid)
                meta["artifacts"] = sessions.artifacts(root, sid)
                return self._json(200, meta)
            if sub == "/event_log":
                data = sessions.read_json_artifact(root, sid, sessions.EVENT_LOG)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "not_reconstructed"})
            if sub == "/validation":
                data = sessions.read_json_artifact(root, sid, sessions.VALIDATION)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "not_reconstructed"})
            if sub == "/raw":
                data = sessions.read_raw_events(root, sid)
                return self._json(200 if data is not None else 404,
                                  data if data is not None else {"error": "no_raw"})
            if sub == "/reconstruct/status":
                job = state.recon_jobs.get(sid)
                return self._json(200, job or {"state": "idle"})
            return self._json(404, {"error": "not_found"})

        # --- reconstruction ---
        def _dry_run(self, sid, body):
            raw_path = sessions.path(root, sid, sessions.RAW)
            try:
                raw = chunking.load_raw(raw_path)
            except FileNotFoundError:
                return self._json(404, {"error": "no_raw"})
            cs = int((body or {}).get("chunk_size", 120))
            ov = int((body or {}).get("overlap", 20))
            chunks = chunking.chunk(raw, chunk_size=cs, overlap=ov)
            return self._json(200, {
                "event_count": len(raw),
                "system_prompt": SYSTEM_PROMPT,
                "chunk_count": len(chunks),
                "chunks": [
                    {"index": i + 1, "total": len(chunks),
                     "lines": [chunking.render_event(ev) for ev in ch]}
                    for i, ch in enumerate(chunks)
                ],
            })

        def _reconstruct(self, sid, body):
            count = sessions.artifacts(root, sid)["raw_event_count"]
            if count == 0:
                return self._json(400, {"error": "empty_recording"})
            if not state.keystore.get_key():
                return self._json(400, {"error": "no_api_key"})
            if count < SHORT_RECORDING_THRESHOLD and not (body or {}).get("force"):
                return self._json(409, {"warning": "short_recording",
                                        "event_count": count,
                                        "detail": "Few events captured. Reconstructing "
                                                  "will call the model and cost money. "
                                                  "Resend with force=true to proceed."})
            with state.lock:
                existing = state.recon_jobs.get(sid)
                if existing and existing.get("state") == "running":
                    return self._json(409, {"error": "already_running"})
                state.start_reconstruction(sid)
            return self._json(202, {"state": "running", "session_id": sid})

    return Handler


def _test_key(state):
    """Make one tiny, cheap call to confirm the saved key works (handoff §4.1)."""
    key = state.keystore.get_key()
    if not key:
        return {"valid": False, "detail": "No API key saved. Add one in Settings."}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(model="claude-haiku-4-5", max_tokens=1,
                               messages=[{"role": "user", "content": "ping"}])
        return {"valid": True, "detail": "Key accepted by Anthropic."}
    except Exception as e:
        # never echo the key; surface the error type/message only
        return {"valid": False, "detail": f"{type(e).__name__}: {e}"}


def build_server(host="127.0.0.1", port=8765, sessions_root="captures/sessions",
                 collector_port=8137, state=None):
    state = state or AppState(sessions_root, collector_port=collector_port)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    return httpd, state


def main():
    ap = argparse.ArgumentParser(description="Trace Engine 1 — local test UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--sessions-root", default="captures/sessions")
    ap.add_argument("--collector-port", type=int, default=8137)
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    httpd, _ = build_server(port=args.port, sessions_root=args.sessions_root,
                            collector_port=args.collector_port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Trace test UI: {url}")
    print(f"  sessions  -> {os.path.abspath(args.sessions_root)}")
    print(f"  collector -> 127.0.0.1:{args.collector_port} (browser extension target)")
    print("Ctrl-C to stop.")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
