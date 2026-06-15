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


def fake_config_fn(bundle, observed, answers, model_fn, unresolved=None, test_feedback=None):
    # real assembly, stub the model content -> exercises the actual configgen path
    return configgen.compile_config(bundle, observed, answers,
                                    lambda s, u: json.dumps(CONFIG_CONTENT),
                                    unresolved=unresolved, test_feedback=test_feedback)


def fake_gate_fn(advanced=True):
    """A hidden-gate stub returning a verdict record (ignores the real client/judge)."""
    def gate(bundle, test_dict, criteria, event_log, events_path=None):
        return {"bundle_id": bundle["id"],
                "overall": "pass" if advanced else "fail",
                "per_criterion": [{"criterion": (criteria[0] if criteria else "x"),
                                   "verdict": "pass" if advanced else "fail", "reason": "stub"}],
                "notes": "", "trigger": "an order arrived", "transcript": "did the work",
                "session_id": "gate_sess_x", "status": "completed", "error": None,
                "advanced": advanced, "graded_at": "now"}
    return gate


class UIChainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ks = FakeKeystore("sk-ant-test")
        self.state = app.AppState(self.root, key_store=self.ks, interview_fn=fake_interview_fn,
                                  config_fn=fake_config_fn, gate_fn=fake_gate_fn())
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

    def _build(self):
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/build", {"bundle_ids": ["B1"]})
        self.assertEqual(code, 202)
        return self._await("build")

    def test_interview_generate_and_skip(self):
        code, rec = self._interview_and_answer({"q1": "Open_Orders"})    # q2 skipped
        self.assertEqual(code, 200)
        self.assertEqual(rec["status"], "incomplete")
        self.assertEqual(rec["unresolved"], ["q2"])
        self.assertEqual([a["q"] for a in rec["answers"]], ["q1"])
        _, data = self.req("GET", f"/api/sessions/{self.sid}/interview-data")
        self.assertEqual(len(data["interviews"]["B1"]["questions"]), 2)

    def test_full_chain_config_and_gate(self):
        self._interview_and_answer({"q1": "Open_Orders"})                # q2 skipped
        st = self._build()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["generated"], 1)
        self.assertEqual(st["gated"], 1)
        self.assertTrue(st["verdicts"]["B1"])                            # gate advanced

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
        # the hidden gate verdict is attached and advanced
        self.assertTrue(agent["gate"]["advanced"])
        self.assertEqual(agent["gate"]["overall"], "pass")

    def test_held_agent_not_advanced(self):
        self.state.gate_fn = fake_gate_fn(advanced=False)
        self._interview_and_answer({"q1": "x"})
        st = self._build()
        self.assertEqual(st["state"], "done")
        self.assertFalse(st["verdicts"]["B1"])                           # held
        _, data = self.req("GET", f"/api/sessions/{self.sid}/build-bundles")
        self.assertFalse(next(x for x in data["bundles"] if x["id"] == "B1")["advanced"])

    def test_build_bundles_status_before_and_after(self):
        _, data = self.req("GET", f"/api/sessions/{self.sid}/build-bundles")
        b = next(x for x in data["bundles"] if x["id"] == "B1")
        self.assertFalse(b["built"])
        self.assertFalse(b["advanced"])
        self._interview_and_answer({"q1": "x"})
        self._build()
        _, data = self.req("GET", f"/api/sessions/{self.sid}/build-bundles")
        b = next(x for x in data["bundles"] if x["id"] == "B1")
        self.assertTrue(b["built"])
        self.assertTrue(b["advanced"])

    def test_gate_failure_recorded_not_fatal(self):
        self._interview_and_answer({"q1": "x"})
        self.state.gate_fn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        st = self._build()
        self.assertEqual(st["state"], "done")            # never a hard failure
        self.assertEqual(st["generated"], 1)             # config still written
        self.assertIn("B1", st["errors"])

    def test_cleanup_harvests_gate_session(self):
        captured = {}
        def fake_cleanup(session_ids, file_ids, agent_ids):
            captured["args"] = (session_ids, file_ids, agent_ids)
            return {"removed": {"sessions": session_ids, "files": file_ids, "agents": agent_ids}, "errors": []}
        self.state.cleanup_fn = fake_cleanup
        self._interview_and_answer({"q1": "x"})
        self._build()
        code, data = self.req("POST", f"/api/sessions/{self.sid}/cleanup-platform")
        self.assertEqual(code, 200)
        sess_ids, _files, _agents = captured["args"]
        self.assertIn("gate_sess_x", sess_ids)           # the kept gate session is reaped


if __name__ == "__main__":
    unittest.main()
