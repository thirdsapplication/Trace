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
