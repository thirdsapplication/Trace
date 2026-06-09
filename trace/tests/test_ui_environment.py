import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from trace.ui import app, sessions
from trace.artifact.artifact import generate_specs as real_generate_specs

EVENTS = [
    {"id": "e1", "ts_start": "t1", "app": "Viewer", "window": "form-1001.pdf",
     "action": "open_document", "detail": "read the form", "confidence": "high"},
    {"id": "e2", "ts_start": "t2", "app": "Grid", "window": "Ledger — Tab1",
     "action": "enter_data", "detail": "entered the row", "confidence": "high"},
]
REVIEWED = {
    "bundles": [{"id": "B1", "classification": "bundle", "name": "Intake",
                 "description": "d", "trigger": "t", "steps": ["read", "enter"],
                 "disposition": "approve_for_build", "human_checkpoints": [],
                 "included_example_event_ids": ["e1", "e2"], "added_acceptance_criteria": [],
                 "preferred_output": "", "evidence_event_ids": ["e1", "e2"],
                 "confidence": "high", "reclassified_from": None,
                 "original_reason_for_judgment": ""}],
    "anti_bundles": [], "notes": "",
}


class FakeKeystore:
    def __init__(self, key=None):
        self.key = key

    def info(self):
        return {"saved": bool(self.key), "backend": "memory", "masked_tail": None}

    def get_key(self):
        return self.key

    def save_key(self, k):
        self.key = k
        return "memory"

    def clear_key(self):
        self.key = None


def artifact_with_stub(reviewed_path, event_log_path):
    # real engine (so env folds in) but a stub model so no key/network is needed
    return real_generate_specs(reviewed_path, event_log_path,
                               model_fn=lambda s, u: json.dumps(
                                   {"intent": "i", "derived_acceptance_criteria": [],
                                    "example_assessments": {}, "notes": ""}),
                               env_dir=os.path.dirname(reviewed_path))


class UIEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ks = FakeKeystore("sk-ant-test")
        self.state = app.AppState(self.root, key_store=self.ks, artifact_fn=artifact_with_stub)
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

    def req(self, method, path, body=None, raw=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {} if raw is not None else {"Content-Type": "application/json"}
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_environment_data_seeds_requests(self):
        code, data = self.req("GET", f"/api/sessions/{self.sid}/environment-data")
        self.assertEqual(code, 200)
        self.assertEqual(data["approved_ids"], ["B1"])
        rec = data["environments"]["B1"]
        roles = sorted(a["role"] for a in rec["artifacts"])
        self.assertEqual(roles, ["input", "output"])
        self.assertEqual(rec["status"], "incomplete")

    def test_upload_extracts_and_marks_uploaded(self):
        # output request is index 1 (inputs sorted first); upload a CSV
        csv = b"Customer,Item,OrderId\nAcme,widgets,1\n"
        code, rec = self.req(
            "POST", f"/api/sessions/{self.sid}/environment/B1/upload?artifact=1&filename=ledger.csv",
            raw=csv)
        self.assertEqual(code, 200)
        out = rec["artifacts"][1]
        self.assertTrue(out["uploaded"])
        self.assertEqual(out["kind"], "spreadsheet")
        self.assertEqual(out["extracted"]["headers"], ["Customer", "Item", "OrderId"])
        # a derived question should now exist (OrderId not in any source)
        self.assertTrue(any("orderid" in q["question"].lower() for q in rec["answers"]))

    def test_save_environment_answers_and_status(self):
        self.req("POST", f"/api/sessions/{self.sid}/environment/B1/upload?artifact=0&filename=f.pdf",
                 raw=b"%PDF-1.4 fake")
        self.req("POST", f"/api/sessions/{self.sid}/environment/B1/upload?artifact=1&filename=l.csv",
                 raw=b"A,B\n1,2\n")
        _, rec = self.req("GET", f"/api/sessions/{self.sid}/environment/B1")
        # answers alone don't complete it; a user instruction per artifact does
        code, saved = self.req("POST", f"/api/sessions/{self.sid}/environment/B1", rec)
        self.assertEqual(saved["status"], "incomplete")
        for a in rec["artifacts"]:
            a["user_instruction"] = "use this file as instructed"
        code, saved = self.req("POST", f"/api/sessions/{self.sid}/environment/B1", rec)
        self.assertEqual(code, 200)
        self.assertEqual(saved["status"], "complete")

    def test_generate_specs_folds_uploaded_artifact(self):
        self.req("POST", f"/api/sessions/{self.sid}/environment/B1/upload?artifact=1&filename=ledger.csv",
                 raw=b"Customer,Item,OrderId\nAcme,widgets,1\n")
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/generate-specs")
        self.assertEqual(code, 202)
        for _ in range(50):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/generate-specs/status")
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(st["state"], "done")
        _, specs = self.req("GET", f"/api/sessions/{self.sid}/specs")
        md = next(s["markdown"] for s in specs["specs"] if s["filename"] == "spec_B1.md")
        self.assertIn("Real uploaded artifacts", md)
        self.assertIn("Raw file (read live)", md)         # the raw file always travels
        self.assertIn("(live-checked against the uploaded file)", md)


if __name__ == "__main__":
    unittest.main()
