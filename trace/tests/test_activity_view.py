"""Session history must load fast and must never leave the page spinning.

The activity view is the only user-facing GET that makes platform AND model calls,
and it made up to `limit` of each with no ceiling: the summaries ran one after
another inside the run loop. A healthy page was already 30-80s against a browser
that aborts at 65, and the abort surfaced as a permanent "Loading…" because neither
side caught it. These tests pin the bounds that keep the response quick — parallel
summaries, a per-request cap, a deadline, and one query instead of one-per-run.
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

    def test_summaries_run_concurrently_not_one_after_another(self):
        """The whole cost of the page: 4 sequential model calls at 0.2s each is 0.8s;
        fanned out it is ~0.2s. Sequential summarizing is what blew the budget."""
        threads = set()

        def slow(system, user):
            threads.add(threading.current_thread().name)
            time.sleep(0.2)
            return "A summary."
        self.state.summarize_fn = slow
        self._runs(4)
        t0 = time.monotonic()
        out = self.state.agent_activity(self.agent)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(out), 4)
        self.assertGreater(len(threads), 1, "summaries still ran on one thread")
        self.assertLess(elapsed, 0.7, "summaries did not overlap")

    def test_at_most_the_budget_of_summaries_per_request(self):
        calls = {"n": 0}

        def counted(system, user):
            calls["n"] += 1
            return "A summary."
        self.state.summarize_fn = counted
        self._runs(app.ACTIVITY_SUMMARY_BUDGET + 3)
        out = self.state.agent_activity(self.agent, limit=app.ACTIVITY_SUMMARY_BUDGET + 3)
        self.assertEqual(calls["n"], app.ACTIVITY_SUMMARY_BUDGET)
        self.assertEqual(len(out), app.ACTIVITY_SUMMARY_BUDGET + 3,
                         "every run must still render, summarized or not")

    def test_a_passed_deadline_skips_summarizing_but_still_answers(self):
        calls = {"n": 0}

        def counted(system, user):
            calls["n"] += 1
            return "A summary."
        self.state.summarize_fn = counted
        self._runs(3)
        original = app.ACTIVITY_DEADLINE_SECONDS
        app.ACTIVITY_DEADLINE_SECONDS = -1                 # already out of time
        try:
            out = self.state.agent_activity(self.agent)
        finally:
            app.ACTIVITY_DEADLINE_SECONDS = original
        self.assertEqual(calls["n"], 0, "no model call once the deadline has passed")
        self.assertEqual(len(out), 3, "the page still renders its runs")

    def test_pending_history_is_read_once_not_once_per_run(self):
        """This query pulls the user's WHOLE pending history and filters in Python;
        running it per run made the page cost grow with both run count and history."""
        agent = dict(self.agent, staged_apps=["gmail"])
        registry.agent_upsert(self.root, agent)
        self.state.summarize_fn = lambda system, user: "A summary."
        self._runs(5)
        real = registry.pending_for_user
        calls = {"n": 0}

        def counting(*a, **k):
            calls["n"] += 1
            return real(*a, **k)
        registry.pending_for_user = counting
        try:
            self.state.agent_activity(agent)               # first load: stages proposals
            first = calls["n"]
            calls["n"] = 0
            self.state.agent_activity(agent)               # reload: nothing new to stage
        finally:
            registry.pending_for_user = real
        # One prefetch, plus one refresh for the single genuinely-new proposal — the
        # count must not scale with the number of runs (it used to be one per run).
        self.assertLessEqual(first, 2)
        self.assertEqual(calls["n"], 1,
                         "a reload stages nothing, so it must cost exactly one read")

    def test_a_failing_summarizer_never_breaks_the_response(self):
        def boom(system, user):
            raise RuntimeError("model down")
        self.state.summarize_fn = boom
        self._runs(3)
        out = self.state.agent_activity(self.agent)
        self.assertEqual(len(out), 3)

    def test_a_failed_event_fetch_still_renders_the_run(self):
        def boom(session_id):
            raise RuntimeError("Error code: 404 - Session not found")
        self.client.list_session_events = boom
        self._runs(2)
        out = self.state.agent_activity(self.agent)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
