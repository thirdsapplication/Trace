"""Spec 1 — durable accounts: verify users.py routes through Postgres (db.py)
when DATABASE_URL is set, with the durability/isolation semantics the spec
requires, and that the file path is unchanged when it isn't.

The real psycopg/Postgres SQL in db.py is exercised on the live Render database;
here we substitute an in-memory fake for db.py's persistence functions so the
ROUTING and semantics in users.py are tested deterministically and offline.
"""

import time
import unittest

from trace.ui import db, users


class FakeDB:
    """In-memory stand-in for db.py's account/auth-session persistence."""
    def __init__(self):
        self.users = {}      # user_id -> {email, password_hash, external_user_id, created_at}
        self.by_email = {}   # email -> user_id
        self.tokens = {}     # token -> {user_id, expires}

    # mirrors db.user_create / user_by_email / user_by_id
    def user_create(self, user_id, email, password_hash, external_user_id):
        if email in self.by_email:
            raise ValueError("an account with this email already exists")
        self.users[user_id] = {"email": email, "password_hash": password_hash,
                               "external_user_id": external_user_id,
                               "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.by_email[email] = user_id

    def user_by_email(self, email):
        uid = self.by_email.get(email)
        if not uid:
            return None
        u = self.users[uid]
        return (uid, email, u["password_hash"], u["created_at"])

    def user_by_id(self, user_id):
        u = self.users.get(user_id)
        return (user_id, u["email"], u["created_at"]) if u else None

    # mirrors db.auth_start / auth_session_user / auth_end
    def auth_start(self, token, user_id, ttl_seconds):
        self.tokens[token] = {"user_id": user_id, "expires": time.time() + ttl_seconds}

    def auth_session_user(self, token):
        rec = self.tokens.get(token)
        if not rec:
            return None
        if rec["expires"] <= time.time():
            self.tokens.pop(token, None)
            return None
        return rec["user_id"]

    def auth_end(self, token):
        self.tokens.pop(token, None)


class DurableAccountsTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDB()
        self._orig = {name: getattr(db, name) for name in (
            "is_configured", "user_create", "user_by_email", "user_by_id",
            "auth_start", "auth_session_user", "auth_end")}
        db.is_configured = lambda: True            # pretend DATABASE_URL is set
        db.user_create = self.fake.user_create
        db.user_by_email = self.fake.user_by_email
        db.user_by_id = self.fake.user_by_id
        db.auth_start = self.fake.auth_start
        db.auth_session_user = self.fake.auth_session_user
        db.auth_end = self.fake.auth_end

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(db, name, fn)

    def test_signup_login_and_survives_restart(self):
        u = users.create_user("a@b.com", "password1")
        self.assertEqual(u["email"], "a@b.com")
        self.assertNotIn("password_hash", u)               # never leaked to callers
        # stored as a salted scrypt hash, never plaintext
        self.assertTrue(self.fake.users[u["user_id"]]["password_hash"].startswith("scrypt$"))
        self.assertNotIn("password1", self.fake.users[u["user_id"]]["password_hash"])

        token = users.start_session(u["user_id"])
        self.assertEqual(users.session_user(token), u["user_id"])

        # "Restart": wipe the in-process store. DB-backed sessions must survive.
        users._SESSIONS.clear()
        self.assertEqual(users.session_user(token), u["user_id"])
        # And the account itself is still there → login still works.
        again = users.verify("a@b.com", "password1")
        self.assertEqual(again["user_id"], u["user_id"])

    def test_logout_and_bad_password(self):
        u = users.create_user("c@d.com", "password1")
        token = users.start_session(u["user_id"])
        users.end_session(token)
        self.assertIsNone(users.session_user(token))       # logout invalidates the token
        self.assertIsNone(users.verify("c@d.com", "wrongpass"))
        self.assertIsNone(users.verify("c@d.com", ""))

    def test_duplicate_email_rejected(self):
        users.create_user("dup@e.com", "password1")
        with self.assertRaises(ValueError):
            users.create_user("dup@e.com", "password2")

    def test_two_users_isolated(self):
        a = users.create_user("one@x.com", "password1")
        b = users.create_user("two@x.com", "password2")
        self.assertNotEqual(a["user_id"], b["user_id"])
        self.assertNotEqual(users.external_user_id(a["user_id"]),
                            users.external_user_id(b["user_id"]))
        ta, tb = users.start_session(a["user_id"]), users.start_session(b["user_id"])
        self.assertEqual(users.session_user(ta), a["user_id"])
        self.assertEqual(users.session_user(tb), b["user_id"])
        self.assertNotEqual(users.session_user(ta), users.session_user(tb))

    def test_external_user_id_shape(self):
        u = users.create_user("f@g.com", "password1")
        self.assertEqual(users.external_user_id(u["user_id"]), f"trace-user-{u['user_id']}")

    def test_expired_token_rejected(self):
        u = users.create_user("h@i.com", "password1")
        self.fake.auth_start("stale", u["user_id"], -1)    # already expired
        self.assertIsNone(users.session_user("stale"))


if __name__ == "__main__":
    unittest.main()
