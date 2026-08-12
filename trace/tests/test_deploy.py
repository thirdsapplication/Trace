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

from trace.agent import config, configgen, deploy
from trace.tests.test_configgen import _bundle, _load, _stub as _config_stub
from trace.tests.test_ui_agent import EVENTS, REVIEWED, FakeKeystore, fake_config_fn
from trace.tests.test_connect import StubConnectClient
from trace.ui import app, registry, sessions

CONNECTION = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
              "apps": {"gmail": {"usable": True, "binding_ref": "bind-gmail"}}}

# The recording session id the store is keyed on (sessions.create_session format).
SID = "20260101-120000"


class FakeDeployClient:
    """Records the vault injected + the deployment/memory-store creations; returns
    canned held outbound actions; supports approve/reject + deletion. NEVER acts on
    real data. Mirrors the real client's semantics where tests depend on them:
    ``create_or_reuse_agent`` DELETES a same-named agent and returns a NEW id every
    call (the real client recreates on redeploy — a fake that returned a stable id
    hid the redeploy-wipes-memory bug), and the memory-store fake implements the
    same preferred-id gate as the real one."""

    def __init__(self, held=None, existing_stores=None):
        self.agents, self.sessions, self.deleted = [], [], []
        self.last_vault_ids, self.start = None, None
        self.platform_agents = {}                          # name -> live agent id
        self.deleted_agents = []
        self._agent_seq = 0
        self.memory_stores = list(existing_stores or [])   # [{"id", "name"}]
        self.seeded = []                                   # (store_id, path)
        self.deployments = []                              # recorded create kwargs
        self.archived, self.paused, self.unpaused = [], [], []
        self.manual_runs = []
        self._held = {a["action_id"]: dict(a, status="held") for a in (held or [
            {"action_id": "act_1", "kind": "send", "summary": "reply to the customer"},
            {"action_id": "act_2", "kind": "write", "summary": "add a row to the sheet"}])}

    def create_or_reuse_agent(self, envelope):
        self.agents.append(envelope)
        name = envelope["name"]
        recreated = False
        old = self.platform_agents.pop(name, None)
        if old is not None:
            self.deleted_agents.append(old)
            recreated = True
        self._agent_seq += 1
        aid = f"agent_{self._agent_seq}"
        self.platform_agents[name] = aid
        return {"id": aid, "name": name, "reused": False, "recreated": recreated}

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

    # --- memory stores + deployments (the platform-native start) ---
    def create_or_reuse_memory_store(self, name, description, preferred_id=None):
        if preferred_id:                                   # same gate as the real client
            user_prefix = name.rsplit("-wf-", 1)[0] + "-"
            for s in self.memory_stores:
                if s["id"] == preferred_id and str(s["name"]).startswith(user_prefix):
                    return {"id": preferred_id, "reused": True}
        for s in self.memory_stores:
            if s["name"] == name:
                return {"id": s["id"], "reused": True}
        store = {"id": f"mem_{len(self.memory_stores)}", "name": name}
        self.memory_stores.append(store)
        self.seeded.append((store["id"], "/current_workflow_status.md"))
        self.seeded.append((store["id"], "/lessons_learned.md"))
        return {"id": store["id"], "reused": False}

    def create_deployment(self, name, agent_id, environment_id, trigger_text,
                          schedule=None, resources=None, vault_ids=None, metadata=None):
        self.last_vault_ids = vault_ids
        dep = {"id": f"dep_{len(self.deployments)}", "name": name, "agent_id": agent_id,
               "environment_id": environment_id, "trigger_text": trigger_text,
               "schedule": schedule, "resources": resources, "vault_ids": vault_ids,
               "metadata": metadata}
        self.deployments.append(dep)
        return dep["id"]

    def run_deployment(self, deployment_id):
        self.manual_runs.append(deployment_id)
        return {"id": f"drun_{len(self.manual_runs)}", "session_id": f"dep_sess_{len(self.manual_runs)}",
                "error_type": None, "error_message": None, "created_at": "now"}

    def archive_deployment(self, deployment_id):
        self.archived.append(deployment_id)
        return True

    def pause_deployment(self, deployment_id):
        self.paused.append(deployment_id)
        return True

    def unpause_deployment(self, deployment_id):
        self.unpaused.append(deployment_id)
        return True

    def list_deployment_runs(self, deployment_id):
        return []

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
        rec = deploy.deploy_live(bundle, live, CONNECTION, client,
                                 trigger="on a new inbound PO", capture_sid=SID)
        # the LIVE config was deployed with the customer's per-app Pipedream MCP server(s),
        # baked at create time: the URL carries THIS customer's externalUserId + the app.
        servers = client.agents[0].get("mcp_servers")
        self.assertTrue(servers)
        urls = " ".join(s["url"] for s in servers)
        self.assertIn("externalUserId=trace-s1", urls)
        self.assertIn("app=gmail", urls)
        # and exactly one mcp_toolset per server, on the configured (default) permission policy
        toolsets = [t for t in client.agents[0]["tools"] if t.get("type") == "mcp_toolset"]
        self.assertEqual(len(toolsets), len(servers))
        self.assertTrue(all(t["default_config"]["permission_policy"]["type"] == config.LIVE_PERMISSION_POLICY
                            for t in toolsets))
        # the bound vault rides on the DEPLOYMENT (injected into every run session)
        self.assertEqual(client.last_vault_ids, ["vlt-s1"])
        self.assertEqual(client.deployments[0]["vault_ids"], ["vlt-s1"])
        self.assertEqual(rec["vault_ref"], "vlt-s1")
        self.assertEqual(rec["status"], "live")
        self.assertEqual(rec["start"]["mode"], "deployment-manual")

    # --- Spec 8: staging is PROMPT-LEVEL, never platform-gated -------------
    # Per-connector approval would gate read tools (find/list/get) exactly like writes
    # and halt the agent on its first inbox read, so staged toolsets stay always_allow;
    # staging is enforced by a run-time directive (see test_scheduler) instead.
    def test_every_live_toolset_is_always_allow_even_when_staged(self):
        _, toolsets = config.pipedream_mcp_servers(
            "trace-s1", ["gmail", "google_sheets"], staged_slugs=["gmail"])
        self.assertTrue(all(t["default_config"]["permission_policy"]["type"] == config.LIVE_PERMISSION_POLICY
                            for t in toolsets))            # NOTHING is always_ask at the platform

    def test_no_staged_slugs_is_unchanged(self):
        _, toolsets = config.pipedream_mcp_servers("trace-s1", ["gmail", "google_sheets"])
        self.assertTrue(all(t["default_config"]["permission_policy"]["type"] == config.LIVE_PERMISSION_POLICY
                            for t in toolsets))

    def test_deploy_live_records_staged_apps_without_gating(self):
        bundle, live = _live_dict()
        conn = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
                "apps": {"gmail": {"usable": True}, "google_sheets": {"usable": True}}}
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, conn, client,
                                 trigger="on a new task", staged_apps=["gmail"],
                                 capture_sid=SID)
        toolsets = [t for t in client.agents[0]["tools"] if t.get("type") == "mcp_toolset"]
        # staged_apps is recorded for the run-time directive, but the platform gates nothing
        self.assertTrue(all(t["default_config"]["permission_policy"]["type"] == config.LIVE_PERMISSION_POLICY
                            for t in toolsets))
        self.assertEqual(rec["staged_apps"], ["gmail"])

    def test_one_tap_approve_and_reject(self):
        # held-action plumbing (for any always_ask toolset) still works against a session
        client = FakeDeployClient()
        sid = "live_sess_x"
        self.assertEqual(deploy.approve(client, sid, "act_1")["status"], "approved")
        self.assertEqual(deploy.reject(client, sid, "act_2")["status"], "rejected")
        # the held list drains as actions are resolved
        self.assertEqual(deploy.list_held(client, sid), [])

    # --- platform deployments + per-agent memory ---------------------------
    def test_deploy_creates_memory_store_and_manual_deployment(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, CONNECTION, client,
                                 trigger="on a new inbound PO", capture_sid=SID)
        # one memory store, keyed on stable workflow identity (user + recording +
        # bundle — NOT the churning platform agent id), seeded with BOTH documents
        self.assertEqual(len(client.memory_stores), 1)
        self.assertEqual(client.memory_stores[0]["name"],
                         config.memory_store_name("trace-s1", SID, bundle["id"]))
        self.assertEqual({p for _sid, p in client.seeded},
                         {config.MEMORY_STATUS_DOC, config.MEMORY_LESSONS_DOC})
        # one MANUAL deployment (no schedule at deploy; the dashboard attaches cron)
        dep = client.deployments[0]
        self.assertIsNone(dep["schedule"])
        self.assertEqual(dep["agent_id"], rec["agent_id"])
        # the memory store is attached read-write via resources
        self.assertEqual(dep["resources"][0]["type"], "memory_store")
        self.assertEqual(dep["resources"][0]["memory_store_id"], rec["memory_store_id"])
        self.assertEqual(dep["resources"][0]["access"], "read_write")
        # record carries both platform ids; no deploy-time session is created
        self.assertEqual(rec["deployment_id"], dep["id"])
        self.assertTrue(rec["memory_store_id"])
        self.assertIsNone(rec["session_id"])
        self.assertEqual(client.sessions, [])

    def test_deploy_time_cron_schedule_is_honored(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, CONNECTION, client, schedule="0 9 * * 1",
                                 capture_sid=SID)
        dep = client.deployments[0]
        self.assertEqual(dep["schedule"], {"expression": "0 9 * * 1",
                                           "timezone": config.TRACE_SCHEDULE_TZ})
        self.assertEqual(rec["start"], {"mode": "deployment-schedule", "value": "0 9 * * 1"})

    def test_deployment_initial_message_has_task_and_memory_directive(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        deploy.deploy_live(bundle, live, CONNECTION, client,
                           trigger="on a new inbound PO", capture_sid=SID)
        text = client.deployments[0]["trigger_text"]
        self.assertIn("Task: on a new inbound PO", text)
        self.assertIn(config.MEMORY_UPDATE_DIRECTIVE, text)
        self.assertNotIn("STAGING MODE", text)              # nothing staged here

    def test_staged_deploy_message_contains_staging_directive(self):
        bundle, live = _live_dict()
        conn = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
                "apps": {"gmail": {"usable": True}}}
        client = FakeDeployClient()
        deploy.deploy_live(bundle, live, conn, client,
                           trigger="on a new task", staged_apps=["gmail"],
                           capture_sid=SID)
        text = client.deployments[0]["trigger_text"]
        self.assertIn("STAGING MODE", text)
        self.assertIn("Gmail", text)                        # staged app named
        self.assertIn(config.MEMORY_UPDATE_DIRECTIVE, text)

    def test_redeploy_reuses_memory_store_despite_new_agent_id(self):
        # The platform agent is deleted+recreated on redeploy (new id every time), so
        # the store must key on stable workflow identity or every redeploy silently
        # starts an empty store and the accumulated lessons are lost.
        bundle, live = _live_dict()
        client = FakeDeployClient()
        rec1 = deploy.deploy_live(bundle, live, CONNECTION, client, capture_sid=SID)
        seeded_once = list(client.seeded)
        rec2 = deploy.deploy_live(bundle, live, CONNECTION, client, capture_sid=SID)
        self.assertNotEqual(rec1["agent_id"], rec2["agent_id"])   # agent id churned...
        self.assertEqual(rec1["memory_store_id"], rec2["memory_store_id"])  # ...store didn't
        self.assertTrue(rec2["memory_store_reused"])
        self.assertEqual(len(client.memory_stores), 1)
        self.assertEqual(client.seeded, seeded_once)              # reuse never re-seeds

    def test_capture_sid_is_required(self):
        bundle, live = _live_dict()
        with self.assertRaises(ValueError):                 # a silent None would collapse
            deploy.deploy_live(bundle, live, CONNECTION, FakeDeployClient())

    def test_two_workflows_of_one_user_get_distinct_stores(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        other = dict(bundle, id="B2", name="Other workflow")
        rec1 = deploy.deploy_live(bundle, live, CONNECTION, client, capture_sid=SID)
        rec2 = deploy.deploy_live(other, dict(live, name="Other workflow"), CONNECTION,
                                  client, capture_sid=SID)
        self.assertNotEqual(rec1["memory_store_id"], rec2["memory_store_id"])
        self.assertEqual(len(client.memory_stores), 2)
        # and the same bundle id from a DIFFERENT recording is a different workflow
        self.assertNotEqual(config.memory_store_name("trace-s1", SID, bundle["id"]),
                            config.memory_store_name("trace-s1", "20260202-090000",
                                                     bundle["id"]))

    def test_two_users_same_workflow_name_do_not_collide(self):
        # One shared platform account: user 2 deploying a workflow with the SAME name
        # must not delete user 1's live agent, and each keeps their own memory store.
        bundle, live = _live_dict()
        client = FakeDeployClient()
        conn2 = {"external_user_id": "trace-s2", "vault_ref": "vlt-s2",
                 "apps": {"gmail": {"usable": True}}}
        rec1 = deploy.deploy_live(bundle, live, CONNECTION, client, capture_sid=SID)
        rec2 = deploy.deploy_live(bundle, live, conn2, client, capture_sid=SID)
        self.assertEqual(client.deleted_agents, [])         # nobody's agent was deleted
        self.assertEqual(len(client.platform_agents), 2)    # both live, distinct names
        self.assertIn("[trace-s1]", client.agents[0]["name"])
        self.assertIn("[trace-s2]", client.agents[1]["name"])
        self.assertNotEqual(rec1["memory_store_id"], rec2["memory_store_id"])

    def test_platform_agent_name_is_user_namespaced(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        deploy.deploy_live(bundle, live, CONNECTION, client, capture_sid=SID)
        self.assertEqual(client.agents[0]["name"],
                         config.platform_agent_name(live["name"], "trace-s1"))

    def test_memory_store_name_is_stable_and_clamped(self):
        name = config.memory_store_name("trace-user-u1", SID, "B1")
        self.assertEqual(name, config.memory_store_name("trace-user-u1", SID, "B1"))
        self.assertTrue(name.startswith("trace-trace-user-u1-wf-"))
        # a pathological external id still fits and can't collide via truncation
        long_a = "trace-user-" + "a" * 200
        long_b = long_a + "-b"                              # same first 200 chars
        na = config.memory_store_name(long_a, SID, "B1")
        nb = config.memory_store_name(long_b, SID, "B1")
        self.assertLessEqual(len(na), 100)
        self.assertNotEqual(na, nb)                         # digest keeps them distinct
        self.assertNotEqual(config.memory_store_name("trace-user-u1", SID, "B1"),
                            config.memory_store_name("trace-user-u1", SID, "B2"))

    def test_legacy_shared_store_not_adopted_on_redeploy(self):
        # Agents deployed before the fix share one bundle-keyed store per user
        # (trace-B1-<uid>). A redeploy carrying that store id from the registry must
        # NOT keep it — the whole point is to land each agent on its own store.
        bundle, live = _live_dict()
        client = FakeDeployClient(
            existing_stores=[{"id": "mem_shared", "name": "trace-B1-trace-s1"}])
        rec = deploy.deploy_live(bundle, live, CONNECTION, client, capture_sid=SID,
                                 prior_memory_store_id="mem_shared")
        self.assertNotEqual(rec["memory_store_id"], "mem_shared")
        self.assertFalse(rec["memory_store_reused"])        # fresh, isolated, seeded
        self.assertTrue(client.seeded)

    def test_prior_registry_store_id_preferred_over_name(self):
        # A store created under the SHORT-LIVED agent-id keying (post-fix prefix, but a
        # name the new scheme can't derive) is kept via the registry's id, so that
        # workflow's lessons survive the naming-scheme change.
        bundle, live = _live_dict()
        client = FakeDeployClient(
            existing_stores=[{"id": "mem_prior", "name": "trace-trace-s1-agent-abc123"}])
        rec = deploy.deploy_live(bundle, live, CONNECTION, client, capture_sid=SID,
                                 prior_memory_store_id="mem_prior")
        self.assertEqual(rec["memory_store_id"], "mem_prior")
        self.assertTrue(rec["memory_store_reused"])
        self.assertEqual(client.seeded, [])                 # adopted, not re-seeded

    def test_deploy_fails_loud_when_deployment_create_raises(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        client.create_deployment = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("schedule.expression is invalid"))
        with self.assertRaises(RuntimeError):               # propagates — no phantom "live"
            deploy.deploy_live(bundle, live, CONNECTION, client, capture_sid=SID)

    def test_per_customer_isolation(self):
        bundle, live = _live_dict()
        c1, c2 = FakeDeployClient(), FakeDeployClient()
        deploy.deploy_live(bundle, live, CONNECTION, c1, capture_sid=SID)
        deploy.deploy_live(bundle, live, {"external_user_id": "trace-s2", "vault_ref": "vlt-s2",
                                          "apps": {"gmail": {"usable": True}}}, c2,
                           capture_sid=SID)
        self.assertEqual(c1.last_vault_ids, ["vlt-s1"])       # each client only saw its own vault
        self.assertEqual(c2.last_vault_ids, ["vlt-s2"])
        self.assertNotEqual(c1.last_vault_ids, c2.last_vault_ids)

    def test_runs_on_both_domains(self):
        for dom in ("marcy", "recruiter"):
            bundle, live = _live_dict(dom)
            rec = deploy.deploy_live(bundle, live, CONNECTION, FakeDeployClient(),
                                     capture_sid=SID)
            self.assertEqual(rec["status"], "live")
            self.assertTrue(rec["deployment_id"])
            self.assertTrue(rec["memory_store_id"])

    def test_imports_no_platform_sdk(self):
        import inspect
        src = inspect.getsource(deploy)
        self.assertNotIn("import anthropic", src)


class _FakeEvents:
    """Records events.send and serves a canned events.list (one pending held write,
    one already-resolved by a tool_confirmation)."""
    def __init__(self):
        self.sent = []
        self._events = [
            {"type": "agent.mcp_tool_use", "tool_use_id": "sevt_1", "name": "send_email",
             "summary": "reply to the customer"},
            {"type": "agent.mcp_tool_use", "tool_use_id": "sevt_2", "name": "append_row",
             "summary": "add a row"},
            {"type": "user.tool_confirmation", "tool_use_id": "sevt_2", "result": "allow"},
        ]

    def list(self, session_id):
        return list(self._events)

    def send(self, session_id, events=None):
        self.sent.append((session_id, events))
        return {"ok": True}


class RealClientApprovalTest(unittest.TestCase):
    """The verified approval mechanism: held writes surface from the event stream and are
    resolved by replying with ONE tool_confirmation event (no sessions.approvals API)."""

    def _client(self):
        from trace.agent import api_client
        c = api_client.ManagedAgentsClient.__new__(api_client.ManagedAgentsClient)
        c._beta_version = api_client.BETA_VERSION
        events = _FakeEvents()

        class _Sessions:
            pass
        sessions_ns = _Sessions(); sessions_ns.events = events

        class _Beta:
            pass
        beta = _Beta(); beta.sessions = sessions_ns

        class _Client:
            pass
        client = _Client(); client.beta = beta
        c._client = client
        return c, events

    def test_pending_excludes_resolved(self):
        c, _ = self._client()
        pending = c.list_pending_approvals("live_sess_0")
        ids = {p["action_id"] for p in pending}
        self.assertEqual(ids, {"sevt_1"})            # sevt_2 already confirmed -> drained

    def test_approve_sends_allow_tool_confirmation(self):
        c, events = self._client()
        c.approve_action("live_sess_0", "sevt_1")
        _sid, evs = events.sent[-1]
        self.assertEqual(evs[0]["type"], "tool_confirmation")
        self.assertEqual(evs[0]["tool_use_id"], "sevt_1")
        self.assertEqual(evs[0]["result"], "allow")

    def test_reject_sends_deny_with_message(self):
        c, events = self._client()
        c.reject_action("live_sess_0", "sevt_1", message="wrong recipient")
        _sid, evs = events.sent[-1]
        self.assertEqual(evs[0]["result"], "deny")
        self.assertEqual(evs[0]["message"], "wrong recipient")

    def test_set_trigger_makes_no_platform_call(self):
        c, _ = self._client()
        # no triggers namespace exists; set_trigger must not touch the platform
        self.assertEqual(c.set_trigger("agent_live", schedule="0 9 * * *"),
                         {"mode": "schedule", "value": "0 9 * * *"})


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
                                  config_fn=fake_config_fn,
                                  # zero questions => records land "complete", satisfying
                                  # the mandatory-interview build gate offline
                                  interview_fn=lambda b, ev, model_fn=None: {"questions": []},
                                  connect_fn=lambda: StubConnectClient(identity_email="worker@acme.com"),
                                  deploy_fn=_fake_deploy_fn)
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
        self.req("POST", f"/api/sessions/{self.sid}/interview", {})   # mandatory before build
        self._await("interview")
        self.req("POST", f"/api/sessions/{self.sid}/build", {"bundle_ids": ["B1"]})
        self._await("build")
        self.req("POST", f"/api/sessions/{self.sid}/connection-identity", {"email": "worker@acme.com"})
        self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/callback", {})
        self._await("connect")

    def test_not_eligible_until_config_and_connection(self):
        code, data = self.req("POST", f"/api/sessions/{self.sid}/deploy", {})
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "not_eligible")
        self._full_chain()
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/deploy", {"trigger": "on inbound"})
        self.assertEqual(code, 202)
        self.assertEqual(self._await("deploy")["state"], "done")

    def test_run_refreshes_stale_pipedream_token_before_deploy(self):
        # WS5: a token that expired between Connect and run is refreshed-and-rotated by the
        # deploy path with no manual re-verify.
        self._full_chain()
        conn_path = sessions.path(self.root, self.sid, sessions.CONNECTION)
        with open(conn_path) as f:
            conn = json.load(f)
        usable = [s for s, a in conn["apps"].items() if a.get("usable")]
        self.assertTrue(usable)
        for s in usable:                                   # force-expire the stored token
            conn["apps"][s]["token_expires_at"] = "2000-01-01T00:00:00Z"
        with open(conn_path, "w") as f:
            json.dump(conn, f)
        self.req("POST", f"/api/sessions/{self.sid}/deploy", {"trigger": "on inbound"})
        self.assertEqual(self._await("deploy")["state"], "done")
        with open(conn_path) as f:
            refreshed = json.load(f)
        for s in usable:                                   # rotated to the fresh expiry
            self.assertEqual(refreshed["apps"][s]["token_expires_at"], "2099-01-01T00:00:00Z")

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

    def test_total_failure_reports_error_not_phantom_live(self):
        # Regression: when agent creation fails (e.g. the platform rejects the toolset),
        # the deploy must surface the error — not silently mark the session deployed and
        # show "live" with zero agents created.
        self._full_chain()
        def boom(*a, **k):
            raise RuntimeError("400 invalid permission policy")
        self.state.deploy_fn = boom
        self.req("POST", f"/api/sessions/{self.sid}/deploy", {"trigger": "on inbound"})
        st = self._await("deploy")
        self.assertEqual(st["state"], "error")
        self.assertIn("invalid permission policy", st["error"])    # real platform error surfaced
        # the session is NOT marked deployed and no live record was written
        self.assertFalse((sessions.read_meta(self.root, self.sid) or {}).get("deploy_at"))
        self.assertEqual(sessions.list_deploy_records(self.root, self.sid), [])

    def test_cleanup_harvests_live_session(self):
        captured = {}
        def fake_cleanup(session_ids, file_ids, agent_ids, agent_names=None):
            captured["args"] = (session_ids, file_ids, agent_ids)
            captured["agent_names"] = agent_names
            return {"removed": {"sessions": session_ids, "files": file_ids, "agents": agent_ids}, "errors": []}
        self.state.cleanup_fn = fake_cleanup
        self._full_chain()
        self.req("POST", f"/api/sessions/{self.sid}/deploy", {"trigger": "on inbound"})
        self._await("deploy")
        self.req("POST", f"/api/sessions/{self.sid}/cleanup-platform")
        sess_ids, _f, agent_ids = captured["args"]
        self.assertIn("live_sess_0", sess_ids)            # the live session is harvested
        self.assertIn("agent_live", agent_ids)
        # the session's workflow names are passed so cleanup can sweep stale duplicates,
        # in BOTH forms: bare (test-era/pre-namespacing agents) and user-namespaced
        self.assertIn("Order intake", captured["agent_names"])
        self.assertTrue(any(n.startswith("Order intake [")
                            for n in captured["agent_names"]))


def _stateful_deploy_fn():
    """A deploy_fn that behaves like the real one across REDEPLOYS: a fresh agent and
    deployment id every call, the registry's prior store id adopted when passed, and
    every ``start`` dict recorded so threading can be asserted."""
    calls = {"n": 0, "starts": []}

    def fn(bundle, live_dict, connection_record, start):
        calls["n"] += 1
        calls["starts"].append(dict(start))
        return {"bundle_id": bundle["id"], "agent_id": f"agent_{calls['n']}",
                "session_id": None, "deployment_id": f"dep_{calls['n']}",
                "memory_store_id": start.get("prior_memory_store_id") or f"mem_{calls['n']}",
                "vault_ref": connection_record.get("vault_ref"),
                "external_user_id": connection_record.get("external_user_id"),
                "start": {"mode": "trigger", "value": start.get("trigger")},
                "status": "live", "error": None, "staged_apps": [], "held_actions": [],
                "deployed_at": "now", "isolation": "per-customer"}

    fn.calls = calls
    return fn


class _FakeLiveClient:
    def __init__(self):
        self.archived = []

    def archive_deployment(self, deployment_id):
        self.archived.append(deployment_id)
        return True


class RedeployRegistryTest(UIDeployTest):
    """The registry side of a redeploy: one row per workflow (superseded in place, not
    duplicated), the prior store id threaded through, the stranded deployment archived,
    and the prior-row lookup scoped to the recording session."""

    OWNER = "u-redeploy"

    def setUp(self):
        super().setUp()
        self.deploy_fn = _stateful_deploy_fn()
        self.state.deploy_fn = self.deploy_fn
        self.live_client = _FakeLiveClient()
        self.state._live_client = lambda: self.live_client
        self.state._embed_agent_kind = lambda agent: None    # no network in tests
        sessions.update_meta(self.root, self.sid, owner_user_id=self.OWNER)

    # Inherited UIDeployTest tests assume the stock _fake_deploy_fn (stable agent id,
    # held actions, no owner); they already ran in the parent class, so skip them here
    # by overriding with the registry-focused set only.
    def test_not_eligible_until_config_and_connection(self):
        pass

    def test_run_refreshes_stale_pipedream_token_before_deploy(self):
        pass

    def test_deploy_shows_held_action_and_approve_flips_it(self):
        pass

    def test_total_failure_reports_error_not_phantom_live(self):
        pass

    def test_cleanup_harvests_live_session(self):
        pass

    def _deploy(self):
        self.req("POST", f"/api/sessions/{self.sid}/deploy", {"trigger": "on inbound"})
        self.assertEqual(self._await("deploy")["state"], "done")

    def test_redeploy_updates_registry_row_in_place(self):
        self._full_chain()
        self._deploy()
        rows = registry.agents_for_user(self.root, self.OWNER)
        self.assertEqual([r["agent_id"] for r in rows], ["agent_1"])
        # first deploy: capture_sid threaded, no prior store to carry
        self.assertEqual(self.deploy_fn.calls["starts"][0]["capture_sid"], self.sid)
        self.assertIsNone(self.deploy_fn.calls["starts"][0]["prior_memory_store_id"])

        self._deploy()                                       # redeploy the same workflow
        rows = registry.agents_for_user(self.root, self.OWNER)
        self.assertEqual(len(rows), 1)                       # superseded, not duplicated
        self.assertEqual(rows[0]["agent_id"], "agent_2")
        self.assertEqual(rows[0]["memory_store_id"], "mem_1")   # store carried forward
        self.assertEqual(self.deploy_fn.calls["starts"][1]["prior_memory_store_id"], "mem_1")
        self.assertEqual(self.live_client.archived, ["dep_1"])  # stranded deployment gone

    def test_prior_lookup_scoped_to_recording_session(self):
        # Another recording's workflow also called B1 must be untouched by this deploy:
        # matching on bundle id alone used to archive its deployment.
        registry.agent_upsert(self.root, {
            "agent_id": "agent_other", "owner_user_id": self.OWNER,
            "external_user_id": "x", "bundle_id": "B1", "name": "Other recording's WF",
            "vault_ref": "v", "trigger": "t", "session_id": "20250101-000000",
            "deployment_id": "dep_other", "memory_store_id": "mem_other"})
        self._full_chain()
        self._deploy()
        self.assertNotIn("dep_other", self.live_client.archived)
        self.assertIsNone(self.deploy_fn.calls["starts"][0]["prior_memory_store_id"])
        rows = {r["agent_id"] for r in registry.agents_for_user(self.root, self.OWNER)}
        self.assertEqual(rows, {"agent_other", "agent_1"})   # both workflows keep a row


if __name__ == "__main__":
    unittest.main()
