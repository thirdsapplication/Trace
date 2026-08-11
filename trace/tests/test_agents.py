"""Spec 2 — re-runnable pipeline: the durable per-user agent index and the
aggregate /api/u/agents endpoint, exercised on the file-mode (no-DATABASE_URL)
path so it doubles as a dual-run check. Two deploys add two agents (no clobber),
scoped to the owner.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from trace.ui import app, registry, sessions, users
from trace.tests.test_ui import FakeController, FakeKeystore, fake_reconstruct


class AgentsEndpointTest(unittest.TestCase):
    def setUp(self):
        self._users_tmp = tempfile.TemporaryDirectory()
        self._op, self._od = users._PATH, users._DIR
        users._DIR = self._users_tmp.name
        users._PATH = f"{self._users_tmp.name}/users.json"
        users._SESSIONS.clear()

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
        self._tmp.cleanup(); self._users_tmp.cleanup()

    def _req(self, method, path, body=None, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(r)
        except urllib.error.HTTPError as e:
            resp = e
        sc = resp.headers.get("Set-Cookie")
        tok = sc.split(";", 1)[0] if sc and sc.startswith(users.COOKIE_NAME + "=") else None
        out = resp.read()
        return resp.status, (json.loads(out) if out else None), tok

    def _signup(self, email):
        _, data, cookie = self._req("POST", "/api/auth/signup",
                                    {"email": email, "password": "password1"})
        return data["user"]["user_id"], cookie

    def test_two_agents_added_and_scoped(self):
        uid, cookie = self._signup("a@b.com")
        # Two deploys (simulated) add two agents for the same user — no clobber.
        for i in (1, 2):
            registry.agent_upsert(self.root, {
                "agent_id": f"agent-{i}", "owner_user_id": uid,
                "external_user_id": users.external_user_id(uid),
                "bundle_id": f"wf-{i}", "name": f"Workflow {i}",
                "vault_ref": "vlt-1", "trigger": "on a new task", "session_id": f"sess-{i}"})

        status, data, _ = self._req("GET", "/api/u/agents", cookie=cookie)
        self.assertEqual(status, 200)
        ids = sorted(a["agent_id"] for a in data["agents"])
        self.assertEqual(ids, ["agent-1", "agent-2"])         # both present, neither clobbered
        # each carries the schedule/runs shape the dashboard + activity read
        self.assertIn("schedule", data["agents"][0])
        self.assertIn("recent_runs", data["agents"][0])

    def test_agents_isolated_per_user(self):
        uid_a, cookie_a = self._signup("one@x.com")
        uid_b, cookie_b = self._signup("two@x.com")
        registry.agent_upsert(self.root, {
            "agent_id": "agent-a", "owner_user_id": uid_a, "external_user_id": "x",
            "bundle_id": "wf", "name": "A", "vault_ref": None, "trigger": "t",
            "session_id": "s"})
        _, data_b, _ = self._req("GET", "/api/u/agents", cookie=cookie_b)
        self.assertEqual(data_b["agents"], [])               # B sees none of A's agents
        _, data_a, _ = self._req("GET", "/api/u/agents", cookie=cookie_a)
        self.assertEqual([a["agent_id"] for a in data_a["agents"]], ["agent-a"])

    def test_agents_requires_auth(self):
        status, _, _ = self._req("GET", "/api/u/agents")
        self.assertEqual(status, 401)

    def test_activity_runs_surface_and_render_gracefully(self):
        # Spec 3: real run records surface per agent, scoped to the user; an agent
        # with no runs still renders (empty list, not an error).
        uid, cookie = self._signup("act@b.com")
        registry.agent_upsert(self.root, {
            "agent_id": "agent-1", "owner_user_id": uid, "external_user_id": "x",
            "bundle_id": "wf1", "name": "WF1", "vault_ref": "v", "trigger": "t", "session_id": "s"})
        registry.run_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
                                     "status": "clean", "summary": "did X",
                                     "run_session_id": "s1", "console_url": "https://c"})
        registry.run_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
                                     "status": "failed", "summary": "oops",
                                     "run_session_id": None, "console_url": "https://c"})
        registry.agent_upsert(self.root, {
            "agent_id": "agent-2", "owner_user_id": uid, "external_user_id": "x",
            "bundle_id": "wf2", "name": "WF2", "vault_ref": "v", "trigger": "t", "session_id": "s"})

        _, data, _ = self._req("GET", "/api/u/agents", cookie=cookie)
        by_id = {a["agent_id"]: a for a in data["agents"]}
        self.assertEqual({r["status"] for r in by_id["agent-1"]["recent_runs"]}, {"clean", "failed"})
        self.assertEqual(by_id["agent-2"]["recent_runs"], [])     # no runs -> graceful empty


class PendingEndpointTest(AgentsEndpointTest):
    """Spec 8: the /api/u/pending review surface — owner-scoped listing, count,
    and approve/reject. File-mode (dual-run) path."""

    def _stage(self, uid):
        registry.agent_upsert(self.root, {
            "agent_id": "agent-1", "owner_user_id": uid, "external_user_id": "x",
            "bundle_id": "wf", "name": "WF", "vault_ref": "v", "trigger": "t", "session_id": "s"})
        registry.pending_add(self.root, {
            "agent_id": "agent-1", "owner_user_id": uid, "run_session_id": "rs",
            "app": "gmail", "kind": "gmail-send-email", "summary": "Email Bob",
            "payload": {"to": "bob@x.com"}, "content_hash": "h1"})

    def test_pending_listed_and_scoped(self):
        uid_a, cookie_a = self._signup("pa@x.com")
        _, cookie_b = self._signup("pb@x.com")
        self._stage(uid_a)
        status, data, _ = self._req("GET", "/api/u/pending", cookie=cookie_a)
        self.assertEqual(status, 200)
        self.assertEqual(data["pending_count"], 1)
        self.assertEqual(data["pending"][0]["payload"], {"to": "bob@x.com"})
        # the agents endpoint exposes the badge count too
        _, ag, _ = self._req("GET", "/api/u/agents", cookie=cookie_a)
        self.assertEqual(ag["pending_count"], 1)
        # another user sees none of it
        _, data_b, _ = self._req("GET", "/api/u/pending", cookie=cookie_b)
        self.assertEqual(data_b["pending"], [])

    def test_reject_discards_without_executing(self):
        uid, cookie = self._signup("pr@x.com")
        self._stage(uid)
        item = registry.pending_for_user(self.root, uid)[0]
        status, data, _ = self._req("POST", f"/api/u/pending/{item['id']}/reject", {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(registry.pending_count_for_user(self.root, uid), 0)

    def test_pending_requires_auth_and_ownership(self):
        uid, _ = self._signup("po@x.com")
        _, other_cookie = self._signup("pother@x.com")
        self._stage(uid)
        item = registry.pending_for_user(self.root, uid)[0]
        # someone else cannot act on it
        status, _, _ = self._req("POST", f"/api/u/pending/{item['id']}/reject", {}, cookie=other_cookie)
        self.assertEqual(status, 404)
        # unauthenticated listing is rejected
        status, _, _ = self._req("GET", "/api/u/pending")
        self.assertEqual(status, 401)


class DeleteCaptureDataTest(AgentsEndpointTest):
    """User-facing "delete capture data": removes the capture session everywhere,
    purges capture-derived platform data, and keeps the live agent. File-mode."""

    def _make_session(self, uid, with_run=True):
        sid, _ = sessions.create_upload_session(
            self.root, uid, '{"ts":"t","source":"browser","payload":{}}\n')
        registry.agent_upsert(self.root, {
            "agent_id": "agent-1", "owner_user_id": uid, "external_user_id": "x",
            "bundle_id": "wf", "name": "WF", "vault_ref": "v", "trigger": "t",
            "session_id": sid})
        if with_run:                                       # a capture-derived run record
            specs = sessions.path(self.root, sid, sessions.SPECS_DIR)
            os.makedirs(specs, exist_ok=True)
            with open(os.path.join(specs, "run_1.json"), "w", encoding="utf-8") as f:
                json.dump({"sessions": [{"session_id": "test-sess"}],
                           "uploaded_file_ids": ["file_1"], "agent_id": "agent-1"}, f)
        return sid

    def test_delete_removes_session_and_keeps_agent_live(self):
        uid, cookie = self._signup("d@x.com")
        calls = []
        self.state.cleanup_fn = lambda s, f, a, agent_names=None: (
            calls.append((s, f, a, agent_names)) or {"removed": {}, "errors": []})
        sid = self._make_session(uid)
        self.assertTrue(os.path.isdir(sessions.session_dir(self.root, sid)))

        status, data, _ = self._req("POST", "/api/u/agents/agent-1/delete-capture-data",
                                    {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["deleted"])
        self.assertFalse(os.path.isdir(sessions.session_dir(self.root, sid)))   # capture gone
        # platform purge targeted derived data only — live agent preserved
        self.assertEqual(len(calls), 1)
        s_ids, f_ids, a_ids, a_names = calls[0]
        self.assertEqual(a_ids, [])                         # no live agent deleted
        self.assertEqual(a_names, [])                       # no name-sweep of live agents
        self.assertEqual(f_ids, ["file_1"])
        self.assertEqual(s_ids, ["test-sess"])
        # the agent is still listed and still runnable (registry untouched), capture gone
        _, ag, _ = self._req("GET", "/api/u/agents", cookie=cookie)
        a0 = ag["agents"][0]
        self.assertEqual(a0["agent_id"], "agent-1")
        self.assertFalse(a0["capture_present"])

    def test_delete_scoped_to_owner(self):
        uid, _ = self._signup("o1@x.com")
        _, other = self._signup("o2@x.com")
        sid = self._make_session(uid, with_run=False)
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/delete-capture-data",
                                 {}, cookie=other)
        self.assertEqual(status, 404)                       # not this user's agent
        self.assertTrue(os.path.isdir(sessions.session_dir(self.root, sid)))    # untouched

    def test_delete_proceeds_when_platform_purge_errors(self):
        uid, cookie = self._signup("be@x.com")
        def boom(*a, **k):
            raise RuntimeError("MA unreachable")
        self.state.cleanup_fn = boom
        sid = self._make_session(uid)
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/delete-capture-data",
                                    {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["deleted"])
        self.assertFalse(os.path.isdir(sessions.session_dir(self.root, sid)))   # best-effort


class ActivityEndpointTest(AgentsEndpointTest):
    """Activity view endpoints: real session summaries + per-action approval, owner-scoped."""

    def _setup_staged(self, uid):
        from trace.tests.test_scheduler import FakeMA, _PROPOSAL_BLOCK
        registry.agent_upsert(self.root, {
            "agent_id": "agent-1", "owner_user_id": uid, "external_user_id": "x",
            "bundle_id": "wf", "name": "WF", "vault_ref": "v", "trigger": "t",
            "session_id": "s", "staged_apps": ["gmail"]})
        registry.run_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
                                     "status": "ran", "summary": "ran", "details": [],
                                     "run_session_id": "sess-1"})
        ma = FakeMA(events=[{"type": "agent.message", "text": _PROPOSAL_BLOCK},
                            {"type": "session.thread_status_idle", "stop_reason": {"type": "end_turn"}}])
        self.state._live_client = lambda: ma
        self.state.summarize_fn = lambda s, u: "Checked the inbox; proposed actions."
        return ma

    def test_activity_returns_summary_and_proposals(self):
        uid, cookie = self._signup("a1@x.com")
        self._setup_staged(uid)
        status, data, _ = self._req("GET", "/api/u/agents/agent-1/activity", cookie=cookie)
        self.assertEqual(status, 200)
        act = data["activity"]
        self.assertEqual(len(act), 1)
        self.assertEqual(act[0]["summary"], "Checked the inbox; proposed actions.")
        self.assertEqual(act[0]["status"], "staged")
        self.assertEqual(len(act[0]["proposals"]), 1)

    def test_activity_scoped_to_owner(self):
        uid, _ = self._signup("o1@x.com")
        _, other = self._signup("o2@x.com")
        self._setup_staged(uid)
        status, _, _ = self._req("GET", "/api/u/agents/agent-1/activity", cookie=other)
        self.assertEqual(status, 404)
        status, _, _ = self._req("GET", "/api/u/agents/agent-1/activity")   # unauthenticated
        self.assertEqual(status, 401)

    def test_approve_actions_endpoint_accepts_selection(self):
        uid, cookie = self._signup("a3@x.com")
        self._setup_staged(uid)
        self._req("GET", "/api/u/agents/agent-1/activity", cookie=cookie)   # creates pending
        pend = registry.pending_for_user(self.root, uid)
        self.assertTrue(pend)
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/approve-actions",
                                 {"pending_ids": [pend[0]["id"]]}, cookie=cookie)
        self.assertEqual(status, 202)


class DeploymentAgentActionsTest(AgentsEndpointTest):
    """Schedule/pause/run-now on an agent backed by a platform DEPLOYMENT re-point
    the deployment instead of driving the legacy Trace scheduler."""

    def _dep_agent_for(self, uid, agent_id="agent-d"):
        registry.agent_upsert(self.root, {
            "agent_id": agent_id, "owner_user_id": uid,
            "external_user_id": users.external_user_id(uid),
            "bundle_id": "wf", "name": "WF", "vault_ref": "vlt-1",
            "trigger": "on a new task", "session_id": "sess-1",
            "deployment_id": "dep_live", "memory_store_id": "mem_live"})

    def _wire_ma(self):
        from trace.tests.test_scheduler import FakeMA, FakeConnect
        ma = FakeMA()
        ma.list_deployment_runs("boot")                      # init the fake's stores
        self.state._live_client = lambda: ma
        self.state.connect_fn = lambda: FakeConnect()
        return ma

    def test_schedule_set_repoints_deployment(self):
        uid, cookie = self._signup("dep@x.com")
        self._dep_agent_for(uid)
        ma = self._wire_ma()
        status, data, _ = self._req("POST", "/api/u/agents/agent-d/schedule",
                                    {"cadence": {"kind": "daily", "time": "09:00"}},
                                    cookie=cookie)
        self.assertEqual(status, 200, data)
        self.assertEqual(ma.archived, ["dep_live"])           # old archived, new created
        self.assertEqual(ma.created_deployments[0]["schedule"]["expression"], "0 9 * * *")
        row = next(a for a in registry.agents_all(self.root) if a["agent_id"] == "agent-d")
        self.assertEqual(row["deployment_id"], ma.created_deployments[0]["id"])

    def test_schedule_clear_creates_manual_deployment(self):
        uid, cookie = self._signup("dep2@x.com")
        self._dep_agent_for(uid)
        ma = self._wire_ma()
        status, _, _ = self._req("POST", "/api/u/agents/agent-d/schedule",
                                 {"cadence": None}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIsNone(ma.created_deployments[0]["schedule"])

    def test_schedule_platform_failure_surfaces_502(self):
        uid, cookie = self._signup("dep3@x.com")
        self._dep_agent_for(uid)
        ma = self._wire_ma()
        ma.create_deployment = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("cron expression rejected"))
        status, data, _ = self._req("POST", "/api/u/agents/agent-d/schedule",
                                    {"cadence": {"kind": "daily", "time": "09:00"}},
                                    cookie=cookie)
        self.assertEqual(status, 502)
        self.assertEqual(data["error"], "platform_schedule_failed")
        self.assertIn("cron expression rejected", data["detail"])

    def test_pause_hits_platform(self):
        uid, cookie = self._signup("dep4@x.com")
        self._dep_agent_for(uid)
        ma = self._wire_ma()
        registry.schedule_set(self.root, "agent-d", uid, {"kind": "daily", "time": "09:00"})
        status, _, _ = self._req("POST", "/api/u/agents/agent-d/schedule/pause",
                                 {"paused": True}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(ma.paused_deps, ["dep_live"])
        status, _, _ = self._req("POST", "/api/u/agents/agent-d/schedule/pause",
                                 {"paused": False}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(ma.unpaused_deps, ["dep_live"])

    def test_run_now_triggers_deployment_run(self):
        uid, cookie = self._signup("dep5@x.com")
        self._dep_agent_for(uid)
        ma = self._wire_ma()
        status, _, _ = self._req("POST", "/api/u/agents/agent-d/run-now", {}, cookie=cookie)
        self.assertEqual(status, 202)
        for _ in range(100):                                  # async thread
            if ma.manual_runs:
                break
            time.sleep(0.02)
        self.assertEqual(ma.manual_runs, ["dep_live"])
        self.assertEqual(ma.sessions, [])                     # no legacy session composed
        runs = []
        for _ in range(100):     # the run record is written just AFTER manual_runs, same thread
            runs = registry.runs_for_agent(self.root, "agent-d")
            if runs:
                break
            time.sleep(0.02)
        self.assertEqual(runs[0]["status"], "ran")

    def test_legacy_agent_run_now_unchanged(self):
        uid, cookie = self._signup("leg@x.com")
        registry.agent_upsert(self.root, {
            "agent_id": "agent-l", "owner_user_id": uid,
            "external_user_id": users.external_user_id(uid), "bundle_id": "wf",
            "name": "WF", "vault_ref": "vlt-1", "trigger": "t", "session_id": "s"})
        ma = self._wire_ma()
        status, _, _ = self._req("POST", "/api/u/agents/agent-l/run-now", {}, cookie=cookie)
        self.assertEqual(status, 202)
        for _ in range(100):
            if ma.sessions:
                break
            time.sleep(0.02)
        self.assertTrue(ma.sessions)                          # legacy path composed a session
        self.assertEqual(ma.manual_runs, [])


if __name__ == "__main__":
    unittest.main()
