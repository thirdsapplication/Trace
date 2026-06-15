"""Phase 5 — dry run + feedback (offline, FakeReader + FakeRunClient + stub replay).

Verifies: the real snapshot is READ-ONLY and saved as a mounted COPY; a held-out real
trigger is used (with the capture-reconstructed fallback); the worker-facing payload is
the plain replay array, never the raw transcript; the agent has no connection (real
source structurally untouched); and a thumbs-down correction flows into Phase-1
generation as a user_test_feedback source.
"""

import json
import threading
import time
import unittest
import urllib.request

from trace.agent import configgen, dryrun
from trace.tests.test_configgen import _bundle, _load, _stub as _config_stub
from trace.tests.test_ui_agent import EVENTS, REVIEWED, FakeKeystore, fake_config_fn, fake_gate_fn
from trace.tests.test_connect import StubConnectClient
from trace.ui import app, sessions


# a connection record (Phase 4 shape) with two usable apps
CONNECTION = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
              "apps": {"gmail": {"usable": True, "binding_ref": "bind-gmail"},
                       "google_sheets": {"usable": True, "binding_ref": "bind-sheets"}}}


class FakeReader:
    """READ-ONLY: returns a destination snapshot + a recent held-out trigger. Exposes
    NO write method — a test asserts the orchestration only ever reads."""

    def __init__(self, recent=True):
        self.calls = []
        self.recent = recent

    def read_destination(self, external_user_id, app, binding_ref):
        self.calls.append(("read_destination", external_user_id, app))
        return {"kind": "spreadsheet", "columns": ["order_id", "customer", "total"],
                "rows": [["PO-1", "Acme", "30"]], "raw": None}

    def read_recent_trigger(self, external_user_id, app, binding_ref, limit=1):
        self.calls.append(("read_recent_trigger", external_user_id, app))
        return ([{"detail": "Recent inbound PO email from Beta Cafe, total 55.", "raw": {}}]
                if self.recent else [])


class FakeRunClient:
    """Mounts the copy + returns a transcript + downloaded outputs. Records resources so
    we can assert the copy is mounted; has NO write/update of the real source."""

    def __init__(self):
        self.uploaded, self.last_resources, self.messages = [], None, []

    def upload_file(self, path):
        self.uploaded.append(path)
        return f"file_{len(self.uploaded)}"

    def create_or_reuse_agent(self, envelope):
        return {"id": "agent_0", "name": envelope["name"], "reused": False}

    def create_environment(self, defaults=None):
        return "env_0"

    def create_session(self, agent_id, environment_id, resources, title=None):
        self.last_resources = resources
        return "sess_0"

    def send_trigger_and_collect(self, session_id, message):
        self.messages.append(message)
        return [
            {"type": "agent.message", "text": "Read the order from Beta Cafe, total 55.", "name": None},
            {"type": "agent.tool_use", "text": "", "name": "write_file"},
            {"type": "agent.message", "text": "Entered Beta Cafe / 55 into the copy.", "name": None},
            {"type": "session.status_idle", "text": "", "name": None},
        ]

    def download_outputs(self, session_id, exclude_ids=None):
        return {"destination_google_sheets.csv": "order_id,customer,total\nPO-1,Acme,30\nBeta,55\n"}, []


def _stub_replay(out=None):
    out = out or ["It read a new order from Beta Cafe for 55.",
                  "It added Beta Cafe / 55 to the orders sheet."]
    return lambda system, transcript: json.dumps(out)


def _test_dict(domain="marcy"):
    event_log, ak = _load(domain)
    bundle = _bundle(ak["bundles"][0])
    compiled = configgen.compile_config(
        bundle, configgen.make_engine_inputs(bundle, event_log)[1], [], _config_stub())
    return bundle, event_log, compiled["test"]["dict"]


class DryRunTest(unittest.TestCase):
    def test_snapshot_is_read_only_copy_and_mounted(self):
        import tempfile
        bundle, ev, td = _test_dict()
        reader, client = FakeReader(), FakeRunClient()
        with tempfile.TemporaryDirectory() as d:
            rec = dryrun.dry_run(bundle, td, CONNECTION, ev, d, reader, client, _stub_replay())
        # the destination was copied to a file and mounted under MOUNT_PATH
        self.assertEqual(len(client.uploaded), 1)
        self.assertTrue(client.last_resources)
        self.assertIn("/workspace/files/", client.last_resources[0]["mount_path"])
        # reader only ever READ (no write method exists / was called)
        self.assertTrue(all(c[0].startswith("read_") for c in reader.calls))
        self.assertTrue(rec["real_source_untouched"])

    def test_held_out_trigger_from_connection(self):
        bundle, ev, td = _test_dict()
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rec = dryrun.dry_run(bundle, td, CONNECTION, ev, d, FakeReader(recent=True),
                                 FakeRunClient(), _stub_replay())
        self.assertEqual(rec["trigger_source"], "connection")

    def test_fallback_reconstructs_trigger_from_capture(self):
        bundle, ev, td = _test_dict()
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            snap = dryrun.read_snapshot(FakeReader(recent=False), CONNECTION, bundle, ev, d)
        self.assertEqual(snap["trigger_source"], "reconstructed")
        self.assertTrue(snap["trigger_text"])      # a usable trigger from the capture

    def test_worker_sees_replay_not_transcript(self):
        bundle, ev, td = _test_dict()
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rec = dryrun.dry_run(bundle, td, CONNECTION, ev, d, FakeReader(), FakeRunClient(),
                                 _stub_replay())
        self.assertIsInstance(rec["replay"], list)
        self.assertTrue(all(isinstance(s, str) for s in rec["replay"]))
        # the replay does not leak tool names / the raw transcript
        joined = " ".join(rec["replay"]).lower()
        self.assertNotIn("tool_use", joined)
        self.assertNotIn(rec["transcript"], rec["replay"])

    def test_runs_on_both_domains(self):
        import tempfile
        for dom in ("marcy", "recruiter"):
            bundle, ev, td = _test_dict(dom)
            with tempfile.TemporaryDirectory() as d:
                rec = dryrun.dry_run(bundle, td, CONNECTION, ev, d, FakeReader(), FakeRunClient(),
                                     _stub_replay())
            self.assertTrue(rec["replay"])
            self.assertIn(rec["status"], ("completed", "errored"))

    def test_replay_parse_defensive(self):
        fenced = lambda s, t: "```json\n" + json.dumps(["one", "two"]) + "\n```"
        self.assertEqual(dryrun.render_replay("x", fenced), ["one", "two"])
        self.assertEqual(dryrun.render_replay("x", lambda s, t: "not json"), [])


class FeedbackToConfigTest(unittest.TestCase):
    """A dry-run correction flows into Phase-1 generation as a user_test_feedback source."""

    def test_test_feedback_threads_into_grounding(self):
        # a config stub that sources a step to the test feedback when present
        CANNED = {
            "description": "x", "role": "y", "tools_and_artifacts": ["Email"],
            "steps": [{"text": "Enter the total exactly as on the order.",
                       "source": {"type": "user_test_feedback", "note": "use the order total, not today's date"}}],
            "working_rules": [], "requirements": [],
        }
        ev, ak = _load("marcy")
        b = _bundle(ak["bundles"][0])
        observed = configgen.make_engine_inputs(b, ev)[1]
        compiled = configgen.compile_config(
            b, observed, [], lambda s, u: json.dumps(CANNED),
            test_feedback=[{"note": "use the order total, not today's date"}])
        # the prompt material carried the feedback...
        self.assertIn("test_feedback", configgen.build_prompt_material({}, [], [], [{"note": "x"}]))
        # ...and the grounding records a user_test_feedback source (observed False)
        fb = [i for i in compiled["grounding"]["instructions"]
              if isinstance(i["source"], dict) and i["source"].get("type") == "user_test_feedback"]
        self.assertTrue(fb)
        self.assertFalse(fb[0]["observed"])

    def test_no_test_feedback_is_additive_noop(self):
        # omitting test_feedback leaves build_prompt_material output unchanged
        self.assertNotIn("test_feedback", configgen.build_prompt_material({}, [], []))


def _fake_dryrun_fn(bundle, test_dict, connection_record, event_log, save_dir, round=0):
    return {"bundle_id": bundle["id"], "replay": [f"It read a new order (round {round})."],
            "outputs": {}, "trigger_source": "connection", "session_id": "sess_dr",
            "status": "completed", "error": None, "round": round,
            "real_source_untouched": True, "transcript": "internal — not shown"}


class UIDryrunTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(self.root, key_store=FakeKeystore("sk-ant-test"),
                                  config_fn=fake_config_fn, gate_fn=fake_gate_fn(advanced=True),
                                  connect_fn=lambda: StubConnectClient(identity_email="worker@acme.com"),
                                  dryrun_fn=_fake_dryrun_fn)
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

    def _build_and_connect(self):
        self.req("POST", f"/api/sessions/{self.sid}/build", {"bundle_ids": ["B1"]})
        self.assertEqual(self._await("build")["state"], "done")
        self.req("POST", f"/api/sessions/{self.sid}/connection-identity", {"email": "worker@acme.com"})
        self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/callback", {})
        self.assertEqual(self._await("connect")["state"], "done")

    def test_eligible_only_after_gate_and_connection(self):
        # before build+connect, not eligible
        code, data = self.req("POST", f"/api/sessions/{self.sid}/dryrun", {})
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "not_eligible")
        self._build_and_connect()
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/dryrun", {})
        self.assertEqual(code, 202)
        self.assertEqual(self._await("dryrun")["state"], "done")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/dryrun-bundles")
        b = next(x for x in data["bundles"] if x["id"] == "B1")
        self.assertTrue(b["replay"])                       # the plain replay is surfaced
        self.assertEqual(b["round"], 0)

    def test_feedback_regenerates_reruns_and_caps(self):
        self._build_and_connect()
        self.req("POST", f"/api/sessions/{self.sid}/dryrun", {})
        self._await("dryrun")
        # round 1 feedback
        code, data = self.req("POST", f"/api/sessions/{self.sid}/dryrun/feedback",
                              {"bundle_id": "B1", "correction": "use the order total, not today's date"})
        self.assertEqual(code, 200)
        self.assertFalse(data["capped"])
        self.assertEqual(self._await("dryrun")["state"], "done")
        # feedback persisted + fed into regeneration
        fb = sessions.read_json_artifact(self.root, self.sid, sessions.feedback_name("B1"))
        self.assertEqual(len(fb["corrections"]), 1)
        _, dd = self.req("GET", f"/api/sessions/{self.sid}/dryrun-bundles")
        self.assertEqual(next(x for x in dd["bundles"] if x["id"] == "B1")["round"], 1)
        # round 2 feedback
        self.req("POST", f"/api/sessions/{self.sid}/dryrun/feedback",
                 {"bundle_id": "B1", "correction": "second fix"})
        self._await("dryrun")
        # 3rd feedback is capped (DRYRUN_MAX_ROUNDS = 2)
        _, capped = self.req("POST", f"/api/sessions/{self.sid}/dryrun/feedback",
                            {"bundle_id": "B1", "correction": "third"})
        self.assertTrue(capped["capped"])


if __name__ == "__main__":
    unittest.main()
