"""Preflight — credential/connectivity checks before a live run (offline).

Verifies the per-check shape, that Pipedream reports "not configured" when the env vars are
unset (Tier-1 users aren't alarmed), that a reachable project reports green, that a raised
error fails the check WITHOUT echoing the saved API key, and the two thin client probes
(ManagedAgentsClient.ping, ConnectClient.list_apps).
"""

import tempfile
import unittest

from trace.agent import api_client, config, connect_client
from trace.tests.test_ui_agent import FakeKeystore
from trace.ui import app


class _FakeMA:
    def __init__(self, *a, **k):
        pass

    def ping(self):
        return True


class _FakeConnect:
    def __init__(self, apps=None, error=None):
        self._apps = apps if apps is not None else [{"slug": "gmail", "name": "Gmail"}]
        self._error = error

    def list_apps(self):
        if self._error:
            raise self._error
        return self._apps


def _state(connect_fn=None, key="sk-ant-secret-XYZ"):
    tmp = tempfile.mkdtemp()
    return app.AppState(tmp, key_store=FakeKeystore(key),
                        connect_fn=connect_fn or (lambda: _FakeConnect()))


def _check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


class PreflightTest(unittest.TestCase):
    def setUp(self):
        # default test env has no Pipedream creds; control them per test.
        self._saved = (config.PIPEDREAM_PROJECT, config.PIPEDREAM_CLIENT_ID,
                       config.PIPEDREAM_CLIENT_SECRET)
        config.PIPEDREAM_PROJECT = config.PIPEDREAM_CLIENT_ID = config.PIPEDREAM_CLIENT_SECRET = ""
        self._saved_ma = api_client.ManagedAgentsClient
        api_client.ManagedAgentsClient = _FakeMA

    def tearDown(self):
        (config.PIPEDREAM_PROJECT, config.PIPEDREAM_CLIENT_ID,
         config.PIPEDREAM_CLIENT_SECRET) = self._saved
        api_client.ManagedAgentsClient = self._saved_ma

    def test_shape_and_named_checks(self):
        res = app._preflight(_state())
        names = [c["name"] for c in res["checks"]]
        self.assertIn("Anthropic API key", names)
        self.assertIn("Managed Agents access", names)
        self.assertIn("Pipedream Connect", names)
        for c in res["checks"]:
            self.assertIn("ok", c); self.assertIn("detail", c)

    def test_pipedream_not_configured_is_neutral(self):
        c = _check(app._preflight(_state()), "Pipedream Connect")
        self.assertIsNone(c["ok"])                       # not red — just not set up
        self.assertIn("not configured", c["detail"].lower())

    def test_pipedream_reachable_when_configured(self):
        config.PIPEDREAM_PROJECT = "proj_x"
        config.PIPEDREAM_CLIENT_ID = "cid"
        config.PIPEDREAM_CLIENT_SECRET = "csec"
        c = _check(app._preflight(_state()), "Pipedream Connect")
        self.assertTrue(c["ok"])
        self.assertIn("reachable", c["detail"].lower())

    def test_error_fails_check_without_echoing_key(self):
        config.PIPEDREAM_PROJECT = "proj_x"
        config.PIPEDREAM_CLIENT_ID = "cid"
        config.PIPEDREAM_CLIENT_SECRET = "csec"
        boom = lambda: _FakeConnect(error=RuntimeError("401 unauthorized"))
        res = app._preflight(_state(connect_fn=boom, key="sk-ant-secret-XYZ"))
        c = _check(res, "Pipedream Connect")
        self.assertFalse(c["ok"])
        # the saved API key must never appear anywhere in the output
        self.assertNotIn("sk-ant-secret-XYZ", repr(res))

    def test_managed_agents_check_uses_ping(self):
        c = _check(app._preflight(_state()), "Managed Agents access")
        self.assertTrue(c["ok"])                         # _FakeMA.ping() -> True


class PingTest(unittest.TestCase):
    def test_ping_lists_agents(self):
        calls = {}

        class _Agents:
            def list(self):
                calls["listed"] = True
                return []

        class _Beta:
            pass
        beta = _Beta(); beta.agents = _Agents()

        class _Client:
            pass
        client = _Client(); client.beta = beta

        c = api_client.ManagedAgentsClient.__new__(api_client.ManagedAgentsClient)
        c._beta_version = api_client.BETA_VERSION
        c._client = client
        self.assertTrue(c.ping())
        self.assertTrue(calls["listed"])


class ListAppsTest(unittest.TestCase):
    def test_list_apps_normalizes_slugs(self):
        c = connect_client.ConnectClient(api_key="k", pipedream_project="proj_x")
        captured = {}

        def fake_pd(method, path, body=None):
            captured["call"] = (method, path)
            return {"data": [{"name_slug": "google_sheets", "name": "Google Sheets"},
                             {"name_slug": "gmail", "name": "Gmail"}]}
        c._pd_request = fake_pd
        apps = c.list_apps()
        self.assertEqual(captured["call"][0], "GET")
        self.assertIn("/apps", captured["call"][1])
        self.assertEqual({a["slug"] for a in apps}, {"google_sheets", "gmail"})


if __name__ == "__main__":
    unittest.main()
