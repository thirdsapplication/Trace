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

from trace.agent import config, connect, verify_apps
from trace.tests.test_configgen import _bundle, _load
from trace.tests.test_ui_agent import EVENTS, REVIEWED, FakeKeystore
from trace.ui import app, sessions


class StubConnectClient:
    """Records calls; returns a controllable simulated account email."""

    def __init__(self, identity_email="worker@acme.com", authorized=None):
        self.calls = []
        self.identity_email = identity_email
        self._vaults = {}
        # Apps this customer has already authorized. Pipedream accounts are scoped to
        # external_user_id, so an app stays connected across sessions once approved —
        # which is what lets connect_app skip the OAuth round trip on later agents.
        # None = "already authorized" (the legacy stub behaviour, used by tests that
        # call bind_and_verify directly). A set models real authorization state.
        self.authorized = None if authorized is None else set(authorized)

    def create_connect_token(self, external_user_id, app):
        self.calls.append(("token", external_user_id, app))
        if self.authorized is not None:
            self.authorized.add(app)  # the worker approves in the browser
        return {"token": "tok", "connect_url": f"https://connect/{external_user_id}/{app}"}

    def ensure_vault(self, external_user_id):
        self.calls.append(("vault", external_user_id))
        created = external_user_id not in self._vaults
        self._vaults[external_user_id] = f"vlt-{external_user_id}"
        return {"vault_ref": self._vaults[external_user_id], "created": created}

    def bind_connection(self, vault_ref, external_user_id, app, account=None):
        self.calls.append(("bind", vault_ref, external_user_id, app))
        return {"binding_ref": f"bind-{external_user_id}-{app}", "expires_at": "2099-01-01T00:00:00Z"}

    def refresh_binding(self, vault_ref, external_user_id, app):
        self.calls.append(("refresh", vault_ref, external_user_id, app))
        return {"expires_at": "2099-01-01T00:00:00Z"}

    def get_account(self, external_user_id, app):
        self.calls.append(("account", external_user_id, app))
        if self.authorized is not None and app not in self.authorized:
            return {"connected": False}
        return {"connected": True, "account_id": f"acc-{app}",
                "identity_email": self.identity_email}

    def verify_bindings(self, vault_ref, expected_urls):
        self.calls.append(("verify", vault_ref, tuple(expected_urls)))
        return {u: True for u in expected_urls}


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

    def test_union_records_which_workflows_need_each_app(self):
        """Frames 12 and 26 caption an app with the workflows that need it. app_union was
        already iterating bundles when it found each app and threw that away collapsing
        into by_slug — so neither screen could say it, for want of a name already in hand.
        """
        bundles, ev = _approved("marcy")
        apps, _ = connect.app_union(bundles, ev)
        named = {a["slug"]: a.get("used_by") or [] for a in apps}
        self.assertTrue(any(named.values()), "no app recorded a workflow")
        for slug, used in named.items():
            self.assertEqual(len(used), len(set(used)), f"{slug} repeated a workflow")
            for name in used:
                self.assertTrue(name.strip())              # never a blank caption

    def test_used_by_survives_into_the_record_and_a_refresh(self):
        bundles, ev = _approved("marcy")
        union, unmapped = connect.app_union(bundles, ev)
        rec = connect.new_record("trace-user-x", union)
        pick = next(s for s, a in rec["apps"].items() if a["used_by"])
        before = list(rec["apps"][pick]["used_by"])
        # a re-review recomputes the union; the caption must follow it, not go stale
        connect.merge_union(rec, union, unmapped)
        self.assertEqual(rec["apps"][pick]["used_by"], before)

    def test_union_maps_known_apps_and_surfaces_unmapped(self):
        bundles, ev = _approved("marcy")
        apps, unmapped = connect.app_union(bundles, ev)
        slugs = {a["slug"] for a in apps}
        self.assertIn("gmail", slugs)
        self.assertIn("google_sheets", slugs)
        # an observed app with no table row (e.g. the PDF viewer) is surfaced, not dropped
        self.assertTrue(any(u.lower() not in ("gmail", "sheets") for u in unmapped) or unmapped == [])

    def test_union_detects_apps_from_window_titles(self):
        # a real browser capture: the service is only in the window title, not `app`
        event_log = [
            {"id": "e1", "app": "Google Chrome", "window": "Inbox (3) - you@acme.com - Gmail",
             "action": "read_email", "detail": "Read the inbound order email."},
            {"id": "e2", "app": "Google Chrome", "window": "Open_Orders - Google Sheets",
             "action": "enter_data", "detail": "Entered a new order row."},
            {"id": "e3", "app": "Preview", "window": "PO-1001.pdf",
             "action": "open_document", "detail": "Opened the attached PO."},
        ]
        bundle = {"id": "B1", "evidence_event_ids": ["e1", "e2", "e3"]}
        apps, unmapped = connect.app_union([bundle], event_log)
        slugs = {a["slug"] for a in apps}
        self.assertIn("gmail", slugs)                  # found in the window title
        self.assertIn("google_sheets", slugs)
        self.assertIn("Preview", unmapped)             # truly unrecognized, surfaced
        self.assertNotIn("Google Chrome", unmapped)    # not surfaced — its events matched

    def test_union_from_bundle_text_without_events(self):
        # A described (no-recording) workflow: zero events, apps named in the
        # bundle's own text still yield a connectable union.
        bundle = {"id": "B1", "name": "Order intake",
                  "description": "Read each new order email in Gmail.",
                  "trigger": "A new order email arrives.",
                  "steps": ["Open the order email in Gmail",
                            "Add one row to the tracker in Google Sheets"],
                  "evidence_event_ids": []}
        apps, unmapped = connect.app_union([bundle], [])
        slugs = {a["slug"] for a in apps}
        self.assertEqual(slugs, {"gmail", "google_sheets"})
        self.assertEqual(unmapped, [])                 # unmapped stays event-driven
        named = next(a for a in apps if a["slug"] == "gmail")
        self.assertEqual(named["observed_as"], ["named in workflow: Order intake"])

    def test_union_text_word_boundaries(self):
        # prose must not substring-match ('sales-driven' -> drive, 'spreadsheets' -> sheets)
        bundle = {"id": "B1", "name": "Weekly report",
                  "description": "A sales-driven workflow; update the spreadsheets.",
                  "trigger": "", "steps": [], "evidence_event_ids": []}
        apps, _ = connect.app_union([bundle], [])
        self.assertEqual(apps, [])
        # but a real word match works, case-insensitive, at sentence edges
        bundle["steps"] = ["Post the summary to Slack."]
        apps, _ = connect.app_union([bundle], [])
        self.assertEqual({a["slug"] for a in apps}, {"slack"})

    def test_union_includes_apps_named_in_correction(self):
        # a worker's review-step correction naming an app makes it connectable,
        # even when neither the events nor the discovered text mention it
        bundle = {"id": "B1", "name": "Daily update", "description": "",
                  "trigger": "", "steps": [], "evidence_event_ids": [],
                  "user_description": "Post each day's shipping updates to Slack."}
        apps, unmapped = connect.app_union([bundle], [])
        self.assertEqual({a["slug"] for a in apps}, {"slack"})
        self.assertEqual(unmapped, [])

    def test_union_text_and_events_dedupe(self):
        # the same app observed in an event AND named in text -> one entry, with the
        # event observation kept (first-seen wins)
        event_log = [{"id": "e1", "app": "Gmail", "window": "Inbox",
                      "action": "read_email", "detail": "read the order"}]
        bundle = {"id": "B1", "name": "Orders", "description": "Handle Gmail orders",
                  "trigger": "", "steps": ["Reply in Gmail"], "evidence_event_ids": ["e1"]}
        apps, unmapped = connect.app_union([bundle], event_log)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["slug"], "gmail")
        self.assertEqual(apps[0]["observed_as"], ["Gmail"])   # event-derived, not the text tag
        self.assertEqual(unmapped, [])


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

    def test_app_with_no_identity_email_is_confirmable_by_the_worker(self):
        """QuickBooks, Shopify, Stripe and friends hand Pipedream a token and a company
        name, never an email. That is not a mismatch: the entry waits for the worker's
        one-tap confirmation instead of dead-ending on 'exposed no identity email'."""
        client = StubConnectClient(identity_email=None)
        rec = self._record("s1", expected="worker@acme.com")
        connect.bind_and_verify(client, rec, "quickbooks", rec["expected_identity"])
        q = rec["apps"]["quickbooks"]
        self.assertEqual(q["verified"], "unverified")
        self.assertFalse(q["usable"])                 # still nothing proceeds silently
        self.assertTrue(q["confirmable"])
        self.assertIn("doesn't share an account email", q["flag"])
        self.assertIn("confirm it below", q["flag"])
        # the worker vouches for it
        entry = connect.confirm_by_worker(rec, "quickbooks")
        self.assertEqual(entry["verified"], "worker_confirmed")
        self.assertTrue(entry["usable"])
        self.assertIsNone(entry["flag"])
        self.assertFalse(entry["confirmable"])
        self.assertTrue(entry["confirmed_at"])

    def test_a_mismatch_or_an_unconnected_app_is_never_confirmable(self):
        # a real email that differs is a mismatch — the worker cannot wave it through
        client = StubConnectClient(identity_email="someone-else@gmail.com")
        rec = self._record("s1", expected="worker@acme.com")
        connect.bind_and_verify(client, rec, "gmail", rec["expected_identity"])
        self.assertFalse(rec["apps"]["gmail"].get("confirmable"))
        self.assertIsNone(connect.confirm_by_worker(rec, "gmail"))
        self.assertFalse(rec["apps"]["gmail"]["usable"])
        # an app that was never connected has nothing to confirm
        self.assertIsNone(connect.confirm_by_worker(rec, "quickbooks"))
        # and the missing-EXPECTED-email case is the worker's email to confirm, not this
        client = StubConnectClient(identity_email=None)
        rec = self._record("s1", expected="")
        connect.bind_and_verify(client, rec, "quickbooks", "")
        self.assertFalse(rec["apps"]["quickbooks"].get("confirmable"))
        self.assertIn("confirm your work email", rec["apps"]["quickbooks"]["flag"])

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
    """In-memory vault credentials namespace. ``existing`` seeds pre-existing
    credentials; create 409s when ``create_conflicts`` is set OR when a seeded
    credential shares the display name (MA-style per-vault name uniqueness — the
    collision that triggers URL-format migration); delete removes a seeded one."""

    def __init__(self, create_conflicts=False, create_error=None, existing=None):
        self.created, self.updated, self.deleted = [], [], []
        self._conflict = create_conflicts          # raise 409 on create
        self._create_error = create_error           # raise this on every create
        self._existing = existing                   # None -> default current-URL cred

    def _seeded(self):
        if self._existing is not None:
            return self._existing
        # one pre-existing credential keyed to the gmail server URL (for the 409 path)
        from trace.agent import config
        return [{"id": "cred-existing",
                 "mcp_server_url": config.pipedream_mcp_url("trace-s1", "gmail")}]

    def create(self, vault_ref, display_name=None, auth=None):
        if self._create_error:
            raise self._create_error
        if self._conflict or any(display_name and c.get("display_name") == display_name
                                 for c in self._seeded()):
            raise RuntimeError("409 credential already exists")
        rec = {"vault_ref": vault_ref, "display_name": display_name, "auth": auth}
        self.created.append(rec)
        return {"id": f"cred-{len(self.created)}", "mcp_server_url": (auth or {}).get("mcp_server_url")}

    def update(self, cred_id, vault_id=None, auth=None, display_name=None):
        self.updated.append({"cred_id": cred_id, "vault_id": vault_id, "auth": auth,
                             "display_name": display_name})
        return {"id": cred_id}

    def delete(self, cred_id, vault_id=None):
        self.deleted.append({"cred_id": cred_id, "vault_id": vault_id})
        if self._existing is not None:
            self._existing = [c for c in self._existing if c.get("id") != cred_id]

    def list(self, vault_ref):
        return list(self._seeded())


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
        # external_user_id + app live in the URL path, never as credential keys
        self.assertNotIn("external_user_id", auth)
        self.assertNotIn("app", auth)
        self.assertEqual(auth["mcp_server_url"],
                         config.pipedream_mcp_url("trace-s1", "gmail"))
        self.assertTrue(auth["mcp_server_url"].startswith(
            f"{config.PIPEDREAM_MCP_BASE}/trace-s1/gmail?"))

    def test_409_update_rotates_token_without_immutable_url(self):
        c, vaults, _ = self._client(_FakeVaults(credentials=_FakeCreds(create_conflicts=True)))
        out = c.bind_connection("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["binding_ref"])
        upd = vaults.credentials.updated[-1]
        self.assertEqual(upd["cred_id"], "cred-existing")            # resolved from the list
        self.assertEqual(upd["vault_id"], "vlt-s1")
        self.assertEqual(upd["auth"]["type"], "mcp_oauth")
        self.assertEqual(upd["auth"]["access_token"], "pdtok_abc")   # token rotated
        self.assertNotIn("mcp_server_url", upd["auth"])              # immutable: never on update

    def test_non_409_error_propagates(self):
        c, vaults, _ = self._client(
            _FakeVaults(credentials=_FakeCreds(create_error=RuntimeError("500 server error"))))
        with self.assertRaises(Exception):
            c.bind_connection("vlt-s1", "trace-s1", "gmail")

    # --- URL-format migration (the silent-failure fix) ----------------------
    _OLD_URL = ("https://remote.mcp.pipedream.net/v3?projectId=proj_test"
                "&environment=production&externalUserId=trace-s1&app=gmail")

    def _old_format_cred(self):
        return [{"id": "cred-old", "display_name": "Trace gmail",
                 "mcp_server_url": self._OLD_URL}]

    def test_url_migration_replaces_old_format_credential(self):
        # A name-colliding credential keyed to a PREVIOUS URL format must be replaced,
        # not left to shadow the bind: the old behavior raised (no new-URL credential
        # matched) and the caller swallowed it -> agents deployed with no run-time bearer.
        creds = _FakeCreds(existing=self._old_format_cred())
        c, _, config = self._client(_FakeVaults(credentials=creds))
        out = c.bind_connection("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["binding_ref"])
        self.assertEqual(creds.deleted[-1]["cred_id"], "cred-old")   # stale cred removed
        rec = creds.created[-1]
        self.assertEqual(rec["display_name"], "Trace gmail")         # name survives migration
        self.assertEqual(rec["auth"]["mcp_server_url"],
                         config.pipedream_mcp_url("trace-s1", "gmail"))

    def test_url_migration_without_sdk_delete_disambiguates_name(self):
        # BETA SDK without credentials.delete: the new URL still gets its own credential,
        # created under a disambiguated display name (run-time auth over vault tidiness).
        creds = _FakeCreds(existing=self._old_format_cred())
        creds.delete = None                                          # no delete on this SDK
        c, _, config = self._client(_FakeVaults(credentials=creds))
        out = c.bind_connection("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["binding_ref"])
        rec = creds.created[-1]
        self.assertEqual(rec["display_name"], "Trace gmail (per-user)")
        self.assertEqual(rec["auth"]["mcp_server_url"],
                         config.pipedream_mcp_url("trace-s1", "gmail"))

    def test_verify_bindings_reports_match_and_miss(self):
        creds = _FakeCreds(existing=self._old_format_cred())
        c, _, config = self._client(_FakeVaults(credentials=creds))
        new_url = config.pipedream_mcp_url("trace-s1", "gmail")
        out = c.verify_bindings("vlt-s1", [new_url, self._OLD_URL])
        self.assertEqual(out, {new_url: False, self._OLD_URL: True})

    def test_verify_bindings_listing_failure_raises(self):
        class _BoomCreds(_FakeCreds):
            def list(self, vault_ref):
                raise RuntimeError("400 bad request")
        c, _, _ = self._client(_FakeVaults(credentials=_BoomCreds()))
        with self.assertRaises(Exception):                       # caller decides blocking
            c.verify_bindings("vlt-s1", ["u"])

    def test_bind_returns_token_expiry(self):
        c, _, _ = self._client()
        out = c.bind_connection("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["expires_at"])                          # TTL surfaced for refresh (WS5)

    def test_refresh_binding_rotates_existing_token(self):
        # WS5: refresh re-mints + rotates the token into the existing credential, never
        # re-sending the immutable mcp_server_url, and creates nothing new.
        c, vaults, _ = self._client()                               # list serves cred-existing
        out = c.refresh_binding("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["expires_at"])
        upd = vaults.credentials.updated[-1]
        self.assertEqual(upd["cred_id"], "cred-existing")
        self.assertEqual(upd["auth"]["type"], "mcp_oauth")
        self.assertEqual(upd["auth"]["access_token"], "pdtok_abc")  # rotated token
        self.assertNotIn("mcp_server_url", upd["auth"])             # immutable: never on rotation
        self.assertEqual(vaults.credentials.created, [])           # rotation only, no new cred

    def test_refresh_vault_rotates_every_credential_from_vault_ref_only(self):
        # Spec fix: the scheduler refreshes using only vault_ref (no app slug / no
        # connection.json), rotating a freshly minted token into each credential.
        creds = _FakeCreds()
        c, vaults, _ = self._client(_FakeVaults(credentials=creds))
        out = c.refresh_vault("vlt-s1")
        self.assertEqual(out["refreshed"], 1)
        upd = creds.updated[-1]
        self.assertEqual(upd["auth"]["access_token"], "pdtok_abc")   # fresh token
        self.assertIn("expires_at", upd["auth"])
        self.assertNotIn("mcp_server_url", upd["auth"])              # immutable key never re-sent
        self.assertEqual(creds.created, [])                          # rotation only, no new cred

    def test_refresh_binding_falls_back_to_bind_when_unbound(self):
        creds = _FakeCreds()
        creds.list = lambda vault_ref: []                          # nothing bound yet
        c, _, _ = self._client(_FakeVaults(credentials=creds))
        out = c.refresh_binding("vlt-s1", "trace-s1", "gmail")
        self.assertTrue(out["expires_at"])
        self.assertEqual(len(creds.created), 1)                    # full bind created the cred
        self.assertEqual(creds.updated, [])


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
        # ONE stub for the whole flow (a fresh instance per call would forget that the
        # worker just authorized), starting with nothing authorized so the first connect
        # takes the real OAuth path.
        self.stub = StubConnectClient(identity_email=self.identity, authorized=())
        self.state = app.AppState(self.root, key_store=self.ks,
                                  connect_fn=lambda: self.stub)
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

    def test_no_identity_app_is_confirmed_over_http(self):
        """The QuickBooks case end to end: verify leaves the app awaiting the worker's
        confirmation, the confirm endpoint makes it usable, and a second confirm (or a
        confirm on an app that never needed one) is a 409, not a silent yes."""
        self.req("POST", f"/api/sessions/{self.sid}/connection-identity", {"email": "worker@acme.com"})
        self.stub.identity_email = None                    # the provider exposes no email
        self.stub.authorized.add("gmail")
        self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/callback", {})
        self.assertEqual(self._await_connect()["state"], "done")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/connection-data")
        g = data["record"]["apps"]["gmail"]
        self.assertEqual(g["verified"], "unverified")
        self.assertFalse(g["usable"])
        self.assertTrue(g["confirmable"])
        code, body = self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/confirm", {})
        self.assertEqual(code, 200)
        self.assertEqual(body["verified"], "worker_confirmed")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/connection-data")
        g = data["record"]["apps"]["gmail"]
        self.assertTrue(g["usable"])
        self.assertFalse(g["confirmable"])
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/confirm", {})
        self.assertEqual(code, 409)                        # nothing left to confirm
        code, _ = self.req("POST", f"/api/sessions/{self.sid}/connect/google_sheets/confirm", {})
        self.assertEqual(code, 409)                        # never connected

    def test_mismatch_is_flagged_not_usable(self):
        self.req("POST", f"/api/sessions/{self.sid}/connection-identity", {"email": "worker@acme.com"})
        # The worker DID authorize, but with a different Google account than the session
        # expects — the case the flag exists to catch.
        self.stub.identity_email = "intruder@gmail.com"
        self.stub.authorized.add("gmail")
        self.req("POST", f"/api/sessions/{self.sid}/connect/gmail/callback", {})
        self.assertEqual(self._await_connect()["state"], "done")
        _, data = self.req("GET", f"/api/sessions/{self.sid}/connection-data")
        g = data["record"]["apps"]["gmail"]
        self.assertEqual(g["verified"], "no")
        self.assertFalse(g["usable"])
        self.assertTrue(g["flag"])


class BroaderAppTableTest(unittest.TestCase):
    """The connect table was ten Google-and-friends entries, so a Shopify capture
    reached the connect step with nothing to connect — and said nothing about why.
    """

    def _shopify_event(self):
        # Shaped like the real capture: the browser in `app`, the service only in
        # the window title.
        return {"id": "e001", "app": "Safari",
                "window": "My Store · Products · Add product · Shopify",
                "detail": "entered the product title and price, set Status to Active"}

    def test_shopify_is_found_in_a_browser_window_title(self):
        apps, unmapped = connect.app_union(
            [{"id": "B1", "name": "Add products", "evidence_event_ids": ["e001"]}],
            [self._shopify_event()])
        self.assertIn("shopify", [a["slug"] for a in apps])
        self.assertEqual(unmapped, [])

    def test_a_domain_is_enough_on_its_own(self):
        """Now that Accessibility capture reads AXURL, the address is a stronger
        signal than a window title anyone can rename."""
        ev = {"id": "e001", "app": "Safari", "window": "",
              "detail": "opened https://admin.shopify.com/store/90fb08-32/products/new"}
        apps, _ = connect.app_union(
            [{"id": "B1", "name": "x", "evidence_event_ids": ["e001"]}], [ev])
        self.assertEqual([a["slug"] for a in apps], ["shopify"])

    def test_an_unknown_service_is_named_not_its_browser(self):
        """Reporting 'Safari' was true, useless, and pointed at the wrong thing."""
        ev = {"id": "e001", "app": "Safari",
              "window": "Widgets Inc — Fulfilment Console", "detail": ""}
        apps, unmapped = connect.app_union(
            [{"id": "B1", "name": "x", "evidence_event_ids": ["e001"]}], [ev])
        self.assertEqual(apps, [])
        self.assertEqual(unmapped, ["Widgets Inc — Fulfilment Console"])
        self.assertNotIn("Safari", unmapped)

    def test_a_native_app_still_reports_its_own_name(self):
        ev = {"id": "e001", "app": "Riverbend ERP", "window": "Orders", "detail": ""}
        _, unmapped = connect.app_union(
            [{"id": "B1", "name": "x", "evidence_event_ids": ["e001"]}], [ev])
        self.assertEqual(unmapped, ["Riverbend ERP"])

    def test_specific_keys_still_beat_general_ones(self):
        """Substring matching means order is load-bearing: 'docs' must not swallow
        'google docs', and a domain must not be shadowed by a bare word."""
        self.assertEqual(connect._slug_for("Google Docs")["slug"], "google_docs")
        self.assertEqual(connect._slug_for("sheets.google.com")["slug"], "google_sheets")
        self.assertEqual(connect._slug_for("admin.shopify.com")["slug"], "shopify")

    def test_every_entry_is_well_formed(self):
        """A malformed row fails at connect time in front of the worker."""
        for observed, entry in config.CONNECT_APP_TABLE.items():
            self.assertEqual(observed, observed.lower(),
                             f"{observed!r} must be lowercase to match")
            self.assertTrue(entry.get("slug"), f"{observed!r} has no slug")
            self.assertTrue(entry.get("label"), f"{observed!r} has no label")


class VerifyAppsTest(unittest.TestCase):
    """The slugs are ours to guess and Pipedream's to define. verify_apps turns a
    guess into a checkable fact BEFORE a worker hits it."""

    def test_a_slug_missing_from_the_catalogue_is_reported(self):
        unknown, known = verify_apps.check([{"slug": "shopify", "name": "Shopify"}])
        self.assertIn("shopify", known)
        self.assertTrue(unknown, "everything else should be unconfirmed here")

    def test_the_report_says_what_would_break(self):
        unknown, _ = verify_apps.check([])
        self.assertIn("shopify", unknown)
        self.assertIn("admin.shopify.com", unknown["shopify"]["observed"])

    def test_a_full_catalogue_confirms_everything(self):
        catalogue = [{"slug": e["slug"], "name": e["label"]}
                     for e in config.CONNECT_APP_TABLE.values()]
        unknown, known = verify_apps.check(catalogue)
        self.assertEqual(unknown, {})
        self.assertTrue(known)


if __name__ == "__main__":
    unittest.main()
