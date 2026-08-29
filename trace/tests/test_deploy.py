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
from trace.ui import app, sessions

CONNECTION = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
              "apps": {"gmail": {"usable": True, "binding_ref": "bind-gmail"}}}


class FakeDeployClient:
    """Records the vault injected + the deployment/memory-store creations; returns
    canned held outbound actions; supports approve/reject + deletion. NEVER acts on
    real data."""

    def __init__(self, held=None, existing_stores=None):
        self.agents, self.sessions, self.deleted = [], [], []
        self.last_vault_ids, self.start = None, None
        self.memory_stores = list(existing_stores or [])   # [{"id", "name"}]
        self.seeded = []                                   # (store_id, path)
        self.environments = []                             # names passed to create_environment
        self.deployments = []                              # recorded create kwargs
        self.archived, self.paused, self.unpaused = [], [], []
        self.manual_runs = []
        self._held = {a["action_id"]: dict(a, status="held") for a in (held or [
            {"action_id": "act_1", "kind": "send", "summary": "reply to the customer"},
            {"action_id": "act_2", "kind": "write", "summary": "add a row to the sheet"}])}

    def create_or_reuse_agent(self, envelope):
        self.agents.append(envelope)
        return {"id": "agent_live", "name": envelope["name"], "reused": False}

    def create_environment(self, defaults=None):
        self.environments.append((defaults or {}).get("name"))
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
    def create_or_reuse_memory_store(self, name, description, documents=None):
        # mirrors the real client: the caller's documents are ensured on BOTH the
        # fresh and the reused path (a shared store's "reused" only means another
        # agent got there first); already-seeded paths are left untouched
        documents = documents or [("/current_workflow_status.md", ""),
                                  ("/shared_lessons.md", "")]
        store = next((s for s in self.memory_stores if s["name"] == name), None)
        reused = store is not None
        if store is None:
            store = {"id": f"mem_{len(self.memory_stores)}", "name": name}
            self.memory_stores.append(store)
        for path, _content in documents:
            if (store["id"], path) not in self.seeded:
                self.seeded.append((store["id"], path))
        return {"id": store["id"], "reused": reused}

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
        rec = deploy.deploy_live(bundle, live, CONNECTION, client, trigger="on a new inbound PO")
        # the LIVE config was deployed with the customer's per-app Pipedream MCP server(s),
        # baked at create time: the URL path carries THIS customer's external_user_id +
        # the app (the documented per-user format — NOT the old /v3 query-param surface,
        # which served a reduced toolset).
        servers = client.agents[0].get("mcp_servers")
        self.assertTrue(servers)
        urls = " ".join(s["url"] for s in servers)
        self.assertIn("/trace-s1/gmail", urls)
        self.assertNotIn("/v3", urls)
        self.assertNotIn("externalUserId=", urls)
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
                                 trigger="on a new task", staged_apps=["gmail"])
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
        rec = deploy.deploy_live(bundle, live, CONNECTION, client, trigger="on a new inbound PO")
        # one DEDICATED memory store for this agent, keyed on user + workflow name
        # (NOT the platform agent id, which redeploys rotate), seeded with BOTH
        # canonical documents
        self.assertEqual(len(client.memory_stores), 1)
        self.assertEqual(client.memory_stores[0]["name"],
                         config.memory_store_name("trace-s1", live["name"]))
        self.assertEqual({p for _sid, p in client.seeded},
                         {config.MEMORY_STATUS_DOC, config.MEMORY_SHARED_DOC})
        # one MANUAL deployment (no schedule at deploy; the dashboard attaches cron)
        dep = client.deployments[0]
        self.assertIsNone(dep["schedule"])
        self.assertEqual(dep["agent_id"], "agent_live")
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
        rec = deploy.deploy_live(bundle, live, CONNECTION, client, schedule="0 9 * * 1")
        dep = client.deployments[0]
        self.assertEqual(dep["schedule"], {"expression": "0 9 * * 1",
                                           "timezone": config.TRACE_SCHEDULE_TZ})
        self.assertEqual(rec["start"], {"mode": "deployment-schedule", "value": "0 9 * * 1"})

    def test_autonomous_agent_gets_one_deployment_and_a_bare_message(self):
        """No approval required -> one deployment, and the word "stage" appears nowhere.

        Not a style preference. An instruction to withhold an action, present in the
        prompt of an agent that is never meant to withhold, is a standing invitation to
        withhold at the wrong moment — and the human who turned approval off is not
        watching for a proposal nobody told them about.
        """
        bundle, live = _live_dict()
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, CONNECTION, client,
                                 trigger="on a new inbound PO", approval_required=False)
        self.assertEqual(len(client.deployments), 1)
        dep = client.deployments[0]
        self.assertEqual(dep["trigger_text"], "Begin.")
        self.assertEqual(rec["deployment_mode"], config.MODE_EXECUTION)
        self.assertEqual(rec["deployments"], {config.MODE_EXECUTION: dep["id"]})
        for text in (dep["trigger_text"], client.agents[0]["system"]):
            for word in ("stage", "staging", "propose", "proposed_actions"):
                self.assertNotIn(word, text.lower())

    def test_approval_agent_gets_both_deployments_with_one_line_messages(self):
        bundle, live = _live_dict()
        conn = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
                "apps": {"gmail": {"usable": True}}}
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, conn, client,
                                 trigger="on a new task", approval_required=True)
        by_mode = {d["name"].rsplit("-", 1)[-1]: d for d in client.deployments}
        self.assertEqual(set(by_mode), {config.MODE_STAGING, config.MODE_EXECUTION})
        self.assertEqual(by_mode[config.MODE_STAGING]["trigger_text"],
                         "Stage, don't execute.\n\nBegin.")
        self.assertIn("Execution mode", by_mode[config.MODE_EXECUTION]["trigger_text"])
        # both point at the same agent and the same memory store
        self.assertEqual({d["agent_id"] for d in client.deployments}, {"agent_live"})
        self.assertEqual({d["resources"][0]["memory_store_id"] for d in client.deployments},
                         {rec["memory_store_id"]})
        # exactly one runs; a new approval agent starts by proposing, not acting
        self.assertEqual(rec["deployment_mode"], config.MODE_STAGING)
        self.assertEqual(client.paused, [by_mode[config.MODE_EXECUTION]["id"]])
        self.assertEqual(rec["deployment_id"], by_mode[config.MODE_STAGING]["id"])

    def test_the_contracts_live_in_the_system_prompt_not_the_message(self):
        """Everything constant across runs moved out of the opening message.

        A deployment's opening message is baked in at create time and deployments are
        immutable, so anything living there can only be changed by tearing the
        deployment down — which is how the approval toggle came to have no effect on
        scheduled runs. It is also per-run cost for text that never varies.
        """
        bundle, live = _live_dict()
        conn = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
                "apps": {"gmail": {"usable": True}}}
        client = FakeDeployClient()
        deploy.deploy_live(bundle, live, conn, client,
                           trigger="on a new inbound PO", approval_required=True)
        system = client.agents[0]["system"]
        self.assertIn(config.MEMORY_UPDATE_DIRECTIVE, system)
        self.assertIn("needs_input", system)                # the uncertainty contract
        self.assertIn("proposed_actions", system)           # the staging contract
        self.assertIn("on a new inbound PO", system)        # the workflow's own trigger
        for dep in client.deployments:
            self.assertNotIn(config.MEMORY_UPDATE_DIRECTIVE, dep["trigger_text"])
            self.assertNotIn("needs_input", dep["trigger_text"])
            self.assertLess(len(dep["trigger_text"]), 120)

    def test_legacy_staged_apps_still_means_approval_required(self):
        """Callers that predate the whole-agent setting keep working."""
        bundle, live = _live_dict()
        conn = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
                "apps": {"gmail": {"usable": True}}}
        client = FakeDeployClient()
        rec = deploy.deploy_live(bundle, live, conn, client,
                                 trigger="on a new task", staged_apps=["gmail"])
        self.assertTrue(rec["approval_required"])
        self.assertEqual(len(client.deployments), 2)

    def test_memory_store_reused_on_redeploy_keeps_lessons(self):
        # a redeploy rotates the platform agent id, but the store name keys on
        # user + workflow, so the SAME dedicated store is found and reused
        bundle, live = _live_dict()
        name = config.memory_store_name("trace-s1", live["name"])
        client = FakeDeployClient(existing_stores=[{"id": "mem_prior", "name": name}])
        rec = deploy.deploy_live(bundle, live, CONNECTION, client)
        self.assertEqual(rec["memory_store_id"], "mem_prior")
        self.assertTrue(rec["memory_store_reused"])
        # reuse never creates a second store, and existing documents stay untouched
        # (the conflict-tolerant seed contract in test_agent_api_client)
        self.assertEqual(len(client.memory_stores), 1)

    def test_each_agent_gets_its_own_store(self):
        # a user's SECOND workflow gets its OWN dedicated store — one store per
        # agent, never shared — and both names still identify the user
        b1, live1 = _live_dict("marcy")
        b2, live2 = _live_dict("recruiter")
        client = FakeDeployClient()
        r1 = deploy.deploy_live(b1, live1, CONNECTION, client)
        r2 = deploy.deploy_live(b2, live2, CONNECTION, client)
        self.assertEqual(len(client.memory_stores), 2)
        self.assertNotEqual(r1["memory_store_id"], r2["memory_store_id"])
        self.assertFalse(r1["memory_store_reused"])
        self.assertFalse(r2["memory_store_reused"])
        for s in client.memory_stores:
            self.assertIn("trace-s1", s["name"])            # whose it is, at a glance

    def test_deployment_and_environment_named_for_the_user(self):
        # the old names repeated per user ("trace-B1" for every first workflow); now
        # deployment + environment carry the owner identity + the workflow name
        bundle, live = _live_dict()
        client = FakeDeployClient()
        conn = dict(CONNECTION, owner_email="jane@acme.com")
        deploy.deploy_live(bundle, live, conn, client, trigger="on a new inbound PO")
        self.assertEqual(client.deployments[0]["name"],
                         config.deployment_name("jane@acme.com", live["name"]))
        self.assertIn("jane-acme-com", client.deployments[0]["name"])
        self.assertIn("jane-acme-com", client.environments[0])
        # without an email the stable external user id still identifies the user
        client2 = FakeDeployClient()
        deploy.deploy_live(bundle, live, CONNECTION, client2)
        self.assertIn("trace-s1", client2.deployments[0]["name"])

    def test_deploy_fails_loud_when_deployment_create_raises(self):
        bundle, live = _live_dict()
        client = FakeDeployClient()
        client.create_deployment = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("schedule.expression is invalid"))
        with self.assertRaises(RuntimeError):               # propagates — no phantom "live"
            deploy.deploy_live(bundle, live, CONNECTION, client)

    def test_per_customer_isolation(self):
        bundle, live = _live_dict()
        c1, c2 = FakeDeployClient(), FakeDeployClient()
        deploy.deploy_live(bundle, live, CONNECTION, c1)
        deploy.deploy_live(bundle, live, {"external_user_id": "trace-s2", "vault_ref": "vlt-s2",
                                          "apps": {"gmail": {"usable": True}}}, c2)
        self.assertEqual(c1.last_vault_ids, ["vlt-s1"])       # each client only saw its own vault
        self.assertEqual(c2.last_vault_ids, ["vlt-s2"])
        self.assertNotEqual(c1.last_vault_ids, c2.last_vault_ids)

    def test_platform_agent_name_is_owner_scoped(self):
        # create_or_reuse_agent deletes by exact name across the whole platform
        # account, so the bare workflow name must never reach it: two customers
        # whose workflows share a name ("Inbox triage") would delete each other's
        # live agents on deploy.
        bundle, live = _live_dict()
        c1, c2 = FakeDeployClient(), FakeDeployClient()
        conn1 = dict(CONNECTION, owner_email="jane@acme.com")
        conn2 = {"external_user_id": "trace-s2", "vault_ref": "vlt-s2",
                 "apps": {"gmail": {"usable": True}}, "owner_email": "bob@blep.io"}
        deploy.deploy_live(bundle, live, conn1, c1)
        deploy.deploy_live(bundle, live, conn2, c2)      # same workflow, other owner
        n1, n2 = c1.agents[0]["name"], c2.agents[0]["name"]
        self.assertEqual(n1, config.agent_platform_name("jane@acme.com", live["name"]))
        self.assertIn("jane-acme-com", n1)
        self.assertIn("bob-blep-io", n2)
        self.assertNotEqual(n1, n2)
        # same owner redeploying keeps the same name — that IS the reuse key
        c1b = FakeDeployClient()
        deploy.deploy_live(bundle, live, conn1, c1b)
        self.assertEqual(c1b.agents[0]["name"], n1)
        # the memory store stays keyed on the BARE workflow name: renaming the
        # platform agent must not orphan what the agent has learned
        self.assertEqual(
            c1.memory_stores[0]["name"],
            config.memory_store_name(CONNECTION["external_user_id"], live["name"]))

    def test_runs_on_both_domains(self):
        for dom in ("marcy", "recruiter"):
            bundle, live = _live_dict(dom)
            rec = deploy.deploy_live(bundle, live, CONNECTION, FakeDeployClient())
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


class MemoryStoreIsolationTest(unittest.TestCase):
    """A memory store is reused BY NAME, so the name is the isolation boundary.

    name_slug falls back when given nothing, and memory_store_name used to pass "user"
    as that fallback — producing "trace-user-<workflow>-memory", a name with no owner in
    it. Two customers deploying a similarly-named workflow with a missing
    external_user_id resolved to ONE store and could read each other's notes.

    external_user_id comes from the connection record, which lives on the container
    filesystem that Render wipes on deploy, so an empty value is a thing that happens.
    """

    def test_two_users_never_share_a_store_name(self):
        a = config.memory_store_name("trace-user-aaa", "Shipping")
        b = config.memory_store_name("trace-user-bbb", "Shipping")
        self.assertNotEqual(a, b)
        self.assertIn("aaa", a)
        self.assertIn("bbb", b)

    def test_missing_external_user_id_is_refused_not_defaulted(self):
        for empty in ("", None, "   ", "!!!"):
            with self.assertRaises(ValueError):
                config.memory_store_name(empty, "Shipping")

    def test_same_user_different_workflows_get_different_stores(self):
        self.assertNotEqual(config.memory_store_name("trace-user-aaa", "Shipping"),
                            config.memory_store_name("trace-user-aaa", "Remittance"))


class UsersApiWiringTest(unittest.TestCase):
    """app.py's owner-email lookups are wrapped in `except Exception: pass` so naming
    can never block a deploy. That also makes an AttributeError from a misspelled module
    function completely silent — and it shipped that way: users.py defines get(), app.py
    called users.get_user(), so owner_email resolved to None on every path and the whole
    owner-naming feature was inert. Nothing failed, so nothing caught it.

    Asserting the call sites against the real module catches that class outright.
    """

    def test_app_only_calls_users_functions_that_exist(self):
        import inspect
        import re
        from trace.ui import users
        src = inspect.getsource(app)
        called = set(re.findall(r"\busers\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", src))
        self.assertIn("get", called)                     # the lookup is still wired up
        missing = sorted(n for n in called if not hasattr(users, n))
        self.assertEqual(missing, [], "app.py calls users.%s, which do not exist"
                         % ", users.".join(missing))


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
        # the session's workflow names are passed so cleanup can sweep stale duplicates
        self.assertIn("Order intake", captured["agent_names"])


if __name__ == "__main__":
    unittest.main()
