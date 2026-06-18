"""Spec 2 — re-runnable pipeline: the durable per-user agent index and the
aggregate /api/u/agents endpoint, exercised on the file-mode (no-DATABASE_URL)
path so it doubles as a dual-run check. Two deploys add two agents (no clobber),
scoped to the owner.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from trace.ui import app, registry, users
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


if __name__ == "__main__":
    unittest.main()
