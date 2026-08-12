"""The Aug-2026 "no update tool" fix: per-user MCP URLs + deploy-time tool preflight.

Two halves, both offline:
  * ``config.pipedream_mcp_url`` builds Pipedream's documented
    ``/{external_user_id}/{app}`` URL. The old pinned ``/v3`` query-param surface
    served a reduced Google Sheets toolset with NO update tool, stranding any
    workflow that edits existing rows.
  * ``mcp_preflight`` lists each per-app server's served tools before the agent is
    created, records them on the live record, and warns when a toolset can append
    rows but cannot update them — best-effort, never blocking a deploy.

Plus the migration property that makes redeploys self-heal after a URL-format
change: the deploy path re-binds EVERY usable app (vault credentials are keyed to
the exact server URL, so fresh-token apps must re-bind too, not just stale ones).
"""

import json
import os
import tempfile
import unittest

from trace.agent import config, deploy, mcp_preflight
from trace.tests.test_deploy import CONNECTION, FakeDeployClient
from trace.tests.test_connect import StubConnectClient
from trace.tests.test_ui_agent import FakeKeystore
from trace.ui import app, sessions


class PipedreamMcpUrlTest(unittest.TestCase):
    def test_per_user_path_format(self):
        self.assertEqual(config.pipedream_mcp_url("trace-s1", "google_sheets"),
                         f"{config.PIPEDREAM_MCP_BASE}/trace-s1/google_sheets")

    def test_no_query_params_and_no_version_pin(self):
        url = config.pipedream_mcp_url("trace-s1", "gmail")
        self.assertNotIn("?", url)
        self.assertNotIn("/v3", url)

    def test_external_user_id_is_percent_quoted(self):
        url = config.pipedream_mcp_url("user id/with junk", "gmail")
        self.assertIn("/user%20id%2Fwith%20junk/gmail", url)

    def test_empty_external_user_id_is_an_error(self):
        # the uid routes to the customer's connected account — silently emitting a
        # uid-less URL (the old behavior) would misroute, so it must raise instead
        for bad in ("", None):
            with self.assertRaises(ValueError):
                config.pipedream_mcp_url(bad, "gmail")

    def test_project_environment_kwargs_accepted_and_ignored(self):
        # call-site compatibility: both now ride in the project-scoped bearer token
        self.assertEqual(
            config.pipedream_mcp_url("u", "gmail", project="p", environment="dev"),
            config.pipedream_mcp_url("u", "gmail"))


class JsonRpcParsingTest(unittest.TestCase):
    TOOLS_MSG = {"jsonrpc": "2.0", "id": 2,
                 "result": {"tools": [{"name": "google_sheets-add-rows"},
                                      {"name": "google_sheets-update-row"}]}}

    def test_plain_json_body(self):
        results = mcp_preflight._jsonrpc_results(json.dumps(self.TOOLS_MSG))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["tools"]), 2)

    def test_sse_body_with_noise(self):
        raw = ("event: message\n"
               "data: " + json.dumps(self.TOOLS_MSG) + "\n"
               "\n"
               ": keepalive\n")
        results = mcp_preflight._jsonrpc_results(raw)
        self.assertEqual(len(results), 1)

    def test_non_result_messages_ignored(self):
        raw = '{"jsonrpc":"2.0","id":9,"error":{"code":-32601}}\nnot json at all'
        self.assertEqual(mcp_preflight._jsonrpc_results(raw), [])


class ListMcpToolsTest(unittest.TestCase):
    def setUp(self):
        self._orig = mcp_preflight._mcp_post
        self.calls = []

        def fake_post(url, token, body, session_id=None):
            self.calls.append((body.get("method"), session_id))
            if body.get("method") == "initialize":
                return '{"jsonrpc":"2.0","id":1,"result":{}}', "sess-1"
            if body.get("method") == "tools/list":
                return ("data: " + json.dumps(JsonRpcParsingTest.TOOLS_MSG),
                        session_id)
            return "", session_id

        mcp_preflight._mcp_post = fake_post

    def tearDown(self):
        mcp_preflight._mcp_post = self._orig

    def test_handshake_order_session_carry_and_sorted_names(self):
        names = mcp_preflight.list_mcp_tools("tok", "https://mcp/u/google_sheets")
        self.assertEqual(names, ["google_sheets-add-rows", "google_sheets-update-row"])
        self.assertEqual([m for m, _ in self.calls],
                         ["initialize", "notifications/initialized", "tools/list"])
        # the initialize-issued session id rides on the tools/list call
        self.assertEqual(self.calls[2][1], "sess-1")

    def test_failed_initialized_notification_is_tolerated(self):
        orig = mcp_preflight._mcp_post

        def flaky(url, token, body, session_id=None):
            if body.get("method") == "notifications/initialized":
                raise RuntimeError("202 no body")
            return orig(url, token, body, session_id)

        mcp_preflight._mcp_post = flaky
        names = mcp_preflight.list_mcp_tools("tok", "https://mcp/u/google_sheets")
        self.assertIn("google_sheets-update-row", names)


class ToolsetWarningsTest(unittest.TestCase):
    def test_append_without_update_warns(self):
        # the exact incident shape: the served sheets toolset appends but can't edit
        w = mcp_preflight.toolset_warnings({"google_sheets": [
            "google_sheets-add-rows", "google_sheets-find-rows",
            "google_sheets-read-rows", "retrieve_options"]})
        self.assertEqual(len(w), 1)
        self.assertIn("google_sheets", w[0])
        self.assertIn("update", w[0].lower())

    def test_update_or_upsert_silences_the_warning(self):
        for fixer in ("google_sheets-update-row", "google_sheets-upsert-row"):
            w = mcp_preflight.toolset_warnings(
                {"google_sheets": ["google_sheets-add-rows", fixer]})
            self.assertEqual(w, [], fixer)

    def test_apps_without_row_appends_never_warn(self):
        self.assertEqual(mcp_preflight.toolset_warnings(
            {"gmail": ["gmail-find-email", "gmail-send-email"]}), [])
        self.assertEqual(mcp_preflight.toolset_warnings({}), [])


class _MintingClient:
    """The one ConnectClient method the preflight needs, controllable."""

    def __init__(self, fail=False):
        self.fail = fail
        self.mints = 0

    def mint_access_token(self):
        self.mints += 1
        if self.fail:
            raise RuntimeError("Pipedream 401 on POST /oauth/token: bad client")
        return {"access_token": "pdtok", "expires_in": 3600}


class PreflightAppsTest(unittest.TestCase):
    def setUp(self):
        self._orig = mcp_preflight.list_mcp_tools

    def tearDown(self):
        mcp_preflight.list_mcp_tools = self._orig

    def test_one_token_all_apps_per_user_urls(self):
        seen = []
        mcp_preflight.list_mcp_tools = \
            lambda tok, url: (seen.append((tok, url)) or ["t1"])
        client = _MintingClient()
        out = mcp_preflight.preflight_apps(client, "trace-s1",
                                           ["gmail", "google_sheets"])
        self.assertEqual(client.mints, 1)                      # minted once, reused
        self.assertEqual(set(out["toolsets"]), {"gmail", "google_sheets"})
        self.assertEqual(out["errors"], {})
        self.assertEqual([u for _, u in seen],
                         [config.pipedream_mcp_url("trace-s1", "gmail"),
                          config.pipedream_mcp_url("trace-s1", "google_sheets")])

    def test_per_app_failure_recorded_not_raised(self):
        def flaky(tok, url):
            if "google_sheets" in url:
                raise RuntimeError("Pipedream MCP 500 on POST " + url)
            return ["gmail-find-email"]

        mcp_preflight.list_mcp_tools = flaky
        out = mcp_preflight.preflight_apps(_MintingClient(), "u",
                                           ["gmail", "google_sheets"])
        self.assertEqual(out["toolsets"], {"gmail": ["gmail-find-email"]})
        self.assertIn("google_sheets", out["errors"])
        self.assertIn("500", out["errors"]["google_sheets"])

    def test_token_mint_failure_short_circuits(self):
        out = mcp_preflight.preflight_apps(_MintingClient(fail=True), "u", ["gmail"])
        self.assertEqual(out["toolsets"], {})
        self.assertIn("*", out["errors"])


class DeployPreflightTest(unittest.TestCase):
    """deploy_live records the preflight on the live record and never blocks on it."""

    def _envelope(self):
        return {"name": "wf", "model": "m", "system": "s",
                "tools": [{"type": config.AGENT_TOOLSET_TYPE}], "mcp_servers": []}

    def setUp(self):
        self._orig = mcp_preflight.preflight_apps

    def tearDown(self):
        mcp_preflight.preflight_apps = self._orig

    def test_preflight_result_lands_on_live_record(self):
        mcp_preflight.preflight_apps = lambda c, uid, slugs: {
            "toolsets": {"gmail": ["gmail-find-email"]},
            "warnings": ["google_sheets: served toolset has append tools but NO "
                         "update/upsert tool"],
            "errors": {}}
        rec = deploy.deploy_live({"id": "B1"}, self._envelope(), CONNECTION,
                                 FakeDeployClient(), connect_client=object())
        self.assertEqual(rec["status"], "live")
        self.assertEqual(rec["toolsets"], {"gmail": ["gmail-find-email"]})
        self.assertEqual(len(rec["preflight_warnings"]), 1)
        self.assertEqual(rec["preflight_errors"], {})

    def test_no_connect_client_skips_preflight_quietly(self):
        called = []
        mcp_preflight.preflight_apps = lambda *a: called.append(a)
        rec = deploy.deploy_live({"id": "B1"}, self._envelope(), CONNECTION,
                                 FakeDeployClient())
        self.assertEqual(called, [])
        self.assertEqual(rec["status"], "live")
        self.assertEqual(rec["toolsets"], {})

    def test_preflight_crash_never_blocks_deploy(self):
        def boom(*a):
            raise RuntimeError("pipedream unreachable")

        mcp_preflight.preflight_apps = boom
        rec = deploy.deploy_live({"id": "B1"}, self._envelope(), CONNECTION,
                                 FakeDeployClient(), connect_client=object())
        self.assertEqual(rec["status"], "live")
        self.assertIn("*", rec["preflight_errors"])


class DeployRebindsAllUsableAppsTest(unittest.TestCase):
    """URL-migration property: the deploy path re-binds EVERY usable app, fresh
    token or not — refresh_binding's bind fallback is what moves vault credentials
    to a changed MCP URL format, and it only runs if the app is actually visited."""

    def test_fresh_tokens_are_rebound_too(self):
        tmp = tempfile.mkdtemp()
        stub = StubConnectClient()
        state = app.AppState(tmp, key_store=FakeKeystore("sk-ant-x"),
                             connect_fn=lambda: stub)
        os.makedirs(os.path.join(state.sessions_root, "s1"), exist_ok=True)
        conn = {"external_user_id": "trace-s1", "vault_ref": "vlt-s1",
                "apps": {"gmail": {"usable": True,
                                   "token_expires_at": "2099-01-01T00:00:00Z"},
                         "google_sheets": {"usable": True,
                                           "token_expires_at": "2099-01-01T00:00:00Z"},
                         "slack": {"usable": False}}}
        out = state._refresh_stale_bindings("s1", conn)
        refreshed = sorted(c[3] for c in stub.calls if c[0] == "refresh")
        self.assertEqual(refreshed, ["gmail", "google_sheets"])   # usable only
        self.assertEqual(out["apps"]["gmail"]["token_expires_at"],
                         "2099-01-01T00:00:00Z")
        # and the record was persisted for later phases
        path = sessions.path(state.sessions_root, "s1", sessions.CONNECTION)
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
