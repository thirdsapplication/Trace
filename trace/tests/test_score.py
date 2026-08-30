"""The score: mean total tokens per valid session (trace/agent/score.py).

Rewritten when the difficulty-normalised score was removed. The old version divided
tokens by a scorer model's item count; seven real sessions spanning one to seven items
showed total tokens varying 9.3% and the normalised figure 31%, so the normalised one
needed a bar of roughly 34% where raw tokens needs 10% -- and almost no real change
clears 34%. The arithmetic here is trivial on purpose: that is the point of it.
"""

import unittest

from trace.agent import score


def _rec(total):
    return {"tokens": {"total_tokens": total, "estimated": False}}


class MeanTokensTest(unittest.TestCase):
    def test_it_is_the_plain_mean_of_the_valid_sessions(self):
        # The seven production sessions this design was chosen from.
        recs = [_rec(n) for n in (531972, 595839, 521524, 523007, 455709, 604397, 534060)]
        self.assertEqual(score.mean_tokens(recs), 538072.6)

    def test_an_empty_group_scores_nothing_rather_than_zero(self):
        """None means "this group has no measurement", which the loop must be able to be
        in. Zero would read as a perfect score and keep whatever was pending."""
        self.assertIsNone(score.mean_tokens([]))
        self.assertIsNone(score.mean_tokens(None))

    def test_a_record_with_no_usage_is_dropped_not_counted_as_zero(self):
        self.assertEqual(score.mean_tokens([_rec(100), {"tokens": {}}, _rec(300)]), 200.0)

    def test_junk_never_raises(self):
        self.assertIsNone(score.mean_tokens([{"tokens": {"total_tokens": "lots"}}]))
        self.assertEqual(score.mean_tokens(["nonsense", _rec(50)]), 50.0)


class InputKbTest(unittest.TestCase):
    """Not part of the score any more; it survives because the profile reports it."""

    def test_it_counts_what_arrived_and_not_what_the_agent_wrote(self):
        events = [{"type": "user.message", "text": "x" * 1024},
                  {"type": "tool_result", "text": "y" * 1024},
                  {"type": "agent.message", "text": "z" * 4096}]
        self.assertEqual(score.input_kb(events), 2.0)

    def test_no_events_is_zero_not_an_error(self):
        self.assertEqual(score.input_kb([]), 0.0)
        self.assertEqual(score.input_kb(None), 0.0)
