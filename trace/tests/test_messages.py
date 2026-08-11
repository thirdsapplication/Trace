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
from trace.ui import app, registry, sessions, users


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

    def test_needs_input_run_sends_question_dm(self):
        from trace.tests.test_scheduler import _NEEDS_INPUT_BLOCK
        self.ma.events = [{"type": "agent.message", "text": _NEEDS_INPUT_BLOCK},
                          {"type": "session.status_idle", "stop_reason": "end_turn", "text": ""}]
        self.state.run_agent(_agent())
        thread = registry.messages_for_agent(self.root, "usr_1", "agent-1")
        self.assertTrue(any("need your call" in m["text"] for m in thread))
        self.assertTrue(any("ship date" in m["text"] for m in thread))

    def test_needs_input_run_persists_answerable_decision(self):
        from trace.tests.test_scheduler import _NEEDS_INPUT_BLOCK
        self.ma.events = [{"type": "agent.message", "text": _NEEDS_INPUT_BLOCK},
                          {"type": "session.status_idle", "stop_reason": "end_turn", "text": ""}]
        self.state.run_agent(_agent())
        decs = registry.decisions_for_user(self.root, "usr_1", status="pending")
        self.assertEqual(len(decs), 1)
        self.assertIn("ship date", decs[0]["question"])
        self.assertEqual(decs[0]["context"]["options"], ["Assume 5-day", "Ask the buyer"])

    def test_resume_after_answer_seeds_trigger_and_resolves(self):
        registry.decision_add(self.root, {
            "agent_id": "agent-1", "owner_user_id": "usr_1", "run_session_id": "s0",
            "question": "Assume 5-day or ask the buyer?",
            "context": {"context": "no ship date on PO-1234", "options": []}})
        dec = registry.decisions_for_user(self.root, "usr_1")[0]
        self.state.resume_after_answer(_agent(), dec, "Assume standard 5-day")
        self.assertIn("Assume standard 5-day", self.ma.triggers[-1])
        self.assertIn("previously paused", self.ma.triggers[-1])
        self.assertIn("no ship date on PO-1234", self.ma.triggers[-1])
        self.assertEqual(registry.decision_get(self.root, dec["id"])["status"], "resolved")

    def test_learn_from_decision_generalizes_and_embeds(self):
        from unittest import mock
        self.state.summarize_fn = lambda system, user: (
            '{"reusable": true, "lesson": "When an order lacks a ship date, ask the customer."}')
        registry.decision_add(self.root, {
            "agent_id": "agent-1", "owner_user_id": "usr_1", "run_session_id": "s0",
            "question": "no ship date?", "context": {"context": "PO omits date", "options": []}})
        dec = registry.decisions_for_user(self.root, "usr_1")[0]
        with mock.patch("trace.ui.app.agent_embeddings.embed",
                        return_value=[[0.2] * 1024]) as emb:
            self.state.resume_after_answer(_agent(), dec, "Ask the buyer for a date")
        stored = registry.decision_get(self.root, dec["id"])
        self.assertTrue(stored["reusable"])
        self.assertIn("ship date", stored["generalized_lesson"])
        self.assertEqual(len(registry.decision_embedding_get(self.root, dec["id"])), 1024)
        emb.assert_called()   # embed is used for both B4 retrieval and the B3 learn step

    def _learn_and_get(self, uid="usr_1"):
        from unittest import mock
        registry.decision_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
            "run_session_id": "s0", "question": "q", "context": {}})
        dec = registry.decisions_for_user(self.root, uid)[0]
        with mock.patch("trace.ui.app.agent_embeddings.embed", return_value=[[0.2] * 1024]):
            self.state.resume_after_answer(_agent(), dec, "ans")
        return registry.decision_get(self.root, dec["id"])

    def test_learn_sets_global_verified_when_consented_and_clean(self):
        def mf(system, user):
            return ('{"safe": true}' if "privacy gate" in system
                    else '{"reusable": true, "lesson": "A clean general rule."}')
        self.state.summarize_fn = mf                      # contribute scope defaults to global
        self.assertTrue(self._learn_and_get()["global_verified"])

    def test_learn_no_global_verify_when_scrub_flags_pii(self):
        def mf(system, user):
            return ('{"safe": false}' if "privacy gate" in system
                    else '{"reusable": true, "lesson": "Rule mentioning bob@x.com."}')
        self.state.summarize_fn = mf
        self.assertFalse(self._learn_and_get()["global_verified"])

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
            "name": name, "vault_ref": "vlt-1", "trigger": "a new order arrives",
            "session_id": "s",
            "description": "Reads order emails and logs them in the tracker."})

    def _write_run(self, agent_id, uid, ran_at, status="completed", summary="Did the thing.",
                   run_session_id=None):
        # file-mode run_add stamps its own ran_at; write the jsonl directly so the
        # 24h stories window can be tested with an OLD run.
        p = os.path.join(registry._dir(self.root), f"runs_{registry._safe(agent_id)}.jsonl")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"agent_id": agent_id, "owner_user_id": uid,
                                "status": status, "summary": summary, "details": [],
                                "run_session_id": run_session_id, "ran_at": ran_at}) + "\n")

    def test_home_stories_are_unreviewed_sessions_and_review_moves_them_out(self):
        import datetime
        uid, cookie = self._signup("ig@x.com")
        self._seed_agent(uid)
        now = datetime.datetime.now().astimezone()
        older = (now - datetime.timedelta(hours=30)).isoformat(timespec="seconds")
        newer = (now - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
        self._write_run("agent-1", uid, older, summary="Old run.")   # no session -> synthetic key
        self._write_run("agent-1", uid, newer, summary="Fresh note.", run_session_id="sess-f")
        registry.summary_set(self.root, "sess-f", "Read 3 emails and logged 2 orders.", "completed")
        registry.message_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
                                         "sender": "agent", "text": "hi"})
        status, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        self.assertEqual(status, 200)
        a = data["agents"][0]
        # BOTH sessions are stories (age is irrelevant — unreviewed is what counts),
        # oldest first for review order; the cached summary is the newer one's note
        self.assertEqual([s["note"] for s in a["stories"]],
                         ["Old run.", "Read 3 emails and logged 2 orders."])
        self.assertEqual(a["unreviewed"], 2)
        self.assertEqual(a["description"], "Reads order emails and logs them in the tracker.")
        self.assertEqual(a["unread"], 1)
        self.assertEqual(data["unread_total"], 1)
        self.assertNotIn("feed", data)                       # no global feed — history lives per agent
        # reviewing one story (by its key) moves it out; the other remains
        keys = [s["key"] for s in a["stories"]]
        self.assertIn("sess-f", keys)                        # session-keyed story
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/reviews",
                                 {"key": "sess-f"}, cookie=cookie)
        self.assertEqual(status, 200)
        _, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        a = data["agents"][0]
        self.assertEqual([s["note"] for s in a["stories"]], ["Old run."])
        # reviewing the synthetic-keyed run empties the queue
        self._req("POST", "/api/u/agents/agent-1/reviews",
                  {"key": a["stories"][0]["key"]}, cookie=cookie)
        _, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        self.assertEqual(data["agents"][0]["unreviewed"], 0)

    def test_story_with_pending_proposals_stays_until_processed(self):
        uid, cookie = self._signup("staged@x.com")
        self._seed_agent(uid)
        self._write_run("agent-1", uid, "2026-07-01T09:00:00-04:00",
                        status="ran", summary="Staged run.", run_session_id="sess-p")
        registry.pending_add(self.root, {
            "agent_id": "agent-1", "owner_user_id": uid, "run_session_id": "sess-p",
            "app": "Gmail", "kind": "send", "summary": "Confirm PO-1",
            "payload": {"to": "a@b.com"}, "content_hash": "h1"})
        # marking it reviewed is NOT enough while a proposal is still pending
        self._req("POST", "/api/u/agents/agent-1/reviews", {"key": "sess-p"}, cookie=cookie)
        _, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        a = data["agents"][0]
        self.assertEqual(a["unreviewed"], 1)                 # still a story (unapproved)
        self.assertEqual(a["stories"][0]["proposals"][0]["summary"], "Confirm PO-1")
        self.assertEqual(a["stories"][0]["proposals"][0]["payload"], {"to": "a@b.com"})
        # processing the proposal (approve->executed or reject) releases it
        pend = registry.pending_for_user(self.root, uid)[0]
        registry.pending_set_status(self.root, pend["id"], "executed")
        _, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        self.assertEqual(data["agents"][0]["unreviewed"], 0)

    def test_home_splits_needs_you_from_fyi(self):
        # Gap #4: home_data separates ACTIONABLE (questions + approvals) from FYI reviews.
        uid, cookie = self._signup("needs@x.com")
        self._seed_agent(uid)
        self._write_run("agent-1", uid, "2026-07-01T09:00:00-04:00", status="needs_approval",
                        summary="staged", run_session_id="sess-a")
        registry.pending_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
            "run_session_id": "sess-a", "app": "Gmail", "kind": "send", "summary": "Send X",
            "payload": {}, "content_hash": "h-a"})
        self._write_run("agent-1", uid, "2026-07-01T10:00:00-04:00", status="needs_input",
                        summary="stuck", run_session_id="sess-b")
        registry.decision_add(self.root, {"agent_id": "agent-1", "owner_user_id": uid,
            "run_session_id": "sess-b", "question": "Which sheet?", "context": {}})
        self._write_run("agent-1", uid, "2026-07-01T11:00:00-04:00", status="completed",
                        summary="did it", run_session_id="sess-c")
        _, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        a = data["agents"][0]
        self.assertEqual(a["open_decisions"], 1)
        self.assertEqual(a["pending_approvals"], 1)
        self.assertEqual(a["needs_you"], 2)
        self.assertEqual(a["fyi_reviews"], 1)
        self.assertEqual(data["needs_you_total"], 2)

    def test_dm_chat_is_rate_limited(self):
        import trace.ui.app as appmod
        old = appmod.GROUP_MSG_RATE
        appmod.GROUP_MSG_RATE = 1                             # tiny cap for the test
        try:
            uid, cookie = self._signup("dmrl@x.com")
            self._seed_agent(uid)
            code, _, _ = self._req("POST", "/api/u/agents/agent-1/messages",
                                   {"text": "hi"}, cookie=cookie)
            self.assertEqual(code, 202)                       # first DM allowed (uses the 1 slot)
            self.state.chat_jobs.pop("agent-1", None)         # clear so we hit the rate path, not 409
            code, data, _ = self._req("POST", "/api/u/agents/agent-1/messages",
                                      {"text": "again"}, cookie=cookie)
            self.assertEqual((code, data["error"]), (429, "rate_limited"))   # a DM = a full session
        finally:
            appmod.GROUP_MSG_RATE = old

    def test_reviews_endpoint_scoped_and_validated(self):
        uid, cookie = self._signup("owner2@x.com")
        self._seed_agent(uid)
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/reviews",
                                    {}, cookie=cookie)
        self.assertEqual((status, data["error"]), (400, "missing_key"))
        other_cookie = self._req("POST", "/api/auth/signup",
                                 {"email": "nosy@x.com", "password": "password1"})[2]
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/reviews",
                                 {"key": "sess-x"}, cookie=other_cookie)
        self.assertEqual(status, 404)

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


class StagingToggleTest(HomeAndMessagesEndpointTest):
    """Gap #2: post-deploy staging toggle — edit which apps are held for approval
    without redeploying. Reuses the HTTP harness (setUp/_req/_signup/_seed_agent)."""

    def _seed_connection(self, sid="s", usable=("gmail", "google_sheets")):
        """Write a connection record so agent_available_apps has usable apps to offer."""
        os.makedirs(sessions.session_dir(self.root, sid), exist_ok=True)
        rec = {"external_user_id": "x", "expected_identity": "", "vault_ref": None,
               "apps": {slug: {"label": slug.replace("_", " ").title(), "usable": True,
                               "connected": True, "verified": "yes", "observed_as": []}
                        for slug in usable},
               "unmapped": [], "updated_at": ""}
        with open(sessions.path(self.root, sid, sessions.CONNECTION), "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def test_set_staged_apps_registry_round_trip(self):
        uid, _ = self._signup("stage-reg@x.com")
        self._seed_agent(uid)
        registry.set_staged_apps(self.root, "agent-1", ["gmail", "", "google_sheets"])
        agent = registry.agents_for_user(self.root, uid)[0]
        self.assertEqual(agent["staged_apps"], ["gmail", "google_sheets"])   # blanks dropped
        registry.set_staged_apps(self.root, "agent-1", [])                   # fully autonomous
        self.assertEqual(registry.agents_for_user(self.root, uid)[0]["staged_apps"], [])

    def test_activity_exposes_available_apps_with_staged_flags(self):
        uid, cookie = self._signup("avail@x.com")
        self._seed_agent(uid)
        self._seed_connection()
        registry.set_staged_apps(self.root, "agent-1", ["gmail"])
        status, data, _ = self._req("GET", "/api/u/agents/agent-1/activity", cookie=cookie)
        self.assertEqual(status, 200)
        apps = {a["slug"]: a for a in data["agent"]["available_apps"]}
        self.assertEqual(set(apps), {"gmail", "google_sheets"})
        self.assertTrue(apps["gmail"]["staged"])
        self.assertFalse(apps["google_sheets"]["staged"])
        self.assertEqual(apps["google_sheets"]["label"], "Google Sheets")

    def test_staging_replace_validates_subset(self):
        uid, cookie = self._signup("replace@x.com")
        self._seed_agent(uid)
        self._seed_connection()
        # request includes an app the agent isn't connected to -> dropped
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/staging",
                                    {"staged_apps": ["gmail", "slack"]}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["staged_apps"], ["gmail"])                     # slack rejected
        self.assertEqual(registry.agents_for_user(self.root, uid)[0]["staged_apps"], ["gmail"])

    def test_staging_empty_allowed_fully_autonomous(self):
        uid, cookie = self._signup("auto@x.com")
        self._seed_agent(uid)
        self._seed_connection()
        registry.set_staged_apps(self.root, "agent-1", ["gmail"])
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/staging",
                                    {"staged_apps": []}, cookie=cookie)
        self.assertEqual((status, data["staged_apps"]), (200, []))

    def test_staging_unstage_one_app_server_side(self):
        uid, cookie = self._signup("unstage@x.com")
        self._seed_agent(uid)
        self._seed_connection()
        registry.set_staged_apps(self.root, "agent-1", ["gmail", "google_sheets"])
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/staging",
                                    {"unstage": "gmail"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["staged_apps"], ["google_sheets"])            # only gmail dropped

    def test_staging_scoped_to_owner(self):
        uid, _ = self._signup("me@x.com")
        self._seed_agent(uid)
        other_cookie = self._req("POST", "/api/auth/signup",
                                 {"email": "nosy2@x.com", "password": "password1"})[2]
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/staging",
                                 {"staged_apps": ["gmail"]}, cookie=other_cookie)
        self.assertEqual(status, 404)


class DecisionMemoryTest(HomeAndMessagesEndpointTest):
    """Gap #3 B2: decisions persist, surface as answerable cards, and answering
    auto-resumes the agent. Reuses the HTTP harness."""

    def _run_needs_input(self, uid):
        from trace.tests.test_scheduler import _NEEDS_INPUT_BLOCK
        self.ma.events = [{"type": "agent.message", "text": _NEEDS_INPUT_BLOCK},
                          {"type": "session.status_idle", "stop_reason": "end_turn", "text": ""}]
        agent = {**_agent(agent_id="agent-1"), "owner_user_id": uid,
                 "external_user_id": users.external_user_id(uid)}
        self.state.run_agent(agent)

    def test_decision_add_dedups(self):
        rec = {"agent_id": "agent-1", "owner_user_id": "u", "run_session_id": "s",
               "question": "Q?", "context": {}}
        self.assertTrue(registry.decision_add(self.root, rec))
        self.assertFalse(registry.decision_add(self.root, rec))     # same -> deduped

    def test_generalization_and_embedding_round_trip(self):
        registry.decision_add(self.root, {"agent_id": "a", "owner_user_id": "u",
                                          "run_session_id": "s", "question": "Q?", "context": {}})
        dec = registry.decisions_for_user(self.root, "u")[0]
        registry.decision_set_generalization(self.root, dec["id"], True, "A general rule.")
        registry.decision_embedding_add(self.root, dec["id"], [0.1] * 1024)
        got = registry.decision_get(self.root, dec["id"])
        self.assertTrue(got["reusable"])
        self.assertEqual(got["generalized_lesson"], "A general rule.")
        self.assertEqual(len(registry.decision_embedding_get(self.root, dec["id"])), 1024)

    def test_needs_input_surfaces_in_home_story(self):
        uid, cookie = self._signup("dm-home@x.com")
        self._seed_agent(uid)
        self._run_needs_input(uid)
        _, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        story = data["agents"][0]["stories"][0]
        self.assertEqual(story["status"], "needs_input")
        self.assertTrue(story["decisions"])
        self.assertIn("ship date", story["decisions"][0]["question"])

    def test_answer_endpoint_records_and_validates(self):
        uid, cookie = self._signup("dm-ans@x.com")
        self._seed_agent(uid)
        self._run_needs_input(uid)
        dec = registry.decisions_for_user(self.root, uid, status="pending")[0]
        status, data, _ = self._req("POST", f"/api/u/decisions/{dec['id']}/answer",
                                    {"answer": "  "}, cookie=cookie)
        self.assertEqual((status, data["error"]), (400, "empty_answer"))
        status, _, _ = self._req("POST", f"/api/u/decisions/{dec['id']}/answer",
                                 {"answer": "Assume 5-day"}, cookie=cookie)
        self.assertEqual(status, 202)
        self.assertEqual(registry.decision_get(self.root, dec["id"])["answer"], "Assume 5-day")
        status, _, _ = self._req("POST", f"/api/u/decisions/{dec['id']}/answer",
                                 {"answer": "again"}, cookie=cookie)
        self.assertEqual(status, 409)                               # already answered

    def test_answer_scoped_to_owner(self):
        uid, _ = self._signup("dm-owner@x.com")
        self._seed_agent(uid)
        self._run_needs_input(uid)
        dec = registry.decisions_for_user(self.root, uid, status="pending")[0]
        other = self._req("POST", "/api/auth/signup",
                          {"email": "dm-nosy@x.com", "password": "password1"})[2]
        status, _, _ = self._req("POST", f"/api/u/decisions/{dec['id']}/answer",
                                 {"answer": "x"}, cookie=other)
        self.assertEqual(status, 404)

    # --- B4 retrieval -------------------------------------------------------
    def _seed_lesson(self, owner, lesson, embedding, aid="a", sid="s"):
        registry.decision_add(self.root, {"agent_id": aid, "owner_user_id": owner,
                                          "run_session_id": sid + owner, "question": "q" + lesson,
                                          "context": {}})
        d = [x for x in registry.decisions_for_user(self.root, owner)
             if x["question"] == "q" + lesson][0]
        registry.decision_set_generalization(self.root, d["id"], True, lesson)
        registry.decision_embedding_add(self.root, d["id"], embedding)
        return d

    def test_match_decisions_is_owner_scoped_and_ranked(self):
        self._seed_lesson("u1", "Ask the customer for a ship date.", [1.0, 0.0])
        self._seed_lesson("u1", "Unrelated older rule.", [0.0, 1.0])
        self._seed_lesson("u2", "Another user's lesson.", [1.0, 0.0])
        hits = registry.match_decisions(self.root, [1.0, 0.05], "u1")
        self.assertEqual(hits[0], "Ask the customer for a ship date.")   # closest first
        self.assertNotIn("Another user's lesson.", hits)                 # never cross-owner

    def test_match_decisions_gates_on_reusable_and_similarity(self):
        d = self._seed_lesson("u", "A rule.", [1.0, 0.0])
        self.assertEqual(registry.match_decisions(self.root, [0.0, 1.0], "u"), [])  # orthogonal
        registry.decision_set_generalization(self.root, d["id"], False, "A rule.")   # un-mark
        self.assertEqual(registry.match_decisions(self.root, [1.0, 0.0], "u"), [])   # not reusable

    def test_retrieved_lesson_injected_into_run(self):
        from unittest import mock
        uid, _ = self._signup("b4-inject@x.com")
        self._seed_agent(uid)
        self._seed_lesson(uid, "Always CC the manager on refunds.", [1.0, 0.0])
        agent = {**_agent(agent_id="agent-1"), "owner_user_id": uid}
        with mock.patch("trace.ui.app.agent_embeddings.embed", return_value=[[1.0, 0.0]]):
            self.state.run_agent(agent)
        self.assertIn("Always CC the manager on refunds.", self.ma.triggers[-1])


class DecisionTrustTest(HomeAndMessagesEndpointTest):
    """Gap #3 B5c: cross-user lessons are provisional (surface a confirm checkpoint), inject
    only after confirmation, retire on dismiss, and log provenance when applied."""

    def _seed_cross_lesson(self, owner, agent_id, lesson, sit_emb, kind_emb):
        registry.decision_add(self.root, {"agent_id": agent_id, "owner_user_id": owner,
            "run_session_id": "s" + lesson, "question": "q" + lesson, "context": {}})
        d = [x for x in registry.decisions_for_user(self.root, owner)
             if x["question"] == "q" + lesson][0]
        registry.decision_set_generalization(self.root, d["id"], True, lesson)
        registry.decision_set_global_verified(self.root, d["id"], True)   # scrub-verified + shared
        registry.decision_embedding_add(self.root, d["id"], sit_emb)
        registry.agent_embedding_upsert(self.root, agent_id, kind_emb)
        return d

    def _agent_for(self, uid):
        return {**_agent(agent_id="agent-1"), "owner_user_id": uid}

    def _guidance(self, agent):
        from unittest import mock
        with mock.patch("trace.ui.app.agent_embeddings.embed", side_effect=lambda *a, **k: [[1.0, 0.0]]):
            return self.state._retrieve_guidance(agent)

    def test_cross_lesson_is_provisional_not_injected(self):
        uid, _ = self._signup("trust-prov@x.com")
        self._seed_agent(uid)
        self._seed_cross_lesson("u2", "a2", "Always CC the manager on refunds.", [1.0, 0.0], [1.0, 0.0])
        self.assertEqual(self._guidance(self._agent_for(uid)), [])          # not injected
        promos = registry.lesson_promotions_for_user(self.root, uid, status="provisional")
        self.assertEqual(len(promos), 1)
        self.assertIn("refunds", promos[0]["lesson"])                        # snapshot for the card

    def test_confirm_promotes_then_injects_and_logs_provenance(self):
        uid, cookie = self._signup("trust-confirm@x.com")
        self._seed_agent(uid)
        dec = self._seed_cross_lesson("u2", "a2", "Always CC the manager on refunds.", [1.0, 0.0], [1.0, 0.0])
        self._guidance(self._agent_for(uid))                                 # -> provisional
        status, _, _ = self._req("POST", "/api/u/lessons/confirm",
            {"agent_id": "agent-1", "decision_id": dec["id"], "action": "confirm"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Always CC the manager on refunds.", self._guidance(self._agent_for(uid)))
        apps = json.load(open(os.path.join(registry._dir(self.root), "applications.json")))
        self.assertTrue(any(v["decision_id"] == dec["id"] for v in apps.values()))

    def test_dismiss_retires_and_never_resurfaces(self):
        uid, cookie = self._signup("trust-dismiss@x.com")
        self._seed_agent(uid)
        dec = self._seed_cross_lesson("u2", "a2", "Some rule.", [1.0, 0.0], [1.0, 0.0])
        self._guidance(self._agent_for(uid))
        self._req("POST", "/api/u/lessons/confirm",
                  {"agent_id": "agent-1", "decision_id": dec["id"], "action": "dismiss"}, cookie=cookie)
        self.assertEqual(self._guidance(self._agent_for(uid)), [])           # not injected
        self.assertEqual(registry.lesson_promotions_for_user(self.root, uid, "provisional"), [])  # gone

    def test_home_surfaces_lesson_confirmation_in_needs_you(self):
        uid, cookie = self._signup("trust-home@x.com")
        self._seed_agent(uid)
        self._seed_cross_lesson("u2", "a2", "A shared rule.", [1.0, 0.0], [1.0, 0.0])
        self._guidance(self._agent_for(uid))
        _, data, _ = self._req("GET", "/api/u/home", cookie=cookie)
        a = data["agents"][0]
        self.assertEqual(len(a["lesson_confirmations"]), 1)
        self.assertEqual(a["needs_you"], 1)
        self.assertEqual(data["needs_you_total"], 1)

    def test_confirm_scoped_to_owner(self):
        uid, _ = self._signup("trust-scope@x.com")
        self._seed_agent(uid)
        dec = self._seed_cross_lesson("u2", "a2", "Rule.", [1.0, 0.0], [1.0, 0.0])
        self._guidance(self._agent_for(uid))
        other = self._req("POST", "/api/auth/signup",
                          {"email": "trust-nosy@x.com", "password": "password1"})[2]
        status, _, _ = self._req("POST", "/api/u/lessons/confirm",
            {"agent_id": "agent-1", "decision_id": dec["id"], "action": "confirm"}, cookie=other)
        self.assertEqual(status, 404)                                        # not their agent

    def test_fleet_default_graduation_auto_injects_without_checkpoint(self):
        uid, _ = self._signup("trust-grad@x.com")
        self._seed_agent(uid)
        dec = self._seed_cross_lesson("u2", "a2", "Fleet rule.", [1.0, 0.0], [1.0, 0.0])
        for i in range(5):                                                   # 5 distinct users confirmed
            registry.lesson_promotion_upsert(self.root, f"oa{i}", f"ou{i}", dec["id"], status="confirmed")
        self.assertIn("Fleet rule.", self._guidance(self._agent_for(uid)))   # auto-trusted, injected
        self.assertEqual(registry.lesson_promotions_for_user(self.root, uid, "provisional"), [])  # no checkpoint

    def test_retire_confirmed_stops_injection(self):
        uid, cookie = self._signup("trust-ret@x.com")
        self._seed_agent(uid)
        dec = self._seed_cross_lesson("u2", "a2", "Rule to retire.", [1.0, 0.0], [1.0, 0.0])
        self._guidance(self._agent_for(uid))
        self._req("POST", "/api/u/lessons/confirm",
                  {"agent_id": "agent-1", "decision_id": dec["id"], "action": "confirm"}, cookie=cookie)
        self.assertIn("Rule to retire.", self._guidance(self._agent_for(uid)))    # confirmed -> injects
        self._req("POST", "/api/u/lessons/confirm",
                  {"agent_id": "agent-1", "decision_id": dec["id"], "action": "retire"}, cookie=cookie)
        self.assertEqual(self._guidance(self._agent_for(uid)), [])                # retired -> stops

    def test_agent_consume_override_blocks_cross_lessons(self):
        uid, _ = self._signup("trust-ovr@x.com")
        self._seed_agent(uid)
        self._seed_cross_lesson("u2", "a2", "A rule.", [1.0, 0.0], [1.0, 0.0])
        agent = {**self._agent_for(uid), "lesson_consume_scope": "owner"}    # opt out of others'
        self.assertEqual(self._guidance(agent), [])                          # cross lessons excluded


class ConsentTest(HomeAndMessagesEndpointTest):
    """Phase B B5e: account-wide consent defaults + per-agent overrides."""

    def test_account_consent_get_and_set(self):
        uid, cookie = self._signup("consent@x.com")
        _, data, _ = self._req("GET", "/api/u/consent", cookie=cookie)
        self.assertEqual(data, {"contribute_scope": "global", "consume_scope": "global"})  # opt-out
        status, data, _ = self._req("POST", "/api/u/consent", {"consume_scope": "owner"}, cookie=cookie)
        self.assertEqual((status, data["consume_scope"]), (200, "owner"))
        self.assertEqual(data["contribute_scope"], "global")                 # untouched
        _, data, _ = self._req("GET", "/api/u/consent", cookie=cookie)
        self.assertEqual(data["consume_scope"], "owner")                     # persisted

    def test_agent_override_then_inherit(self):
        uid, cookie = self._signup("consent-a@x.com")
        self._seed_agent(uid)
        _, data, _ = self._req("POST", "/api/u/agents/agent-1/consent",
                               {"consume_scope": "owner"}, cookie=cookie)
        self.assertEqual(data["override"]["consume_scope"], "owner")
        self.assertEqual(data["effective"]["consume_scope"], "owner")
        self.assertEqual(data["effective"]["contribute_scope"], "global")    # inherits default
        _, data, _ = self._req("POST", "/api/u/agents/agent-1/consent",
                               {"consume_scope": "inherit"}, cookie=cookie)
        self.assertIsNone(data["override"]["consume_scope"])                 # cleared
        self.assertEqual(data["effective"]["consume_scope"], "global")

    def test_agent_consent_scoped_to_owner(self):
        uid, _ = self._signup("consent-own@x.com")
        self._seed_agent(uid)
        other = self._req("POST", "/api/auth/signup",
                          {"email": "consent-nosy@x.com", "password": "password1"})[2]
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/consent",
                                 {"consume_scope": "owner"}, cookie=other)
        self.assertEqual(status, 404)


class GroupChatTest(HomeAndMessagesEndpointTest):
    """Group chat G1: agent colors + group CRUD (add agents to a group chat)."""

    def test_agent_color_deterministic_and_override(self):
        c1 = registry.agent_color({"agent_id": "agent-x"})
        self.assertTrue(c1.startswith("#"))
        self.assertEqual(c1, registry.agent_color({"agent_id": "agent-x"}))   # stable
        self.assertEqual(registry.agent_color({"agent_id": "agent-x", "color": "#000"}), "#000")

    def test_create_list_update_delete_group(self):
        uid, cookie = self._signup("grp@x.com")
        self._seed_agent(uid, agent_id="agent-1", name="Invoicing")
        self._seed_agent(uid, agent_id="agent-2", name="Comms")
        # create with one non-owned id (filtered out) + one real
        _, data, _ = self._req("POST", "/api/u/groups",
            {"name": "Ops", "agent_ids": ["agent-1", "not-mine"]}, cookie=cookie)
        g = data["groups"][0]
        self.assertEqual(g["name"], "Ops")
        self.assertEqual([m["agent_id"] for m in g["members"]], ["agent-1"])   # non-owned dropped
        self.assertTrue(g["members"][0]["color"].startswith("#"))
        # update: rename + set members
        gid = g["id"]
        _, data, _ = self._req("POST", f"/api/u/groups/{gid}",
            {"name": "Operations", "agent_ids": ["agent-1", "agent-2"]}, cookie=cookie)
        g = data["groups"][0]
        self.assertEqual(g["name"], "Operations")
        self.assertEqual({m["agent_id"] for m in g["members"]}, {"agent-1", "agent-2"})
        # delete
        _, data, _ = self._req("POST", f"/api/u/groups/{gid}/delete", {}, cookie=cookie)
        self.assertEqual(data["groups"], [])

    def test_group_scoped_to_owner(self):
        uid, cookie = self._signup("grp-own@x.com")
        self._seed_agent(uid)
        _, data, _ = self._req("POST", "/api/u/groups", {"name": "G", "agent_ids": []}, cookie=cookie)
        gid = data["groups"][0]["id"]
        other = self._req("POST", "/api/auth/signup",
                          {"email": "grp-nosy@x.com", "password": "password1"})[2]
        status, _, _ = self._req("POST", f"/api/u/groups/{gid}", {"name": "hijack"}, cookie=other)
        self.assertEqual(status, 404)

    def test_set_agent_color_endpoint(self):
        uid, cookie = self._signup("grp-color@x.com")
        self._seed_agent(uid)
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/color",
                                    {"color": "#123456"}, cookie=cookie)
        self.assertEqual((status, data["color"]), (200, "#123456"))
        _, data, _ = self._req("GET", "/api/u/groups", cookie=cookie)   # reflected in resolved members
        # (no groups yet, but the color persisted on the agent row)
        self.assertEqual(registry.agents_for_user(self.root, uid)[0]["color"], "#123456")

    def test_rename_agent_endpoint(self):
        uid, cookie = self._signup("grp-rename@x.com")
        self._seed_agent(uid)
        status, data, _ = self._req("POST", "/api/u/agents/agent-1/name",
                                    {"name": "Operations — EOD shipping"}, cookie=cookie)
        self.assertEqual((status, data["name"]), (200, "Operations — EOD shipping"))
        self.assertEqual(registry.agents_for_user(self.root, uid)[0]["name"],
                         "Operations — EOD shipping")

    def test_rename_agent_rejects_empty_and_foreign(self):
        uid, cookie = self._signup("grp-rename2@x.com")
        self._seed_agent(uid)
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/name",
                                 {"name": "   "}, cookie=cookie)
        self.assertEqual(status, 400)
        _, other = self._signup("grp-rename3@x.com")          # another owner cannot rename it
        status, _, _ = self._req("POST", "/api/u/agents/agent-1/name",
                                 {"name": "hijacked"}, cookie=other)
        self.assertEqual(status, 404)
        self.assertNotEqual(registry.agents_for_user(self.root, uid)[0]["name"], "hijacked")


class GroupBroadcastTest(HomeAndMessagesEndpointTest):
    """Group chat G2: read-only broadcast — every member answers grounded in its own run
    data, in its color, with NO agent session (structurally can't act)."""

    def _group_with(self, cookie, members):
        _, data, _ = self._req("POST", "/api/u/groups",
                               {"name": "All", "agent_ids": members}, cookie=cookie)
        return data["groups"][0]["id"]

    def test_broadcast_each_member_replies_in_color_and_grounded(self):
        uid, cookie = self._signup("bc@x.com")
        self._seed_agent(uid, agent_id="agent-1", name="Invoicing")
        self._seed_agent(uid, agent_id="agent-2", name="Comms")
        self._write_run("agent-1", uid, "2026-08-01T09:00:00-04:00",
                        summary="raw", run_session_id="s1")
        registry.summary_set(self.root, "s1", "Logged 3 invoices today.", "completed")
        self._write_run("agent-2", uid, "2026-08-01T10:00:00-04:00",
                        summary="Sent 2 emails.", run_session_id="s2")
        calls = []
        self.state.summarize_fn = lambda system, u: (calls.append(u), "here's what I did")[1]
        gid = self._group_with(cookie, ["agent-1", "agent-2"])
        _, data, _ = self._req("POST", f"/api/u/groups/{gid}/broadcast",
                               {"text": "what did yall do?"}, cookie=cookie)
        replies = data["replies"]
        self.assertEqual({r["agent_id"] for r in replies}, {"agent-1", "agent-2"})
        self.assertTrue(all(r["color"].startswith("#") and r["reply"] for r in replies))
        joined = " ".join(calls)                                # grounded in each agent's OWN data
        self.assertIn("Logged 3 invoices today.", joined)       # agent-1 cached summary
        self.assertIn("Sent 2 emails.", joined)                 # agent-2 run summary
        self.assertEqual(self.ma.sessions, [])                  # read-only: NO agent session created

    def test_broadcast_validation_and_scoping(self):
        uid, cookie = self._signup("bc2@x.com")
        self._seed_agent(uid)
        gid = self._group_with(cookie, ["agent-1"])
        status, data, _ = self._req("POST", f"/api/u/groups/{gid}/broadcast",
                                    {"text": "  "}, cookie=cookie)
        self.assertEqual((status, data["error"]), (400, "empty_message"))
        other = self._req("POST", "/api/auth/signup",
                          {"email": "bc-nosy@x.com", "password": "password1"})[2]
        status, _, _ = self._req("POST", f"/api/u/groups/{gid}/broadcast",
                                 {"text": "hi"}, cookie=other)
        self.assertEqual(status, 404)


class GroupMessageTest(HomeAndMessagesEndpointTest):
    """Group chat G3: @mention routes to an actionable direct reply (real session, staging
    respected); no @mention falls back to the read-only broadcast."""

    def _group_with(self, cookie, members, name="Team"):
        _, data, _ = self._req("POST", "/api/u/groups",
                               {"name": name, "agent_ids": members}, cookie=cookie)
        return data["groups"][0]["id"]

    def test_parse_mentions(self):
        members = [{"agent_id": "a1", "name": "Operations"},
                   {"agent_id": "a2", "name": "Invoicing Bot"}]
        self.assertEqual(app.AppState.parse_mentions("hey @operations what's up", members), ["a1"])
        self.assertEqual(app.AppState.parse_mentions("@invoicing check this", members), ["a2"])  # first word
        self.assertEqual(app.AppState.parse_mentions("no mentions here", members), [])

    def test_mention_routes_to_direct_actionable(self):
        uid, cookie = self._signup("gm@x.com")
        self._seed_agent(uid, agent_id="agent-1", name="Operations")
        self.ma.events = [{"type": "agent.message", "text": "On it — SKU is ABC-123."},
                          {"type": "session.status_idle", "stop_reason": "end_turn", "text": ""}]
        gid = self._group_with(cookie, ["agent-1"])
        _, data, _ = self._req("POST", f"/api/u/groups/{gid}/message",
                               {"text": "@operations what's the sku on xyz?"}, cookie=cookie)
        self.assertEqual(data["mode"], "direct")
        self.assertEqual(data["replies"][0]["agent_id"], "agent-1")
        self.assertIn("ABC-123", data["replies"][0]["reply"])
        self.assertEqual(len(self.ma.sessions), 1)              # a REAL session ran (can act)

    def test_no_mention_falls_back_to_broadcast(self):
        uid, cookie = self._signup("gm2@x.com")
        self._seed_agent(uid, agent_id="agent-1", name="Operations")
        self.state.summarize_fn = lambda s, u: "did stuff"
        gid = self._group_with(cookie, ["agent-1"])
        _, data, _ = self._req("POST", f"/api/u/groups/{gid}/message",
                               {"text": "hey team what did yall do?"}, cookie=cookie)
        self.assertEqual(data["mode"], "broadcast")
        self.assertEqual(self.ma.sessions, [])                  # read-only, no session

    def test_mention_stages_actions_when_agent_staged(self):
        from trace.tests.test_scheduler import _PROPOSAL_BLOCK
        uid, cookie = self._signup("gm3@x.com")
        self._seed_agent(uid, agent_id="agent-1", name="Operations")
        registry.set_staged_apps(self.root, "agent-1", ["gmail"])
        self.ma.events = [{"type": "agent.message", "text": "Drafted it.\n" + _PROPOSAL_BLOCK},
                          {"type": "session.status_idle", "stop_reason": "end_turn", "text": ""}]
        gid = self._group_with(cookie, ["agent-1"])
        _, data, _ = self._req("POST", f"/api/u/groups/{gid}/message",
                               {"text": "@operations send the confirmation"}, cookie=cookie)
        r = data["replies"][0]
        self.assertTrue(r["staged"])                            # action PROPOSED, not performed
        self.assertEqual(r["reply"], "Drafted it.")            # fence stripped from human reply
        self.assertIn("STAGING MODE", self.ma.triggers[-1])    # staging directive was applied

    def test_rate_limit_guards_credits(self):
        import trace.ui.app as appmod
        old = appmod.GROUP_MSG_RATE
        appmod.GROUP_MSG_RATE = 2
        try:
            uid, cookie = self._signup("rl@x.com")
            self._seed_agent(uid, agent_id="agent-1", name="Ops")
            self.state.summarize_fn = lambda s, u: "ok"
            gid = self._group_with(cookie, ["agent-1"])
            for _ in range(2):
                code, _, _ = self._req("POST", f"/api/u/groups/{gid}/message",
                                       {"text": "what's up team"}, cookie=cookie)
                self.assertEqual(code, 200)
            code, data, _ = self._req("POST", f"/api/u/groups/{gid}/message",
                                      {"text": "again"}, cookie=cookie)
            self.assertEqual((code, data["error"]), (429, "rate_limited"))   # credit guard trips
        finally:
            appmod.GROUP_MSG_RATE = old

    def test_mention_fanout_is_capped(self):
        uid, cookie = self._signup("cap@x.com")
        for i, nm in enumerate(["Alpha", "Bravo", "Charlie", "Delta"], 1):
            self._seed_agent(uid, agent_id=f"agent-{i}", name=nm)
        gid = self._group_with(cookie, [f"agent-{i}" for i in range(1, 5)])
        _, data, _ = self._req("POST", f"/api/u/groups/{gid}/message",
            {"text": "@alpha @bravo @charlie @delta do it"}, cookie=cookie)
        self.assertEqual(data["mode"], "direct")
        self.assertEqual(len(data["replies"]), 3)               # capped at MAX_MENTIONS_PER_MESSAGE


if __name__ == "__main__":
    unittest.main()
