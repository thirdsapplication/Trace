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
        u = users.create_user("Worker@Acme.com", "hunter2hunter")
        self.assertEqual(u["email"], "worker@acme.com")          # normalized
        self.assertNotIn("password", u)                          # never returned
        self.assertNotIn("password_hash", u)
        self.assertTrue(users.verify("worker@acme.com", "hunter2hunter"))
        self.assertIsNone(users.verify("worker@acme.com", "wrong-password"))
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

    def test_login_session_lifecycle_and_key(self):
        u = users.create_user("x@y.com", "longenough1")
        token = users.start_session(u["user_id"])
        self.assertEqual(users.session_user(token), u["user_id"])
        self.assertEqual(users.external_user_id(u["user_id"]), f"trace-user-{u['user_id']}")
        users.end_session(token)
        self.assertIsNone(users.session_user(token))
        self.assertIsNone(users.session_user("bogus"))


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


if __name__ == "__main__":
    unittest.main()
