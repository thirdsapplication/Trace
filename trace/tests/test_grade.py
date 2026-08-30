"""Grading (trace/agent/grade.py) — the A-F table and what each letter does.

Moved here from test_learn when the verdict stopped being keep/revert and became a
grade. The boundaries are the whole test: they are where binary floating point lands
wrong (3 * 0.10 is 0.30000000000000004), and an improvement of exactly three times the
bar graded B until that was found.
"""

import unittest

from trace.agent import config, grade


class GradeBoundaryTest(unittest.TestCase):
    BAR = 0.10

    def test_every_boundary_lands_on_the_better_grade(self):
        for imp, expected in ((0.31, "A"), (0.30, "A"), (0.299, "B"),
                              (0.21, "B"), (0.20, "B"), (0.199, "C"),
                              (0.11, "C"), (0.10, "C"), (0.099, "D"),
                              (0.0001, "D"), (0.0, "F"), (-0.5, "F")):
            self.assertEqual(grade.grade_for(imp, self.BAR), expected,
                             f"improvement {imp} against a {self.BAR} bar")

    def test_exactly_at_a_multiple_is_the_higher_grade(self):
        """3 * 0.10 is not 0.30 in binary. Without a tolerance an improvement of exactly
        three times the bar reads as under it."""
        self.assertEqual(grade.grade_for(3 * self.BAR, self.BAR), "A")
        self.assertEqual(grade.grade_for(0.30, self.BAR), "A")

    def test_no_improvement_at_all_is_an_F_not_a_D(self):
        """A D is a technique that worked and underdelivered. Zero is not that."""
        self.assertEqual(grade.grade_for(0.0, self.BAR), "F")

    def test_an_unmeasured_attempt_has_no_grade_and_is_reverted(self):
        self.assertIsNone(grade.grade_for(None, self.BAR))
        self.assertEqual(grade.outcome_for(None), grade.REVERTED)

    def test_keeps_and_reverts(self):
        for g in ("A", "B", "C"):
            self.assertEqual(grade.outcome_for(g), grade.KEPT)
        for g in ("D", "F"):
            self.assertEqual(grade.outcome_for(g), grade.REVERTED)


class ImprovementTest(unittest.TestCase):
    def test_lower_tokens_is_positive_improvement(self):
        self.assertAlmostEqual(grade.improvement(1000.0, 900.0), 0.1)

    def test_more_tokens_is_negative(self):
        self.assertAlmostEqual(grade.improvement(1000.0, 1200.0), -0.2)

    def test_a_missing_end_is_unmeasured_not_zero(self):
        self.assertIsNone(grade.improvement(None, 900.0))
        self.assertIsNone(grade.improvement(1000.0, None))

    def test_a_zero_baseline_is_unmeasured_not_an_infinite_win(self):
        self.assertIsNone(grade.improvement(0.0, 900.0))


class JudgeTest(unittest.TestCase):
    def test_nothing_pending_grades_nothing(self):
        self.assertIsNone(grade.judge(None, 900.0, 1000.0))

    def test_the_grade_and_the_outcome_travel_together(self):
        v = grade.judge({"change_id": "c1"}, 700.0, 1000.0, 0.10)
        self.assertEqual((v["grade"], v["outcome"]), ("A", grade.KEPT))
        self.assertAlmostEqual(v["improvement"], 0.3)

    def test_an_unmeasured_group_reverts_rather_than_keeping_on_faith(self):
        v = grade.judge({"change_id": "c1"}, None, 1000.0, 0.10)
        self.assertEqual(v["outcome"], grade.REVERTED)
        self.assertIsNone(v["grade"])
        self.assertIn("no comparable measurement", v["reason"])


class LogListsTest(unittest.TestCase):
    def test_the_three_standing_lists(self):
        lists = grade.log_lists([
            {"attempt_id": "won", "outcome": "kept", "grade": "B"},
            {"attempt_id": "near", "outcome": "reverted", "grade": "D"},
            {"attempt_id": "hurt", "outcome": "reverted", "grade": "F"},
        ])
        self.assertEqual([a["attempt_id"] for a in lists["kept"]], ["won"])
        self.assertEqual([a["attempt_id"] for a in lists["retryable"]], ["near"])
        self.assertEqual([a["attempt_id"] for a in lists["banned"]], ["hurt"])

    def test_a_D_already_retried_once_is_banned(self):
        """Retryable means retryable ONCE. Without this a technique that keeps nearly
        working is proposed forever."""
        lists = grade.log_lists([
            {"attempt_id": "first", "outcome": "reverted", "grade": "D"},
            {"attempt_id": "second", "outcome": "reverted", "grade": "D",
             "retry_of": "first"},
        ])
        self.assertEqual([a["attempt_id"] for a in lists["banned"]], ["first"])
        self.assertEqual([a["attempt_id"] for a in lists["retryable"]], ["second"])

    def test_an_attempt_still_awaiting_judgment_is_on_no_list(self):
        self.assertEqual(grade.log_lists([{"attempt_id": "c", "outcome": None}]),
                         {"kept": [], "retryable": [], "banned": []})

    def test_an_unrecognised_outcome_is_ignored_not_filed_somewhere_plausible(self):
        """A failure recorded as a near-miss is worse than one the engine never sees."""
        for verb in ("keep", "revert", "banned", ""):
            lists = grade.log_lists([{"attempt_id": "c", "outcome": verb}])
            self.assertEqual(sum(len(v) for v in lists.values()), 0, verb)

    def test_a_row_written_before_grades_is_read_from_its_numbers(self):
        """The previous engine recorded numbers and no letter. Reading those rows as
        retryable would file an attempt that made things measurably worse as a near-miss
        and invite the engine to repeat it."""
        real = config.MIN_IMPROVEMENT
        config.MIN_IMPROVEMENT = 0.05
        self.addCleanup(setattr, config, "MIN_IMPROVEMENT", real)
        lists = grade.log_lists([
            {"attempt_id": "hurt", "outcome": "reverted",
             "baseline": 1000.0, "observed": 1200.0},
            {"attempt_id": "near", "outcome": "reverted",
             "baseline": 1000.0, "observed": 990.0},
        ])
        self.assertEqual([a["attempt_id"] for a in lists["banned"]], ["hurt"])
        self.assertEqual([a["attempt_id"] for a in lists["retryable"]], ["near"])
