import json
import tempfile
import threading
import time
import unittest
import urllib.request

from trace.ui import app, sessions


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


def fake_discovery(log_path, **kw):
    disc = {
        "bundles": [{"id": "B1", "name": "Intake", "description": "d", "trigger": "t",
                     "steps": ["s"], "frequency": "2x", "evidence_event_ids": ["e001"],
                     "why_automatable": "rule", "confidence": "high"}],
        "anti_bundles": [{"id": "A1", "name": "Judgment", "description": "d",
                          "why_judgment": "varies", "recommended_handling": "human",
                          "evidence_event_ids": ["e013"], "confidence": "high"}],
        "unclustered_or_noise_event_ids": ["e007"], "notes": "ok",
    }
    return disc, None


def fake_score(discovery, answer_key):
    return {"bundles_identified": 7, "bundles_total": 8, "passed": True,
            "automation_penalties": 0, "bundle_results": [], "anti_bundle_results": [],
            "sensitivity": {}, "overlap_threshold": 0.5, "rubric": answer_key.get("rubric", {})}


class UIDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ks = FakeKeystore()
        self.state = app.AppState(self._tmp.name, key_store=self.ks,
                                  discovery_fn=fake_discovery, score_fn=fake_score)
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

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

    def test_fixtures_listed(self):
        code, data = self.req("GET", "/api/fixtures")
        self.assertEqual(code, 200)
        names = {f["name"] for f in data["fixtures"]}
        self.assertIn("marcy", names)
        self.assertTrue(next(f for f in data["fixtures"] if f["name"] == "marcy")["has_answer_key"])

    def test_load_fixture_creates_session_with_event_log(self):
        code, data = self.req("POST", "/api/load-fixture", {"name": "marcy"})
        self.assertEqual(code, 200)
        sid = data["session_id"]
        self.assertTrue(data["has_answer_key"])
        art = sessions.artifacts(self._tmp.name, sid)
        self.assertTrue(art["event_log"])
        self.assertFalse(art["discovery"])

    def test_unknown_fixture_404(self):
        code, _ = self.req("POST", "/api/load-fixture", {"name": "nope"})
        self.assertEqual(code, 404)

    def test_discovery_dry_run_renders_prompt_and_log_without_key(self):
        _, ld = self.req("POST", "/api/load-fixture", {"name": "marcy"})
        sid = ld["session_id"]
        code, data = self.req("POST", f"/api/sessions/{sid}/discovery-dry-run")
        self.assertEqual(code, 200)
        self.assertGreater(data["event_count"], 0)
        self.assertIn("workflow discovery engine", data["system_prompt"].lower())
        self.assertIn("e001", data["rendered_log"])

    def test_discover_requires_key(self):
        _, ld = self.req("POST", "/api/load-fixture", {"name": "marcy"})
        sid = ld["session_id"]
        code, data = self.req("POST", f"/api/sessions/{sid}/discover")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "no_api_key")

    def test_discover_then_read_and_score(self):
        self.ks.key = "sk-ant-test"
        _, ld = self.req("POST", "/api/load-fixture", {"name": "marcy"})
        sid = ld["session_id"]
        code, _ = self.req("POST", f"/api/sessions/{sid}/discover")
        self.assertEqual(code, 202)
        # poll discovery job
        for _ in range(50):
            _, st = self.req("GET", f"/api/sessions/{sid}/discover/status")
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(st["state"], "done")

        code, disc = self.req("GET", f"/api/sessions/{sid}/discovery")
        self.assertEqual(code, 200)
        self.assertEqual(disc["bundles"][0]["id"], "B1")

        # score uses the fixture's answer key path stored in meta
        code, sc = self.req("POST", f"/api/sessions/{sid}/score")
        self.assertEqual(code, 200)
        self.assertEqual(sc["bundles_identified"], 7)
        self.assertTrue(sc["passed"])

        code, sc2 = self.req("GET", f"/api/sessions/{sid}/scorecard")
        self.assertEqual(code, 200)
        self.assertEqual(sc2["bundles_total"], 8)

    def test_score_without_discovery_rejected(self):
        self.ks.key = "sk-ant-test"
        sid = sessions.create_session(self._tmp.name)
        code, data = self.req("POST", f"/api/sessions/{sid}/score")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "not_discovered")


if __name__ == "__main__":
    unittest.main()
