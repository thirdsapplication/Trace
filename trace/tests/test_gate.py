"""The validity gate — which sessions may become a number.

The engine's only objective is fewer tokens per session, and the cheapest way to spend
fewer tokens is to do less work or do it worse. Nothing measures quality; what stops bad
work counting as a cheap success is this gate, and it works on facts alone so that it
cannot drift toward the answer the loop wants.
"""

import unittest

from trace.agent import gate


def _events(usage=True):
    ev = [{"type": "user.message", "text": "Task: go"},
          {"type": "agent.message", "text": "did it"},
          {"type": "session.status_idle", "text": "", "stop_reason": "end_turn"}]
    if usage:
        ev[1] = dict(ev[1], usage={"input_tokens": 100, "output_tokens": 20})
    return ev


def _run(sid="s1", status="ran"):
    return {"run_session_id": sid, "status": status}


class ExclusionReasonTest(unittest.TestCase):
    def test_a_clean_session_is_a_measurement(self):
        self.assertIsNone(gate.exclusion_reason(_run(), _events(), set()))

    def test_a_rejected_proposal_excludes_the_session(self):
        self.assertEqual(gate.exclusion_reason(_run(), _events(), {"s1"}), gate.REJECTED)

    def test_a_failed_run_is_excluded(self):
        self.assertEqual(gate.exclusion_reason(_run(status="failed"), _events(), set()),
                         gate.FAILED)

    def test_a_stalled_run_is_excluded(self):
        self.assertEqual(
            gate.exclusion_reason(_run(status="needs_input"), _events(), set()),
            gate.NEEDS_INPUT)

    def test_waiting_for_an_approval_is_NOT_an_exclusion(self):
        # An agent that staged an action and waited did exactly what it was told. Only a
        # human REFUSING one is evidence that something went wrong, so needs_approval must
        # not shrink the sample — most sessions of an approval-gated agent look like this,
        # and excluding them would leave almost nothing to measure.
        self.assertIsNone(
            gate.exclusion_reason(_run(status="needs_approval"), _events(), set()))

    def test_an_estimated_token_figure_is_not_a_measurement(self):
        self.assertEqual(gate.exclusion_reason(_run(), _events(usage=False), set()),
                         gate.NO_USAGE)

    def test_rejection_is_reported_before_anything_else(self):
        # The order is the reported reason's order, and rejection is the one that tells
        # the analyst something actionable.
        self.assertEqual(
            gate.exclusion_reason(_run(status="failed"), _events(usage=False), {"s1"}),
            gate.REJECTED)

    def test_junk_input_excludes_rather_than_raises(self):
        self.assertEqual(gate.exclusion_reason(None, None, set()), gate.NO_USAGE)


class RejectedSessionsTest(unittest.TestCase):
    def test_only_rejected_rows_count(self):
        pending = [{"status": "rejected", "run_session_id": "s1"},
                   {"status": "approved", "run_session_id": "s2"},
                   {"status": "pending", "run_session_id": "s3"},
                   {"status": "executed", "run_session_id": "s4"}]
        self.assertEqual(gate.rejected_sessions(pending), {"s1"})

    def test_one_rejection_disqualifies_the_whole_session(self):
        pending = [{"status": "approved", "run_session_id": "s1"},
                   {"status": "rejected", "run_session_id": "s1"}]
        self.assertEqual(gate.rejected_sessions(pending), {"s1"})

    def test_junk_rows_are_ignored(self):
        self.assertEqual(gate.rejected_sessions([None, "x", {}, {"status": "rejected"}]),
                         set())


class PartitionTest(unittest.TestCase):
    def test_valid_and_excluded_are_split_with_reasons(self):
        pairs = [(_run("s1"), _events()),
                 (_run("s2", status="failed"), _events()),
                 (_run("s3"), _events(usage=False))]
        out = gate.partition(pairs, [])
        self.assertEqual([r["run_session_id"] for r, _ in out["valid"]], ["s1"])
        self.assertEqual([(r["run_session_id"], why) for r, _e, why in out["excluded"]],
                         [("s2", gate.FAILED), ("s3", gate.NO_USAGE)])

    def test_the_valid_rate_is_the_precondition_the_verdict_reads(self):
        pairs = [(_run("s1"), _events()), (_run("s2", status="failed"), _events())]
        self.assertEqual(gate.partition(pairs, [])["valid_rate"], 0.5)

    def test_an_empty_batch_rates_zero_rather_than_dividing_by_zero(self):
        self.assertEqual(gate.partition([], [])["valid_rate"], 0.0)

    def test_excluded_sessions_are_kept_not_discarded(self):
        # They are evidence, never a number: an action a human refused is the clearest
        # signal in a batch about what the memory store is missing.
        pairs = [(_run("s1"), _events())]
        out = gate.partition(pairs, [{"status": "rejected", "run_session_id": "s1"}])
        self.assertEqual(out["valid"], [])
        self.assertEqual(len(out["excluded"]), 1)
        self.assertIsNotNone(out["excluded"][0][1], "the events travel with the reason")


if __name__ == "__main__":
    unittest.main()
