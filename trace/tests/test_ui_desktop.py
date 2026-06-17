"""Phase 2 — desktop-node ⇄ cloud-node integration.

Stands up TWO real servers in-process: a "cloud" node (no cloud_backend, owns the
pipeline + users) and a "desktop" node pointed at it (cloud_backend set). Proves
the architecture over genuine loopback HTTP:
  - auth proxies desktop → cloud and the cookie flows back and is replayed;
  - capture runs LOCALLY on the desktop node (no cloud round-trip to start);
  - on stop, the captured raw stream is uploaded to the cloud, which creates a
    cloud-owned session the pipeline can read;
  - the desktop node hands the UI the CLOUD session id;
  - a plain node (no cloud_backend) is completely unchanged (dual-run).
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from trace.ui import app, sessions, users
from trace.tests.test_ui import FakeController, FakeKeystore, fake_reconstruct


def _serve(state):
    httpd, _ = app.build_server(port=0, state=state)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


class DesktopProxyTest(unittest.TestCase):
    def setUp(self):
        # Isolate the (process-global) user store so signup doesn't touch ~/.trace.
        self._users_tmp = tempfile.TemporaryDirectory()
        self._orig_path, self._orig_dir = users._PATH, users._DIR
        users._DIR = self._users_tmp.name
        users._PATH = f"{self._users_tmp.name}/users.json"
        users._SESSIONS.clear()

        # Cloud node: owns pipeline + users; stubbed reconstruct/controller/keystore.
        self._cloud_tmp = tempfile.TemporaryDirectory()
        cloud_ks = FakeKeystore()
        cloud_ks.save_key("sk-ant-test")          # cloud pays for runs (server-side key)
        self.cloud_state = app.AppState(
            self._cloud_tmp.name, reconstruct_fn=fake_reconstruct,
            controller_factory=lambda out: FakeController(out), key_store=cloud_ks)
        self.cloud_httpd, self.cloud_port = _serve(self.cloud_state)

        # Desktop node: capture local, everything else proxied to the cloud node.
        self._desk_tmp = tempfile.TemporaryDirectory()
        self.desk_state = app.AppState(
            self._desk_tmp.name, controller_factory=lambda out: FakeController(out),
            key_store=FakeKeystore(), cloud_backend=f"http://127.0.0.1:{self.cloud_port}")
        self.desk_httpd, self.desk_port = _serve(self.desk_state)

    def tearDown(self):
        for h in (self.desk_httpd, self.cloud_httpd):
            h.shutdown(); h.server_close()
        users._PATH, users._DIR = self._orig_path, self._orig_dir
        users._SESSIONS.clear()
        for t in (self._desk_tmp, self._cloud_tmp, self._users_tmp):
            t.cleanup()

    # one tiny client with a cookie jar (urllib doesn't keep cookies itself)
    def _req(self, port, method, path, body=None, cookie=None):
        url = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(r)
        except urllib.error.HTTPError as e:
            resp = e
        set_cookie = resp.headers.get("Set-Cookie")
        tok = None
        if set_cookie and set_cookie.startswith(users.COOKIE_NAME + "="):
            tok = set_cookie.split(";", 1)[0]
        out = resp.read()
        return resp.status, (json.loads(out) if out else None), tok

    def desk(self, *a, **k):
        return self._req(self.desk_port, *a, **k)

    def cloud(self, *a, **k):
        return self._req(self.cloud_port, *a, **k)

    def test_auth_proxies_and_cookie_round_trips(self):
        # Signup hits the desktop node but is served by the cloud (which owns users).
        status, data, cookie = self.desk("POST", "/api/auth/signup",
                                         {"email": "a@b.com", "password": "password1"})
        self.assertEqual(status, 200)
        self.assertEqual(data["user"]["email"], "a@b.com")
        self.assertTrue(cookie and cookie.startswith(users.COOKIE_NAME + "="))
        # The cloud-issued cookie, replayed through the desktop proxy, authenticates.
        status, me, _ = self.desk("GET", "/api/u/me", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(me["user"]["email"], "a@b.com")
        # And without it, the cloud (via proxy) rejects the scoped call.
        status, _, _ = self.desk("GET", "/api/u/sessions")
        self.assertEqual(status, 401)

    def test_capture_uploads_to_cloud_and_pipeline_reads_it(self):
        _, _, cookie = self.desk("POST", "/api/auth/signup",
                                 {"email": "c@d.com", "password": "password1"})
        # Capture is handled LOCALLY by the desktop node (no local user needed).
        status, started, _ = self.desk("POST", "/api/u/capture/start", cookie=cookie)
        self.assertEqual(status, 200)
        local_sid = started["session_id"]
        # Stop → uploads raw to the cloud → returns the CLOUD session id.
        status, stopped, _ = self.desk("POST", "/api/u/capture/stop", cookie=cookie)
        self.assertEqual(status, 200)
        cloud_sid = stopped["session_id"]
        self.assertEqual(stopped["local_session_id"], local_sid)
        self.assertEqual(stopped["raw_event_count"], 2)        # FakeController writes 2
        # The cloud node now owns a session with the uploaded raw stream...
        self.assertTrue(sessions.artifacts(self._cloud_tmp.name, cloud_sid)["raw"])
        self.assertEqual(
            sessions.read_meta(self._cloud_tmp.name, cloud_sid)["owner_user_id"],
            users.session_user(cookie.split("=", 1)[1]))
        # ...and the logged-in user sees it through the proxied scoped API.
        status, mine, _ = self.desk("GET", "/api/u/sessions", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(cloud_sid, [s["id"] for s in mine["sessions"]])
        # A proxied pipeline action reaches the cloud and starts the run there,
        # against the uploaded data (force past the short-recording guard; the
        # cloud holds the API key, never the desktop).
        status, started_recon, _ = self.desk(
            "POST", f"/api/u/sessions/{cloud_sid}/reconstruct", {"force": True},
            cookie=cookie)
        self.assertEqual(status, 202)
        self.assertEqual(started_recon["state"], "running")

    def test_raw_upload_endpoint_scopes_to_owner(self):
        _, _, cookie = self.cloud("POST", "/api/auth/signup",
                                  {"email": "e@f.com", "password": "password1"})
        raw = json.dumps({"ts": "t", "source": "click", "payload": {}}) + "\n"
        status, data, _ = self.cloud("POST", "/api/u/sessions/raw-upload",
                                     {"raw": raw}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["raw_event_count"], 1)
        # Unauthenticated upload is refused.
        status, _, _ = self.cloud("POST", "/api/u/sessions/raw-upload", {"raw": raw})
        self.assertEqual(status, 401)

    def test_plain_node_unchanged_dual_run(self):
        # No cloud_backend → capture/stop returns the LOCAL sid, no upload, no proxy.
        self.assertIsNone(self.cloud_state.cloud_backend)
        status, started, _ = self.cloud("POST", "/api/capture/start")
        self.assertEqual(status, 200)
        status, stopped, _ = self.cloud("POST", "/api/capture/stop")
        self.assertEqual(status, 200)
        self.assertEqual(stopped["session_id"], started["session_id"])
        self.assertNotIn("local_session_id", stopped)


if __name__ == "__main__":
    unittest.main()
