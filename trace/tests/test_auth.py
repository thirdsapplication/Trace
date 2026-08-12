"""WS3 — end-user accounts, login sessions, and per-user isolation.

Covers the users module (salted scrypt hashing, create/verify/duplicate, login-session
tokens, the external_user_id key) and the HTTP surface (signup/login/logout, auth-gated
/api/u/*, and the hard isolation guarantee: one user can neither read nor act on another
user's sessions). The founder's Test UI is untouched and needs no login.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from trace.ui import app, keystore, sessions, users


class UsersModuleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (users._DIR, users._PATH, dict(users._SESSIONS))
        users._DIR = self._tmp.name
        users._PATH = os.path.join(self._tmp.name, "users.json")
        users._SESSIONS.clear()

    def tearDown(self):
        users._DIR, users._PATH, sess = self._saved
        users._SESSIONS.clear()
        users._SESSIONS.update(sess)
        self._tmp.cleanup()

    def test_create_verify_and_hashing(self):
        u, token = users.create_user("Worker@Acme.com", "hunter2hunter")   # (user, token) tuple
        self.assertEqual(u["email"], "worker@acme.com")          # normalized
        self.assertTrue(token)                                   # cookie token issued
        self.assertNotIn("password", u)                          # never returned
        self.assertNotIn("password_hash", u)
        self.assertIsNotNone(users.verify("worker@acme.com", "hunter2hunter")[0])
        self.assertIsNone(users.verify("worker@acme.com", "wrong-password")[0])
        # password is hashed+salted on disk, never plaintext
        with open(users._PATH) as f:
            raw = f.read()
        self.assertNotIn("hunter2hunter", raw)
        self.assertIn("scrypt$", raw)

    def test_duplicate_and_weak_rejected(self):
        users.create_user("a@b.com", "longenough1")
        with self.assertRaises(ValueError):
            users.create_user("A@B.com", "anotherlongpw")        # duplicate (case-insensitive)
        with self.assertRaises(ValueError):
            users.create_user("c@d.com", "short")                # too short
        with self.assertRaises(ValueError):
            users.create_user("not-an-email", "longenough1")

    def test_list_users_public_shape_only(self):
        users.create_user("a@b.com", "longenough1")
        users.create_user("c@d.com", "longenough1")
        out = users.list_users()
        self.assertEqual({u["email"] for u in out}, {"a@b.com", "c@d.com"})
        for u in out:
            self.assertEqual(u["external_user_id"], f"trace-user-{u['user_id']}")
            self.assertNotIn("password_hash", u)               # never password material

    def test_login_session_lifecycle_and_key(self):
        u, _ = users.create_user("x@y.com", "longenough1")       # (user, token) tuple
        token = users.start_session(u["user_id"])
        self.assertEqual(users.session_user(token), u["user_id"])
        self.assertEqual(users.external_user_id(u["user_id"]), f"trace-user-{u['user_id']}")
        users.end_session(token)
        self.assertIsNone(users.session_user(token))
        self.assertIsNone(users.session_user("bogus"))


class SupabaseSessionUserTest(unittest.TestCase):
    """Supabase-mode JWT validation: cached within the TTL (one round trip, not
    one per request), evicted on logout, and bounded when the auth service hangs
    (fails closed instead of hanging the request)."""

    class _FakeAuth:
        def __init__(self):
            self.calls = 0
            self.delay = 0.0

        def get_user(self, token):
            self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            user = type("U", (), {"id": "uid-1"})()
            return type("R", (), {"user": user})()

    def setUp(self):
        self.auth = self._FakeAuth()
        client = type("C", (), {"auth": self.auth})()
        self._saved = (users.supa.is_configured, users.supa.get_client,
                       users._JWT_TIMEOUT_S)
        users.supa.is_configured = lambda: True
        users.supa.get_client = lambda: client
        users._JWT_CACHE.clear()

    def tearDown(self):
        users.supa.is_configured, users.supa.get_client, users._JWT_TIMEOUT_S = self._saved
        users._JWT_CACHE.clear()

    def test_validation_is_cached_within_ttl(self):
        self.assertEqual(users.session_user("jwt-a"), "uid-1")
        self.assertEqual(users.session_user("jwt-a"), "uid-1")
        self.assertEqual(self.auth.calls, 1)                     # second hit served from cache

    def test_logout_evicts_immediately(self):
        users.session_user("jwt-b")
        users.end_session("jwt-b")                               # get_admin raises; swallowed
        users.session_user("jwt-b")
        self.assertEqual(self.auth.calls, 2)                     # re-validated after logout

    def test_hung_auth_service_fails_closed_quickly(self):
        users._JWT_TIMEOUT_S = 0.1
        self.auth.delay = 1.0
        t0 = time.time()
        self.assertIsNone(users.session_user("jwt-c"))
        self.assertLess(time.time() - t0, 0.9)                   # bounded, not the full delay


class _AuthHTTPBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self._saved_ks = (keystore._FALLBACK_DIR, keystore._FALLBACK_PATH, keystore._keyring)
        keystore._FALLBACK_DIR = d
        keystore._FALLBACK_PATH = os.path.join(d, "ks.json")
        keystore._keyring = lambda: None
        self._saved_users = (users._DIR, users._PATH, dict(users._SESSIONS))
        users._DIR = d
        users._PATH = os.path.join(d, "users.json")
        users._SESSIONS.clear()
        self.root = os.path.join(d, "sessions")
        self.state = app.AppState(self.root, key_store=keystore)
        self.httpd, _ = app.build_server(port=0, state=self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        keystore._FALLBACK_DIR, keystore._FALLBACK_PATH, keystore._keyring = self._saved_ks
        users._DIR, users._PATH, sess = self._saved_users
        users._SESSIONS.clear(); users._SESSIONS.update(sess)
        self._tmp.cleanup()

    def req(self, method, path, body=None, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(r)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), None
        set_cookie = resp.headers.get("Set-Cookie")
        return resp.status, json.loads(resp.read() or b"null"), set_cookie

    def _cookie_from(self, set_cookie):
        return set_cookie.split(";", 1)[0] if set_cookie else None

    def signup(self, email, pw):
        code, data, sc = self.req("POST", "/api/auth/signup", {"email": email, "password": pw})
        self.assertEqual(code, 200, data)
        return self._cookie_from(sc), data["user"]


class AuthRouteTest(_AuthHTTPBase):
    def test_signup_login_logout(self):
        cookie, user = self.signup("worker@acme.com", "longenough1")
        self.assertTrue(cookie and cookie.startswith(users.COOKIE_NAME + "="))
        # the cookie authenticates /api/auth/me
        code, data, _ = self.req("GET", "/api/auth/me", cookie=cookie)
        self.assertEqual(data["user"]["email"], "worker@acme.com")
        # logout clears the session
        self.req("POST", "/api/auth/logout", {}, cookie=cookie)
        code, data, _ = self.req("GET", "/api/auth/me", cookie=cookie)
        self.assertIsNone(data["user"])
        # re-login works
        code, data, sc = self.req("POST", "/api/auth/login",
                                  {"email": "worker@acme.com", "password": "longenough1"})
        self.assertEqual(code, 200)
        self.assertTrue(self._cookie_from(sc))

    def test_bad_login_rejected(self):
        self.signup("a@b.com", "longenough1")
        code, data, _ = self.req("POST", "/api/auth/login",
                                 {"email": "a@b.com", "password": "nope"})
        self.assertEqual(code, 401)

    def test_unauthenticated_user_api_rejected(self):
        for method, path in (("GET", "/api/u/sessions"), ("POST", "/api/u/load-fixture")):
            code, data, _ = self.req(method, path, {} if method == "POST" else None)
            self.assertEqual(code, 401)
            self.assertEqual(data["error"], "auth_required")


class IsolationTest(_AuthHTTPBase):
    def _own_session(self, cookie):
        code, data, _ = self.req("POST", "/api/u/load-fixture", {"name": "marcy"}, cookie=cookie)
        self.assertEqual(code, 200, data)
        return data["session_id"]

    def test_users_cannot_touch_each_others_sessions(self):
        ca, ua = self.signup("a@x.com", "longenough1")
        cb, ub = self.signup("b@x.com", "longenough1")
        sid_a = self._own_session(ca)

        # A sees the session in their scoped list; B does not
        _, da, _ = self.req("GET", "/api/u/sessions", cookie=ca)
        self.assertIn(sid_a, [s["id"] for s in da["sessions"]])
        _, db, _ = self.req("GET", "/api/u/sessions", cookie=cb)
        self.assertEqual(db["sessions"], [])

        # B cannot READ A's session (404, no existence leak)
        code, _, _ = self.req("GET", f"/api/u/sessions/{sid_a}/event_log", cookie=cb)
        self.assertEqual(code, 404)
        # B cannot ACT on A's session
        code, _, _ = self.req("POST", f"/api/u/sessions/{sid_a}/discover", {}, cookie=cb)
        self.assertEqual(code, 404)
        # A can act on their own session
        code, _, _ = self.req("GET", f"/api/u/sessions/{sid_a}/event_log", cookie=ca)
        self.assertEqual(code, 200)

    def test_owned_session_uses_per_user_external_id(self):
        # WS3: an end-user session's connection key is the per-user namespace, not per-session
        ca, ua = self.signup("a@x.com", "longenough1")
        sid_a = self._own_session(ca)
        self.assertEqual(self.state._external_user_id(sid_a),
                         users.external_user_id(ua["user_id"]))
        # a Test-UI (unowned) session keeps the per-session key
        plain = sessions.create_session(self.root)
        self.assertEqual(self.state._external_user_id(plain), f"trace-{plain}")


class EndUserUITest(_AuthHTTPBase):
    """WS2 — the end-user UI is served separately at /app, carries the Palza
    tokens, leaks no platform credentials/IDs/paths, and uses the scoped API."""

    def asset(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        r = urllib.request.urlopen(url)
        return r.status, r.read().decode("utf-8")

    def test_served_separately_with_palza_brand(self):
        code, html = self.asset("/app")
        self.assertEqual(code, 200)
        self.assertIn('id="root"', html)
        self.assertIn("/app/app.js", html)
        code, css = self.asset("/app/app.css")
        self.assertIn("#315EF6", css)                # Core Blue — action token
        self.assertIn("#FF6655", css)                # Action Coral — "needs you" only
        self.assertIn("Manrope", css)                # headline font
        self.assertIn("#121A2F", css)                # ink
        # the founder Test UI is still at / and unchanged
        code, root_html = self.asset("/")
        self.assertEqual(code, 200)
        self.assertIn("Engine 1 Test Harness", root_html)

    def test_signup_routes_through_the_first_run_check(self):
        """A brand-new account must land on Welcome (04), not the wizard.

        Welcome and the terms gate were built and unreachable: the sign-in form called
        loadAndRender() directly, so the first-run check only ever ran on boot() — i.e.
        you had to reload the page to see either screen. Nothing in this suite renders
        app.js, so that whole class of bug (a screen that exists but nothing routes to)
        is invisible unless asserted at the source level, which is what this does.
        """
        _, js = self.asset("/app/app.js")
        self.assertIn("async function routeSignedIn()", js)
        self.assertIn("renderWelcome()", js)            # the router can still reach 04
        # the auth form hands off to the router rather than jumping into the wizard
        go = js[js.index("const go = async () => {"):]
        go = go[:go.index('$("#authGo").onclick')]
        self.assertIn("routeSignedIn()", go)
        self.assertNotIn("loadAndRender()", go)

    def test_spa_fallback_for_unknown_subpath(self):
        code, html = self.asset("/app/anything/here")
        self.assertEqual(code, 200)
        self.assertIn('id="root"', html)             # falls back to the SPA shell

    def test_no_credentials_or_jargon_in_assets(self):
        blob = "".join(self.asset(p)[1] for p in ("/app", "/app/app.css", "/app/app.js"))
        low = blob.lower()
        for forbidden in ("sk-ant", "client_secret", "pipedream_client", "test harness",
                          "test mode", "external_user_id"):
            self.assertNotIn(forbidden, low)

    def test_scoped_api_responses_carry_no_secrets(self):
        cookie, _ = self.signup("a@x.com", "longenough1")
        for path in ("/api/u/me", "/api/u/sessions"):
            _, data, _ = self.req("GET", path, cookie=cookie)
            self.assertNotIn("sk-ant", json.dumps(data))
            self.assertNotIn("client_secret", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
