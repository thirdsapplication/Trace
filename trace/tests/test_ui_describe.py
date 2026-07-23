"""Skip-recording path — POST /api/u/describe-workflow end to end (offline stubs).

Covers the endpoint contract (auth, guards, owner scoping, job lifecycle, the
artifacts it materializes) and the money test: a full describe -> review ->
mandatory questions -> build -> connect -> deploy chain through the scoped
end-user API, with the connectable app coming from the bundle's own text
(there are zero captured events on this path).
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from trace.tests.test_connect import StubConnectClient
from trace.tests.test_deploy import _fake_deploy_fn
from trace.tests.test_ui_agent import FakeKeystore, fake_config_fn, fake_interview_fn
from trace.ui import app, sessions, users

# What the (stubbed) describe engine returns: one bundle naming its apps in text,
# with NO evidence events — the discovery contract of the describe path.
DESCRIBED_DISCOVERY = {
    "bundles": [{
        "id": "B1", "name": "Order intake",
        "description": "Read each new order email in Gmail and log it.",
        "trigger": "A new order email arrives.",
        "steps": ["Open the order email in Gmail", "Add one row to the tracker"],
        "frequency": "daily", "evidence_event_ids": [],
        "why_automatable": "Fixed steps per order.",
        "rationale": "The same steps repeat for every order.", "confidence": "medium",
    }],
    "anti_bundles": [], "unclustered_or_noise_event_ids": [], "notes": "",
}

REVIEWED_FROM_DESCRIBE = {
    "bundles": [{
        "id": "B1", "classification": "bundle", "name": "Order intake",
        "description": "Read each new order email in Gmail and log it.",
        "trigger": "A new order email arrives.",
        "steps": ["Open the order email in Gmail", "Add one row to the tracker"],
        "disposition": "approve_for_build", "human_checkpoints": [],
        "included_example_event_ids": [], "added_acceptance_criteria": [],
        "preferred_output": "", "evidence_event_ids": [], "confidence": "medium",
        "reclassified_from": None, "original_reason_for_judgment": "",
    }],
    "anti_bundles": [], "notes": "",
}


def fake_describe_fn(description, model=None, model_fn=None):
    return json.loads(json.dumps(DESCRIBED_DISCOVERY)), None


class DescribeHTTPBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self._saved_users = (users._DIR, users._PATH, dict(users._SESSIONS))
        users._DIR = d
        users._PATH = os.path.join(d, "users.json")
        users._SESSIONS.clear()
        self.root = os.path.join(d, "sessions")
        self.ks = FakeKeystore("sk-ant-test")
        self.state = app.AppState(
            self.root, key_store=self.ks, describe_fn=fake_describe_fn,
            interview_fn=fake_interview_fn, config_fn=fake_config_fn,
            connect_fn=lambda: StubConnectClient(identity_email="worker@acme.com"),
            deploy_fn=_fake_deploy_fn)
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        users._DIR, users._PATH, sess = self._saved_users
        users._SESSIONS.clear(); users._SESSIONS.update(sess)
        self._tmp.cleanup()

    def req(self, method, path, body=None, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(r)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), None
        set_cookie = resp.headers.get("Set-Cookie")
        return resp.status, json.loads(resp.read() or b"null"), set_cookie

    def signup(self, email="worker@acme.com", pw="longenough1"):
        code, data, sc = self.req("POST", "/api/auth/signup", {"email": email, "password": pw})
        self.assertEqual(code, 200, data)
        return sc.split(";", 1)[0], data["user"]

    def _await(self, sid, action, cookie):
        for _ in range(100):
            _, st, _ = self.req("GET", f"/api/u/sessions/{sid}/{action}/status", cookie=cookie)
            if st["state"] in ("done", "error"):
                return st
            time.sleep(0.05)
        return st

    def describe(self, cookie, text="I read order emails in Gmail and log them."):
        code, data, _ = self.req("POST", "/api/u/describe-workflow",
                                 {"description": text}, cookie=cookie)
        self.assertEqual(code, 202, data)
        sid = data["session_id"]
        self.assertEqual(self._await(sid, "describe", cookie)["state"], "done")
        return sid


class DescribeEndpointTest(DescribeHTTPBase):
    def test_requires_auth(self):
        code, data, _ = self.req("POST", "/api/u/describe-workflow", {"description": "x"})
        self.assertEqual(code, 401)
        self.assertEqual(data["error"], "auth_required")

    def test_empty_description_rejected(self):
        cookie, _ = self.signup()
        code, data, _ = self.req("POST", "/api/u/describe-workflow",
                                 {"description": "   "}, cookie=cookie)
        self.assertEqual((code, data["error"]), (400, "empty_description"))

    def test_no_api_key_rejected(self):
        cookie, _ = self.signup()
        self.ks.key = None
        try:
            code, data, _ = self.req("POST", "/api/u/describe-workflow",
                                     {"description": "orders in Gmail"}, cookie=cookie)
            self.assertEqual((code, data["error"]), (400, "no_api_key"))
        finally:
            self.ks.key = "sk-ant-test"

    def test_materializes_owned_session_with_artifacts(self):
        cookie, user = self.signup()
        sid = self.describe(cookie, "I read order emails in Gmail and log them.")
        # event_log is an EMPTY list (nothing was observed — never fabricated)
        self.assertEqual(sessions.read_json_artifact(self.root, sid, sessions.EVENT_LOG), [])
        disc = sessions.read_json_artifact(self.root, sid, sessions.DISCOVERY)
        self.assertEqual([b["id"] for b in disc["bundles"]], ["B1"])
        meta = sessions.read_meta(self.root, sid)
        self.assertEqual(meta["owner_user_id"], user["user_id"])
        self.assertTrue(meta["described"])
        self.assertEqual(meta["description"], "I read order emails in Gmail and log them.")
        self.assertTrue(meta["describe_at"])
        self.assertEqual(meta["event_count"], 0)
        # the session shows up in the user's list and review-data is reachable
        _, mine, _ = self.req("GET", "/api/u/sessions", cookie=cookie)
        self.assertIn(sid, [s["id"] for s in mine["sessions"]])
        code, _, _ = self.req("GET", f"/api/u/sessions/{sid}/review-data", cookie=cookie)
        self.assertEqual(code, 200)

    def test_other_user_cannot_see_the_session(self):
        cookie, _ = self.signup()
        sid = self.describe(cookie)
        other, _ = self.signup("intruder@acme.com")
        code, _, _ = self.req("GET", f"/api/u/sessions/{sid}/describe/status", cookie=other)
        self.assertEqual(code, 404)
        code, _, _ = self.req("GET", f"/api/u/sessions/{sid}/review-data", cookie=other)
        self.assertEqual(code, 404)

    def test_engine_error_lands_in_job_no_artifacts(self):
        def boom(description, model=None, model_fn=None):
            raise RuntimeError("couldn't find a workflow in that description")
        self.state.describe_fn = boom
        cookie, _ = self.signup()
        code, data, _ = self.req("POST", "/api/u/describe-workflow",
                                 {"description": "???"}, cookie=cookie)
        self.assertEqual(code, 202)
        sid = data["session_id"]
        st = self._await(sid, "describe", cookie)
        self.assertEqual(st["state"], "error")
        self.assertIn("couldn't find a workflow", st["error"])
        self.assertIsNone(sessions.read_json_artifact(self.root, sid, sessions.EVENT_LOG))
        self.assertIsNone(sessions.read_json_artifact(self.root, sid, sessions.DISCOVERY))

    def test_full_describe_to_deploy_chain(self):
        cookie, _ = self.signup()
        sid = self.describe(cookie)
        # review: approve the described workflow (same endpoint as the capture path)
        code, _, _ = self.req("POST", f"/api/u/sessions/{sid}/reviewed-bundles",
                              REVIEWED_FROM_DESCRIBE, cookie=cookie)
        self.assertEqual(code, 200)
        # the questions phase is mandatory here too
        code, data, _ = self.req("POST", f"/api/u/sessions/{sid}/build", {}, cookie=cookie)
        self.assertEqual((code, data["error"]), (400, "interview_incomplete"))
        code, _, _ = self.req("POST", f"/api/u/sessions/{sid}/interview", {}, cookie=cookie)
        self.assertEqual(code, 202)
        self.assertEqual(self._await(sid, "interview", cookie)["state"], "done")
        code, rec, _ = self.req("POST", f"/api/u/sessions/{sid}/interview/B1",
                                {"answers": {"q1": "the main tab", "q2": "the source wins"}},
                                cookie=cookie)
        self.assertEqual(rec["status"], "complete")
        # build works with ZERO observed events (config from the description + answers)
        code, _, _ = self.req("POST", f"/api/u/sessions/{sid}/build", {}, cookie=cookie)
        self.assertEqual(code, 202)
        self.assertEqual(self._await(sid, "build", cookie)["state"], "done")
        # connect offers gmail — derived from the bundle TEXT, not events
        code, conn, _ = self.req("GET", f"/api/u/sessions/{sid}/connection-data", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertIn("gmail", conn["record"]["apps"])
        code, _, _ = self.req("POST", f"/api/u/sessions/{sid}/connection-identity",
                              {"email": "worker@acme.com"}, cookie=cookie)
        self.assertEqual(code, 200)
        code, _, _ = self.req("POST", f"/api/u/sessions/{sid}/connect/gmail/callback",
                              {}, cookie=cookie)
        self.assertEqual(code, 202)
        self.assertEqual(self._await(sid, "connect", cookie)["state"], "done")
        # deploy goes live
        code, _, _ = self.req("POST", f"/api/u/sessions/{sid}/deploy",
                              {"trigger": "on inbound"}, cookie=cookie)
        self.assertEqual(code, 202)
        self.assertEqual(self._await(sid, "deploy", cookie)["state"], "done")


if __name__ == "__main__":
    unittest.main()
