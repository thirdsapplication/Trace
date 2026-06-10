import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from trace.ui import app, sessions

SPEC_MD = """# Build spec: Order intake
## 0. Preflight — read the attached files before writing any code
- `./spec_B1_files/PO.pdf` — input pdf. Instruction (authoritative): "the PO".
## 1. Intent
Append one row per task.
## 3. Acceptance criteria
- The agent appends exactly one row.  _(user-specified)_
"""
REVIEWED = {"bundles": [{"id": "B1", "classification": "bundle", "name": "Order intake",
                         "description": "Append one row per task.", "trigger": "A new task arrived.",
                         "steps": ["read", "enter"], "disposition": "approve_for_build",
                         "human_checkpoints": [], "included_example_event_ids": ["e1"],
                         "added_acceptance_criteria": [], "preferred_output": "",
                         "evidence_event_ids": ["e1", "e2"], "confidence": "high",
                         "reclassified_from": None, "original_reason_for_judgment": ""}],
            "anti_bundles": [], "notes": ""}
EVENTS = [{"id": "e1", "app": "Gmail", "window": "Inbox", "action": "read_email", "detail": "x"},
          {"id": "e2", "app": "Google Sheets", "window": "Ledger", "action": "enter_data", "detail": "x"}]


class FakeKeystore:
    def __init__(self, key=None): self.key = key
    def info(self): return {"saved": bool(self.key), "backend": "memory", "masked_tail": None}
    def get_key(self): return self.key
    def save_key(self, k): self.key = k; return "memory"
    def clear_key(self): self.key = None


def fake_run_test(bundle, spec_md, env_record, event_log, test_dict, events_path=None):
    report = {"bundle_id": bundle["id"], "criteria_total": 1, "passed": 1, "failed": 0,
              "not_testable_in_sandbox": 0, "results": [{"criterion": "x", "verdict": "pass"}],
              "zero_coaching": {"stalled": False, "asked_question": False},
              "deploy_recommended": True, "sessions_run": 1, "retention_clean": True}
    return report, {"output_files": {"SHEET.xlsx": "order_id\n1\n"}}


class UIAgentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ks = FakeKeystore("sk-ant-test")
        # default compile_agent_fn (pure), injected run_test_fn stub
        self.state = app.AppState(self.root, key_store=self.ks, run_test_fn=fake_run_test)
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.sid = sessions.create_session(self.root)
        with open(sessions.path(self.root, self.sid, sessions.EVENT_LOG), "w") as f:
            json.dump(EVENTS, f)
        with open(sessions.path(self.root, self.sid, sessions.REVIEWED), "w") as f:
            json.dump(REVIEWED, f)
        specs = sessions.path(self.root, self.sid, sessions.SPECS_DIR)
        os.makedirs(specs, exist_ok=True)
        with open(os.path.join(specs, "spec_B1.md"), "w") as f:
            f.write(SPEC_MD)
        # an environment record with an input artifact (real file)
        af = os.path.join(self.root, "PO.pdf")
        with open(af, "wb") as f:
            f.write(b"%PDF-1.4 po")
        env = {"bundle_id": "B1", "artifacts": [
            {"role": "input", "kind": "pdf", "derived_from_event_ids": ["e1"],
             "label_shown_to_user": "the PO", "user_instruction": "the PO", "uploaded": True,
             "file_ref": af, "extraction_status": "failed", "extracted": None}],
            "answers": [], "status": "complete"}
        from trace.artifact.artifact import environment_filename
        with open(sessions.path(self.root, self.sid, environment_filename("B1")), "w") as f:
            json.dump(env, f)

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self._tmp.cleanup()

    def req(self, method, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        r = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_compile_test_end_to_end(self):
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/compile-test")
        self.assertEqual(code, 202)
        for _ in range(60):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/compile-test/status")
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(st.get("state"), "done", st)
        self.assertEqual(st["compiled"], 1)
        self.assertEqual(st["tested"], 1)

        code, data = self.req("GET", f"/api/sessions/{self.sid}/agents")
        self.assertEqual(code, 200)
        agent = next(a for a in data["agents"] if a["bundle_id"] == "B1")
        # both envelopes present and valid YAML
        self.assertIn("system:", agent["test_yaml"])
        self.assertIn("mcp_server_name", agent["live_yaml"])      # live has connectors
        self.assertNotIn("mcp_server_name", agent["test_yaml"])   # test has none
        # test report present + recommends deploy
        self.assertTrue(agent["test_report"]["deploy_recommended"])

    def test_compile_test_without_specs_rejected(self):
        code, data = self.req("POST", "/api/sessions/no-such-session/compile-test")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "no_specs")

    def test_sandbox_test_failure_does_not_crash_job(self):
        # a failing test run (API/SDK drift, no key) must still leave the envelopes
        # compiled and the job 'done', recording the failure per bundle.
        def boom(*a, **k):
            raise AttributeError("'Anthropic' object has no attribute 'files'")
        self.state.run_test_fn = boom
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/compile-test")
        self.assertEqual(code, 202)
        for _ in range(60):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/compile-test/status")
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(st["state"], "done")        # never a hard failure
        self.assertEqual(st["compiled"], 1)          # envelopes still produced
        self.assertEqual(st["tested"], 0)
        self.assertIn("test_skipped", st["reports"]["B1"])
        # the compiled envelopes are present and readable
        _, data = self.req("GET", f"/api/sessions/{self.sid}/agents")
        agent = next(a for a in data["agents"] if a["bundle_id"] == "B1")
        self.assertIn("system:", agent["test_yaml"])


if __name__ == "__main__":
    unittest.main()
