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


class UIChainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ks = FakeKeystore("sk-ant-test")
        self.state = app.AppState(self.root, key_store=self.ks, interview_fn=fake_interview_fn,
                                  config_fn=fake_config_fn)
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

    def test_correction_threads_to_interview_and_build(self):
        # a worker correction saved at review reaches BOTH downstream engines'
        # bundle input (through _approved_for_stage4's re-validation)
        p = sessions.path(self.root, self.sid, sessions.REVIEWED)
        with open(p) as f:
            rb = json.load(f)
        rb["bundles"][0]["understanding"] = "corrected"
        rb["bundles"][0]["user_description"] = \
            "I enter each tracking number into the destination record."
        with open(p, "w") as f:
            json.dump(rb, f)
        captured = {}
        real_iv, real_cf = self.state.interview_fn, self.state.config_fn

        def iv_spy(bundle, event_log, model_fn=None):
            captured["interview"] = bundle
            return real_iv(bundle, event_log, model_fn=model_fn)

        def cf_spy(bundle, observed, answers, model_fn, unresolved=None, test_feedback=None):
            captured["config"] = bundle
            return real_cf(bundle, observed, answers, model_fn,
                           unresolved=unresolved, test_feedback=test_feedback)

        self.state.interview_fn, self.state.config_fn = iv_spy, cf_spy
        self._interview_and_answer({"q1": "x", "q2": "y"})
        self.assertEqual(self._build()["state"], "done")
        self.assertEqual(captured["interview"]["user_description"],
                         "I enter each tracking number into the destination record.")
        self.assertEqual(captured["config"]["user_description"],
                         "I enter each tracking number into the destination record.")

    def test_stale_answers_dropped_when_question_text_changes(self):
        # ids are positional (q1..qN): after a regenerate that rewrites a question,
        # the old answer must not silently attach to the different new q1
        self._interview_and_answer({"q1": "Open_Orders", "q2": "the source wins"})

        def new_iv(bundle, event_log, model_fn=None):
            qs = fake_interview_fn(bundle, event_log, model_fn=model_fn)["questions"]
            qs[0]["question"] = "Where does each tracking number go?"   # q1 rewritten
            return {"questions": qs}                                    # q2 unchanged

        self.state.interview_fn = new_iv
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/interview", {})
        self.assertEqual(code, 202)
        self.assertEqual(self._await("interview")["state"], "done")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/interview-data")
        rec = data["interviews"]["B1"]
        self.assertEqual([a["q"] for a in rec["answers"]], ["q2"])   # q1's stale answer dropped
        self.assertEqual(rec["status"], "incomplete")

    def test_interview_generate_and_skip(self):
        code, rec = self._interview_and_answer({"q1": "Open_Orders"})    # q2 skipped
        self.assertEqual(code, 200)
        self.assertEqual(rec["status"], "incomplete")
        self.assertEqual(rec["unresolved"], ["q2"])
        self.assertEqual([a["q"] for a in rec["answers"]], ["q1"])
        _, data = self.req("GET", f"/api/sessions/{self.sid}/interview-data")
        self.assertEqual(len(data["interviews"]["B1"]["questions"]), 2)

    def test_full_chain_config(self):
        self._interview_and_answer({"q1": "Open_Orders", "q2": "the source wins"})
        st = self._build()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["generated"], 1)

        _, data = self.req("GET", f"/api/sessions/{self.sid}/agents")
        agent = next(a for a in data["agents"] if a["bundle_id"] == "B1")
        self.assertIn("system:", agent["live_yaml"])
        self.assertIn("mcp_server_name", agent["live_yaml"])     # live has Pipedream
        # grounding carries the answered question as a user_answer source; a fully
        # answered interview leaves no unresolved key (the unresolved->grounding
        # pipe stays covered by test_interview.InterviewToConfigFeedTest)
        g = agent["grounding"]
        self.assertTrue(any(isinstance(i["source"], dict) and i["source"].get("type") == "user_answer"
                            for i in g["instructions"]))
        self.assertNotIn("unresolved", g)

    def test_build_bundles_status_before_and_after(self):
        _, data = self.req("GET", f"/api/sessions/{self.sid}/build-bundles")
        b = next(x for x in data["bundles"] if x["id"] == "B1")
        self.assertFalse(b["built"])
        self._interview_and_answer({"q1": "x", "q2": "y"})
        self._build()
        _, data = self.req("GET", f"/api/sessions/{self.sid}/build-bundles")
        b = next(x for x in data["bundles"] if x["id"] == "B1")
        self.assertTrue(b["built"])

    # --- mandatory questions phase -----------------------------------------
    def test_build_400_until_interview_complete(self):
        # before any interview: build is refused
        code, body = self.req("POST", f"/api/sessions/{self.sid}/build", {"bundle_ids": ["B1"]})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "interview_incomplete")
        # partial answers: still refused
        self._interview_and_answer({"q1": "Open_Orders"})                # q2 skipped
        code, body = self.req("POST", f"/api/sessions/{self.sid}/build", {"bundle_ids": ["B1"]})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "interview_incomplete")
        # every question answered: build proceeds
        code, rec = self.req("POST", f"/api/sessions/{self.sid}/interview/B1",
                             {"answers": {"q1": "Open_Orders", "q2": "the source wins"}})
        self.assertEqual(rec["status"], "complete")
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/build", {"bundle_ids": ["B1"]})
        self.assertEqual(code, 202)
        self.assertEqual(self._await("build")["state"], "done")

    def test_interview_complete_at_lifecycle(self):
        # full answers set the meta flag
        self._interview_and_answer({"q1": "a", "q2": "b"})
        self.assertTrue(sessions.read_meta(self.root, self.sid).get("interview_complete_at"))
        # re-review with a NEW approved bundle (no interview record) re-gates
        reviewed = json.loads(json.dumps(REVIEWED))
        b2 = json.loads(json.dumps(reviewed["bundles"][0]))
        b2["id"] = "B2"
        reviewed["bundles"].append(b2)
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", reviewed)
        self.assertEqual(code, 200)
        meta = sessions.read_meta(self.root, self.sid)
        self.assertIsNone(meta.get("interview_complete_at"))
        self.assertIsNone(meta.get("interview_at"))              # UI regenerates questions
        code, body = self.req("POST", f"/api/sessions/{self.sid}/build", {})
        self.assertEqual((code, body["error"]), (400, "interview_incomplete"))

    def test_safety_rules_ride_the_reserved_answer_and_survive_regeneration(self):
        """Frame 10's rules store as a reserved interview answer, like extra_notes.

        Two things have to hold. They must not count as a question — a bundle stays
        'complete' with them and gates nothing. And regenerating the questions must not
        throw them away: the worker set them once for every agent, and re-reviewing a
        workflow is not a reason to silently drop the conditions under which that agent
        stops and asks. The same guarantee extra_notes already has.
        """
        rules = "Stop and ask me when any of these is true:\n- Someone new: a contact it hasn't touched"
        code, body = self._interview_and_answer(
            {"q1": "a", "q2": "b", "safety_rules": rules})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "complete")          # never gates
        saved = {a["q"]: a["answer"] for a in body["answers"]}
        self.assertEqual(saved.get("safety_rules"), rules)
        self.assertNotIn("safety_rules", body["unresolved"])

        # regenerate the questions; the rules must still be there afterwards
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/interview", {})
        self.assertEqual(code, 202)
        self.assertEqual(self._await("interview")["state"], "done")
        rec = sessions.read_json_artifact(
            self.root, self.sid, sessions.interview_filename("B1"))
        kept = {a["q"]: a["answer"] for a in rec["answers"]}
        self.assertEqual(kept.get("safety_rules"), rules)

    def test_interview_vacuously_complete_with_zero_approved(self):
        reviewed = json.loads(json.dumps(REVIEWED))
        reviewed["bundles"][0]["disposition"] = "reject"
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", reviewed)
        self.assertEqual(code, 200)
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/interview", {})
        self.assertEqual(code, 202)
        self.assertEqual(self._await("interview")["state"], "done")
        self.assertTrue(sessions.read_meta(self.root, self.sid).get("interview_complete_at"))
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/build", {})
        self.assertEqual(code, 202)                              # nothing to interview -> builds


if __name__ == "__main__":
    unittest.main()
