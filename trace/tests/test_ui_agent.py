"""Phase 2 UI chain: Review -> Interview -> Generate config -> Run (offline stubs)."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from trace.agent import configgen
from trace.ui import app, sessions

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

CONFIG_CONTENT = {
    "description": "Order intake agent.",
    "role": "You process inbound orders one at a time.",
    "tools_and_artifacts": ["Email", "Spreadsheet"],
    "steps": [{"text": "Open the order and read it.", "source": {"type": "events", "ids": ["e1"]}},
              {"text": "Enter it into the destination tab.", "source": {"type": "user_answer", "q": "q1"}}],
    "working_rules": [{"text": "Enter values exactly as on the source.",
                       "source": {"type": "events", "ids": ["e2"]}}],
    "requirements": [{"text": "Each order is fully processed before the next.",
                      "source": {"type": "discovery"}}],
}


class FakeKeystore:
    def __init__(self, key=None): self.key = key
    def info(self): return {"saved": bool(self.key), "backend": "memory", "masked_tail": None}
    def get_key(self): return self.key
    def save_key(self, k): self.key = k; return "memory"
    def clear_key(self): self.key = None


def fake_interview_fn(bundle, event_log, model_fn=None):
    return {"questions": [
        {"id": "q1", "unknown": "tab", "lens": "capture_blind_spot", "why_it_matters": "wrong tab",
         "question": "Which tab do new records go in?", "suggested_answers": ["the main tab"],
         "answer_type": "short_text", "cost_if_wrong": "high", "runtime_discoverable": False},
        {"id": "q2", "unknown": "conflict", "lens": "undetermined_decision", "why_it_matters": "which wins",
         "question": "If the source differs, which wins?", "suggested_answers": [],
         "answer_type": "short_text", "cost_if_wrong": "medium", "runtime_discoverable": False},
    ]}


def fake_config_fn(bundle, observed, answers, model_fn, unresolved=None):
    # real assembly, stub the model content -> exercises the actual configgen path
    return configgen.compile_config(bundle, observed, answers,
                                    lambda s, u: json.dumps(CONFIG_CONTENT), unresolved=unresolved)


def fake_run_test(bundle, spec_md, env_record, event_log, test_dict, events_path=None):
    return {"bundle_id": bundle["id"], "agent_id": "agent_x", "agent_name": "Order intake",
            "agent_reused": False, "environment_id": "env_x", "uploaded_file_ids": ["file_0"],
            "sessions": [{"session_id": "sess_x",
                          "console_url": "https://platform.claude.com/managed-agents",
                          "console_hint": "Console -> Managed Agents -> Sessions",
                          "status": "completed", "error": None,
                          "output_files": ["result.csv"], "trigger": "go"}],
            "events_file": "events_B1.json", "ran_at": "now"}


class UIChainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ks = FakeKeystore("sk-ant-test")
        self.state = app.AppState(self.root, key_store=self.ks, interview_fn=fake_interview_fn,
                                  config_fn=fake_config_fn, run_test_fn=fake_run_test)
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.sid = sessions.create_session(self.root)
        with open(sessions.path(self.root, self.sid, sessions.EVENT_LOG), "w") as f:
            json.dump(EVENTS, f)
        with open(sessions.path(self.root, self.sid, sessions.REVIEWED), "w") as f:
            json.dump(REVIEWED, f)

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

    def _await(self, action):
        for _ in range(80):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/{action}/status")
            if st["state"] in ("done", "error"):
                return st
            time.sleep(0.05)
        return st

    def _interview_and_answer(self, answers):
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/interview", {})
        self.assertEqual(code, 202)
        self.assertEqual(self._await("interview")["state"], "done")
        return self.req("POST", f"/api/sessions/{self.sid}/interview/B1", {"answers": answers})

    def _generate_config(self):
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/generate-config", {"bundle_ids": ["B1"]})
        self.assertEqual(code, 202)
        return self._await("generate-config")

    def test_interview_generate_and_skip(self):
        code, rec = self._interview_and_answer({"q1": "Open_Orders"})    # q2 skipped
        self.assertEqual(code, 200)
        self.assertEqual(rec["status"], "incomplete")
        self.assertEqual(rec["unresolved"], ["q2"])
        self.assertEqual([a["q"] for a in rec["answers"]], ["q1"])
        # interview-data reflects the saved record
        _, data = self.req("GET", f"/api/sessions/{self.sid}/interview-data")
        self.assertEqual(len(data["interviews"]["B1"]["questions"]), 2)

    def test_full_chain_to_run(self):
        self._interview_and_answer({"q1": "Open_Orders"})                # q2 skipped
        st = self._generate_config()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["generated"], 1)

        _, data = self.req("GET", f"/api/sessions/{self.sid}/agents")
        agent = next(a for a in data["agents"] if a["bundle_id"] == "B1")
        self.assertIn("system:", agent["test_yaml"])
        self.assertIn("mcp_server_name", agent["live_yaml"])     # live has Pipedream
        self.assertNotIn("mcp_server_name", agent["test_yaml"])  # test has none
        # grounding carries the answered question as a user_answer source + the skip
        g = agent["grounding"]
        self.assertTrue(any(isinstance(i["source"], dict) and i["source"].get("type") == "user_answer"
                            for i in g["instructions"]))
        self.assertEqual(g["unresolved"], ["q2"])

        # Stage 4 runs the generated config
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/compile-test", {"bundle_ids": ["B1"]})
        self.assertEqual(code, 202)
        st = self._await("compile-test")
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["ran"], 1)
        self.assertEqual(st["runs"]["B1"]["sessions"][0]["session_id"], "sess_x")

    def test_run_without_config_rejected(self):
        code, data = self.req("POST", f"/api/sessions/{self.sid}/compile-test", {"bundle_ids": ["B1"]})
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "no_config")

    def test_stage4_bundles_default_to_config_ready(self):
        # before config generation: offered but not config_ready (unchecked by default)
        _, data = self.req("GET", f"/api/sessions/{self.sid}/stage4-bundles")
        self.assertFalse(next(x for x in data["bundles"] if x["id"] == "B1")["config_ready"])
        self._interview_and_answer({"q1": "x"})
        self._generate_config()
        _, data = self.req("GET", f"/api/sessions/{self.sid}/stage4-bundles")
        self.assertTrue(next(x for x in data["bundles"] if x["id"] == "B1")["config_ready"])

    def test_run_failure_recorded_not_fatal(self):
        self._interview_and_answer({"q1": "x"})
        self._generate_config()
        self.state.run_test_fn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        self.req("POST", f"/api/sessions/{self.sid}/compile-test", {"bundle_ids": ["B1"]})
        st = self._await("compile-test")
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["ran"], 0)
        self.assertIn("run_skipped", st["runs"]["B1"])

    def test_cleanup_platform_collects_ids(self):
        captured = {}
        def fake_cleanup(session_ids, file_ids, agent_ids):
            captured["args"] = (session_ids, file_ids, agent_ids)
            return {"removed": {"sessions": session_ids, "files": file_ids, "agents": agent_ids}, "errors": []}
        self.state.cleanup_fn = fake_cleanup
        self._interview_and_answer({"q1": "x"})
        self._generate_config()
        self.req("POST", f"/api/sessions/{self.sid}/compile-test", {"bundle_ids": ["B1"]})
        self._await("compile-test")
        code, data = self.req("POST", f"/api/sessions/{self.sid}/cleanup-platform")
        self.assertEqual(code, 200)
        self.assertEqual(captured["args"], (["sess_x"], ["file_0"], ["agent_x"]))


if __name__ == "__main__":
    unittest.main()
