"""Instagram-style "My agents" backend: DM store (dual-path), the chat runner
(user -> agent message gets a REAL reply from a fresh session), agent-initiated
DMs on actionable outcomes, and the /api/u/home stories+feed read. Offline:
file-mode registry + FakeMA/FakeConnect stubs + real HTTP server.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from trace.tests.test_scheduler import FakeConnect, FakeMA, _agent, _dep_agent
from trace.tests.test_ui import FakeController, FakeKeystore, fake_reconstruct
from trace.ui import app, registry, users


class MessageStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_thread_order_read_and_unread_counts(self):
        for i, (sender, read) in enumerate([("agent", False), ("user", True), ("agent", False)]):
            registry.message_add(self.root, {"agent_id": "a1", "owner_user_id": "u1",
                                             "sender": sender, "text": f"m{i}", "read": read})
        registry.message_add(self.root, {"agent_id": "a2", "owner_user_id": "u1",
                                         "sender": "agent", "text": "other"})
        registry.message_add(self.root, {"agent_id": "a1", "owner_user_id": "u2",
                                         "sender": "agent", "text": "not mine"})
        thread = registry.messages_for_agent(self.root, "u1", "a1")
        self.assertEqual([m["text"] for m in thread], ["m0", "m1", "m2"])   # oldest-first
        self.assertEqual(registry.messages_unread_counts(self.root, "u1"), {"a1": 2, "a2": 1})
        registry.messages_mark_read(self.root, "u1", "a1")
        self.assertEqual(registry.messages_unread_counts(self.root, "u1"), {"a2": 1})
        # the user's own messages never count as unread
        self.assertEqual(registry.messages_unread_counts(self.root, "u2"), {"a1": 1})


class ChatAndDMTest(unittest.TestCase):
    """AppState-level: the chat runner + proactive agent DMs on run outcomes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(self.root, key_store=FakeKeystore())
        self.ma = FakeMA()
        self.state._live_client = lambda: self.ma
        self.state.connect_fn = lambda: FakeConnect()

    def tearDown(self):
        self.state.scheduler_stop.set()
        self._tmp.cleanup()

    def _await_chat(self, agent_id):
        for _ in range(200):
            if (self.state.chat_jobs.get(agent_id) or {}).get("state") != "running":
                return
            time.sleep(0.02)

    def test_send_message_gets_real_agent_reply(self):
        agent = _dep_agent()
        self.state.send_agent_message(agent, "What did you do today?")
        self._await_chat("agent-d1")
        thread = registry.messages_for_agent(self.root, "usr_1", "agent-d1")
        self.assertEqual([m["sender"] for m in thread], ["user", "agent"])
        self.assertEqual(thread[0]["text"], "What did you do today?")
        self.assertEqual(thread[1]["text"], "Handled 2 emails")    # the session's final message
        # the chat session carried the memory store + the chat framing with the message
        self.assertEqual(self.ma.sessions[0]["resources"][0]["memory_store_id"], "mem_live")
        self.assertIn("DIRECT MESSAGE", self.ma.triggers[0])
        self.assertIn("What did you do today?", self.ma.triggers[0])

    def test_chat_failure_still_replies_apologetically(self):
        self.ma.fail = True                                        # create_session raises
        self.state.send_agent_message(_agent(), "hello?")
        self._await_chat("agent-1")
        thread = registry.messages_for_agent(self.root, "usr_1", "agent-1")
        self.assertEqual(thread[1]["sender"], "agent")
        self.assertIn("couldn't reply", thread[1]["text"].lower())  # thread never goes silent
        self.assertEqual(self.state.chat_jobs["agent-1"]["state"], "error")

    def test_failed_run_sends_agent_dm(self):
        self.ma.fail = True
        registry.agent_upsert(self.root, _agent())
        self.state.run_agent(_agent())
        thread = registry.messages_for_agent(self.root, "usr_1", "agent-1")
        self.assertEqual(len(thread), 1)
        self.assertEqual(thread[0]["sender"], "agent")
        self.assertIn("problem", thread[0]["text"])
        self.assertFalse(thread[0]["read"])                        # unread -> badge

    def test_staged_run_sends_approval_dm(self):
        from trace.tests.test_scheduler import _PROPOSAL_BLOCK
        self.ma.events = [{"type": "agent.message", "text": _PROPOSAL_BLOCK},
                          {"type": "session.status_idle", "stop_reason": "end_turn", "text": ""}]
        self.state.run_agent(_agent(staged_apps=["gmail"]))
        thread = registry.messages_for_agent(self.root, "usr_1", "agent-1")
        self.assertTrue(any("approval" in m["text"] for m in thread))

    def test_completed_run_sends_no_dm(self):
        self.state.run_agent(_agent())                             # default: completes fine
        self.assertEqual(registry.messages_for_agent(self.root, "usr_1", "agent-1"), [])

    def test_deployment_run_failure_sends_dm(self):
        self.ma.run_deployment = lambda dep_id: (_ for _ in ()).throw(RuntimeError("boom"))
        self.state.run_deployment_now(_dep_agent())
        thread = registry.messages_for_agent(self.root, "usr_1", "agent-d1")
        self.assertTrue(thread and "problem" in thread[0]["text"])


class HomeAndMessagesEndpointTest(unittest.TestCase):
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
        self.ma = FakeMA()
        self.state._live_client = lambda: self.ma
        self.state.connect_fn = lambda: FakeConnect()
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

    def _seed_agent(self, uid, agent_id="agent-1", name="Order intake"):
        registry.agent_upsert(self.root, {
            "agent_id": agent_id, "owner_user_id": uid,
            "external_user_id": users.external_user_id(uid), "bundle_id": "wf",
            "name": name, "vault_ref": "vlt-1", "trigger": "t", "session_id": "s"})

    def _write_run(self, agent_id, uid, ran_at, status="completed", summary="Did the thing.",
                   run_session_id=None):
        # file-mode run_add stamps its own ran_at; write the jsonl directly so the
        # 24h stories window can be tested with an OLD run.
        p = os.path.join(registry._dir(self.root), f"runs_{registry._safe(agent_id)}.jsonl")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"agent_id": agent_id, "owner_user_id": uid,
                                "status": status, "summary": summary, "details": [],
                                "run_session_id": run_session_id, "ran_at": ran_at}) + "\n")

    def test_home_stories_window_feed_and_unread(self):
        import datetime
        uid, cookie = self._signup("ig@x.com")
        self._seed_agent(uid)
        now = datetime.datetime.now().astimezone()
        fresh = (now - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
        stale = (now - datetime.timedelta(hours=30)).isoformat(timespec="seconds")
        self._write_run("agent-1", uid, stale, summary="Old news.")
        self._write_run("agent-1", uid, fresh, summary="Fresh note.", run_session_id="sess-f")
        registry.summary_set(self.root, "sess-f", "Read 3 emails and logged 2 orders.", "completed")
        registry.message_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
                                         "sender": "agent", "text": "hi"})
        status, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        self.assertEqual(status, 200)
        a = data["agents"][0]
        # stories = last 24h only, and the cached session summary is the note
        self.assertEqual(len(a["stories"]), 1)
        self.assertEqual(a["stories"][0]["note"], "Read 3 emails and logged 2 orders.")
        self.assertEqual(a["unread"], 1)
        self.assertEqual(data["unread_total"], 1)
        # the feed carries BOTH runs (it is history, not a 24h window), newest first
        self.assertEqual([f["note"] for f in data["feed"]][:2],
                         ["Read 3 emails and logged 2 orders.", "Old news."])
        self.assertEqual(data["feed"][0]["agent_name"], "Order intake")

    def test_home_requires_auth_and_scopes(self):
        status, data, _ = self._req("GET", "/api/u/home")
        self.assertEqual(status, 401)
        uid, cookie = self._signup("mine@x.com")
        other_uid, _ = self._signup("other@x.com")
        self._seed_agent(other_uid, agent_id="agent-theirs")
        status, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["agents"], [])                       # never another user's agents

    def test_thread_endpoints_send_read_and_scope(self):
        uid, cookie = self._signup("dm@x.com")
        self._seed_agent(uid)
        # empty message rejected
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/messages",
                                    {"text": "  "}, cookie=cookie)
        self.assertEqual((status, data["error"]), (400, "empty_message"))
        # send -> 202, reply lands asynchronously
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/messages",
                                 {"text": "status?"}, cookie=cookie)
        self.assertEqual(status, 202)
        for _ in range(200):
            if (self.state.chat_jobs.get("agent-1") or {}).get("state") != "running":
                break
            time.sleep(0.02)
        status, data, _ = self._req("GET", "/api/u/agents/agent-1/messages", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([m["sender"] for m in data["messages"]], ["user", "agent"])
        self.assertFalse(data["replying"])
        # the agent's reply is unread until the thread is marked read
        _, home, _ = self._req("GET", "/api/u/home", cookie=cookie)
        self.assertEqual(home["unread_total"], 1)
        self._req("POST", "/api/u/agents/agent-1/messages/read", {}, cookie=cookie)
        _, home, _ = self._req("GET", "/api/u/home", cookie=cookie)
        self.assertEqual(home["unread_total"], 0)
        # another user cannot see or message this agent
        _, other_cookie = self._signup("intruder@x.com")[1], None
        other_cookie = self._req("POST", "/api/auth/login",
                                 {"email": "intruder@x.com", "password": "password1"})[2]
        status, _, _ = self._req("GET", "/api/u/agents/agent-1/messages", cookie=other_cookie)
        self.assertEqual(status, 404)
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/messages",
                                 {"text": "hi"}, cookie=other_cookie)
        self.assertEqual(status, 404)

    def test_send_while_replying_conflicts(self):
        uid, cookie = self._signup("busy@x.com")
        self._seed_agent(uid)
        self.state.chat_jobs["agent-1"] = {"state": "running", "error": None}
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/messages",
                                    {"text": "again"}, cookie=cookie)
        self.assertEqual((status, data["error"]), (409, "already_replying"))


if __name__ == "__main__":
    unittest.main()
