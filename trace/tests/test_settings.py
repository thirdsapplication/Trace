"""Settings storage — Anthropic key + Pipedream Connect values, entered in the UI.

Verifies the keystore stores the three Pipedream values (secret kept out of the exposed
status), that saving one field never clobbers the others or the API key, that the values
are pushed into the agent config, and that the HTTP routes round-trip.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from trace.agent import config
from trace.ui import app, keystore, sessions


class KeystorePipedreamTest(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (keystore._FALLBACK_DIR, keystore._FALLBACK_PATH, keystore._keyring)
        keystore._FALLBACK_DIR = self._tmp.name
        keystore._FALLBACK_PATH = os.path.join(self._tmp.name, "config.json")
        keystore._keyring = lambda: None            # force the 0600 file backend

    def tearDown(self):
        keystore._FALLBACK_DIR, keystore._FALLBACK_PATH, keystore._keyring = self._saved
        self._tmp.cleanup()

    def test_round_trip_and_masking(self):
        keystore.save_pipedream(project_id="proj_x", client_id="cid_1234",
                                client_secret="shh_secret", environment="production")
        info = keystore.pipedream_info()
        self.assertTrue(info["configured"])
        self.assertEqual(info["project_id"], "proj_x")        # ids are not secret
        self.assertEqual(info["client_id_tail"], "…1234")
        self.assertTrue(info["has_secret"])
        self.assertEqual(info["environment"], "production")
        self.assertNotIn("shh_secret", json.dumps(info))       # secret never exposed

    def test_partial_update_keeps_secret(self):
        keystore.save_pipedream(project_id="proj_x", client_id="cid", client_secret="sec")
        keystore.save_pipedream(project_id="proj_y")           # update only the project
        pd = keystore.get_pipedream()
        self.assertEqual(pd["project_id"], "proj_y")
        self.assertEqual(pd["client_secret"], "sec")           # secret preserved

    def test_key_and_pipedream_coexist(self):
        keystore.save_key("sk-ant-key")
        keystore.save_pipedream(project_id="proj_x", client_id="cid", client_secret="sec")
        self.assertEqual(keystore.get_key(), "sk-ant-key")     # not clobbered
        self.assertTrue(keystore.pipedream_info()["configured"])
        keystore.clear_pipedream()
        self.assertEqual(keystore.get_key(), "sk-ant-key")     # still there after clear
        self.assertFalse(keystore.pipedream_info()["configured"])

    def test_apply_pushes_into_config(self):
        saved = (config.PIPEDREAM_PROJECT, config.PIPEDREAM_CLIENT_ID,
                 config.PIPEDREAM_CLIENT_SECRET, config.PIPEDREAM_ENVIRONMENT)
        try:
            keystore.save_pipedream(project_id="proj_z", client_id="cidz",
                                    client_secret="secz", environment="production")
            app._apply_pipedream_settings(keystore)
            self.assertEqual(config.PIPEDREAM_PROJECT, "proj_z")
            self.assertEqual(config.PIPEDREAM_CLIENT_SECRET, "secz")
            self.assertEqual(config.PIPEDREAM_ENVIRONMENT, "production")
            # and the per-app MCP URL now carries the real project id
            self.assertIn("projectId=proj_z", config.pipedream_mcp_url("trace-s1", "gmail"))
        finally:
            (config.PIPEDREAM_PROJECT, config.PIPEDREAM_CLIENT_ID,
             config.PIPEDREAM_CLIENT_SECRET, config.PIPEDREAM_ENVIRONMENT) = saved


class PipedreamRouteTest(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_ks = (keystore._FALLBACK_DIR, keystore._FALLBACK_PATH, keystore._keyring)
        keystore._FALLBACK_DIR = self._tmp.name
        keystore._FALLBACK_PATH = os.path.join(self._tmp.name, "config.json")
        keystore._keyring = lambda: None
        self._saved_cfg = (config.PIPEDREAM_PROJECT, config.PIPEDREAM_CLIENT_ID,
                           config.PIPEDREAM_CLIENT_SECRET, config.PIPEDREAM_ENVIRONMENT)
        self.root = os.path.join(self._tmp.name, "sessions")
        self.state = app.AppState(self.root, key_store=keystore)
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        keystore._FALLBACK_DIR, keystore._FALLBACK_PATH, keystore._keyring = self._saved_ks
        (config.PIPEDREAM_PROJECT, config.PIPEDREAM_CLIENT_ID,
         config.PIPEDREAM_CLIENT_SECRET, config.PIPEDREAM_ENVIRONMENT) = self._saved_cfg
        self._tmp.cleanup()

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

    def test_save_get_delete_and_config_applied(self):
        code, data = self.req("GET", "/api/settings/pipedream")
        self.assertEqual(code, 200)
        self.assertFalse(data["configured"])

        code, data = self.req("POST", "/api/settings/pipedream",
                              {"project_id": "proj_http", "client_id": "cid_9999",
                               "client_secret": "topsecret", "environment": "production"})
        self.assertEqual(code, 200)
        self.assertTrue(data["configured"])
        self.assertEqual(data["client_id_tail"], "…9999")
        self.assertNotIn("topsecret", json.dumps(data))        # secret never returned
        self.assertEqual(config.PIPEDREAM_PROJECT, "proj_http")  # applied live

        code, data = self.req("DELETE", "/api/settings/pipedream")
        self.assertEqual(code, 200)
        self.assertFalse(data["configured"])


if __name__ == "__main__":
    unittest.main()
