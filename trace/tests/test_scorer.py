import importlib
import json
import os
import unittest

from trace.discovery import scorer

engine = importlib.import_module("trace.discovery.discover")
FIXTURES = os.path.join(os.path.dirname(engine.__file__), "fixtures")


def yes_judge(candidate, intent):
    return True


def no_judge(candidate, intent):
    return False


class OverlapTest(unittest.TestCase):
    def test_overlap_fraction(self):
        self.assertEqual(scorer._overlap(["e1", "e2", "e3"], ["e1", "e2"]), 1.0)
        self.assertEqual(scorer._overlap(["e1"], ["e1", "e2"]), 0.5)
        self.assertEqual(scorer._overlap([], ["e1"]), 0.0)


class ScoreLogicTest(unittest.TestCase):
    def _ak(self):
        return {
            "bundles": [{"id": "K1", "intent": "do X", "event_ids": ["e1", "e2", "e3", "e4"]}],
            "anti_bundles": [{"id": "KA1", "intent": "judge Y", "event_ids": ["e9", "e10"]}],
            "rubric": {"overlap_threshold": 0.5, "pass_min_bundles": 1,
                       "automation_penalty_per_antibundle": 1},
        }

    def _disc(self, bundles=None, anti=None):
        return {"bundles": bundles or [], "anti_bundles": anti or [],
                "unclustered_or_noise_event_ids": [], "notes": ""}

    def test_bundle_identified_on_overlap_and_intent(self):
        disc = self._disc(bundles=[{"id": "B1", "evidence_event_ids": ["e1", "e2", "e3"]}])
        sc = scorer.score(disc, self._ak(), intent_judge_fn=yes_judge)
        self.assertEqual(sc["bundles_identified"], 1)
        self.assertTrue(sc["passed"])

    def test_not_identified_when_intent_judge_rejects(self):
        disc = self._disc(bundles=[{"id": "B1", "evidence_event_ids": ["e1", "e2", "e3", "e4"]}])
        sc = scorer.score(disc, self._ak(), intent_judge_fn=no_judge)
        self.assertEqual(sc["bundles_identified"], 0)  # overlap high but intent says no

    def test_not_identified_below_overlap_threshold(self):
        disc = self._disc(bundles=[{"id": "B1", "evidence_event_ids": ["e1"]}])  # 0.25
        sc = scorer.score(disc, self._ak(), intent_judge_fn=yes_judge)
        self.assertEqual(sc["bundles_identified"], 0)

    def test_anti_bundle_flagged_is_good(self):
        disc = self._disc(anti=[{"id": "A1", "evidence_event_ids": ["e9", "e10"]}])
        sc = scorer.score(disc, self._ak(), intent_judge_fn=yes_judge)
        self.assertEqual(sc["anti_bundle_results"][0]["handling"], "flagged_human_checkpoint")
        self.assertEqual(sc["automation_penalties"], 0)

    def test_anti_bundle_automated_is_penalized(self):
        # the anti-bundle's events proposed as an automatable bundle -> trust failure
        disc = self._disc(bundles=[{"id": "B1", "evidence_event_ids": ["e9", "e10"]}])
        sc = scorer.score(disc, self._ak(), intent_judge_fn=yes_judge)
        self.assertEqual(sc["anti_bundle_results"][0]["handling"], "proposed_for_automation")
        self.assertEqual(sc["automation_penalties"], 1)
        self.assertFalse(sc["passed"])

    def test_anti_bundle_unclustered_is_neutral(self):
        sc = scorer.score(self._disc(), self._ak(), intent_judge_fn=yes_judge)
        self.assertEqual(sc["anti_bundle_results"][0]["handling"], "unclustered")
        self.assertEqual(sc["automation_penalties"], 0)

    def test_threshold_is_configurable_and_sensitivity_reported(self):
        disc = self._disc(bundles=[{"id": "B1", "evidence_event_ids": ["e1", "e2"]}])  # 0.5
        sc_loose = scorer.score(disc, self._ak(), overlap_threshold=0.4, intent_judge_fn=yes_judge)
        sc_strict = scorer.score(disc, self._ak(), overlap_threshold=0.75, intent_judge_fn=yes_judge)
        self.assertEqual(sc_loose["bundles_identified"], 1)
        self.assertEqual(sc_strict["bundles_identified"], 0)
        self.assertIn(0.5, sc_strict["sensitivity"])


class FixtureGateTest(unittest.TestCase):
    """End-to-end scorer behavior against the real fixtures, with a stub intent
    judge — proving the gate machinery passes a good discovery and penalizes one
    that automates an anti-bundle. (The real model run is what produces the
    discovery; this validates the grading harness.)"""

    def _ak(self, name):
        with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
            return json.load(f)

    def _discovery_mirroring(self, ak, automate_first_anti=False):
        bundles = [{"id": f"B{i+1}", "name": b["id"], "description": b["intent"],
                    "evidence_event_ids": list(b["event_ids"])}
                   for i, b in enumerate(ak["bundles"])]
        anti = []
        for i, a in enumerate(ak["anti_bundles"]):
            target = bundles if (automate_first_anti and i == 0) else anti
            target.append({"id": (f"B{len(bundles)+1}" if target is bundles else f"A{i+1}"),
                           "name": a["id"], "description": a["intent"],
                           "evidence_event_ids": list(a["event_ids"]),
                           "why_judgment": a["intent"], "recommended_handling": "human"})
        return {"bundles": bundles, "anti_bundles": anti,
                "unclustered_or_noise_event_ids": [], "notes": ""}

    def test_good_discovery_passes_marcy_gate(self):
        ak = self._ak("marcy_answer_key.json")
        sc = scorer.score(self._discovery_mirroring(ak), ak, intent_judge_fn=yes_judge)
        self.assertEqual(sc["bundles_identified"], len(ak["bundles"]))
        self.assertEqual(sc["automation_penalties"], 0)
        self.assertTrue(sc["passed"])

    def test_automating_an_antibundle_fails_marcy_gate(self):
        ak = self._ak("marcy_answer_key.json")
        sc = scorer.score(self._discovery_mirroring(ak, automate_first_anti=True),
                          ak, intent_judge_fn=yes_judge)
        self.assertGreaterEqual(sc["automation_penalties"], 1)
        self.assertFalse(sc["passed"])

    def test_good_discovery_passes_recruiter_gate(self):
        ak = self._ak("recruiter_answer_key.json")
        sc = scorer.score(self._discovery_mirroring(ak), ak, intent_judge_fn=yes_judge)
        self.assertEqual(sc["bundles_identified"], len(ak["bundles"]))
        self.assertTrue(sc["passed"])


if __name__ == "__main__":
    unittest.main()
