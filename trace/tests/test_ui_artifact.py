import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from trace.ui import app, sessions

EVENTS = [
    {"id": "e1", "ts_start": "t1", "app": "Mail", "window": "Inbox", "action": "read_email",
     "detail": "read order from Acme", "confidence": "high"},
    {"id": "e2", "ts_start": "t2", "app": "Sheet", "window": "Orders", "action": "enter_data",
     "detail": "entered Acme 3 widgets", "confidence": "high"},
]
DISCOVERY = {
    "bundles": [{"id": "B1", "name": "Order intake", "description": "d", "trigger": "email",
                 "steps": ["read", "enter"], "frequency": "1x",
                 "evidence_event_ids": ["e1", "e2"], "why_automatable": "rules", "confidence": "high"}],
    "anti_bundles": [{"id": "A1", "name": "Pricing", "description": "d", "why_judgment": "discretionary",
                      "recommended_handling": "human", "evidence_event_ids": ["e1"], "confidence": "high"}],
    "unclustered_or_noise_event_ids": [], "notes": "",
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


def fake_artifact(reviewed_path, event_log_path):
    rb = json.load(open(reviewed_path))
    results = []
    for b in rb.get("bundles", []):
        if b.get("disposition") == "approve_for_build":
            results.append({"id": b["id"], "classification": "bundle", "built": True,
                            "filename": f"spec_{b['id']}.md",
                            "markdown": f"# Build spec: {b.get('name')}\n## 1. Intent\n{b.get('description')}\n"})
        else:
            results.append({"id": b["id"], "classification": "bundle", "built": False,
                            "filename": None, "markdown": None})
    for a in rb.get("anti_bundles", []):
        results.append({"id": a["id"], "classification": "anti_bundle", "built": False,
                        "filename": f"spec_{a['id']}_manual.md", "markdown": "# Manual handling"})

    class R:
        errors = []
        flags = []
    return results, R()


class UIArtifactTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ks = FakeKeystore()
        self.state = app.AppState(self.root, key_store=self.ks, artifact_fn=fake_artifact)
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        # a session with an event log + discovery
        self.sid = sessions.create_session(self.root)
        with open(sessions.path(self.root, self.sid, sessions.EVENT_LOG), "w") as f:
            json.dump(EVENTS, f)
        with open(sessions.path(self.root, self.sid, sessions.DISCOVERY), "w") as f:
            json.dump(DISCOVERY, f)

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

    def test_review_data_seeds_with_examples(self):
        code, data = self.req("GET", f"/api/sessions/{self.sid}/review-data")
        self.assertEqual(code, 200)
        self.assertEqual(data["reviewed"]["bundles"][0]["disposition"], "approve_for_build")
        # verbatim examples available for rendering
        self.assertEqual(data["examples_by_id"]["e2"]["detail"], "entered Acme 3 widgets")

    def test_save_reviewed_validates_and_persists(self):
        _, rd = self.req("GET", f"/api/sessions/{self.sid}/review-data")
        reviewed = rd["reviewed"]
        # an edit + a checkpoint + a reclassification of the anti-bundle to a bundle
        reviewed["bundles"][0]["description"] = "EDITED description"
        reviewed["bundles"][0]["human_checkpoints"] = [{"step_index": 1, "note": "verify totals"}]
        code, data = self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", reviewed)
        self.assertEqual(code, 200)
        self.assertTrue(data["saved"])
        # persisted
        code, saved = self.req("GET", f"/api/sessions/{self.sid}/reviewed-bundles")
        self.assertEqual(saved["bundles"][0]["description"], "EDITED description")
        self.assertEqual(saved["bundles"][0]["human_checkpoints"][0]["note"], "verify totals")

    def test_save_reviewed_persists_understanding(self):
        _, rd = self.req("GET", f"/api/sessions/{self.sid}/review-data")
        reviewed = rd["reviewed"]
        # seeded unresolved on both shapes
        self.assertEqual(reviewed["bundles"][0]["understanding"], "")
        self.assertEqual(reviewed["anti_bundles"][0]["understanding"], "")
        reviewed["bundles"][0]["understanding"] = "corrected"
        reviewed["bundles"][0]["user_description"] = \
            "I also enter each tracking number into the destination record."
        reviewed["anti_bundles"][0]["understanding"] = "confirmed"
        code, data = self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", reviewed)
        self.assertEqual(code, 200)
        self.assertTrue(data["saved"])
        code, saved = self.req("GET", f"/api/sessions/{self.sid}/reviewed-bundles")
        self.assertEqual(saved["bundles"][0]["user_description"],
                         "I also enter each tracking number into the destination record.")
        self.assertEqual(saved["bundles"][0]["understanding"], "corrected")
        self.assertEqual(saved["anti_bundles"][0]["understanding"], "confirmed")

    def test_corrected_without_text_repaired_on_save(self):
        _, rd = self.req("GET", f"/api/sessions/{self.sid}/review-data")
        reviewed = rd["reviewed"]
        reviewed["bundles"][0]["understanding"] = "corrected"    # no correction text
        code, data = self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", reviewed)
        self.assertEqual(code, 200)
        self.assertEqual(data["reviewed"]["bundles"][0]["understanding"], "")
        self.assertTrue(any("corrected" in e for e in data["errors"]))

    def test_reclassification_routes_item(self):
        _, rd = self.req("GET", f"/api/sessions/{self.sid}/review-data")
        reviewed = rd["reviewed"]
        # reclassify the bundle as anti_bundle
        reviewed["bundles"][0]["classification"] = "anti_bundle"
        reviewed["bundles"][0]["why_judgment"] = "actually needs a human"
        code, data = self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", reviewed)
        self.assertEqual(code, 200)
        ids_in_anti = {a["id"] for a in data["reviewed"]["anti_bundles"]}
        self.assertIn("B1", ids_in_anti)  # moved across

    def test_generate_specs_requires_key_when_approved(self):
        _, rd = self.req("GET", f"/api/sessions/{self.sid}/review-data")
        self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", rd["reviewed"])
        code, data = self.req("POST", f"/api/sessions/{self.sid}/generate-specs")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "no_api_key")

    def test_generate_specs_end_to_end(self):
        self.ks.key = "sk-ant-test"
        _, rd = self.req("GET", f"/api/sessions/{self.sid}/review-data")
        self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", rd["reviewed"])
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/generate-specs")
        self.assertEqual(code, 202)
        for _ in range(50):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/generate-specs/status")
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["built"], 1)
        code, specs = self.req("GET", f"/api/sessions/{self.sid}/specs")
        self.assertEqual(code, 200)
        names = {s["filename"] for s in specs["specs"]}
        self.assertIn("spec_B1.md", names)
        self.assertIn("spec_A1_manual.md", names)

    def test_specs_dry_run_no_key(self):
        _, rd = self.req("GET", f"/api/sessions/{self.sid}/review-data")
        self.req("POST", f"/api/sessions/{self.sid}/reviewed-bundles", rd["reviewed"])
        code, data = self.req("POST", f"/api/sessions/{self.sid}/specs-dry-run")
        self.assertEqual(code, 200)
        self.assertEqual(data["approved_count"], 1)
        self.assertIn("entered Acme 3 widgets", data["bundles"][0]["material"])

    def test_generate_without_review_rejected(self):
        code, data = self.req("POST", f"/api/sessions/{self.sid}/generate-specs")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "not_reviewed")


if __name__ == "__main__":
    unittest.main()
