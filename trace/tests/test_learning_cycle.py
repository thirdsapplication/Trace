"""Learning cycles must actually fire, and only edit what they may.

The trigger lives on the two run-recording choke points (run_agent and the
deployment sync pass); the worker fetches the batch's real session events, makes
ONE Fable call, and writes ONLY /lessons_learned.md back to the agent's memory
store. These tests pin the counting (5 eligible sessions since the last cycle),
the terminality gate, the retry-vs-reset distinction (a failed call records no
cycle; "nothing to change" records one), the code-side edit whitelist, and that
both choke points invoke the trigger at all.

The collective path rides on that same cycle and is off by default
(TRACE_COLLECTIVE_ENABLED): peer material goes IN to the analysis payload, a scrubbed
bundle goes OUT after the memory write. Both directions are pinned here rather than in
the peers/scrub unit tests because both failure modes are failures of the WIRING, not of
either engine — a peer's owner identifiable inside the payload handed to a DIFFERENT
customer's model, or an agent losing the lessons it earned because the publish behind
them failed.

Offline: tempdir file-mode registry, fake platform client, injected learn_fn/scrub_fn.
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


def _tool_events(tools=("gmail-search", "sheets-read")):
    """A completed session that actually CALLED tools. Peer matching runs on tool names
    and nothing else, so a batch of tool-free sessions fingerprints to {} and matches
    nobody — which is a real property of the design, and would make every collective
    assertion below pass for the wrong reason."""
    return ([{"type": "user.message", "text": "Task: go"}]
            + [{"type": "mcp_tool_use", "name": t, "input": {}} for t in tools]
            + [{"type": "agent.message", "text": "did the work",
                "usage": {"input_tokens": 100, "output_tokens": 20}},
               {"type": "session.status_idle", "text": "", "stop_reason": "end_turn"}])


class FakeLearnClient:
    """The platform surface a learning cycle touches."""

    def __init__(self):
        self.events = {}                       # session_id -> events
        self.dead = set()                      # session_ids deleted from the platform
        self.fetches = 0                       # list_session_events call count
        self.docs = {agent_config.MEMORY_LESSONS_DOC: "# Lessons learned\n- old lesson\n"}
        self.written = []                      # {store_id, path, content}
        self.deleted = []                      # paths removed from the store
        # One interleaved order of the calls a cycle makes to anything outside itself.
        # `written` says WHAT was written, this says WHEN relative to everything else —
        # which is the only way to assert that publishing a peer bundle happens after
        # the agent's own memory write and never instead of it.
        self.timeline = []

    def list_session_events(self, session_id):
        self.fetches += 1
        if session_id in self.dead:
            raise RuntimeError(f"Error code: 404 - Session not found: {session_id}")
        return list(self.events.get(session_id, []))

    def list_memory(self, store_id):
        self.timeline.append("list_memory")
        return dict(self.docs)

    def read_memory(self, store_id, path):
        return self.docs.get(path)

    def write_memory(self, store_id, path, content):
        self.written.append({"store_id": store_id, "path": path, "content": content})
        self.docs[path] = content                # the store the NEXT cycle will list
        self.timeline.append(f"write_memory {path}")
        return "update"

    def delete_memory(self, store_id, path):
        self.deleted.append(path)
        self.docs.pop(path, None)
        self.timeline.append(f"delete_memory {path}")
        return True


class _CycleHarness(unittest.TestCase):
    """Shared fixture: a file-mode registry in a tempdir, the fake platform client, and
    an AppState whose learning call is a stub. No tests of its own — the collective and
    forced-cycle cases below run the same cycle through the same seams, and a second
    copy of this setup is a second thing to keep in step with app.py."""

    N = agent_config.LEARNING_CYCLE_SESSIONS

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.learn_calls = []

        self.score_calls = []

        def learn_fn(system, user):
            self.learn_calls.append({"system": system, "user": user})
            return json.dumps(self.analyst_reply)

        def score_fn(system, user):
            self.score_calls.append({"system": system, "user": user})
            payload = json.loads(user)
            # Two items and no novelty on every session: the scorer's judgement is not
            # what these tests are about, and a constant makes the arithmetic checkable.
            return json.dumps({"sessions": [
                {"session_id": s["session_id"], "items_in_scope": 2,
                 "novel_items": 0, "note": ""} for s in payload["sessions"]]})

        # The default analyst answer: one change, into a document the engine owns.
        self.analyst_reply = {
            "analysis": "fewer rescans",
            "verdict": None,
            "change": {"change_id": "c1", "summary": "check the ledger first",
                       "technique": "front-load the cheap ledger check",
                       "rationale": "saves a rescan per session",
                       "peer_informed": "", "retry_of": "",
                       "docs": {agent_config.MEMORY_LESSONS_DOC:
                                "# Lessons learned\n- check the ledger first\n"},
                       "delete": []},
            "blocked_on": ""}

        self.state = app.AppState(sessions_root=self.root, learn_fn=learn_fn)
        self.state.score_fn = score_fn
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


class LearningCycleTest(_CycleHarness):
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

    def test_the_analyst_sees_a_measurement_memory_and_the_bar(self):
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        payload = json.loads(self.learn_calls[0]["user"])
        self.assertEqual(len(payload["scored"]["sessions"]), self.N)
        self.assertEqual(payload["scored"]["sessions"][0]["total_tokens"], 120)
        self.assertIn("mean_performance", payload["scored"])
        self.assertEqual(payload["min_improvement"], agent_config.MIN_IMPROVEMENT)
        self.assertIn("old lesson", payload["memory"][agent_config.MEMORY_LESSONS_DOC])
        self.assertEqual(payload["agent"]["name"], "Orders")

    def test_the_scorer_is_a_separate_call_that_sees_no_memory_or_log(self):
        # The analyst authors the change it will be graded on. If the same call also set
        # the difficulty, scoring the batch as harder would be the cheapest way to pass.
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual(len(self.score_calls), 1)
        seen = json.loads(self.score_calls[0]["user"])
        self.assertEqual(set(seen), {"sessions"})
        for key in ("memory", "log", "pending", "min_improvement", "baseline_performance"):
            self.assertNotIn(key, seen)

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
        sids = {s["session_id"] for s in payload["scored"]["sessions"]}
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
        sids = {s["session_id"] for s in payload["scored"]["sessions"]}
        self.assertEqual(sids, {f"s{i}" for i in range(self.N)})
        self.assertEqual(len(self._cycles()), 1)

    def test_an_all_dead_batch_defers_without_a_model_call(self):
        self._seed_runs(self.N)
        self.client.dead = {f"s{i}" for i in range(self.N)}
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual(self.learn_calls, [])
        self.assertEqual(self._cycles(), [])

    def test_gone_sessions_are_discovered_once_not_refetched_forever(self):
        """The throttle re-checks every few minutes; without the memo, an agent
        whose history was cleaned up 404-stormed the platform on every pass."""
        self._seed_runs(self.N)
        self.client.dead = {f"s{i}" for i in range(self.N)}
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        fetched = self.client.fetches
        self.assertEqual(fetched, self.N)                  # discovered once
        self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent),
                          "gone sessions must no longer count as eligible runs")
        self.assertEqual(self.client.fetches, fetched, "no re-fetch of dead sessions")

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

    def test_the_engine_may_rewrite_and_create_any_document_but_live_state(self):
        """The engine owns the store: documents it made and documents it inherits alike.
        The one exception is live state, and it is an exception the engine cannot reach
        rather than one it is asked to respect."""
        self.state.learn_fn = lambda s, u: json.dumps({
            "analysis": "x", "verdict": None, "blocked_on": "",
            "change": {"change_id": "c9", "summary": "s", "technique": "t",
                       "rationale": "r", "peer_informed": "", "retry_of": "",
                       "docs": {agent_config.MEMORY_LESSONS_DOC: "rewritten",
                                agent_config.MEMORY_STATUS_DOC: "a ledger rewrite, refused",
                                "/playbook.md": "a document that did not exist before"},
                       "delete": []}})
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual(sorted(w["path"] for w in self.client.written),
                         sorted([agent_config.MEMORY_LESSONS_DOC, "/playbook.md"]))
        self.assertNotIn(agent_config.MEMORY_STATUS_DOC,
                         [w["path"] for w in self.client.written])

    def test_a_path_that_is_not_a_store_path_is_never_written(self):
        """What the engine no longer chooses is WHICH documents exist; what it still
        cannot choose is whether a path is a path. A nested path, a traversal, or a
        non-markdown name is dropped in code however the model asked for it."""
        self.state.learn_fn = lambda s, u: json.dumps({
            "analysis": "x", "verdict": None, "blocked_on": "",
            "change": {"change_id": "c9", "summary": "s", "technique": "t",
                       "rationale": "r", "peer_informed": "", "retry_of": "",
                       "docs": {agent_config.MEMORY_LESSONS_DOC: "ok",
                                "/mnt/other.md": "nested — must not land",
                                "/../escape.md": "traversal — must not land",
                                "/notes.txt": "not markdown — must not land",
                                "/blank.md": "   "},
                       "delete": []}})                # empty content is a delete in disguise
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual([w["path"] for w in self.client.written],
                         [agent_config.MEMORY_LESSONS_DOC])

    def test_the_engine_may_delete_a_document(self):
        self.client.docs["/stale.md"] = "no longer earns its place"
        self.state.learn_fn = lambda s, u: json.dumps({
            "analysis": "x", "verdict": None, "blocked_on": "",
            "change": {"change_id": "c9", "summary": "s", "technique": "t",
                       "rationale": "r", "peer_informed": "", "retry_of": "",
                       "docs": {}, "delete": ["/stale.md"]}})
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual(self.client.deleted, ["/stale.md"])
        self.assertNotIn("/stale.md", self.client.docs)

    def test_a_path_written_and_deleted_in_one_answer_keeps_the_write(self):
        # Applying both gives a different store depending on order, so the contradiction
        # has to resolve one way in code: the write carries content and survives.
        self.state.learn_fn = lambda s, u: json.dumps({
            "analysis": "x", "verdict": None, "blocked_on": "",
            "change": {"change_id": "c9", "summary": "s", "technique": "t",
                       "rationale": "r", "peer_informed": "", "retry_of": "",
                       "docs": {agent_config.MEMORY_LESSONS_DOC: "kept"},
                       "delete": [agent_config.MEMORY_LESSONS_DOC]}})
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        self.assertEqual(self.client.deleted, [])
        self.assertEqual(self.client.docs[agent_config.MEMORY_LESSONS_DOC], "kept")

    def test_the_whole_store_is_listed_not_three_known_paths(self):
        # A document the engine wrote last cycle must be visible to the next one, or it
        # reasons about a store it has already changed.
        self.client.docs["/playbook.md"] = "written by an earlier cycle"
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        t.join(timeout=10)
        payload = json.loads(self.learn_calls[0]["user"])
        self.assertIn("/playbook.md", payload["memory"])

    def test_inflight_guard_blocks_a_concurrent_second_cycle(self):
        gate = threading.Event()

        def slow(system, user):
            gate.wait(10)
            return '{"analysis": "x", "verdict": null, "change": null, "blocked_on": ""}'
        self.state.learn_fn = slow
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        try:
            self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent))
        finally:
            gate.set()
            t.join(timeout=10)

    def test_only_one_cycle_runs_at_a_time_fleet_wide(self):
        """N agents used to mean N concurrent cycles, each a long analysis call plus a
        fan-out of platform reads — competing for the same API headroom as the
        synchronous activity view, which is what pushed that page over its budget."""
        gate = threading.Event()

        def slow(system, user):
            gate.wait(10)
            return '{"analysis": "x", "verdict": null, "change": null, "blocked_on": ""}'
        self.state.learn_fn = slow
        self._seed_runs(self.N)
        registry.agent_upsert(self.root, {"agent_id": "a2", "owner_user_id": "u1",
                                          "name": "Other", "memory_store_id": "mem_2"})
        for i in range(self.N):
            registry.run_add(self.root, {
                "agent_id": "a2", "owner_user_id": "u1", "status": "ran",
                "summary": "", "details": [], "run_session_id": f"b{i}"})
            self.client.events[f"b{i}"] = _terminal_events()
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        try:
            second = self.state.maybe_run_learning_cycle(
                {"agent_id": "a2", "owner_user_id": "u1", "name": "Other",
                 "memory_store_id": "mem_2"})
            self.assertIsNone(second, "a second agent must wait its turn")
        finally:
            gate.set()
            t.join(timeout=10)

    def test_the_kill_switch_stops_cycles(self):
        self._seed_runs(self.N)
        original = agent_config.LEARNING_ENABLED
        agent_config.LEARNING_ENABLED = False              # TRACE_LEARNING_ENABLED=0
        try:
            self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent))
        finally:
            agent_config.LEARNING_ENABLED = original
        self.assertEqual(self.learn_calls, [])

    def test_an_agent_without_a_memory_store_is_skipped(self):
        self._seed_runs(self.N)
        agent = dict(self.agent, memory_store_id=None)
        self.assertIsNone(self.state.maybe_run_learning_cycle(agent))
        self.assertEqual(self.learn_calls, [])

    def test_verbose_says_why_an_agent_was_skipped(self):
        """A silently skipped agent is indistinguishable from an unchecked one —
        'why is nothing learning?' must have an answer in the log."""
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.state.maybe_run_learning_cycle(
                dict(self.agent, memory_store_id=None), verbose=True)
        self.assertIn("no memory store", out.getvalue())
        self.assertIn("redeploy", out.getvalue())

        self._seed_runs(self.N - 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.state.maybe_run_learning_cycle(self.agent, verbose=True)
        self.assertIn(f"{self.N - 2} eligible session(s)", out.getvalue())

    def test_the_change_log_feeds_the_next_analysis(self):
        """prior_cycles is replaced by a real record: what was tried, what it was measured
        against, and what happened — as three standing lists the analyst reads and never
        writes."""
        registry.change_add(self.root, {
            "agent_id": "a1", "owner_user_id": "u1", "change_id": "old_win",
            "summary": "an earlier change that worked", "technique": "prune dead advice",
            "rationale": "", "paths": [], "baseline": 1000.0})
        registry.change_settle(self.root, "a1", "old_win", "kept", observed=800.0)
        registry.change_add(self.root, {
            "agent_id": "a1", "owner_user_id": "u1", "change_id": "old_loss",
            "summary": "an earlier change that hurt", "technique": "merge everything",
            "rationale": "", "paths": [], "baseline": 1000.0})
        registry.change_settle(self.root, "a1", "old_loss", "reverted", observed=1200.0)
        time.sleep(1.1)
        self._seed_runs(self.N)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        t.join(timeout=10)
        payload = json.loads(self.learn_calls[0]["user"])
        self.assertEqual([c["change_id"] for c in payload["log"]["kept"]], ["old_win"])
        self.assertEqual([c["change_id"] for c in payload["log"]["banned"]], ["old_loss"])
        self.assertEqual(payload["log"]["retryable"], [])


class MeasurementIsRecordedTest(_CycleHarness):
    """The per-session measurement has to survive the cycle that computed it.

    score.measure works out items_in_scope, difficulty and performance for every session
    and code averages them into one number. Only the mean was stored, so a batch was a
    single figure with no way to see inside it — whether the scorer counted the work that
    arrived at all, and whether D is absorbing volume or P is still tracking the workload.
    Both are read from items against performance, and both were being thrown away.
    """

    def _run_one_cycle(self):
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        t.join(timeout=10)
        return self._cycles()[0]

    def test_every_measured_session_carries_its_own_numbers(self):
        self._seed_runs(self.N)
        stats = self._run_one_cycle()["session_stats"]
        self.assertEqual(len(stats), self.N)
        for row in stats:
            # The stub scorer says two items and no novelty; the trigger is the only
            # arriving text, so D = 2 and each session spends 120 tokens.
            self.assertEqual(row["items_in_scope"], 2)
            self.assertEqual(row["novel_items"], 0)
            self.assertEqual(row["difficulty"], 2.0)
            self.assertEqual(row["performance"], 60.0)
            self.assertIn("input_kb", row)
            self.assertEqual(row["total_tokens"], 120, "the old fields survive")

    def test_an_excluded_session_says_why_instead_of_carrying_a_score(self):
        """A batch whose valid rate moved is unreadable without the reason."""
        self._seed_runs(self.N)
        registry.run_add(self.root, {
            "agent_id": "a1", "owner_user_id": "u1", "status": "failed",
            "summary": "", "details": [], "run_session_id": "bad0",
            "drun_id": "d_bad0"})
        self.client.events["bad0"] = _terminal_events()
        cycle = self._run_one_cycle()
        rows = {r["session_id"]: r for r in cycle["session_stats"]}
        self.assertEqual(rows["bad0"]["excluded"], "failed")
        self.assertNotIn("performance", rows["bad0"],
                         "an excluded session is not a measurement and must not read "
                         "as one")
        self.assertEqual(rows["s0"]["performance"], 60.0)
        self.assertLess(cycle["valid_rate"], 1.0)


class ObserveOnlyTest(_CycleHarness):
    """Calibration mode. Observe-only has to mean the store does not change and the
    measurements stay comparable, or the numbers the bar is set from are not measuring
    what they claim to."""

    ORIGINAL = "# Lessons learned\n- old lesson\n"
    CHANGED = "# Lessons learned\n- a bold new idea\n"

    def _observe_only(self):
        original = agent_config.LEARNING_PROPOSE
        agent_config.LEARNING_PROPOSE = False
        self.addCleanup(setattr, agent_config, "LEARNING_PROPOSE", original)

    def _propose(self):
        self.analyst_reply = {
            "analysis": "trying something", "verdict": None, "blocked_on": "",
            "change": {"change_id": "c1", "summary": "a bold new idea",
                       "technique": "restructure the lessons file", "rationale": "r",
                       "peer_informed": "", "retry_of": "",
                       "docs": {agent_config.MEMORY_LESSONS_DOC: self.CHANGED},
                       "delete": []}}

    def _cycle(self, prefix):
        self._seed_runs(self.N, prefix=prefix)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        t.join(timeout=10)

    def test_a_change_left_over_from_propose_on_is_not_judged_or_reverted(self):
        """_apply_change was gated on the switch and _settle_pending was not, so a
        revert verdict still WROTE — the one thing observe-only promises it will not do.
        The change stays pending for whenever propose comes back on."""
        self._propose()
        self._cycle("a")                       # propose is on: the change is applied
        self.assertEqual(self.client.docs[agent_config.MEMORY_LESSONS_DOC], self.CHANGED)
        self._observe_only()
        self.analyst_reply = {"analysis": "no better", "verdict": None,
                              "change": None, "blocked_on": ""}
        time.sleep(1.1)
        writes_before = len(self.client.written)
        self._cycle("b")                       # same cost: it would have been reverted
        self.assertEqual(self.client.docs[agent_config.MEMORY_LESSONS_DOC], self.CHANGED,
                         "observe-only must not restore a snapshot")
        self.assertEqual(len(self.client.written), writes_before,
                         "observe-only must not write to the store at all")
        pending = registry.change_pending(self.root, "a1")
        self.assertEqual(pending["change_id"], "c1")
        self.assertIsNone(pending["outcome"], "left pending, not judged")

    def test_a_failed_analysis_does_not_discard_the_measurement(self):
        """Batches are 'runs since the last recorded cycle', so deferring folds this one
        into the next. A mean over eight sessions is not comparable with a mean over
        five, and comparing batch means is the whole of a calibration run."""
        self._observe_only()
        self.state.learn_fn = lambda system, user: "not json at all"
        self._cycle("a")
        cycles = self._cycles()
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["mean_performance"], 60.0)
        self.assertEqual(cycles[0]["analysis"], "")
        self.assertEqual(self.client.written, [], "still changes nothing")

    def test_with_propose_on_a_failed_analysis_still_defers(self):
        """The retry is right when the analysis is the point: it costs one
        re-measurement of overlapping sessions and buys another attempt at the change."""
        self.state.learn_fn = lambda system, user: "not json at all"
        self._cycle("a")
        self.assertEqual(self._cycles(), [])


class VerdictAndRevertTest(_CycleHarness):
    """The loop closing: a change applied, measured, then kept or restored.

    The revert is the part worth distrusting until it is seen, so it is asserted
    byte-for-byte. A model asked to undo its own change paraphrases it back into
    existence, which is why this is a file restore — and a restore is only sound because
    every document the engine may write has exactly one author."""

    ORIGINAL = "# Lessons learned\n- old lesson\n"
    CHANGED = "# Lessons learned\n- a bold new idea\n"

    def _propose(self, change_id="c1"):
        self.analyst_reply = {
            "analysis": "trying something", "verdict": None, "blocked_on": "",
            "change": {"change_id": change_id, "summary": "a bold new idea",
                       "technique": "restructure the lessons file", "rationale": "r",
                       "peer_informed": "", "retry_of": "",
                       "docs": {agent_config.MEMORY_LESSONS_DOC: self.CHANGED},
                       "delete": []}}

    def _cycle(self, prefix):
        self._seed_runs(self.N, prefix=prefix)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        t.join(timeout=10)

    def test_a_change_is_snapshotted_applied_and_logged(self):
        self._propose()
        self._cycle("a")
        self.assertEqual(self.client.docs[agent_config.MEMORY_LESSONS_DOC], self.CHANGED)
        snap = registry.snapshot_get(self.root, "a1", "c1")
        self.assertEqual(snap[agent_config.MEMORY_LESSONS_DOC], self.ORIGINAL)
        pending = registry.change_pending(self.root, "a1")
        self.assertEqual(pending["change_id"], "c1")
        self.assertIsNone(pending["outcome"], "not judged until the next cycle")

    def test_a_change_that_misses_the_bar_is_restored_byte_for_byte(self):
        self._propose()
        self._cycle("a")
        # Second cycle: same token cost as the first, so performance did not fall and the
        # change cannot clear the bar.
        self.analyst_reply = {"analysis": "no better", "verdict": None,
                              "change": None, "blocked_on": ""}
        time.sleep(1.1)
        self._cycle("b")
        self.assertEqual(self.client.docs[agent_config.MEMORY_LESSONS_DOC],
                         self.ORIGINAL,
                         "the revert must restore the exact prior bytes")
        settled = [c for c in registry.changes_for_agent(self.root, "a1")
                   if c["change_id"] == "c1"][0]
        self.assertEqual(settled["outcome"], "reverted")
        self.assertIsNotNone(settled["threshold"])

    def test_a_document_the_change_created_is_removed_by_the_revert(self):
        # The snapshot stores None for a path that did not exist, so undoing a creation
        # is a deletion rather than a write of empty content.
        self.analyst_reply = {
            "analysis": "new doc", "verdict": None, "blocked_on": "",
            "change": {"change_id": "c1", "summary": "s", "technique": "t",
                       "rationale": "r", "peer_informed": "", "retry_of": "",
                       "docs": {"/playbook.md": "invented"}, "delete": []}}
        self._cycle("a")
        self.assertIn("/playbook.md", self.client.docs)
        self.analyst_reply = {"analysis": "no better", "verdict": None,
                              "change": None, "blocked_on": ""}
        time.sleep(1.1)
        self._cycle("b")
        self.assertNotIn("/playbook.md", self.client.docs)

    def test_a_change_that_cannot_be_snapshotted_is_not_applied(self):
        # A change that cannot be reverted must not be made.
        real = registry.snapshot_add

        def boom(*a, **k):
            raise RuntimeError("no snapshot table")
        registry.snapshot_add = boom
        self.addCleanup(setattr, registry, "snapshot_add", real)
        self._propose()
        self._cycle("a")
        self.assertEqual(self.client.docs[agent_config.MEMORY_LESSONS_DOC],
                         self.ORIGINAL)

    def test_observe_only_measures_and_records_but_changes_nothing(self):
        # Calibration mode, and the convergence stop: the bar cannot be set honestly
        # before the metric's noise floor has been seen, and this is how it is seen.
        original = agent_config.LEARNING_PROPOSE
        agent_config.LEARNING_PROPOSE = False
        self.addCleanup(setattr, agent_config, "LEARNING_PROPOSE", original)
        self._propose()
        self._cycle("a")
        self.assertEqual(self.client.docs[agent_config.MEMORY_LESSONS_DOC],
                         self.ORIGINAL)
        self.assertIsNone(registry.change_pending(self.root, "a1"))
        cycles = registry.learning_cycles_for_agent(self.root, "a1")
        self.assertIsNotNone(cycles[0]["mean_performance"],
                             "the measurement is still recorded — it IS the calibration")

    def test_the_arithmetic_overrules_the_analysts_stated_verdict(self):
        self._propose()
        self._cycle("a")
        # The analyst claims its change worked. The numbers say otherwise, and the
        # numbers are what execute: the one pen a graded party must not hold.
        self.analyst_reply = {
            "analysis": "it worked", "change": None, "blocked_on": "",
            "verdict": {"change_id": "c1", "decision": "keep", "baseline": 1.0,
                        "threshold": 1.0, "observed": 1.0, "reason": "trust me"}}
        time.sleep(1.1)
        self._cycle("b")
        settled = [c for c in registry.changes_for_agent(self.root, "a1")
                   if c["change_id"] == "c1"][0]
        self.assertEqual(settled["outcome"], "reverted")
        self.assertEqual(self.client.docs[agent_config.MEMORY_LESSONS_DOC],
                         self.ORIGINAL)


class CollectiveLearningCycleTest(_CycleHarness):
    """The collective path end to end: a peer's scrubbed material reaching the analysis
    call, and this agent's batch leaving as a bundle after the cycle is safely done.

    The switch is off by default, so the first test is the one describing production
    today; every other test here turns it on explicitly. The scrub is stubbed as an
    IDENTITY rewrite — same session ids, same line counts, no identifier shapes — so the
    gate's own deterministic checks pass and what is left under test is the wiring.
    scrub.py's tests are where a bad scrub is caught.
    """

    PEER_ID = "peer_agent_7"
    PEER_OWNER = "u-peer-owner"
    PEER_NAME = "Bakery Concierge"
    PEER_EMAIL = "chef@bakery.example"
    PEER_LESSON = "read the sheet once per run, not once per row"

    def setUp(self):
        super().setUp()
        self.scrub_calls, self.verify_calls = [], []

        def scrub_fn(system, user):
            self.scrub_calls.append(user)
            payload = json.loads(user)
            return json.dumps({"sessions": payload["sessions"],
                               "memory": payload["memory"], "redactions": []})

        def verify_fn(system, user):
            self.verify_calls.append(user)
            return '{"safe": true}'

        self.state.scrub_fn = scrub_fn
        self.state.scrub_verify_fn = verify_fn

    def _enable_collective(self):
        original = agent_config.COLLECTIVE_ENABLED
        agent_config.COLLECTIVE_ENABLED = True             # TRACE_COLLECTIVE_ENABLED=1
        self.addCleanup(setattr, agent_config, "COLLECTIVE_ENABLED", original)
        # _peer_vector embeds the agent's profile. With no VOYAGE key that is already a
        # local no-op returning None, but a developer machine that HAS one would make
        # this suite call Voyage over the network — so the absence is pinned rather than
        # assumed. None is the ordinary answer, and peers then match on tool overlap.
        original_embed = app.agent_embeddings.embed
        app.agent_embeddings.embed = lambda *a, **k: None
        self.addCleanup(setattr, app.agent_embeddings, "embed", original_embed)

    def _seed_peer_bundle(self, tools=("gmail-search", "sheets-read"), verified=True):
        """A publishable bundle from ANOTHER customer's agent, alongside that agent's
        real registry row — the identity the analysis payload must not carry."""
        registry.agent_upsert(self.root, {
            "agent_id": self.PEER_ID, "owner_user_id": self.PEER_OWNER,
            "name": self.PEER_NAME, "owner_email": self.PEER_EMAIL,
            "memory_store_id": "mem_peer"})
        registry.peer_bundle_add(self.root, {
            "agent_id": self.PEER_ID, "owner_user_id": self.PEER_OWNER,
            "sessions": [{"session_id": "p1", "status": "completed",
                          "transcript": "[agent] chased restaurant client A"}],
            "memory": {agent_config.MEMORY_LESSONS_DOC: self.PEER_LESSON},
            "fingerprint": {"tools": {t: 3 for t in tools}, "sessions": 5,
                            "avg_tokens": 900},
            "embedding": None, "verified": verified})

    def _run_cycle(self):
        self._seed_runs(self.N, events_fn=_tool_events)
        t = self.state.maybe_run_learning_cycle(self.agent)
        self.assertIsNotNone(t)
        t.join(timeout=10)
        self.assertEqual(len(self.learn_calls), 1)
        return json.loads(self.learn_calls[0]["user"])

    def _published(self):
        return [b for b in registry.peer_bundles_latest(self.root)
                if b.get("agent_id") == "a1"]

    def test_switched_off_no_peer_goes_in_and_no_bundle_comes_out(self):
        """The default. A bundle that would match is present and must still be ignored
        in BOTH directions: an install that never set TRACE_COLLECTIVE_ENABLED has not
        agreed to read other customers' material or to publish its own."""
        self._seed_peer_bundle()
        payload = self._run_cycle()
        self.assertNotIn("peers", payload,
                         "with the collective path off the payload must be byte-identical "
                         "to the solo one — an empty 'peers' key is still a claim")
        self.assertEqual(self.scrub_calls, [], "nothing may be scrubbed for publication")
        self.assertEqual(self._published(), [])
        self.assertEqual([w["path"] for w in self.client.written],
                         [agent_config.MEMORY_LESSONS_DOC])   # the cycle itself is normal

    def test_a_matching_bundle_reaches_the_analysis_payload(self):
        self._enable_collective()
        self._seed_peer_bundle()
        payload = self._run_cycle()
        self.assertEqual(len(payload["peers"]), 1)
        peer = payload["peers"][0]
        self.assertEqual(peer["peer_ref"], "peer agent 1")
        self.assertEqual(peer["tier"], "same_tooling")       # no embeddings -> no cosine
        self.assertEqual(sorted(peer["shared_tools"]), ["gmail-search", "sheets-read"])
        self.assertEqual(peer["tool_overlap"], 1.0)
        self.assertIn(self.PEER_LESSON, json.dumps(peer["memory"]))
        self.assertEqual(peer["sessions"][0]["session_id"], "p1")

    def test_a_bundle_sharing_no_tooling_is_not_offered(self):
        """Tool overlap is the gate, not a preference — a peer that drives different
        integrations has nothing mechanical to teach this agent."""
        self._enable_collective()
        self._seed_peer_bundle(tools=("calendar-list", "slack-post"))
        payload = self._run_cycle()
        self.assertNotIn("peers", payload)

    def test_no_peer_identity_reaches_the_analysis_model(self):
        """THE privacy assertion for this direction. The peer belongs to a different
        customer, and the model receiving this payload works for ours: nothing in it may
        name the source agent, the source agent's owner, or that owner's address —
        directly or in passing. peer_payloads gives a positional label and nothing else,
        and this is the test that fails if a future field carries identity along with it.
        """
        self._enable_collective()
        self._seed_peer_bundle()
        payload = self._run_cycle()
        blob = self.learn_calls[0]["user"].lower()
        for secret in (self.PEER_ID, self.PEER_OWNER, self.PEER_NAME, self.PEER_EMAIL):
            self.assertNotIn(secret.lower(), blob,
                             f"{secret!r} identifies another customer's agent and is in "
                             f"the payload handed to this one's model")
        # Not passing by sharing nothing: the peer's material is genuinely in there.
        self.assertIn(self.PEER_LESSON, blob)
        # The contract's keys exactly — a new key added to peer_payloads has to be
        # weighed against the paragraph above before it can land.
        self.assertEqual(set(payload["peers"][0]),
                         {"peer_ref", "tier", "tool_overlap", "shared_tools",
                          "sessions", "memory", "improvement_log"})

    def test_a_failed_scrub_still_completes_the_agents_own_cycle(self):
        """Publication is best-effort and must never cost the agent its own learning:
        the lessons doc and the cycle row are already durable when the publish runs, and
        a scrub that throws is a lost bundle and nothing else."""
        self._enable_collective()

        def boom(system, user):
            raise RuntimeError("scrub model down")
        self.state.scrub_fn = boom
        self._run_cycle()
        self.assertEqual([w["path"] for w in self.client.written],
                         [agent_config.MEMORY_LESSONS_DOC])
        self.assertIn("check the ledger first", self.client.written[0]["content"])
        self.assertEqual(len(self._cycles()), 1, "the counter must still reset")
        self.assertEqual(self._published(), [], "a failed scrub publishes nothing")

    def test_a_rejected_scrub_costs_the_bundle_and_nothing_else(self):
        """The gate failing closed is a routine outcome, not an error — same result for
        the agent as a scrub that raised."""
        self._enable_collective()
        self.state.scrub_verify_fn = lambda system, user: '{"safe": false}'
        self._run_cycle()
        self.assertEqual([w["path"] for w in self.client.written],
                         [agent_config.MEMORY_LESSONS_DOC])
        self.assertEqual(len(self._cycles()), 1)
        self.assertEqual(self._published(), [])

    def test_the_bundle_is_published_after_the_memory_write(self):
        """Ordering, not just presence. Publishing is a second long model call behind a
        gate that fails closed — the step most likely to fail — so it goes last, once
        everything the cycle owed its own agent is durable."""
        self._enable_collective()
        real_add = registry.peer_bundle_add

        def recording_add(root, bundle):
            self.client.timeline.append("peer_bundle_add")
            return real_add(root, bundle)
        registry.peer_bundle_add = recording_add
        self.addCleanup(setattr, registry, "peer_bundle_add", real_add)
        self._run_cycle()
        self.assertEqual(self.client.timeline,
                         ["list_memory",
                          f"write_memory {agent_config.MEMORY_LESSONS_DOC}",
                          "peer_bundle_add"],
                         "a bundle published before the memory write would advertise "
                         "practice this agent had not adopted yet — and would be lost "
                         "work if the write then failed")

    def test_what_is_published_is_the_batch_the_agent_now_runs_on(self):
        self._enable_collective()
        self._run_cycle()
        published = self._published()
        self.assertEqual(len(published), 1)
        bundle = published[0]
        self.assertTrue(bundle["verified"])
        self.assertEqual(bundle["owner_user_id"], "u1")
        self.assertEqual(len(bundle["sessions"]), self.N)
        self.assertEqual(bundle["fingerprint"]["tools"],
                         {"gmail-search": self.N, "sheets-read": self.N})
        lessons = bundle["memory"][agent_config.MEMORY_LESSONS_DOC]
        self.assertIn("check the ledger first", lessons)
        self.assertNotIn("old lesson", lessons,
                         "the doc just written is what this agent runs on; the pre-cycle "
                         "text describes practice it has already left behind")


class PublishConcurrencyTest(CollectiveLearningCycleTest):
    """Publishing must not hold the fleet-wide learning slot.

    LEARNING_MAX_CONCURRENT is 1, so whatever holds a place in _learning_inflight
    serialises every other agent's cycle behind it. The scrub is the longest call in the
    whole path — a verbatim rewrite streams as many tokens as it reads — with the privacy
    gate behind it. Holding the slot across both would roughly double the window in which
    no other agent can learn, to publish a bundle nobody is waiting on. This is the same
    shape as the lock that was held across network I/O and stalled the app in August.
    """

    def test_the_fleet_slot_is_released_before_the_scrub_runs(self):
        self._enable_collective()
        seen = {}
        real = self.state._publish_peer_bundle

        def watch(agent, records, docs, fp):
            seen["inflight"] = set(self.state._learning_inflight)
            seen["publishing"] = set(self.state._publish_inflight)
            return real(agent, records, docs, fp)

        self.state._publish_peer_bundle = watch
        aid = self.agent["agent_id"]
        # The real path claims the slot in maybe_run_learning_cycle and holds it in the
        # worker's finally, so the claim under test is the one the cycle itself took.
        self._run_cycle()
        self.assertIn("inflight", seen, "publish never ran")
        self.assertNotIn(aid, seen["inflight"],
                         "the fleet-wide learning slot was still held during the scrub")
        self.assertIn(aid, seen["publishing"],
                      "nothing guards this agent against publishing twice at once")


class ForcedLearningCycleTest(_CycleHarness):
    """"Learn now" (force=True): the same cycle the scheduler runs, minus the session
    COUNT gate. It exists because five sessions at a daily cadence made every change to
    the learning path a week-long round trip to check. Everything else still applies —
    which is the half worth testing, since a force that also skipped terminality would
    drop a still-running session from this batch AND from every later one."""

    def setUp(self):
        super().setUp()
        if self.N < 2:
            self.skipTest("TRACE_LEARNING_CYCLE_SESSIONS=1 leaves no room below "
                          "the threshold to force past")

    def test_learn_now_forces_a_cycle_below_the_session_threshold(self):
        self._seed_runs(self.N - 1)
        self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent),
                          "unforced, this batch is still short of a cycle")
        t = self.state.maybe_run_learning_cycle(self.agent, force=True)
        self.assertIsNotNone(t)
        t.join(timeout=10)
        payload = json.loads(self.learn_calls[0]["user"])
        self.assertEqual(len(payload["scored"]["sessions"]), self.N - 1)
        self.assertEqual([w["path"] for w in self.client.written],
                         [agent_config.MEMORY_LESSONS_DOC])
        self.assertEqual(len(self._cycles()), 1)

    def test_forcing_with_nothing_new_analyzes_nothing(self):
        """Forcing cannot conjure material — an empty batch would surface downstream as
        a confusing "analysis failed"."""
        self.assertIsNone(self.state.maybe_run_learning_cycle(self.agent, force=True))
        self.assertEqual(self.learn_calls, [])
        self.assertEqual(self._cycles(), [])

    def test_forcing_does_not_reach_terminality(self):
        self._seed_runs(self.N - 1, events_fn=_running_events)
        t = self.state.maybe_run_learning_cycle(self.agent, force=True)
        self.assertIsNotNone(t)
        t.join(timeout=10)
        self.assertEqual(self.learn_calls, [], "a session still running has nothing to "
                                               "analyze, and forcing past it would drop "
                                               "it from every later batch too")
        self.assertEqual(self._cycles(), [])

    def test_forcing_does_not_reach_the_kill_switch(self):
        self._seed_runs(self.N - 1)
        original = agent_config.LEARNING_ENABLED
        agent_config.LEARNING_ENABLED = False              # TRACE_LEARNING_ENABLED=0
        try:
            self.assertIsNone(
                self.state.maybe_run_learning_cycle(self.agent, force=True))
        finally:
            agent_config.LEARNING_ENABLED = original
        self.assertEqual(self.learn_calls, [])


class TriggerWiringTest(unittest.TestCase):
    """The trigger only works if the choke points actually call it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.state = app.AppState(sessions_root=self.root)
        self.asked = []
        self.state.maybe_run_learning_cycle = \
            lambda a, **kw: self.asked.append(a.get("agent_id")) or None

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
