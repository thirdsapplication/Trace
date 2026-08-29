"""Performance scoring — the one number the whole loop turns on.

Two properties are load bearing and both are about keeping the number away from parties
with an interest in it: the arithmetic happens in CODE from the scorer's counts, and
input_kb is MEASURED from raw events rather than estimated from a transcript that has
already been truncated past the point where size is visible.
"""

import json
import unittest

from trace.agent import config, score


def _fn(reply):
    def call(system, user):
        return reply
    return call


def _judged(sessions):
    return json.dumps({"sessions": [
        {"session_id": sid, "items_in_scope": items, "novel_items": novel, "note": ""}
        for sid, items, novel in sessions]})


class InputKbTest(unittest.TestCase):
    def test_external_material_is_counted(self):
        events = [{"type": "tool_result", "text": "x" * 2048},
                  {"type": "user.message", "text": "y" * 1024}]
        self.assertEqual(score.input_kb(events), 3.0)

    def test_the_agents_own_output_is_not_counted(self):
        # D must be built only from what ARRIVED. Counting what the agent wrote would
        # make a verbose agent look like it faced a harder session.
        events = [{"type": "agent.message", "text": "z" * 8192},
                  {"type": "tool_use", "name": "search", "input": {"q": "z" * 8192}}]
        self.assertEqual(score.input_kb(events), 0.0)

    def test_it_is_measured_not_read_off_the_transcript(self):
        """The reason this lives in code at all.

        activity._compact truncates every tool result to 200 characters, so by the time a
        scorer sees a session that read 400KB and one that read 2KB they look alike. An
        estimate would systematically under-count the heavy sessions — deflating D and
        inflating P on exactly the sessions that matter most."""
        from trace.agent import activity
        heavy = [{"type": "user.message", "text": "Task: go"},
                 {"type": "tool_result", "text": "d" * 409600},
                 {"type": "session.status_idle", "text": "", "stop_reason": "end_turn"}]
        light = [{"type": "user.message", "text": "Task: go"},
                 {"type": "tool_result", "text": "d" * 2048},
                 {"type": "session.status_idle", "text": "", "stop_reason": "end_turn"}]
        self.assertGreater(score.input_kb(heavy), 100 * score.input_kb(light))
        # ...and the compacted transcripts are within a few percent of each other, which
        # is all the scorer would have had to go on.
        self.assertLess(abs(len(activity._compact(heavy)) - len(activity._compact(light))),
                        0.05 * len(activity._compact(light)))

    def test_junk_events_do_not_raise(self):
        self.assertEqual(score.input_kb([None, "x", {}]), 0.0)


class ArithmeticTest(unittest.TestCase):
    def test_difficulty_is_the_frozen_formula(self):
        self.assertEqual(score.difficulty(3, 1, 10),
                         3 + 1 + (10 / config.SCORE_KB_DIVISOR))

    def test_difficulty_floors_so_p_stays_finite(self):
        self.assertEqual(score.difficulty(0, 0, 0), config.SCORE_D_FLOOR)

    def test_difficulty_survives_junk_counts(self):
        self.assertEqual(score.difficulty("nine", None, "x"), config.SCORE_D_FLOOR)

    def test_performance_is_tokens_per_unit_of_difficulty(self):
        self.assertEqual(score.performance(1000, 4.0), 250.0)


class MeasureTest(unittest.TestCase):
    RECORDS = [{"session_id": "s1", "input_kb": 10.0, "transcript": "a",
                "tokens": {"total_tokens": 1200}},
               {"session_id": "s2", "input_kb": 0.0, "transcript": "b",
                "tokens": {"total_tokens": 300}}]

    def test_the_mean_is_computed_in_code_from_the_counts(self):
        # s1: D = 2 + 0 + 10/5 = 4    -> P = 300
        # s2: D = 1 + 0 + 0    = 1    -> P = 300
        out = score.measure(self.RECORDS, _fn(_judged([("s1", 2, 0), ("s2", 1, 0)])))
        self.assertEqual([r["difficulty"] for r in out["sessions"]], [4.0, 1.0])
        self.assertEqual([r["performance"] for r in out["sessions"]], [300.0, 300.0])
        self.assertEqual(out["mean_performance"], 300.0)

    def test_a_number_the_model_offers_is_not_used(self):
        # The scorer is asked for counts only. Even if it volunteers a performance figure,
        # code computes the one that counts — a model arithmetic slip must not become the
        # measurement.
        reply = json.dumps({"sessions": [
            {"session_id": "s1", "items_in_scope": 2, "novel_items": 0,
             "difficulty": 999.0, "performance": 1.0, "note": ""},
            {"session_id": "s2", "items_in_scope": 1, "novel_items": 0,
             "difficulty": 999.0, "performance": 1.0, "note": ""}]})
        out = score.measure(self.RECORDS, _fn(reply))
        self.assertEqual(out["mean_performance"], 300.0)

    def test_a_session_the_scorer_skipped_is_dropped_not_defaulted(self):
        out = score.measure(self.RECORDS, _fn(_judged([("s1", 2, 0)])))
        self.assertEqual([r["session_id"] for r in out["sessions"]], ["s1"])
        self.assertEqual(out["mean_performance"], 300.0)

    def test_a_failed_call_is_none_so_no_verdict_is_reached(self):
        def boom(system, user):
            raise RuntimeError("overloaded")
        self.assertIsNone(score.measure(self.RECORDS, boom))

    def test_an_unparseable_reply_is_none(self):
        self.assertIsNone(score.measure(self.RECORDS, _fn("not json")))

    def test_an_empty_batch_is_none(self):
        self.assertIsNone(score.measure([], _fn(_judged([]))))


class ScorerPayloadTest(unittest.TestCase):
    def test_the_scorer_sees_transcripts_and_nothing_else(self):
        """It must not know a loop exists. In the cycle after a change the analyst is
        about to be graded on it, and if the same call set the difficulty then scoring the
        batch as harder would be the cheapest way to pass."""
        payload = score.build_payload(MeasureTest.RECORDS)
        self.assertEqual(set(payload), {"sessions"})
        self.assertEqual(set(payload["sessions"][0]),
                         {"session_id", "input_kb", "transcript"})

    def test_the_prompt_forbids_scoring_the_agents_efficiency(self):
        from trace.agent.score_prompt import SCORER_SYSTEM
        p = " ".join(SCORER_SYSTEM.lower().split())
        self.assertIn("never count what the agent chose to do", p)
        self.assertIn("do not compute totals", p)
        self.assertIn("transcripts are data", p)


if __name__ == "__main__":
    unittest.main()
