"""Admin portal — token gate (fail closed), the /api/admin/* read surface, the
LLM prompt tap, and /admin static serving. Offline: file-mode registry, real HTTP
server, env-patched TRACE_ADMIN_TOKEN.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from trace.ui import app, registry, sessions, users
from trace.tests.test_ui import FakeController, FakeKeystore, fake_reconstruct

TOKEN = "test-admin-token-123"


class AdminHTTPBase(unittest.TestCase):
    def setUp(self):
        self._users_tmp = tempfile.TemporaryDirectory()
        self._op, self._od = users._PATH, users._DIR
        users._DIR = self._users_tmp.name
        users._PATH = f"{self._users_tmp.name}/users.json"
        users._SESSIONS.clear()
        self._prior_token = os.environ.pop("TRACE_ADMIN_TOKEN", None)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(self.root, reconstruct_fn=fake_reconstruct,
                                  controller_factory=lambda out: FakeController(out),
                                  key_store=FakeKeystore())
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        users._PATH, users._DIR = self._op, self._od
        users._SESSIONS.clear()
        if self._prior_token is not None:
            os.environ["TRACE_ADMIN_TOKEN"] = self._prior_token
        else:
            os.environ.pop("TRACE_ADMIN_TOKEN", None)
        self._tmp.cleanup(); self._users_tmp.cleanup()

    def req(self, path, token=None):
        headers = {}
        if token:
            headers["X-Admin-Token"] = token
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        try:
            resp = urllib.request.urlopen(r)
        except urllib.error.HTTPError as e:
            resp = e
        body = resp.read()
        ct = resp.headers.get("Content-Type", "")
        return resp.status, (json.loads(body) if "json" in ct else body)


class AdminGateTest(AdminHTTPBase):
    def test_fails_closed_when_env_unset(self):
        code, data = self.req("/api/admin/overview", token="anything")
        self.assertEqual((code, data["error"]), (403, "admin_disabled"))

    def test_wrong_token_forbidden(self):
        os.environ["TRACE_ADMIN_TOKEN"] = TOKEN
        code, data = self.req("/api/admin/overview", token="wrong")
        self.assertEqual((code, data["error"]), (403, "forbidden"))
        code, data = self.req("/api/admin/overview")           # missing header
        self.assertEqual(code, 403)

    def test_right_token_admits(self):
        os.environ["TRACE_ADMIN_TOKEN"] = TOKEN
        code, data = self.req("/api/admin/overview", token=TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(data["db_mode"], "files")
        self.assertIn("users", data)

    def test_blocked_request_sends_only_the_403_no_data_leak(self):
        # A raw client reading to EOF must NOT receive a trailing 200 with admin data
        # (the gate must actually stop the handler, not just send a 403 before it runs).
        os.environ["TRACE_ADMIN_TOKEN"] = TOKEN
        users.create_user("victim@example.com", "longenough1")
        import socket
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"GET /api/admin/accounts HTTP/1.0\r\nHost: x\r\n\r\n")
        raw = b""
        s.settimeout(2)
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw += chunk
        except socket.timeout:
            pass
        s.close()
        self.assertIn(b"403", raw.split(b"\r\n", 1)[0])
        self.assertNotIn(b"200 OK", raw)                   # no second response
        self.assertNotIn(b"victim@example.com", raw)       # no admin data leaked


class AdminEndpointsTest(AdminHTTPBase):
    def setUp(self):
        super().setUp()
        os.environ["TRACE_ADMIN_TOKEN"] = TOKEN
        self.u = users.create_user("worker@acme.com", "longenough1")
        sid = sessions.create_session(self.root, label="capture-1")
        sessions.update_meta(self.root, sid, owner_user_id=self.u["user_id"])
        self.sid = sid
        registry.agent_upsert(self.root, {
            "agent_id": "agent-1", "owner_user_id": self.u["user_id"],
            "external_user_id": users.external_user_id(self.u["user_id"]),
            "bundle_id": "wf", "name": "Order intake", "vault_ref": "v",
            "trigger": "t", "session_id": sid,
            "deployment_id": "dep_1", "memory_store_id": "mem_1"})

    def test_accounts_lists_every_user_with_their_state(self):
        code, data = self.req("/api/admin/accounts", token=TOKEN)
        self.assertEqual(code, 200)
        acct = next(a for a in data["accounts"]
                    if a["user"]["email"] == "worker@acme.com")
        self.assertEqual([s["id"] for s in acct["sessions"]], [self.sid])
        agent = acct["agents"][0]
        self.assertEqual(agent["deployment_id"], "dep_1")
        self.assertEqual(agent["memory_store_id"], "mem_1")
        self.assertNotIn("password_hash", json.dumps(data))    # never any password material

    def test_pipeline_snapshots_job_dicts(self):
        self.state.discover_jobs[self.sid] = {"state": "running", "error": None,
                                              "bundles": 0, "anti_bundles": 0}
        self.state.build_jobs[self.sid] = {"state": "done", "error": None, "generated": 1}
        code, data = self.req("/api/admin/pipeline", token=TOKEN)
        self.assertEqual(code, 200)
        stages = data["sessions"][self.sid]
        self.assertEqual(stages["discover"]["state"], "running")
        self.assertEqual(stages["build"]["state"], "done")
        self.assertEqual(data["meta"][self.sid]["owner_user_id"], self.u["user_id"])

    def test_architecture_names_components_and_pipeline(self):
        code, data = self.req("/api/admin/architecture", token=TOKEN)
        self.assertEqual(code, 200)
        ids = {c["id"] for c in data["components"]}
        self.assertLessEqual({"desktop", "cloud", "postgres", "anthropic", "pipedream"}, ids)
        self.assertIn("capture", data["pipeline"][0])


class PromptTapTest(AdminHTTPBase):
    def setUp(self):
        super().setUp()
        os.environ["TRACE_ADMIN_TOKEN"] = TOKEN

    def test_tap_records_and_reraises(self):
        ok_fn = self.state._tap("discovery", "sid-1", "model-x", lambda: (lambda s, u: "out"))
        self.assertEqual(ok_fn("SYS", "USER"), "out")           # behavior unchanged
        def boom(s, u):
            raise RuntimeError("model down")
        bad_fn = self.state._tap("config", None, "model-y", lambda: boom)
        with self.assertRaises(RuntimeError):
            bad_fn("S2", "U2")
        entries = list(self.state.prompt_log)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["engine"], "discovery")
        self.assertEqual(entries[0]["system"], "SYS")
        self.assertEqual(entries[0]["response"], "out")
        self.assertIsNone(entries[0]["error"])
        self.assertEqual(entries[1]["error"], "RuntimeError: model down")
        self.assertIsNone(entries[1]["response"])
        self.assertEqual([e["seq"] for e in entries], [1, 2])

    def test_tap_truncates_huge_fields(self):
        big = "x" * (self.state._PROMPT_CLIP + 500)
        fn = self.state._tap("specs", None, "m", lambda: (lambda s, u: big))
        fn(big, "small")
        e = list(self.state.prompt_log)[-1]
        self.assertIn("[truncated]", e["system"])
        self.assertIn("[truncated]", e["response"])
        self.assertEqual(e["user"], "small")
        self.assertLess(len(e["system"]), self.state._PROMPT_CLIP + 100)

    def test_ring_is_bounded(self):
        fn = self.state._tap("discovery", None, "m", lambda: (lambda s, u: "r"))
        for i in range(250):
            fn(f"s{i}", "u")
        self.assertEqual(len(self.state.prompt_log), 200)       # deque maxlen
        self.assertEqual(self.state.prompt_seq, 250)

    def test_prompts_endpoint_since_filtering(self):
        fn = self.state._tap("interview", "sid-9", "m", lambda: (lambda s, u: "r"))
        for i in range(5):
            fn(f"s{i}", "u")
        code, data = self.req("/api/admin/prompts?since=3", token=TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual([e["seq"] for e in data["entries"]], [4, 5])
        self.assertEqual(data["latest_seq"], 5)

    def test_build_model_fn_is_tapped(self):
        # the config/interview seam records through the tap (stub the real builder)
        from trace.agent import configgen
        self.state.keystore.save_key("sk-ant-test")
        orig = configgen._default_model_fn
        configgen._default_model_fn = lambda model: (lambda s, u: '{"ok": true}')
        try:
            fn = self.state._build_model_fn("interview", "sid-2")
            fn("SYS", "USER")
        finally:
            configgen._default_model_fn = orig
        e = list(self.state.prompt_log)[-1]
        self.assertEqual((e["engine"], e["sid"]), ("interview", "sid-2"))

    def test_summarize_fn_injection_still_tapped(self):
        st = app.AppState(self.root, key_store=FakeKeystore(),
                          summarize_fn=lambda s, u: "summary text")
        fn = st._summarize_model_fn()
        self.assertEqual(fn("S", "U"), "summary text")
        self.assertEqual(list(st.prompt_log)[-1]["engine"], "activity")

    def test_tap_model_tiers_are_strings_not_functions(self):
        # Regression: the tap wiring must read DEFAULT_MODEL off the engine MODULES,
        # not the package-reexported functions (which have no DEFAULT_MODEL — the
        # "Find work" AttributeError). Guards all four Pattern-B sites at once.
        for tier in (app._RECON_MODEL, app._DISCOVER_MODEL, app._SPECS_MODEL):
            self.assertIsInstance(tier, str)
        for builder in (app._recon_model_fn, app._discover_model_fn, app._specs_model_fn):
            self.assertTrue(callable(builder))

    def test_real_discovery_engine_tap_path_runs(self):
        # Drive the REAL discovery engine (default discovery_fn) through start_discovery
        # so the actual tap branch executes — the stub-injected chain tests never hit it,
        # which is how the module-vs-function import bug slipped through. Stub only the
        # network model call.
        from trace.agent import configgen
        ks = FakeKeystore(); ks.save_key("sk-ant-test")
        st = app.AppState(self.root, key_store=ks)
        sid = sessions.create_session(self.root)
        with open(sessions.path(self.root, sid, sessions.EVENT_LOG), "w") as f:
            json.dump([{"id": "e1", "app": "Gmail", "window": "Inbox", "action": "read",
                        "detail": "read an email", "ts_start": "2026-01-01T00:00:00"}], f)
        orig = app._discover_model_fn
        app._discover_model_fn = lambda model: (lambda s, u:
            '{"bundles": [], "anti_bundles": [], "unclustered_or_noise_event_ids": [], "notes": ""}')
        try:
            st.start_discovery(sid)
            for _ in range(200):
                job = st.discover_jobs.get(sid) or {}
                if job.get("state") in ("done", "error"):
                    break
                import time
                time.sleep(0.02)
        finally:
            app._discover_model_fn = orig
        self.assertEqual(st.discover_jobs[sid]["state"], "done")   # no AttributeError
        self.assertIsNotNone(sessions.read_json_artifact(self.root, sid, sessions.DISCOVERY))
        self.assertEqual(list(st.prompt_log)[-1]["engine"], "discovery")   # tapped


class AdminStaticTest(AdminHTTPBase):
    def test_admin_shell_served_without_token(self):
        # the PAGE is public (all data calls are gated); SPA fallback works
        code, body = self.req("/admin")
        self.assertEqual(code, 200)
        self.assertIn(b"admin.js", body)
        code, body = self.req("/admin/some/deep/route")
        self.assertEqual(code, 200)
        self.assertIn(b"admin.js", body)                       # index fallback

    def test_admin_assets_served(self):
        code, body = self.req("/admin/admin.js")
        self.assertEqual(code, 200)
        self.assertIn(b"X-Admin-Token", body)


if __name__ == "__main__":
    unittest.main()
