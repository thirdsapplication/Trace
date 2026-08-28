"""Spec 2 — re-runnable pipeline: the durable per-user agent index and the
aggregate /api/u/agents endpoint, exercised on the file-mode (no-DATABASE_URL)
path so it doubles as a dual-run check. Two deploys add two agents (no clobber),
scoped to the owner.
"""

import datetime
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from trace.ui import app, registry, sessions, users
from trace.tests.test_scheduler import FakeMA
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
        # The first load answers from the DB and kicks the enrichment off a thread
        # (reading session events and summarizing them is what used to make this
        # request slow); the enriched rows land on the next load.
        status, data, _ = self._req("GET", "/api/u/agents/agent-1/activity", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["refreshing"])
        self.state.flush_activity_refresh()
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
        self.state.flush_activity_refresh()                 # enrichment is backgrounded
        pend = registry.pending_for_user(self.root, uid)
        self.assertTrue(pend)
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/approve-actions",
                                 {"pending_ids": [pend[0]["id"]]}, cookie=cookie)
        self.assertEqual(status, 202)
        # The 202 is fire-and-forget; wait out the worker thread, then take the exact
        # user path that used to resurrect the button — reload the activity view and
        # look for anything still approvable.
        deadline = time.time() + 2
        st = None
        while time.time() < deadline:
            st = registry.pending_get(self.root, pend[0]["id"])["status"]
            if st not in ("pending", "queued", "executing"):
                break
            time.sleep(0.02)
        self.assertEqual(st, "executed")
        status, data, _ = self._req("GET", "/api/u/agents/agent-1/activity", cookie=cookie)
        self.assertEqual(status, 200)
        live = [p for e in data["activity"] for p in e["proposals"]
                if p["status"] == "pending"]
        self.assertEqual(live, [])


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


class SerialMA(FakeMA):
    """FakeMA that records how many sends overlap and in what order, with an
    optional per-agent gate so a test can hold one agent's send open."""
    def __init__(self, *args, delay=0.05, **kw):
        super().__init__(*args, **kw)
        self.delay = delay
        self.gates = {}              # agent_id -> Event the send blocks on until set
        self.sid_agent = {}          # run-sess-N -> agent_id
        self.active = 0
        self.max_active = 0
        self.send_order = []         # trigger texts, in actual send order
        self._track = threading.Lock()

    def create_session(self, agent_id, env, resources, title=None, vault_ids=None):
        sid = super().create_session(agent_id, env, resources, title, vault_ids)
        self.sid_agent[sid] = agent_id
        return sid

    def send_trigger_and_collect(self, sid, trigger, timeout=None, max_events=None):
        with self._track:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.send_order.append(trigger)
        gate = self.gates.get(self.sid_agent.get(sid))
        if gate is not None:
            gate.wait(timeout=10)
        elif self.delay:
            time.sleep(self.delay)
        with self._track:
            self.active -= 1
        return super().send_trigger_and_collect(sid, trigger, timeout, max_events)


class ApprovalQueueTest(AgentsEndpointTest):
    """Two quick approvals used to run their MA sessions concurrently (one bare
    thread each). They now drain through a per-agent FIFO queue: same agent =
    strictly one session at a time in approval order; different agents stay
    independent."""

    def _stage_two(self, uid, agent_id="agent-1"):
        registry.agent_upsert(self.root, {
            "agent_id": agent_id, "owner_user_id": uid, "external_user_id": "x",
            "bundle_id": "wf", "name": f"WF {agent_id}", "vault_ref": "v",
            "trigger": "t", "session_id": "s"})
        for i in (1, 2):
            registry.pending_add(self.root, {
                "agent_id": agent_id, "owner_user_id": uid, "run_session_id": "rs",
                "app": "gmail", "kind": "gmail-send-email",
                "summary": f"Email {agent_id}-{i}",
                "payload": {"to": f"{i}@x.com"}, "content_hash": f"{agent_id}-h{i}"})
        pend = sorted((p for p in registry.pending_for_user(self.root, uid)
                       if p["agent_id"] == agent_id), key=lambda p: int(p["id"]))
        return [p["id"] for p in pend]

    def _wait_for_send(self, ma, deadline_s=5):
        deadline = time.time() + deadline_s
        while time.time() < deadline and ma.active == 0:
            time.sleep(0.01)
        self.assertEqual(ma.active, 1)                        # a send is in flight

    def test_same_agent_approvals_serialize_fifo(self):
        uid, cookie = self._signup("q1@x.com")
        id1, id2 = self._stage_two(uid)
        ma = SerialMA()
        self.state._live_client = lambda: ma
        s1, _, _ = self._req("POST", f"/api/u/pending/{id1}/approve", {}, cookie=cookie)
        s2, _, _ = self._req("POST", f"/api/u/pending/{id2}/approve", {}, cookie=cookie)
        self.assertEqual((s1, s2), (202, 202))
        self.assertTrue(self.state.flush_approvals())
        self.assertEqual(ma.max_active, 1)                    # sessions never overlapped
        self.assertIn("Email agent-1-1", ma.send_order[0])    # …and ran in approval order
        self.assertIn("Email agent-1-2", ma.send_order[1])
        for pid in (id1, id2):
            self.assertEqual(registry.pending_get(self.root, pid)["status"], "executed")

    def test_second_approval_waits_as_queued(self):
        uid, cookie = self._signup("q2@x.com")
        id1, id2 = self._stage_two(uid)
        ma = SerialMA()
        gate = threading.Event()
        ma.gates["agent-1"] = gate
        self.state._live_client = lambda: ma
        try:
            self._req("POST", f"/api/u/pending/{id1}/approve", {}, cookie=cookie)
            self._wait_for_send(ma)
            self._req("POST", f"/api/u/pending/{id2}/approve", {}, cookie=cookie)
            # first is mid-send, so the second sits honestly queued, not executing
            self.assertEqual(registry.pending_get(self.root, id1)["status"], "executing")
            self.assertEqual(registry.pending_get(self.root, id2)["status"], "queued")
        finally:
            gate.set()
        self.assertTrue(self.state.flush_approvals())
        for pid in (id1, id2):
            self.assertEqual(registry.pending_get(self.root, pid)["status"], "executed")

    def test_different_agents_run_independently(self):
        uid, cookie = self._signup("q3@x.com")
        a1, _ = self._stage_two(uid, agent_id="agent-1")
        b1, _ = self._stage_two(uid, agent_id="agent-2")
        ma = SerialMA(delay=0)
        gate = threading.Event()
        ma.gates["agent-1"] = gate
        self.state._live_client = lambda: ma
        try:
            self._req("POST", f"/api/u/pending/{a1}/approve", {}, cookie=cookie)
            self._wait_for_send(ma)
            self._req("POST", f"/api/u/pending/{b1}/approve", {}, cookie=cookie)
            deadline = time.time() + 5
            st = None
            while time.time() < deadline:
                st = registry.pending_get(self.root, b1)["status"]
                if st == "executed":
                    break
                time.sleep(0.01)
            self.assertEqual(st, "executed")     # agent-2 ran while agent-1 was held
            self.assertEqual(registry.pending_get(self.root, a1)["status"], "executing")
        finally:
            gate.set()
        self.assertTrue(self.state.flush_approvals())
        self.assertEqual(registry.pending_get(self.root, a1)["status"], "executed")

    def test_double_click_still_409(self):
        uid, cookie = self._signup("q4@x.com")
        id1, _ = self._stage_two(uid)
        ma = SerialMA()
        self.state._live_client = lambda: ma
        s1, _, _ = self._req("POST", f"/api/u/pending/{id1}/approve", {}, cookie=cookie)
        s2, d2, _ = self._req("POST", f"/api/u/pending/{id1}/approve", {}, cookie=cookie)
        self.assertEqual(s1, 202)
        self.assertEqual(s2, 409)                             # claim-to-queued is the guard
        self.assertEqual(d2["error"], "already_processed")
        self.assertTrue(self.state.flush_approvals())

    def test_batch_approve_runs_once_through_queue(self):
        uid, cookie = self._signup("q5@x.com")
        id1, id2 = self._stage_two(uid)
        ma = SerialMA(delay=0.2)
        self.state._live_client = lambda: ma
        s1, _, _ = self._req("POST", "/api/u/agents/agent-1/approve-actions",
                             {"pending_ids": [id1, id2]}, cookie=cookie)
        # both rows were claimed to "queued" before the 202 returned, so a repeat
        # click claims nothing and enqueues nothing — yet still answers 202
        s2, _, _ = self._req("POST", "/api/u/agents/agent-1/approve-actions",
                             {"pending_ids": [id1, id2]}, cookie=cookie)
        self.assertEqual((s1, s2), (202, 202))
        self.assertTrue(self.state.flush_approvals())
        self.assertEqual(len(ma.sessions), 1)                 # ONE batch session, not two
        for pid in (id1, id2):
            self.assertEqual(registry.pending_get(self.root, pid)["status"], "executed")

    def test_worker_survives_bad_task(self):
        ran = threading.Event()
        def boom():
            raise RuntimeError("bad task")
        self.state.enqueue_approval("agent-x", boom)
        self.state.enqueue_approval("agent-x", ran.set)
        self.assertTrue(self.state.flush_approvals())
        self.assertTrue(ran.is_set())                         # drain outlived the bad task


class QueuedReaperTest(AgentsEndpointTest):
    """The stale-"executing" reaper on the activity read path must never settle a
    row that is only waiting in the approval queue — queue wait is not a crash."""

    _TWO_ACTIONS = ('```proposed_actions\n'
                    '[{"app": "Gmail", "action": "send email", "summary": "Email One", '
                    '"details": {"to": "one@x.com"}}, '
                    '{"app": "Gmail", "action": "send email", "summary": "Email Two", '
                    '"details": {"to": "two@x.com"}}]\n```')

    def test_reaper_leaves_queued_rows_alone(self):
        uid, cookie = self._signup("qr@x.com")
        registry.agent_upsert(self.root, {
            "agent_id": "agent-1", "owner_user_id": uid, "external_user_id": "x",
            "bundle_id": "wf", "name": "WF", "vault_ref": "v", "trigger": "t",
            "session_id": "s", "staged_apps": ["gmail"]})
        registry.run_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
                                     "status": "ran", "summary": "ran", "details": [],
                                     "run_session_id": "sess-1"})
        ma = SerialMA(events=[{"type": "agent.message", "text": self._TWO_ACTIONS},
                              {"type": "session.thread_status_idle",
                               "stop_reason": {"type": "end_turn"}}])
        gate = threading.Event()
        ma.gates["agent-1"] = gate
        self.state._live_client = lambda: ma
        self.state.summarize_fn = lambda s, u: "x"
        try:
            self._req("GET", "/api/u/agents/agent-1/activity", cookie=cookie)
            self.state.flush_activity_refresh()             # staging is backgrounded now
            id1, id2 = sorted((p["id"] for p in registry.pending_for_user(self.root, uid)),
                              key=int)
            self._req("POST", f"/api/u/pending/{id1}/approve", {}, cookie=cookie)
            deadline = time.time() + 5
            while time.time() < deadline and ma.active == 0:
                time.sleep(0.01)
            self._req("POST", f"/api/u/pending/{id2}/approve", {}, cookie=cookie)
            self.assertEqual(registry.pending_get(self.root, id2)["status"], "queued")
            # Backdate the queued row past the 15-minute stale-executing window,
            # then reload the activity view — the read path that reaps. Safe to
            # write the file directly: the worker is held inside the gated send.
            path = os.path.join(self.root, "_registry", "pending.json")
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            d[str(id2)]["processed_at"] = (
                datetime.datetime.now().astimezone()
                - datetime.timedelta(minutes=16)).isoformat(timespec="seconds")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(d, f)
            _, data, _ = self._req("GET", "/api/u/agents/agent-1/activity", cookie=cookie)
            self.assertEqual(registry.pending_get(self.root, id2)["status"], "queued")
            statuses = {p["id"]: p["status"]
                        for e in data["activity"] for p in e["proposals"]}
            self.assertEqual(statuses[id2], "queued")         # rendered as waiting, not failed
        finally:
            gate.set()
        self.assertTrue(self.state.flush_approvals())
        for pid in (id1, id2):
            self.assertEqual(registry.pending_get(self.root, pid)["status"], "executed")


class RunMappingTest(unittest.TestCase):
    """Every field the UI needs must survive _run_to_caller.

    This is the fourth instance of one bug: a column written faithfully and never
    mapped back, so the data exists in Supabase and is invisible to every caller.
    color, owner_email and the two consent scopes went the same way in
    _agent_to_caller. File mode returns the raw row, so the suite passes either way
    and only a Supabase deployment shows the gap — which is exactly why this asserts
    on the mapping function rather than on behaviour.
    """

    def test_both_timestamps_survive(self):
        row = {"started_at": "2026-08-26T06:00:00Z", "ended_at": "2026-08-26T06:09:00Z",
               "status": "clean", "summary": "s", "run_session_id": "sess", "kind": None}
        out = registry._run_to_caller(row)
        self.assertEqual(out["started_at"], "2026-08-26T06:00:00Z")
        self.assertEqual(out["ended_at"], "2026-08-26T06:09:00Z",
                         "ended_at dropped again — run duration becomes uncomputable")
        self.assertEqual(out["ran_at"], "2026-08-26T06:00:00Z")   # kept for old callers

    def test_a_run_still_in_flight_maps_cleanly(self):
        out = registry._run_to_caller({"started_at": "2026-08-26T06:00:00Z",
                                       "status": "running"})
        self.assertIsNone(out["ended_at"])


class AgentWriteMapTest(unittest.TestCase):
    """_agent_to_sb is an allow-list, so a field it does not name is dropped in
    silence — the write-side twin of the _agent_to_caller bugs.

    Deploy state was the casualty: Supabase agents recorded a deployment_id and
    never that they had gone live, so all 13 production agents read back as
    status='draft' with a null deployed_at. File mode stamps deployed_at on its own
    line, so the suite was green throughout. These assert on the map directly,
    because behaviour is precisely what hides this.
    """

    def test_deploy_state_survives_the_write_map(self):
        sb = registry._agent_to_sb({
            "agent_id": "a1", "owner_user_id": "u1", "name": "Shipping",
            "status": "live", "deployed_at": "2026-08-26T10:00:00Z"})
        # "deployed", not "live": the column's CHECK constraint allows
        # draft|built|deployed|archived, and sending the deploy record's word for the
        # same state is what refused every go-live for two days. The state still
        # survives the map, which is what this test is for — it just arrives in the
        # vocabulary the column actually has.
        self.assertEqual(sb["status"], "deployed",
                         "status dropped — every agent reads back as draft")
        self.assertEqual(sb["deployed_at"], "2026-08-26T10:00:00Z",
                         "deployed_at dropped — 'which agents are live?' is unanswerable")

    def test_the_identity_columns_still_map(self):
        sb = registry._agent_to_sb({"agent_id": "a1", "owner_user_id": "u1",
                                    "owner_email": "x@y.com", "deployment_id": "d1"})
        self.assertEqual(sb["id"], "a1")
        self.assertEqual(sb["owner_id"], "u1")
        self.assertEqual(sb["owner_email"], "x@y.com")
        self.assertEqual(sb["deployment_id"], "d1")

    def test_absent_fields_are_omitted_not_nulled(self):
        """An upsert that nulls a column it wasn't asked to touch is worse than one
        that drops it — it would erase a name on any partial write."""
        sb = registry._agent_to_sb({"agent_id": "a1", "status": "live"})
        self.assertNotIn("name", sb)
        self.assertNotIn("deployed_at", sb)


if __name__ == "__main__":
    unittest.main()


class AgentUpsertResilienceTest(unittest.TestCase):
    """A deploy that reaches the registry has already created a LIVE platform agent.
    Losing the row there strands it: present in the Managed Agents console, absent
    from the app, with the user pressing Go live again to no effect.

    That is not hypothetical. deploy.py writes status="live"; the agents table has
    carried a CHECK constraint rejecting that value since before this repo, and from
    2026-08-26 (when status began being written) every single go-live failed this way.
    """

    class _Boom(Exception):
        def __init__(self, code="23514"):
            super().__init__({"message": 'violates check constraint '
                                         '"agents_status_check"', "code": code})
            self.code = code

    def _fake_admin(self, reject_when):
        calls = []

        class Table:
            def upsert(self, row, on_conflict=None):
                calls.append(dict(row))
                if reject_when(row):
                    raise AgentUpsertResilienceTest._Boom()
                return self

            def execute(self):
                return type("R", (), {"data": []})()

        class Admin:
            def table(self, _name):
                return Table()

        return Admin(), calls

    def _run(self, admin):
        real_conf, real_admin = registry.supa.is_configured, registry.supa.get_admin
        registry.supa.is_configured = lambda: True
        registry.supa.get_admin = lambda: admin
        try:
            registry.agent_upsert("/tmp", {
                "agent_id": "agent_1", "owner_user_id": "u1", "name": "A",
                "bundle_id": "B1", "status": "live", "deployed_at": "t"})
        finally:
            registry.supa.is_configured, registry.supa.get_admin = real_conf, real_admin

    def test_the_agent_is_registered_even_when_status_is_rejected(self):
        admin, calls = self._fake_admin(lambda row: "status" in row)
        self._run(admin)
        self.assertEqual(len(calls), 2, "it must retry without the optional columns")
        self.assertNotIn("status", calls[1])
        self.assertNotIn("deployed_at", calls[1])
        self.assertEqual(calls[1]["id"], "agent_1", "the agent is still registered")
        self.assertEqual(calls[1]["name"], "A")

    def test_any_other_error_still_raises(self):
        class Admin:
            def table(self, _name):
                raise RuntimeError("connection refused")
        with self.assertRaises(RuntimeError):
            self._run(Admin())

    def test_a_check_violation_unrelated_to_those_columns_still_raises(self):
        admin, _ = self._fake_admin(lambda row: True)      # rejects even without them
        with self.assertRaises(Exception):
            self._run(admin)


class AgentStatusVocabularyTest(unittest.TestCase):
    """agents.status is constrained to draft | built | deployed | archived. The deploy
    record calls the live state "live", and sending that word is what refused every
    go-live from 2026-08-26 — after the platform agent had already been created."""

    def test_live_is_written_as_the_value_the_column_allows(self):
        sb = registry._agent_to_sb({"agent_id": "a1", "status": "live"})
        self.assertEqual(sb["status"], "deployed")

    def test_the_columns_own_vocabulary_passes_through(self):
        for word in ("draft", "built", "deployed", "archived"):
            self.assertEqual(registry._agent_to_sb({"status": word})["status"], word)

    def test_an_unknown_status_is_dropped_rather_than_refused(self):
        sb = registry._agent_to_sb({"agent_id": "a1", "name": "A", "status": "wat"})
        self.assertNotIn("status", sb)
        self.assertEqual(sb["name"], "A", "the rest of the row still goes")

    def test_the_constraints_vocabulary_is_covered(self):
        """If the column ever gains a state, this is where the mapping must learn it."""
        self.assertEqual(set(registry.AGENT_STATUS_DB.values()),
                         {"draft", "built", "deployed", "archived"})
