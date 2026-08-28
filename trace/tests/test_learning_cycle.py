"""Learning cycles must actually fire, and only edit what they may.

The trigger lives on the two run-recording choke points (run_agent and the
deployment sync pass); the worker fetches the batch's real session events, makes
ONE Fable call, and writes ONLY /lessons_learned.md back to the agent's memory
store. These tests pin the counting (5 eligible sessions since the last cycle),
the terminality gate, the retry-vs-reset distinction (a failed call records no
cycle; "nothing to change" records one), the code-side edit whitelist, and that
both choke points invoke the trigger at all.

Offline: tempdir file-mode registry, fake platform client, injected learn_fn.
"""

import json
import tempfile
import threading
import time
import unittest

from trace.agent import config as agent_config
from trace.ui import app, registry


def _terminal_events(text="did the work", usage=None):
    ev = [{"type": "user.message", "text": "Task: go"},
          {"type": "agent.message", "text": text,
           "usage": usage or {"input_tokens": 100, "output_tokens": 20}},
          {"type": "session.status_idle", "text": "", "stop_reason": "end_turn"}]
    return ev


def _running_events():
    return [{"type": "user.message", "text": "Task: go"},
            {"type": "agent.message", "text": "still working"}]


class FakeLearnClient:
    """The platform surface a learning cycle touches."""

    def __init__(self):
        self.events = {}                       # session_id -> events
        self.dead = set()                      # session_ids deleted from the platform
        self.docs = {agent_config.MEMORY_LESSONS_DOC: "# Lessons learned\n- old lesson\n"}
        self.written = []                      # {store_id, path, content}

    def list_session_events(self, session_id):
        if session_id in self.dead:
            raise RuntimeError(f"Error code: 404 - Session not found: {session_id}")
        return list(self.events.get(session_id, []))

    def read_memory(self, store_id, path):
        return self.docs.get(path)

    def write_memory(self, store_id, path, content):
        self.written.append({"store_id": store_id, "path": path, "content": content})
        return "update"


class LearningCycleTest(unittest.TestCase):
    N = agent_config.LEARNING_CYCLE_SESSIONS

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.learn_calls = []

        def learn_fn(system, user):
            self.learn_calls.append({"system": system, "user": user})
            return json.dumps({"analysis": "fewer rescans",
                               "docs": {agent_config.MEMORY_LESSONS_DOC:
                                        "# Lessons learned\n- check the ledger first\n"}})

        self.state = app.AppState(sessions_root=self.root, learn_fn=learn_fn)
        self.client = FakeLearnClient()
        self.state._live_client = lambda: self.client
        self.agent = {"agent_id": "a1", "owner_user_id": "u1", "name": "Orders",
                      "description": "processes orders", "trigger": "Task: go",
                      "memory_store_id": "mem_1"}

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_runs(self, n, kind=None, events_fn=_terminal_events, prefix="s"):
        for i in range(n):
            sid = f"{prefix}{i}"
            registry.run_add(self.root, {
                "agent_id": "a1", "owner_user_id": "u1", "status": "ran",
                "summary": "", "details": [], "run_session_id": sid,
                "drun_id": f"d_{prefix}{i}", **({"kind": kind} if kind else {})})
            self.client.events[sid] = events_fn()

    def _cycles(self):
        return registry.learning_cycles_for_agent(self.root, "a1")

    def test_no_cycle_before_n_eligible_runs(self):
        self._seed_runs(self.N - 1)
        self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent))
        self.assertEqual(self.learn_calls, [])
        self.assertEqual(self._cycles(), [])

    def test_cycle_fires_at_n_and_edits_only_the_lessons_doc(self):
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        t.join(timeout=10)
        self.assertEqual(len(self.learn_calls), 1)
        self.assertEqual([w["path"] for w in self.client.written],
                         [agent_config.MEMORY_LESSONS_DOC])
        self.assertEqual(self.client.written[0]["store_id"], "mem_1")
        cycles = self._cycles()
        self.assertEqual(len(cycles), 1)
        self.assertEqual(len(cycles[0]["session_stats"]), self.N)
        self.assertEqual(cycles[0]["analysis"], "fewer rescans")
        self.assertEqual(cycles[0]["docs_written"], [agent_config.MEMORY_LESSONS_DOC])
        self.assertFalse(cycles[0]["tokens_estimated"])
        self.assertEqual(cycles[0]["total_input_tokens"], 100 * self.N)
        self.assertEqual(cycles[0]["total_output_tokens"], 20 * self.N)

    def test_the_model_sees_costs_memory_and_the_batch(self):
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        payload = json.loads(self.learn_calls[0]["user"])
        self.assertEqual(len(payload["sessions"]), self.N)
        self.assertEqual(payload["sessions"][0]["tokens"]["total_tokens"], 120)
        self.assertIn("old lesson", payload["memory"][agent_config.MEMORY_LESSONS_DOC])
        self.assertEqual(payload["agent"]["name"], "Orders")

    def test_counter_resets_after_a_cycle(self):
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent),
                          "the same batch must not be re-analyzed after a recorded cycle")
        self.assertEqual(len(self.learn_calls), 1)

    def test_approval_runs_neither_count_nor_appear(self):
        self._seed_runs(self.N - 1)
        self._seed_runs(3, kind="approval", prefix="ap")
        self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent))
        self._seed_runs(1, prefix="x")
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        payload = json.loads(self.learn_calls[0]["user"])
        sids = {s["session_id"] for s in payload["sessions"]}
        self.assertFalse(any(s.startswith("ap") for s in sids),
                         "approval-execution sessions run a different directive and "
                         "must not shape the workflow analysis")

    def test_deleted_sessions_do_not_block_the_cycle(self):
        """Old runs whose platform sessions were cleaned up 404 forever; waiting on
        them stalled real agents in production. Gone is not running — skip them and
        analyze the sessions that still exist."""
        self._seed_runs(self.N)
        self._seed_runs(2, prefix="dead")
        self.client.dead = {"dead0", "dead1"}          # the two NEWEST runs are gone
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        payload = json.loads(self.learn_calls[0]["user"])
        sids = {s["session_id"] for s in payload["sessions"]}
        self.assertEqual(sids, {f"s{i}" for i in range(self.N)})
        self.assertEqual(len(self._cycles()), 1)

    def test_an_all_dead_batch_defers_without_a_model_call(self):
        self._seed_runs(self.N)
        self.client.dead = {f"s{i}" for i in range(self.N)}
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual(self.learn_calls, [])
        self.assertEqual(self._cycles(), [])

    def test_a_still_running_latest_session_defers_the_cycle(self):
        self._seed_runs(self.N, events_fn=_running_events)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        t.join(timeout=10)
        self.assertEqual(self.learn_calls, [], "no analysis while the batch is mid-flight")
        self.assertEqual(self._cycles(), [])
        # and the in-flight flag is released, so a later pass can retry
        self.assertNotIn("a1", self.state._learning_inflight)

    def test_a_failed_model_call_records_no_cycle_so_the_batch_retries(self):
        def boom(system, user):
            raise RuntimeError("model down")
        self.state.learn_fn = boom
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual(self._cycles(), [])
        self.assertEqual(self.client.written, [])
        t2 = self.state.maybe_run_learning_cycle(self.agent)   # same batch, still eligible
        self.assertIsNotNone(t2)
        t2.join(timeout=10)

    def test_nothing_to_change_still_records_a_cycle(self):
        self.state.learn_fn = lambda s, u: '{"analysis": "all good", "docs": {}}'
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual(self.client.written, [])
        cycles = self._cycles()
        self.assertEqual(len(cycles), 1, "a clean answer must reset the counter")
        self.assertEqual(cycles[0]["docs_written"], [])

    def test_disallowed_docs_in_the_reply_are_never_written(self):
        self.state.learn_fn = lambda s, u: json.dumps({
            "analysis": "x",
            "docs": {agent_config.MEMORY_LESSONS_DOC: "ok",
                     agent_config.MEMORY_STATUS_DOC: "must not land",
                     agent_config.MEMORY_SHARED_DOC: "must not land",
                     "/mnt/other.md": "must not land"}})
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual([w["path"] for w in self.client.written],
                         [agent_config.MEMORY_LESSONS_DOC])

    def test_inflight_guard_blocks_a_concurrent_second_cycle(self):
        gate = threading.Event()

        def slow(system, user):
            gate.wait(10)
            return '{"analysis": "x", "docs": {}}'
        self.state.learn_fn = slow
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        try:
            self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent))
        finally:
            gate.set()
            t.join(timeout=10)

    def test_an_agent_without_a_memory_store_is_skipped(self):
        self._seed_runs(self.N)
        agent = dict(self.agent, memory_store_id=None)
        self.assertIsNone(self.state.maybe_run_learning_cycle(agent))
        self.assertEqual(self.learn_calls, [])

    def test_prior_cycles_feed_the_next_analysis(self):
        registry.learning_cycle_add(self.root, {
            "agent_id": "a1", "owner_user_id": "u1",
            "session_stats": [{"session_id": "old", "status": "completed",
                               "total_tokens": 900, "estimated": False}],
            "total_input_tokens": 800, "total_output_tokens": 100,
            "tokens_estimated": False, "analysis": "earlier advice", "docs_written": []})
        # seeded a second later so the new runs sort after the cycle watermark
        time.sleep(1.1)
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        t.join(timeout=10)
        payload = json.loads(self.learn_calls[0]["user"])
        self.assertEqual(payload["prior_cycles"][0]["analysis"], "earlier advice")
        self.assertEqual(payload["prior_cycles"][0]["avg_tokens_per_session"], 900)


class TriggerWiringTest(unittest.TestCase):
    """The trigger only works if the choke points actually call it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(sessions_root=self.root)
        self.asked = []
        self.state.maybe_run_learning_cycle = \
            lambda a: self.asked.append(a.get("agent_id")) or None

    def tearDown(self):
        self.state.scheduler_stop.set()
        self._tmp.cleanup()

    def test_run_agent_invokes_the_trigger(self):
        def no_client():
            raise RuntimeError("no_api_key")
        self.state._live_client = no_client
        self.state._refresh_agent_binding = lambda a: None
        self.state._retrieve_guidance = lambda a: []
        self.state._rejected_for = lambda a: []
        self.state._agent_dm = lambda *a, **k: None
        self.state.run_agent({"agent_id": "a1", "owner_user_id": "u1", "name": "A"})
        self.assertEqual(self.asked, ["a1"])

    def test_deployment_sync_invokes_the_trigger_and_throttles(self):
        registry.agent_upsert(self.root, {
            "agent_id": "a1", "owner_user_id": "u1", "name": "A",
            "deployment_id": "dep_1", "memory_store_id": "mem_1"})

        class SyncClient:
            druns = [{"id": "drun_1", "session_id": "s1",
                      "error_type": None, "error_message": None, "created_at": "t"}]

            def list_deployment_runs(self, dep_id):
                return list(self.druns)

        self.state._live_client = lambda: SyncClient()
        self.state._agent_dm = lambda *a, **k: None
        t0 = time.time()
        self.state.sync_deployment_runs(now=t0)                # fresh drun -> checked
        self.assertEqual(self.asked, ["a1"])
        self.state.sync_deployment_runs(now=t0 + 1)            # nothing fresh, throttled
        self.assertEqual(self.asked, ["a1"])
        self.state.sync_deployment_runs(
            now=t0 + agent_config.LEARNING_CHECK_SECONDS + 1)  # window elapsed
        self.assertEqual(self.asked, ["a1", "a1"],
                         "deployment rows are recorded at session START; without the "
                         "periodic re-check a cycle would stall until the next run")


if __name__ == "__main__":
    unittest.main()
