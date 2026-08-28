"""Session history must render from the database, at database speed.

Capping and parallelizing the work was not enough: a page that makes platform calls
and model calls inside the request has a floor of seconds, and this one made up to
ten of each. Everything the screen shows is already persisted — summary and derived
status in session_summaries, proposals in pending_actions, questions in
agent_decisions — so the request now reads those and the enrichment that produces
them runs on a background thread. These tests pin that split: the request path
touches NO network, and the refresh writes what the request path reads.
"""

import tempfile
import threading
import time
import unittest

from trace.ui import app, registry


class FakeActivityClient:
    def __init__(self, events=None):
        self.events = events or [{"type": "agent.message", "text": "did the work"},
                                 {"type": "session.status_idle", "text": ""}]

    def list_session_events(self, session_id):
        return list(self.events)


class ActivityViewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(sessions_root=self.root)
        self.client = FakeActivityClient()
        self.state._live_client = lambda: self.client
        self.agent = {"agent_id": "a1", "owner_user_id": "u1", "name": "Orders"}
        registry.agent_upsert(self.root, dict(self.agent))

    def tearDown(self):
        self._tmp.cleanup()

    def _runs(self, n):
        for i in range(n):
            registry.run_add(self.root, {
                "agent_id": "a1", "owner_user_id": "u1", "status": "ran",
                "summary": "", "details": [], "run_session_id": f"s{i}"})

    def test_the_request_path_makes_no_platform_or_model_calls(self):
        """The whole point. A client or summarizer touched here is a page that waits
        on the network, which is what put this screen past the browser's budget."""
        def forbidden(*a, **k):
            raise AssertionError("the activity request path must not call the network")
        self.state._live_client = forbidden
        self.state.summarize_fn = forbidden
        self._runs(6)
        out = self.state.agent_activity(self.agent)
        self.assertEqual(len(out), 6)

    def test_it_renders_the_cached_summary_and_status(self):
        self._runs(2)
        registry.summary_set(self.root, "s0", "Checked the inbox.", "completed")
        out = self.state.agent_activity(self.agent)
        row = next(r for r in out if r["run_session_id"] == "s0")
        self.assertEqual(row["summary"], "Checked the inbox.")
        self.assertEqual(row["status"], "completed",
                         "the cached status is ground truth; the run row's is its "
                         "insert-time self-report")

    def test_an_unenriched_run_still_renders(self):
        self._runs(1)
        out = self.state.agent_activity(self.agent)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "ran")          # falls back to the run row

    def test_proposals_and_decisions_come_from_the_database(self):
        agent = dict(self.agent, staged_apps=["gmail"])
        registry.agent_upsert(self.root, agent)
        self._runs(1)
        registry.summary_set(self.root, "s0", "Proposed something.", "needs_input")
        registry.pending_add(self.root, {
            "agent_id": "a1", "owner_user_id": "u1", "run_session_id": "s0",
            "app": "gmail", "kind": "send", "summary": "Email Bob",
            "payload": {"to": "bob@x.com"}, "content_hash": "h1"})
        registry.decision_add(self.root, {
            "agent_id": "a1", "owner_user_id": "u1", "run_session_id": "s0",
            "question": "Which address?", "context": {"context": "two matches",
                                                      "options": ["a", "b"]},
            "tags": {}})
        out = self.state.agent_activity(agent)
        self.assertEqual(len(out[0]["proposals"]), 1)
        self.assertEqual(out[0]["proposals"][0]["summary"], "Email Bob")
        self.assertEqual(len(out[0]["decisions"]), 1)
        self.assertEqual(out[0]["decisions"][0]["options"], ["a", "b"])

    def test_cached_data_renders_even_with_no_api_key(self):
        """Regression: the old loop bailed on a missing client BEFORE consulting the
        cache, so an agent with no key showed bare run rows though the DB had more."""
        def no_key():
            raise RuntimeError("no_api_key")
        self.state._live_client = no_key
        self._runs(1)
        registry.summary_set(self.root, "s0", "A summary.", "completed")
        out = self.state.agent_activity(self.agent)
        self.assertEqual(out[0]["summary"], "A summary.")

    def test_history_is_read_once_not_once_per_run(self):
        agent = dict(self.agent, staged_apps=["gmail"])
        registry.agent_upsert(self.root, agent)
        self._runs(6)
        real = registry.pending_for_user
        calls = {"n": 0}

        def counting(*a, **k):
            calls["n"] += 1
            return real(*a, **k)
        registry.pending_for_user = counting
        try:
            self.state.agent_activity(agent)
        finally:
            registry.pending_for_user = real
        self.assertEqual(calls["n"], 1, "must not scale with the number of runs")

    # --- the background half ------------------------------------------------
    def test_refresh_persists_status_and_summary_for_the_view_to_read(self):
        self.state.summarize_fn = lambda system, user: "Checked the inbox."
        self._runs(3)
        self.state.refresh_activity(self.agent)
        for i in range(3):
            row = registry.summary_get(self.root, f"s{i}")
            self.assertEqual(row["summary"], "Checked the inbox.")
            self.assertEqual(row["status"], "completed")
        out = self.state.agent_activity(self.agent)
        self.assertTrue(all(r["summary"] == "Checked the inbox." for r in out))

    def test_refresh_records_a_status_even_when_it_cannot_summarize(self):
        """Deployment runs are recorded as "ran" at session START and never updated,
        so without a persisted status the view would show them as running forever."""
        def boom(system, user):
            raise RuntimeError("model down")
        self.state.summarize_fn = boom
        self._runs(1)
        self.state.refresh_activity(self.agent)
        row = registry.summary_get(self.root, "s0")
        self.assertEqual(row["status"], "completed")
        self.assertFalse(row["summary"])

    def test_a_status_only_write_does_not_blank_an_existing_summary(self):
        self._runs(1)
        registry.summary_set(self.root, "s0", "Earlier summary.", "completed")
        self.state.summarize_fn = lambda system, user: ""    # nothing new to say
        self.state.refresh_activity(self.agent)
        self.assertEqual(registry.summary_get(self.root, "s0")["summary"],
                         "Earlier summary.")

    def test_only_one_refresh_per_agent_at_a_time(self):
        gate = threading.Event()

        def slow(session_id):
            gate.wait(10)
            return [{"type": "session.status_idle", "text": ""}]
        self.client.list_session_events = slow
        self.state.summarize_fn = lambda system, user: "x"
        self._runs(2)
        t = self.state.maybe_refresh_activity(self.agent)
        self.assertIsNotNone(t)
        try:
            self.assertIsNone(self.state.maybe_refresh_activity(self.agent))
        finally:
            gate.set()
            t.join(timeout=10)

    def test_gone_sessions_are_fetched_once_then_skipped(self):
        calls = {"n": 0}

        def gone(session_id):
            calls["n"] += 1
            raise RuntimeError("Error code: 404 - Session not found")
        self.client.list_session_events = gone
        self._runs(2)
        self.state.refresh_activity(self.agent)
        self.assertEqual(calls["n"], 2)
        self.state.refresh_activity(self.agent)
        self.assertEqual(calls["n"], 2, "a gone session must never be re-fetched")

    def test_needs_refresh_is_false_once_everything_is_cached(self):
        self._runs(2)
        self.assertTrue(self.state.activity_needs_refresh(self.agent))
        self.state.summarize_fn = lambda system, user: "A summary."
        self.state.refresh_activity(self.agent)
        self.assertFalse(self.state.activity_needs_refresh(self.agent))

    def test_a_failing_refresh_never_raises(self):
        def boom(session_id):
            raise RuntimeError("platform down")
        self.client.list_session_events = boom
        self._runs(2)
        self.state.refresh_activity(self.agent)            # must not raise


class LockIndependenceTest(unittest.TestCase):
    """A page load must not wait on anything that does I/O.

    `state.lock` is held ACROSS Supabase calls by the scheduler
    (_record_deployment_run) and across platform calls — minutes of them — by a
    schedule change (apply_platform_schedule). When the activity path took that
    same lock, a database-only endpoint still hung behind a 60-second scheduler
    pass, and an unchanged front end waiting on the response sat on "Loading…".
    /api/u/home never took the lock, which is exactly why it kept rendering.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(sessions_root=self.root)
        self.state._live_client = lambda: FakeActivityClient()
        self.agent = {"agent_id": "a1", "owner_user_id": "u1", "name": "Orders"}
        registry.agent_upsert(self.root, dict(self.agent))
        for i in range(3):
            registry.run_add(self.root, {
                "agent_id": "a1", "owner_user_id": "u1", "status": "ran",
                "summary": "", "details": [], "run_session_id": f"s{i}"})

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_view_renders_while_the_global_lock_is_held(self):
        held = threading.Event()
        release = threading.Event()

        def hog():
            with self.state.lock:                          # as the scheduler does
                held.set()
                release.wait(10)

        t = threading.Thread(target=hog, daemon=True)
        t.start()
        held.wait(5)
        try:
            t0 = time.monotonic()
            out = self.state.agent_activity(self.agent)
            self.assertEqual(len(out), 3)
            self.assertLess(time.monotonic() - t0, 2.0,
                            "the activity view waited on the global lock")
        finally:
            release.set()
            t.join(timeout=5)

    def test_the_refresh_trigger_does_not_take_the_global_lock(self):
        held = threading.Event()
        release = threading.Event()

        def hog():
            with self.state.lock:
                held.set()
                release.wait(10)

        t = threading.Thread(target=hog, daemon=True)
        t.start()
        held.wait(5)
        try:
            t0 = time.monotonic()
            self.state.activity_needs_refresh(self.agent)
            worker = self.state.maybe_refresh_activity(self.agent)
            self.assertLess(time.monotonic() - t0, 2.0,
                            "spawning the refresh waited on the global lock")
            if worker:
                worker.join(timeout=10)
        finally:
            release.set()
            t.join(timeout=5)

    def test_refreshes_are_capped_fleet_wide(self):
        gate = threading.Event()

        def slow(session_id):
            gate.wait(10)
            return [{"type": "session.status_idle", "text": ""}]
        client = FakeActivityClient()
        client.list_session_events = slow
        self.state._live_client = lambda: client
        started = []
        try:
            for i in range(app.ACTIVITY_MAX_CONCURRENT + 2):
                aid = f"cap{i}"
                registry.agent_upsert(self.root, {"agent_id": aid, "owner_user_id": "u1",
                                                  "name": aid})
                registry.run_add(self.root, {
                    "agent_id": aid, "owner_user_id": "u1", "status": "ran",
                    "summary": "", "details": [], "run_session_id": f"{aid}-s"})
                t = self.state.maybe_refresh_activity({"agent_id": aid,
                                                       "owner_user_id": "u1", "name": aid})
                if t:
                    started.append(t)
            self.assertEqual(len(started), app.ACTIVITY_MAX_CONCURRENT,
                             "each refresh spawns threads and dozens of queries; the "
                             "fleet needs a ceiling, not just a per-agent guard")
        finally:
            gate.set()
            for t in started:
                t.join(timeout=10)


class ProfileCacheTest(unittest.TestCase):
    """_current_user resolves the profile on EVERY authenticated request; it was an
    uncached round trip sitting in front of every page in the app."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from trace.ui import users
        self.users = users
        users._profile_evict()

    def tearDown(self):
        self.users._profile_evict()
        self._tmp.cleanup()

    def test_a_repeat_lookup_is_served_from_cache(self):
        users = self.users
        calls = {"n": 0}
        real = users._load

        def counting():
            calls["n"] += 1
            return real()
        users._load = counting
        try:
            uid = "u-cache-1"
            store = {uid: {"user_id": uid, "email": "c@x.com", "created_at": "t"}}
            users._load = lambda: (calls.__setitem__("n", calls["n"] + 1) or store)
            first = users.get(uid)
            self.assertEqual(first["email"], "c@x.com")
            hits = calls["n"]
            self.assertEqual(users.get(uid)["email"], "c@x.com")
            self.assertEqual(calls["n"], hits, "the second lookup must not hit storage")
        finally:
            users._load = real

    def test_eviction_forces_a_fresh_read(self):
        users = self.users
        real = users._load
        store = {"u-cache-2": {"user_id": "u-cache-2", "email": "one@x.com",
                               "created_at": "t"}}
        users._load = lambda: store
        try:
            self.assertEqual(users.get("u-cache-2")["email"], "one@x.com")
            store["u-cache-2"]["email"] = "two@x.com"
            self.assertEqual(users.get("u-cache-2")["email"], "one@x.com")  # cached
            users._profile_evict("u-cache-2")
            self.assertEqual(users.get("u-cache-2")["email"], "two@x.com")
        finally:
            users._load = real


if __name__ == "__main__":
    unittest.main()
