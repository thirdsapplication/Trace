import json
import tempfile
import threading
import time
import unittest
import urllib.request

from trace.ui import app, sessions


# --- stubs so the backend is exercised without pynput / a key / the network ---
class FakeController:
    def __init__(self, out):
        self.out = out
        self._running = False
        self._paused = False

    def start(self):
        open(self.out, "w").close()  # create an (empty) raw stream
        self._running = True

    def stop(self):
        self._running = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    @property
    def running(self):
        return self._running

    def status(self):
        return {"running": self._running, "paused": self._paused, "started_at": "t",
                "elapsed_seconds": 1.0, "event_count": 0, "out_path": self.out,
                "browser": {"detected": False, "browser_event_count": 0, "last_event_ts": None}}


class FakeReport:
    def __init__(self):
        self.errors = []
        self.flags = ["one flagged value"]


def fake_reconstruct(raw_path, progress_cb=None, **kw):
    if progress_cb:
        progress_cb(0, 2)
        progress_cb(1, 2)
        progress_cb(2, 2)
    return ([{"id": "e001", "ts_start": "t", "ts_end": "t", "app": "A", "window": "W",
             "action": "navigate", "detail": "did a thing", "evidence": ["r0001"],
             "confidence": "high"}], FakeReport())


class FakeKeystore:
    def __init__(self):
        self.key = None

    def info(self):
        return {"saved": bool(self.key), "backend": "memory",
                "masked_tail": ("…" + self.key[-4:]) if self.key else None}

    def save_key(self, k):
        k = (k or "").strip()
        if not k:
            raise ValueError("empty")
        self.key = k
        return "memory"

    def get_key(self):
        return self.key

    def clear_key(self):
        self.key = None


class UIBackendTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ks = FakeKeystore()
        self.state = app.AppState(
            self._tmp.name,
            reconstruct_fn=fake_reconstruct,
            controller_factory=lambda out: FakeController(out),
            key_store=self.ks,
        )
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    # --- settings ---
    def test_index_served(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"Test Harness", resp.read())

    def test_settings_roundtrip_never_exposes_key(self):
        code, data = self.req("GET", "/api/settings")
        self.assertFalse(data["saved"])
        code, data = self.req("POST", "/api/settings/key", {"key": "sk-ant-12345678"})
        self.assertEqual(code, 200)
        self.assertTrue(data["saved"])
        # the key value must never come back over the API
        self.assertNotIn("12345678", json.dumps(data))
        self.assertEqual(data["masked_tail"], "…5678")

    def test_empty_key_rejected(self):
        code, data = self.req("POST", "/api/settings/key", {"key": ""})
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "empty_key")

    def test_permissions_endpoint(self):
        code, data = self.req("GET", "/api/permissions")
        self.assertEqual(code, 200)
        self.assertIn("accessibility", data)
        self.assertIn("input_monitoring", data)

    # --- capture lifecycle ---
    def test_capture_start_pause_resume_stop(self):
        code, data = self.req("POST", "/api/capture/start")
        self.assertEqual(code, 200)
        sid = data["session_id"]
        self.assertTrue(data["running"])

        code, _ = self.req("POST", "/api/capture/start")  # already running
        self.assertEqual(code, 409)

        code, data = self.req("POST", "/api/capture/pause")
        self.assertEqual(code, 200)
        self.assertTrue(data["paused"])
        code, data = self.req("POST", "/api/capture/resume")
        self.assertFalse(data["paused"])

        code, data = self.req("POST", "/api/capture/stop")
        self.assertEqual(code, 200)
        self.assertEqual(data["session_id"], sid)

        code, data = self.req("GET", "/api/status")
        self.assertFalse(data["running"])
        self.assertEqual(data["state"], "idle")

    def test_pause_when_not_running(self):
        code, data = self.req("POST", "/api/capture/pause")
        self.assertEqual(code, 409)

    # --- guards ---
    def test_reconstruct_empty_recording_rejected(self):
        sid = sessions.create_session(self._tmp.name)  # no raw file
        code, data = self.req("POST", f"/api/sessions/{sid}/reconstruct")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "empty_recording")

    def test_reconstruct_without_key_rejected(self):
        sid = sessions.create_session(self._tmp.name)
        self._write_raw(sid, 10)
        code, data = self.req("POST", f"/api/sessions/{sid}/reconstruct")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "no_api_key")

    def test_short_recording_warns_then_proceeds_with_force(self):
        self.ks.key = "sk-ant-test"
        sid = sessions.create_session(self._tmp.name)
        self._write_raw(sid, 2)  # below threshold
        code, data = self.req("POST", f"/api/sessions/{sid}/reconstruct")
        self.assertEqual(code, 409)
        self.assertEqual(data["warning"], "short_recording")
        code, data = self.req("POST", f"/api/sessions/{sid}/reconstruct", {"force": True})
        self.assertEqual(code, 202)

    # --- full reconstruct + artifacts ---
    def test_reconstruct_writes_artifacts_and_serves_json(self):
        self.ks.key = "sk-ant-test"
        sid = sessions.create_session(self._tmp.name)
        self._write_raw(sid, 10)
        code, data = self.req("POST", f"/api/sessions/{sid}/reconstruct")
        self.assertEqual(code, 202)

        # poll until done
        state = None
        for _ in range(50):
            _, st = self.req("GET", f"/api/sessions/{sid}/reconstruct/status")
            state = st["state"]
            if state in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(state, "done")

        code, log = self.req("GET", f"/api/sessions/{sid}/event_log")
        self.assertEqual(code, 200)
        self.assertEqual(log[0]["action"], "navigate")

        code, val = self.req("GET", f"/api/sessions/{sid}/validation")
        self.assertEqual(code, 200)
        self.assertEqual(val["flags"], ["one flagged value"])

    def test_dry_run_returns_prompt_and_chunks_without_key(self):
        sid = sessions.create_session(self._tmp.name)
        self._write_raw(sid, 6)
        code, data = self.req("POST", f"/api/sessions/{sid}/dry-run", {"chunk_size": 4, "overlap": 1})
        self.assertEqual(code, 200)
        self.assertEqual(data["event_count"], 6)
        self.assertGreaterEqual(data["chunk_count"], 1)
        self.assertIn("perception layer", data["system_prompt"])

    def test_sessions_list(self):
        sessions.create_session(self._tmp.name)
        code, data = self.req("GET", "/api/sessions")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["sessions"]), 1)

    # --- helpers ---
    def _write_raw(self, sid, n):
        p = sessions.path(self._tmp.name, sid, sessions.RAW)
        with open(p, "w") as f:
            for i in range(1, n + 1):
                f.write(json.dumps({"ts": f"2026-06-04T10:00:{i:02d}.000",
                                    "source": "window",
                                    "payload": {"app": "A", "title": f"w{i}"}}) + "\n")


if __name__ == "__main__":
    unittest.main()
