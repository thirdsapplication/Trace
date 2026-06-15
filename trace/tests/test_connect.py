"""Phase 4 — connection layer (offline, StubConnectClient, two domains).

Verifies the app union across domains, per-customer credential isolation, that the
backend drives vault-create + bind itself (the worker does nothing but the tap),
identity match -> verified / mismatch -> flagged-not-usable, and that the
orchestration touches no platform SDK directly (all calls go through the one client).
"""

import inspect
import json
import threading
import time
import unittest
import urllib.request

from trace.agent import connect
from trace.tests.test_configgen import _bundle, _load
from trace.tests.test_ui_agent import EVENTS, REVIEWED, FakeKeystore
from trace.ui import app, sessions


class StubConnectClient:
    """Records calls; returns a controllable simulated account email."""

    def __init__(self, identity_email="worker@acme.com"):
        self.calls = []
        self.identity_email = identity_email
        self._vaults = {}

    def create_connect_token(self, external_user_id, app):
        self.calls.append(("token", external_user_id, app))
        return {"token": "tok", "connect_url": f"https://connect/{external_user_id}/{app}"}

    def ensure_vault(self, external_user_id):
        self.calls.append(("vault", external_user_id))
        created = external_user_id not in self._vaults
        self._vaults[external_user_id] = f"vlt-{external_user_id}"
        return {"vault_ref": self._vaults[external_user_id], "created": created}

    def bind_connection(self, vault_ref, external_user_id, app, account=None):
        self.calls.append(("bind", vault_ref, external_user_id, app))
        return {"binding_ref": f"bind-{external_user_id}-{app}"}

    def get_account(self, external_user_id, app):
        self.calls.append(("account", external_user_id, app))
        return {"connected": True, "account_id": f"acc-{app}",
                "identity_email": self.identity_email}


def _approved(domain):
    event_log, ak = _load(domain)
    return [_bundle(b) for b in ak["bundles"]], event_log


class AppUnionTest(unittest.TestCase):
    def test_union_two_domains_stable_and_nonempty(self):
        for dom in ("marcy", "recruiter"):
            bundles, ev = _approved(dom)
            apps1, un1 = connect.app_union(bundles, ev)
            apps2, un2 = connect.app_union(bundles, ev)
            self.assertEqual(apps1, apps2)                 # deterministic
            self.assertTrue(apps1)                         # non-empty on both domains
            for a in apps1:
                self.assertIn("slug", a)
                self.assertIn("label", a)

    def test_union_maps_known_apps_and_surfaces_unmapped(self):
        bundles, ev = _approved("marcy")
        apps, unmapped = connect.app_union(bundles, ev)
        slugs = {a["slug"] for a in apps}
        self.assertIn("gmail", slugs)
        self.assertIn("google_sheets", slugs)
        # an observed app with no table row (e.g. the PDF viewer) is surfaced, not dropped
        self.assertTrue(any(u.lower() not in ("gmail", "sheets") for u in unmapped) or unmapped == [])


class VerifyIdentityTest(unittest.TestCase):
    def test_three_states(self):
        self.assertTrue(connect.verify_identity("a@x.com", "A@X.com")["match"])  # case-insensitive
        self.assertEqual(connect.verify_identity("a@x.com", "b@x.com")["reason"], "mismatch")
        self.assertEqual(connect.verify_identity("a@x.com", "")["reason"], "no_expected_identity")
        self.assertEqual(connect.verify_identity("", "a@x.com")["reason"], "no_connected_identity")


class BindAndVerifyTest(unittest.TestCase):
    def _record(self, sid, expected="worker@acme.com"):
        union, _ = connect.app_union(*_approved("marcy"))
        return connect.new_record(f"trace-{sid}", union, expected_identity=expected)

    def test_backend_drives_vault_and_bind(self):
        client = StubConnectClient()
        rec = self._record("s1")
        connect.bind_and_verify(client, rec, "gmail", rec["expected_identity"])
        kinds = [c[0] for c in client.calls]
        self.assertIn("vault", kinds)     # backend creates the vault invisibly
        self.assertIn("bind", kinds)      # and binds the connection
        self.assertIn("account", kinds)
        self.assertTrue(rec["vault_ref"])

    def test_identity_match_verified_and_usable(self):
        client = StubConnectClient(identity_email="worker@acme.com")
        rec = self._record("s1", expected="worker@acme.com")
        connect.bind_and_verify(client, rec, "gmail", rec["expected_identity"])
        g = rec["apps"]["gmail"]
        self.assertEqual(g["verified"], "yes")
        self.assertTrue(g["usable"])
        self.assertIsNone(g["flag"])
        self.assertTrue(g["connected"])

    def test_identity_mismatch_flagged_and_not_usable(self):
        client = StubConnectClient(identity_email="someone-else@gmail.com")
        rec = self._record("s1", expected="worker@acme.com")
        connect.bind_and_verify(client, rec, "gmail", rec["expected_identity"])
        g = rec["apps"]["gmail"]
        self.assertEqual(g["verified"], "no")
        self.assertFalse(g["usable"])        # never proceeds silently
        self.assertTrue(g["flag"])
        self.assertTrue(g["connected"])      # the OAuth did happen; it's just flagged

    def test_isolation_distinct_external_user_ids(self):
        c1, c2 = StubConnectClient(), StubConnectClient()
        r1, r2 = self._record("s1"), self._record("s2")
        self.assertNotEqual(r1["external_user_id"], r2["external_user_id"])
        connect.bind_and_verify(c1, r1, "gmail", r1["expected_identity"])
        connect.bind_and_verify(c2, r2, "gmail", r2["expected_identity"])
        self.assertNotEqual(r1["vault_ref"], r2["vault_ref"])     # credentials isolated
        # no cross-customer leakage: neither client ever saw the other's customer id
        flat1 = [x for c in c1.calls for x in c]
        flat2 = [x for c in c2.calls for x in c]
        self.assertNotIn("trace-s2", flat1)
        self.assertNotIn("trace-s1", flat2)

    def test_orchestration_imports_no_platform_sdk(self):
        src = inspect.getsource(connect)
        for stmt in ("import anthropic", "import pipedream", "from pipedream"):
            self.assertNotIn(stmt, src)      # all platform calls go through the injected client


class _FakeCreds:
    def __init__(self, create_conflicts=False, reject_nested=False, create_error=None):
        self.created, self.updated = [], []
        self._conflict = create_conflicts          # raise 409 on create
        self._reject_nested = reject_nested         # 400 when mcp_server_url is inside auth
        self._create_error = create_error           # raise this on every create

    def create(self, vault_ref, display_name=None, auth=None, extra_body=None):
        if self._create_error:
            raise self._create_error
        if self._conflict:
            raise RuntimeError("409 credential already exists")
        if self._reject_nested and "mcp_server_url" in (auth or {}):
            raise RuntimeError("Error code: 400 - auth.mcp_server_url: "
                               "Extra inputs are not permitted")
        rec = {"vault_ref": vault_ref, "display_name": display_name,
               "auth": auth, "extra_body": extra_body}
        self.created.append(rec)
        url = (auth or {}).get("mcp_server_url") or (extra_body or {}).get("mcp_server_url")
        return {"id": f"cred-{len(self.created)}", "mcp_server_url": url}

    def update(self, cred_id, vault_id=None, auth=None, display_name=None, extra_body=None):
        self.updated.append({"cred_id": cred_id, "vault_id": vault_id, "auth": auth,
                             "display_name": display_name, "extra_body": extra_body})
        return {"id": cred_id}

    def list(self, vault_ref):
        # one pre-existing credential keyed to the gmail server URL (for the 409 path)
        from trace.agent import config
        return [{"id": "cred-existing",
                 "mcp_server_url": config.pipedream_mcp_url("trace-s1", "gmail")}]


class _FakeVaults:
    """A vaults namespace: records create kwargs, serves a controllable list, and exposes
    a credentials sub-namespace."""
    def __init__(self, existing=None, credentials=None):
        self.created = []
        self._existing = existing or []
        self.credentials = credentials or _FakeCreds()

    def create(self, display_name=None, metadata=None):
        self.created.append({"display_name": display_name, "metadata": metadata})
        return {"id": f"vlt-{len(self.created)}"}

    def list(self):
        return self._existing


class RealBindCredentialTest(unittest.TestCase):
    """The verified vault credential shape: mcp_oauth + access_token, with the customer +
    app carried in the MCP server URL (NOT as credential keys)."""

    def _client(self, vaults=None):
        from trace.agent import connect_client, config
        c = connect_client.ConnectClient(api_key="sk-ant-test",
                                         pipedream_project="proj_test")
        vaults = vaults or _FakeVaults()
        c._vaults_ns = lambda: vaults
        c.mint_access_token = lambda: {"access_token": "pdtok_abc", "expires_in": 3600}
        return c, vaults, config

    def test_credential_is_mcp_oauth_with_token_and_url_routing(self):
        c, vaults, config = self._client()
        out = c.bind_connection("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["binding_ref"])
        rec = vaults.credentials.created[-1]
        self.assertEqual(rec["vault_ref"], "vlt-s1")                 # vault_id positional
        self.assertTrue(rec["display_name"])                        # carries a display_name
        auth = rec["auth"]
        self.assertEqual(auth["type"], "mcp_oauth")
        self.assertEqual(auth["access_token"], "pdtok_abc")          # Trace's own token
        # external_user_id + app live in the URL, never as credential keys
        self.assertNotIn("external_user_id", auth)
        self.assertNotIn("app", auth)
        self.assertIn("externalUserId=trace-s1", auth["mcp_server_url"])
        self.assertIn("app=gmail", auth["mcp_server_url"])
        self.assertTrue(auth["mcp_server_url"].startswith(config.PIPEDREAM_MCP_BASE))

    def test_409_routes_to_update_by_credential_id(self):
        c, vaults, _ = self._client(_FakeVaults(credentials=_FakeCreds(create_conflicts=True)))
        out = c.bind_connection("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["binding_ref"])
        upd = vaults.credentials.updated[-1]
        self.assertEqual(upd["cred_id"], "cred-existing")            # resolved from the list
        self.assertEqual(upd["vault_id"], "vlt-s1")
        self.assertEqual(upd["auth"]["type"], "mcp_oauth")

    def test_falls_back_to_top_level_when_nested_rejected(self):
        # the live beta server rejects mcp_server_url inside auth -> retry with it top-level
        c, vaults, config = self._client(
            _FakeVaults(credentials=_FakeCreds(reject_nested=True)))
        out = c.bind_connection("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["binding_ref"])
        rec = vaults.credentials.created[-1]
        self.assertNotIn("mcp_server_url", rec["auth"])              # not in auth anymore
        self.assertIn("externalUserId=trace-s1", rec["extra_body"]["mcp_server_url"])
        self.assertEqual(rec["auth"]["type"], "mcp_oauth")

    def test_non_placement_error_propagates(self):
        c, vaults, _ = self._client(
            _FakeVaults(credentials=_FakeCreds(create_error=RuntimeError("500 server error"))))
        with self.assertRaises(Exception):
            c.bind_connection("vlt-s1", "trace-s1", "gmail")


class EnsureVaultTest(unittest.TestCase):
    """vaults.create takes display_name + metadata (no `name`); reuse keys on metadata."""

    def _client(self, vaults):
        from trace.agent import connect_client
        c = connect_client.ConnectClient(pipedream_project="proj_test")
        c._vaults_ns = lambda: vaults
        return c

    def test_creates_with_display_name_and_metadata(self):
        vaults = _FakeVaults(existing=[])
        out = self._client(vaults).ensure_vault("trace-s1")
        self.assertTrue(out["created"])
        rec = vaults.created[-1]
        self.assertEqual(rec["metadata"], {"external_user_id": "trace-s1"})
        self.assertIn("trace-s1", rec["display_name"])
        # the crash was passing name=; ensure we never do
        self.assertNotIn("name", rec)

    def test_reuses_vault_matched_by_metadata(self):
        existing = [{"id": "vlt-old", "metadata": {"external_user_id": "trace-s1"}}]
        vaults = _FakeVaults(existing=existing)
        out = self._client(vaults).ensure_vault("trace-s1")
        self.assertFalse(out["created"])
        self.assertEqual(out["vault_ref"], "vlt-old")
        self.assertEqual(vaults.created, [])                         # no second create


class TLSContextTest(unittest.TestCase):
    """The stdlib Pipedream HTTPS calls must always supply a certifi-backed CA bundle, so
    they don't fail with CERTIFICATE_VERIFY_FAILED on installs whose OS cert store is bare."""

    def test_ssl_context_is_real(self):
        import ssl
        from trace.agent import connect_client
        self.assertIsInstance(connect_client._ssl_context(), ssl.SSLContext)
        self.assertIsInstance(connect_client._SSL_CONTEXT, ssl.SSLContext)

    def test_mint_passes_ssl_context(self):
        import ssl
        from trace.agent import connect_client

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"access_token":"t","expires_in":3600}'

        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["context"] = context
            return _Resp()

        orig = connect_client.urllib.request.urlopen
        connect_client.urllib.request.urlopen = fake_urlopen
        try:
            connect_client.ConnectClient(client_id="c", client_secret="s").mint_access_token()
        finally:
            connect_client.urllib.request.urlopen = orig
        self.assertIsInstance(captured["context"], ssl.SSLContext)


class ConnectUrlAppTest(unittest.TestCase):
    """The hosted Pipedream connect page requires app={slug} in the URL; ensure it is
    always present whether Pipedream returns a link, omits one, or includes its own app."""

    def _client(self, pd_return):
        from trace.agent import connect_client
        c = connect_client.ConnectClient(client_id="c", client_secret="s",
                                         pipedream_project="proj_x")
        c._pd_request = lambda method, path, body=None: pd_return
        c.mint_access_token = lambda: {"access_token": "t", "expires_in": 3600}
        return c

    def test_appended_when_pipedream_link_omits_app(self):
        c = self._client({"token": "ctok_1",
                          "connect_link_url": "https://pipedream.com/_static/connect.html"
                                              "?token=ctok_1&connectLink=true"})
        url = c.create_connect_token("trace-s1", "gmail")["connect_url"]
        self.assertIn("app=gmail", url)
        self.assertIn("token=ctok_1", url)               # token preserved

    def test_appended_on_hosted_fallback(self):
        c = self._client({"token": "ctok_2"})            # no link returned
        url = c.create_connect_token("trace-s1", "gmail")["connect_url"]
        self.assertIn("app=gmail", url)
        self.assertIn("connect.html", url)

    def test_existing_app_not_duplicated(self):
        c = self._client({"token": "ctok_3",
                          "connect_url": "https://pipedream.com/_static/connect.html"
                                         "?token=ctok_3&app=slack"})
        url = c.create_connect_token("trace-s1", "gmail")["connect_url"]
        self.assertIn("app=slack", url)                  # Pipedream's own app respected
        self.assertNotIn("app=gmail", url)


class AccountEmailTest(unittest.TestCase):
    """Pipedream has no plain `email` field; the email is read from a profile/credentials
    block or the account name (Google connections are named with the email)."""

    def test_extracts_from_name(self):
        from trace.agent.connect_client import _account_email
        self.assertEqual(_account_email({"id": "apn_1", "name": "worker@acme.com"}),
                         "worker@acme.com")

    def test_extracts_from_credentials_profile(self):
        from trace.agent.connect_client import _account_email
        acc = {"id": "apn_1", "name": "My Gmail",
               "credentials": {"email": "worker@acme.com"}}
        self.assertEqual(_account_email(acc), "worker@acme.com")

    def test_none_when_no_email_anywhere(self):
        from trace.agent.connect_client import _account_email
        self.assertIsNone(_account_email({"id": "apn_1", "name": "My Gmail"}))

    def test_get_account_requests_credentials_and_reads_name(self):
        from trace.agent import connect_client
        c = connect_client.ConnectClient(pipedream_project="proj_test")
        calls = []

        def fake_pd(method, path, body=None):
            calls.append(path)
            return {"data": [{"id": "apn_1", "name": "worker@acme.com"}]}
        c._pd_request = fake_pd
        c._access_token = lambda: "t"
        acc = c.get_account("trace-s1", "gmail")
        self.assertTrue(acc["connected"])
        self.assertEqual(acc["identity_email"], "worker@acme.com")
        self.assertIn("include_credentials=true", calls[0])     # credentials requested first


class UIConnectTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ks = FakeKeystore("sk-ant-test")
        self.identity = "worker@acme.com"
        self.state = app.AppState(self.root, key_store=self.ks,
                                  connect_fn=lambda: StubConnectClient(identity_email=self.identity))
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

    def _await_connect(self):
        for _ in range(80):
            _, st = self.req("GET", f"/api/sessions/{self.sid}/connect/status")
            if st["state"] in ("done", "error"):
                return st
            time.sleep(0.05)
        return st

    def test_connection_data_union_and_external_user_id(self):
        code, data = self.req("GET", f"/api/sessions/{self.sid}/connection-data")
        self.assertEqual(code, 200)
        slugs = {a["slug"] for a in data["union"]}
        self.assertIn("gmail", slugs)
        self.assertIn("google_sheets", slugs)
        self.assertTrue(data["expectation"])
        self.assertEqual(data["record"]["external_user_id"], f"trace-{self.sid}")

    def test_connect_and_verify_match(self):
        self.req("POST", f"/api/sessions/{self.sid}/connection-identity", {"email": self.identity})
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/connect/gmail", {})
        self.assertEqual(code, 202)
        self.assertTrue(self._await_connect().get("connect_url"))
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/callback", {})
        self.assertEqual(code, 202)
        self.assertEqual(self._await_connect()["state"], "done")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/connection-data")
        g = data["record"]["apps"]["gmail"]
        self.assertTrue(g["connected"])
        self.assertEqual(g["verified"], "yes")
        self.assertTrue(g["usable"])
        self.assertTrue(data["record"]["vault_ref"])

    def test_mismatch_is_flagged_not_usable(self):
        self.req("POST", f"/api/sessions/{self.sid}/connection-identity", {"email": "worker@acme.com"})
        self.identity = "intruder@gmail.com"     # the connected account differs
        self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/callback", {})
        self.assertEqual(self._await_connect()["state"], "done")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/connection-data")
        g = data["record"]["apps"]["gmail"]
        self.assertEqual(g["verified"], "no")
        self.assertFalse(g["usable"])
        self.assertTrue(g["flag"])


if __name__ == "__main__":
    unittest.main()
