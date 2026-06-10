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
    # a run POINTER (no grading): agent + session ids, Console link, status, outputs
    return {"bundle_id": bundle["id"], "agent_id": "agent_x", "agent_name": "Order intake",
            "agent_reused": False, "environment_id": "env_x",
            "uploaded_file_ids": ["file_0"],
            "sessions": [{"session_id": "sess_x",
                          "console_url": "https://platform.claude.com/managed-agents",
                          "console_hint": "Console -> Managed Agents -> Sessions",
                          "status": "completed", "error": None,
                          "output_files": ["result.csv"], "trigger": "go"}],
            "events_file": "events_B1.json", "ran_at": "now"}


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

    def test_compile_test_end_to_end(self):
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/compile-test",
                           {"bundle_ids": ["B1"]})
        self.assertEqual(code, 202)
        for _ in range(60):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/compile-test/status")
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(st.get("state"), "done", st)
        self.assertEqual(st["compiled"], 1)
        self.assertEqual(st["ran"], 1)
        # the status carries the run POINTER, not a graded report
        self.assertEqual(st["runs"]["B1"]["sessions"][0]["session_id"], "sess_x")

        code, data = self.req("GET", f"/api/sessions/{self.sid}/agents")
        self.assertEqual(code, 200)
        agent = next(a for a in data["agents"] if a["bundle_id"] == "B1")
        # both envelopes present and valid YAML
        self.assertIn("system:", agent["test_yaml"])
        self.assertIn("mcp_server_name", agent["live_yaml"])      # live has connectors
        self.assertNotIn("mcp_server_name", agent["test_yaml"])   # test has none
        # the run pointer is persisted and surfaced (no grading verdict)
        self.assertEqual(agent["run"]["agent_id"], "agent_x")
        self.assertEqual(agent["run"]["sessions"][0]["status"], "completed")
        self.assertNotIn("deploy_recommended", agent["run"])

    def test_compile_test_without_specs_rejected(self):
        code, data = self.req("POST", "/api/sessions/no-such-session/compile-test")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "no_specs")

    def test_run_failure_does_not_crash_job(self):
        # a failing run (API/SDK drift, no key) must still leave the envelopes
        # compiled and the job 'done', recording the failure per bundle.
        def boom(*a, **k):
            raise AttributeError("'Anthropic' object has no attribute 'files'")
        self.state.run_test_fn = boom
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/compile-test",
                           {"bundle_ids": ["B1"]})
        self.assertEqual(code, 202)
        for _ in range(60):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/compile-test/status")
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(st["state"], "done")        # never a hard failure
        self.assertEqual(st["compiled"], 1)          # envelopes still produced
        self.assertEqual(st["ran"], 0)
        self.assertIn("run_skipped", st["runs"]["B1"])
        # the compiled envelopes are present and readable
        _, data = self.req("GET", f"/api/sessions/{self.sid}/agents")
        agent = next(a for a in data["agents"] if a["bundle_id"] == "B1")
        self.assertIn("system:", agent["test_yaml"])

    def test_stage4_bundles_defaults_to_artifact_bearing(self):
        # B1 has an uploaded Review 2 artifact -> offered with has_artifacts True
        code, data = self.req("GET", f"/api/sessions/{self.sid}/stage4-bundles")
        self.assertEqual(code, 200)
        b = next(x for x in data["bundles"] if x["id"] == "B1")
        self.assertTrue(b["has_artifacts"])
        self.assertGreaterEqual(b["artifact_count"], 1)

    def test_cleanup_platform_collects_ids_from_runs_and_deletes(self):
        # run first so a run_<id>.json exists, then clean up
        captured = {}
        def fake_cleanup(session_ids, file_ids, agent_ids):
            captured["args"] = (session_ids, file_ids, agent_ids)
            return {"removed": {"sessions": session_ids, "files": file_ids, "agents": agent_ids},
                    "errors": []}
        self.state.cleanup_fn = fake_cleanup
        self.req("POST", f"/api/sessions/{self.sid}/compile-test", {"bundle_ids": ["B1"]})
        for _ in range(60):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/compile-test/status")
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        code, data = self.req("POST", f"/api/sessions/{self.sid}/cleanup-platform")
        self.assertEqual(code, 200)
        sess_ids, file_ids, agent_ids = captured["args"]
        self.assertEqual(sess_ids, ["sess_x"])       # from the run pointer
        self.assertEqual(file_ids, ["file_0"])
        self.assertEqual(agent_ids, ["agent_x"])
        self.assertEqual(data["removed"]["agents"], ["agent_x"])


if __name__ == "__main__":
    unittest.main()
