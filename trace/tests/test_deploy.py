"""Phase 6 — live deployment (offline, FakeDeployClient, two domains).

Verifies: the LIVE config is deployed; the bound vault is injected at session creation
(per-customer isolation); outbound writes/sends are held for approval while reads
proceed; one-tap approve/reject flips a held action; the live session is persisted and
harvestable for deletion; runs unchanged on both fixtures.
"""

import json
import threading
import time
import unittest
import urllib.request

from trace.agent import configgen, deploy
from trace.tests.test_configgen import _bundle, _load, _stub as _config_stub
from trace.tests.test_ui_agent import EVENTS, REVIEWED, FakeKeystore, fake_config_fn, fake_gate_fn
from trace.tests.test_connect import StubConnectClient
from trace.tests.test_dryrun import _fake_dryrun_fn
from trace.ui import app, sessions

CONNECTION = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
              "apps": {"gmail": {"usable": True, "binding_ref": "bind-gmail"}}}


class FakeDeployClient:
    """Records the vault injected + the start config; returns a live session + canned held
    outbound actions; supports approve/reject + deletion. NEVER acts on real data."""

    def __init__(self, held=None):
        self.agents, self.sessions, self.deleted = [], [], []
        self.last_vault_ids, self.start = None, None
        self._held = {a["action_id"]: dict(a, status="held") for a in (held or [
            {"action_id": "act_1", "kind": "send", "summary": "reply to the customer"},
            {"action_id": "act_2", "kind": "write", "summary": "add a row to the sheet"}])}

    def create_or_reuse_agent(self, envelope):
        self.agents.append(envelope)
        return {"id": "agent_live", "name": envelope["name"], "reused": False}

    def create_environment(self, defaults=None):
        return "env_0"

    def create_session(self, agent_id, environment_id, resources, title=None, vault_ids=None):
        self.last_vault_ids = vault_ids
        sid = f"live_sess_{len(self.sessions)}"
        self.sessions.append(sid)
        return sid

    def set_trigger(self, agent_id, trigger=None, schedule=None):
        self.start = {"trigger": trigger, "schedule": schedule}
        return "trig_0"

    def list_pending_approvals(self, session_id):
        return [{"action_id": k, "kind": v["kind"], "summary": v["summary"]}
                for k, v in self._held.items() if v["status"] == "held"]

    def approve_action(self, session_id, action_id):
        self._held[action_id]["status"] = "approved"

    def reject_action(self, session_id, action_id):
        self._held[action_id]["status"] = "rejected"

    def delete_session(self, sid):
        self.deleted.append(sid)


def _live_dict(domain="marcy"):
    event_log, ak = _load(domain)
    bundle = _bundle(ak["bundles"][0])
    compiled = configgen.compile_config(
        bundle, configgen.make_engine_inputs(bundle, event_log)[1], [], _config_stub())
    return bundle, compiled["live"]["dict"]


class DeployTest(unittest.TestCase):
    def test_live_deployed_with_vault_injected(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, CONNECTION, client, trigger="on a new inbound PO")
        # the LIVE config was deployed (it carries the Pipedream MCP server)
        self.assertTrue(client.agents[0].get("mcp_servers"))
        # the bound vault was injected at session creation (the single injection point)
        self.assertEqual(client.last_vault_ids, ["vlt-s1"])
        self.assertEqual(rec["vault_ref"], "vlt-s1")
        self.assertEqual(rec["status"], "live")
        self.assertEqual(rec["start"]["mode"], "trigger")

    def test_outbound_writes_and_sends_are_held(self):
        bundle, live = _live_dict()
        rec = deploy.deploy_live(bundle, live, CONNECTION, FakeDeployClient())
        self.assertTrue(rec["held_actions"])                  # writes/sends held
        self.assertTrue(all(a["status"] == "held" for a in rec["held_actions"]))
        self.assertEqual({a["kind"] for a in rec["held_actions"]}, {"send", "write"})

    def test_one_tap_approve_and_reject(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, CONNECTION, client)
        sid = rec["session_id"]
        self.assertEqual(deploy.approve(client, sid, "act_1")["status"], "approved")
        self.assertEqual(deploy.reject(client, sid, "act_2")["status"], "rejected")
        # the held list drains as actions are resolved
        self.assertEqual(deploy.list_held(client, sid), [])

    def test_schedule_mode(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, CONNECTION, client, schedule="0 9 * * *")
        self.assertEqual(rec["start"]["mode"], "schedule")
        self.assertEqual(client.start["schedule"], "0 9 * * *")

    def test_per_customer_isolation(self):
        bundle, live = _live_dict()
        c1, c2 = FakeDeployClient(), FakeDeployClient()
        deploy.deploy_live(bundle, live, CONNECTION, c1)
        deploy.deploy_live(bundle, live, {"external_user_id": "trace-s2", "vault_ref": "vlt-s2",
                                          "apps": {"gmail": {"usable": True}}}, c2)
        self.assertEqual(c1.last_vault_ids, ["vlt-s1"])       # each client only saw its own vault
        self.assertEqual(c2.last_vault_ids, ["vlt-s2"])
        self.assertNotEqual(c1.last_vault_ids, c2.last_vault_ids)

    def test_runs_on_both_domains(self):
        for dom in ("marcy", "recruiter"):
            bundle, live = _live_dict(dom)
            rec = deploy.deploy_live(bundle, live, CONNECTION, FakeDeployClient())
            self.assertEqual(rec["status"], "live")
            self.assertTrue(rec["session_id"])

    def test_imports_no_platform_sdk(self):
        import inspect
        src = inspect.getsource(deploy)
        self.assertNotIn("import anthropic", src)


def _fake_deploy_fn(bundle, live_dict, connection_record, start):
    return {"bundle_id": bundle["id"], "agent_id": "agent_live", "session_id": "live_sess_0",
            "vault_ref": connection_record.get("vault_ref"),
            "external_user_id": connection_record.get("external_user_id"),
            "start": {"mode": "schedule" if start.get("schedule") else "trigger",
                      "value": start.get("schedule") or start.get("trigger")},
            "status": "live", "error": None,
            "held_actions": [{"action_id": "act_1", "kind": "send",
                              "summary": "reply to the customer", "status": "held"}],
            "deployed_at": "now", "isolation": "per-customer"}


class UIDeployTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(self.root, key_store=FakeKeystore("sk-ant-test"),
                                  config_fn=fake_config_fn, gate_fn=fake_gate_fn(advanced=True),
                                  connect_fn=lambda: StubConnectClient(identity_email="worker@acme.com"),
                                  dryrun_fn=_fake_dryrun_fn, deploy_fn=_fake_deploy_fn)
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

    def _full_chain(self):
        self.req("POST", f"/api/sessions/{self.sid}/build", {"bundle_ids": ["B1"]})
        self._await("build")
        self.req("POST", f"/api/sessions/{self.sid}/connection-identity", {"email": "worker@acme.com"})
        self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/callback", {})
        self._await("connect")
        self.req("POST", f"/api/sessions/{self.sid}/dryrun", {})
        self._await("dryrun")

    def test_not_eligible_until_gate_connection_and_dryrun(self):
        code, data = self.req("POST", f"/api/sessions/{self.sid}/deploy", {})
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "not_eligible")
        self._full_chain()
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/deploy", {"trigger": "on inbound"})
        self.assertEqual(code, 202)
        self.assertEqual(self._await("deploy")["state"], "done")

    def test_deploy_shows_held_action_and_approve_flips_it(self):
        self._full_chain()
        self.req("POST", f"/api/sessions/{self.sid}/deploy", {"trigger": "on inbound"})
        self._await("deploy")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/deploy-bundles")
        b = next(x for x in data["bundles"] if x["id"] == "B1")
        self.assertEqual(b["status"], "live")
        held = b["held_actions"]
        self.assertTrue(held and held[0]["status"] == "held")    # outbound held for approval
        # one-tap approve flips it
        code, res = self.req("POST", f"/api/sessions/{self.sid}/deploy/B1/approve",
                             {"action_id": "act_1"})
        self.assertEqual(code, 200)
        self.assertEqual(res["status"], "approved")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/deploy-bundles")
        held = next(x for x in data["bundles"] if x["id"] == "B1")["held_actions"]
        self.assertEqual(held[0]["status"], "approved")

    def test_cleanup_harvests_live_session(self):
        captured = {}
        def fake_cleanup(session_ids, file_ids, agent_ids):
            captured["args"] = (session_ids, file_ids, agent_ids)
            return {"removed": {"sessions": session_ids, "files": file_ids, "agents": agent_ids}, "errors": []}
        self.state.cleanup_fn = fake_cleanup
        self._full_chain()
        self.req("POST", f"/api/sessions/{self.sid}/deploy", {"trigger": "on inbound"})
        self._await("deploy")
        self.req("POST", f"/api/sessions/{self.sid}/cleanup-platform")
        sess_ids, _f, agent_ids = captured["args"]
        self.assertIn("live_sess_0", sess_ids)            # the live session is harvested
        self.assertIn("agent_live", agent_ids)


if __name__ == "__main__":
    unittest.main()
