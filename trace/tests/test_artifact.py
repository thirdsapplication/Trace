import json
import os
import tempfile
import unittest

from trace.artifact import review
from trace.artifact.artifact import (assemble_manual_note, assemble_spec,
                                     build_prompt_material, generate_specs)

# --- synthetic, domain-neutral-ish test data (NOT the Marcy fixtures) ---
EVENTS = [
    {"id": "e1", "ts_start": "t1", "ts_end": "t1", "app": "Mail", "window": "Inbox",
     "action": "read_email", "detail": "read order from Acme: 3 widgets", "confidence": "high"},
    {"id": "e2", "ts_start": "t2", "ts_end": "t2", "app": "Sheet", "window": "Orders",
     "action": "enter_data", "detail": "entered Acme 3 widgets total 30", "confidence": "high"},
    {"id": "e3", "ts_start": "t3", "ts_end": "t3", "app": "Mail", "window": "Reply",
     "action": "reply_email", "detail": "replied confirming to Acme", "confidence": "high"},
]
DISCOVERY = {
    "bundles": [{"id": "B1", "name": "Order intake", "description": "discovery wording",
                 "trigger": "PO email", "steps": ["read", "enter", "reply"], "frequency": "1x",
                 "evidence_event_ids": ["e1", "e2", "e3"], "why_automatable": "rules",
                 "confidence": "high"}],
    "anti_bundles": [{"id": "A1", "name": "Custom pricing", "description": "surface workflow",
                      "why_judgment": "discretionary call each time", "recommended_handling": "human decides",
                      "evidence_event_ids": ["e1"], "confidence": "high"}],
    "unclustered_or_noise_event_ids": [], "notes": "n",
}


def write(obj):
    fd, p = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    return p


class SeedTest(unittest.TestCase):
    def test_seed_defaults(self):
        r = review.seed_review(DISCOVERY)
        b = r["bundles"][0]
        self.assertEqual(b["classification"], "bundle")
        self.assertEqual(b["disposition"], "approve_for_build")
        self.assertEqual(b["included_example_event_ids"], ["e1", "e2", "e3"])
        self.assertEqual(b["human_checkpoints"], [])
        self.assertEqual(r["anti_bundles"][0]["classification"], "anti_bundle")


class RationaleTest(unittest.TestCase):
    """Spec 5 Change 1: the one-line rationale is carried into the review contract
    for both groups, and survives validate_reviewed (incl. across a reclassification)."""
    def setUp(self):
        self.ids = {"e1", "e2", "e3"}
        self.rep = review.ReviewReport()

    def test_seed_carries_rationale_both_groups(self):
        disc = json.loads(json.dumps(DISCOVERY))
        disc["bundles"][0]["rationale"] = "Same steps every time, so an agent can do it."
        disc["anti_bundles"][0]["rationale"] = "Each case needs a judgment call."
        r = review.seed_review(disc)
        self.assertEqual(r["bundles"][0]["rationale"], "Same steps every time, so an agent can do it.")
        self.assertEqual(r["anti_bundles"][0]["rationale"], "Each case needs a judgment call.")

    def test_seed_falls_back_to_internal_why_when_no_rationale(self):
        r = review.seed_review(DISCOVERY)                  # DISCOVERY has no rationale field
        self.assertEqual(r["bundles"][0]["rationale"], "rules")               # from why_automatable
        self.assertEqual(r["anti_bundles"][0]["rationale"], "discretionary call each time")  # why_judgment

    def test_rationale_survives_validate_and_reclassification(self):
        obj = {"anti_bundles": [{"id": "Y", "classification": "bundle", "name": "n",
                                 "steps": ["a"], "disposition": "approve_for_build",
                                 "rationale": "Actually a fixed update each time.",
                                 "reclassified_from": "anti_bundle",
                                 "evidence_event_ids": ["e1"], "included_example_event_ids": ["e1"]}],
               "bundles": [], "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(out["bundles"][0]["rationale"], "Actually a fixed update each time.")


class FetchExamplesTest(unittest.TestCase):
    def test_examples_are_verbatim(self):
        ex = review.fetch_examples(EVENTS, ["e2", "e3"])
        self.assertEqual(ex[0]["detail"], "entered Acme 3 widgets total 30")
        self.assertEqual(ex[0]["app"], "Sheet")

    def test_unknown_ids_skipped(self):
        self.assertEqual(review.fetch_examples(EVENTS, ["zzz"]), [])


class ValidateReviewedTest(unittest.TestCase):
    def setUp(self):
        self.ids = {"e1", "e2", "e3"}
        self.rep = review.ReviewReport()

    def test_routes_by_classification_bundle_to_anti(self):
        # an item placed in "bundles" but classified anti -> ends up in anti_bundles
        obj = {"bundles": [{"id": "X", "classification": "anti_bundle", "name": "n",
                            "why_judgment": "j", "evidence_event_ids": ["e1"]}],
               "anti_bundles": [], "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(len(out["bundles"]), 0)
        self.assertEqual(out["anti_bundles"][0]["id"], "X")

    def test_routes_anti_to_bundle_with_reclass(self):
        obj = {"anti_bundles": [{"id": "Y", "classification": "bundle", "name": "n",
                                 "steps": ["a", "b"], "disposition": "approve_for_build",
                                 "reclassified_from": "anti_bundle",
                                 "original_reason_for_judgment": "needs a call",
                                 "evidence_event_ids": ["e1"],
                                 "included_example_event_ids": ["e1"]}],
               "bundles": [], "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(out["bundles"][0]["id"], "Y")
        self.assertEqual(out["bundles"][0]["original_reason_for_judgment"], "needs a call")

    def test_disposition_and_evidence_repair(self):
        obj = {"bundles": [{"id": "B1", "classification": "bundle", "steps": ["s"],
                            "disposition": "maybe", "evidence_event_ids": ["e1", "ghost"],
                            "included_example_event_ids": ["e1", "ghost"]}],
               "anti_bundles": [], "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(out["bundles"][0]["disposition"], "reject")
        self.assertEqual(out["bundles"][0]["evidence_event_ids"], ["e1"])
        self.assertTrue(self.rep.errors)

    def test_checkpoint_step_index_normalized(self):
        obj = {"bundles": [{"id": "B1", "classification": "bundle", "steps": ["a", "b"],
                            "disposition": "approve_for_build",
                            "human_checkpoints": [{"step_index": "1", "note": "stop"}],
                            "evidence_event_ids": [], "included_example_event_ids": []}],
               "anti_bundles": [], "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(out["bundles"][0]["human_checkpoints"], [{"step_index": 1, "note": "stop"}])


class GenerateSpecsTest(unittest.TestCase):
    def _reviewed(self, **overrides):
        r = review.seed_review(DISCOVERY)
        r["bundles"][0].update(overrides)
        return r

    def test_approved_bundle_builds_three_layer_spec(self):
        reviewed = self._reviewed(
            description="EDITED: always include the total",
            added_acceptance_criteria=["must include every line item, every time"],
            human_checkpoints=[{"step_index": 2, "note": "if total mismatches the PO, stop and ask"}])
        rp, ep = write(reviewed), write(EVENTS)

        def stub(system, user):
            return json.dumps({"intent": "EDITED: always include the total",
                               "derived_acceptance_criteria": ["output includes all line items and the total"],
                               "example_assessments": {"e1": "representative", "e2": "representative", "e3": "representative"},
                               "notes": ""})

        results, _ = generate_specs(rp, ep, model_fn=stub)
        spec = next(r for r in results if r["id"] == "B1")
        md = spec["markdown"]
        self.assertTrue(spec["built"])
        self.assertEqual(spec["filename"], "spec_B1.md")
        self.assertIn("## 1. Intent", md)
        self.assertIn("## 2. Observed examples", md)
        self.assertIn("## 3. Acceptance criteria", md)
        # verbatim example present
        self.assertIn("entered Acme 3 widgets total 30", md)
        # user criterion present, marked
        self.assertIn("must include every line item, every time", md)
        self.assertIn("(user-specified)", md)
        # checkpoint -> pause-and-ask criterion present
        self.assertIn("pause and request human input", md)
        self.assertIn("if total mismatches the PO", md)
        os.remove(rp); os.remove(ep)

    def test_rejected_bundle_not_built(self):
        reviewed = self._reviewed(disposition="reject")
        rp, ep = write(reviewed), write(EVENTS)
        # model must NOT be called for a rejected bundle
        results, _ = generate_specs(rp, ep, model_fn=self._boom)
        b = next(r for r in results if r["id"] == "B1")
        self.assertFalse(b["built"])
        self.assertIsNone(b["markdown"])
        os.remove(rp); os.remove(ep)

    def test_anti_bundle_emits_manual_note_no_model_call(self):
        reviewed = self._reviewed(disposition="reject")  # no approved bundles
        rp, ep = write(reviewed), write(EVENTS)
        results, _ = generate_specs(rp, ep, model_fn=self._boom)
        a = next(r for r in results if r["id"] == "A1")
        self.assertEqual(a["classification"], "anti_bundle")
        self.assertFalse(a["built"])
        self.assertIn("Manual handling", a["markdown"])
        self.assertIn("discretionary call each time", a["markdown"])
        self.assertTrue(a["filename"].endswith("_manual.md"))
        os.remove(rp); os.remove(ep)

    def test_flipped_anti_to_bundle_builds(self):
        # Spec 5 Change 2: a user marking a "manual" item automatable makes it build.
        r = review.seed_review(DISCOVERY)
        a = r["anti_bundles"][0]
        flipped = {**a, "classification": "bundle", "disposition": "approve_for_build",
                   "steps": [], "reclassified_from": "anti_bundle",
                   "original_reason_for_judgment": a.get("why_judgment", "")}
        reviewed = {"bundles": r["bundles"] + [flipped], "anti_bundles": [], "notes": ""}
        rp, ep = write(reviewed), write(EVENTS)
        stub = lambda s, u: json.dumps({"intent": "x", "derived_acceptance_criteria": [],
                                        "example_assessments": {}, "notes": ""})
        results, _ = generate_specs(rp, ep, model_fn=stub)
        a1 = next(r for r in results if r["id"] == "A1")
        self.assertTrue(a1["built"])
        self.assertEqual(a1["classification"], "bundle")
        os.remove(rp); os.remove(ep)

    def test_flipped_bundle_to_manual_excluded(self):
        # Spec 5 Change 2: a user marking an automatable item manual excludes it.
        r = review.seed_review(DISCOVERY)
        b = r["bundles"][0]
        flipped = {**b, "classification": "anti_bundle", "reclassified_from": "bundle",
                   "why_judgment": "user says this needs a person"}
        reviewed = {"bundles": [], "anti_bundles": r["anti_bundles"] + [flipped], "notes": ""}
        rp, ep = write(reviewed), write(EVENTS)
        results, _ = generate_specs(rp, ep, model_fn=self._boom)   # nothing approved => no model call
        b1 = next(r for r in results if r["id"] == "B1")
        self.assertFalse(b1["built"])
        self.assertEqual(b1["classification"], "anti_bundle")
        os.remove(rp); os.remove(ep)

    def test_divergence_marks_contradicted_example_illustrative(self):
        # DoD end-to-end: an edit contradicts an observed example -> intent + criteria
        # follow the edit, the contradicted example is marked illustrative, kept
        # examples stay verbatim.
        reviewed = self._reviewed(description="EDITED: never CC the manager")
        rp, ep = write(reviewed), write(EVENTS)

        def stub(system, user):
            return json.dumps({"intent": "EDITED: never CC the manager",
                               "derived_acceptance_criteria": ["the reply does not CC anyone"],
                               "example_assessments": {"e3": "illustrative"},  # the reply diverges
                               "notes": "e3 contradicts the edited intent"})

        results, _ = generate_specs(rp, ep, model_fn=stub)
        md = next(r for r in results if r["id"] == "B1")["markdown"]
        self.assertIn("EDITED: never CC the manager", md)            # intent follows edit
        self.assertIn("the reply does not CC anyone", md)            # criteria follow edit
        self.assertIn("`e3`", md)
        self.assertIn("*illustrative*", md)                          # contradicted example marked
        self.assertIn("not a target", md)
        self.assertIn("replied confirming to Acme", md)              # still verbatim
        os.remove(rp); os.remove(ep)

    def test_excluded_example_absent_from_spec(self):
        reviewed = self._reviewed(included_example_event_ids=["e1", "e3"])  # e2 excluded
        rp, ep = write(reviewed), write(EVENTS)
        results, _ = generate_specs(rp, ep, model_fn=lambda s, u: json.dumps(
            {"intent": "i", "derived_acceptance_criteria": [], "example_assessments": {}, "notes": ""}))
        md = next(r for r in results if r["id"] == "B1")["markdown"]
        self.assertNotIn("entered Acme 3 widgets total 30", md)  # e2 excluded
        self.assertIn("read order from Acme", md)                # e1 kept
        os.remove(rp); os.remove(ep)

    def test_dry_run_material_includes_verbatim_examples(self):
        b = review.seed_review(DISCOVERY)["bundles"][0]
        examples = review.fetch_examples(EVENTS, b["included_example_event_ids"])
        material = build_prompt_material(b, examples)
        self.assertIn("entered Acme 3 widgets total 30", material)
        self.assertIn("OBSERVED EXAMPLES", material)

    @staticmethod
    def _boom(system, user):
        raise AssertionError("model_fn must not be called here")


class EnvironmentIntegrationTest(unittest.TestCase):
    """Review 2 folded into the engine: real uploaded artifacts lead the Observed
    Examples and sharpen Acceptance Criteria; a missing record flags the spec."""

    def _setup(self, with_env):
        from trace.artifact.artifact import environment_filename
        d = tempfile.mkdtemp()
        reviewed = review.seed_review(DISCOVERY)
        with open(os.path.join(d, "reviewed.json"), "w") as f:
            json.dump(reviewed, f)
        with open(os.path.join(d, "events.json"), "w") as f:
            json.dump(EVENTS, f)
        if with_env:
            env = {"bundle_id": "B1", "artifacts": [
                {"role": "input", "kind": "spreadsheet",
                 "derived_from_event_ids": ["e2"], "label_shown_to_user": "the source sheet",
                 "user_instruction": "use this sheet as the source for the order fields",
                 "uploaded": True, "file_ref": "/x/src.csv", "extraction_status": "ok",
                 "extracted": {"format": "csv", "headers": ["Customer", "Item", "Qty"],
                               "sample_rows": [["Acme", "widgets", "3"]], "tab_names": ["(single sheet)"]}}],
                "answers": [], "status": "complete"}
            with open(os.path.join(d, environment_filename("B1")), "w") as f:
                json.dump(env, f)
        return d

    def _stub(self, system, user):
        self.captured = user
        return json.dumps({"intent": "i", "derived_acceptance_criteria": [],
                           "example_assessments": {}, "notes": ""})

    def test_environment_folds_into_spec(self):
        d = self._setup(with_env=True)
        results, _ = generate_specs(os.path.join(d, "reviewed.json"),
                                    os.path.join(d, "events.json"), model_fn=self._stub)
        md = next(r for r in results if r["id"] == "B1")["markdown"]
        # the extracted real schema reached the model
        self.assertIn("EXTRACTED ENVIRONMENT ARTIFACTS", self.captured)
        # Observed Examples carry the raw file + the authoritative instruction
        self.assertIn("Real uploaded artifacts", md)
        self.assertIn("use this sheet as the source for the order fields", md)
        self.assertIn("Raw file (read live)", md)
        # auto-read extraction is present but clearly labelled unverified, not authoritative
        self.assertIn("UNVERIFIED", md)
        # acceptance criteria are live-checked against the uploaded file, via the instruction
        self.assertIn("(live-checked against the attached file)", md)
        # privacy boundary surfaced; not flagged incomplete
        self.assertIn("embeds real business documents", md)
        self.assertNotIn("environment-incomplete", md)
        self.assertEqual(next(r for r in results if r["id"] == "B1")["environment_status"], "complete")

    def test_missing_environment_flags_incomplete_but_still_builds(self):
        d = self._setup(with_env=False)
        results, _ = generate_specs(os.path.join(d, "reviewed.json"),
                                    os.path.join(d, "events.json"), model_fn=self._stub)
        spec = next(r for r in results if r["id"] == "B1")
        self.assertTrue(spec["built"])                       # never hard-blocks
        self.assertIn("environment-incomplete", spec["markdown"])
        self.assertEqual(spec["environment_status"], "missing")


class UnderstandingTest(unittest.TestCase):
    """The review step's forced approve/correct: ``user_description`` is the worker's
    verbatim, authoritative correction of what the workflow really is; ``understanding``
    tracks confirmed/corrected/unresolved. Both survive validate_reviewed on both
    shapes, so a correction written on a Manual card survives a later flip."""
    def setUp(self):
        self.ids = {"e1", "e2", "e3"}
        self.rep = review.ReviewReport()

    def test_seed_defaults_understanding_unset(self):
        r = review.seed_review(DISCOVERY)
        self.assertEqual(r["bundles"][0]["user_description"], "")
        self.assertEqual(r["bundles"][0]["understanding"], "")
        self.assertEqual(r["anti_bundles"][0]["user_description"], "")
        self.assertEqual(r["anti_bundles"][0]["understanding"], "")

    def test_correction_whitelisted_both_shapes(self):
        obj = {"bundles": [{"id": "B1", "classification": "bundle", "steps": ["s"],
                            "disposition": "approve_for_build",
                            "understanding": "corrected",
                            "user_description": "I enter each tracking number into the ERP."}],
               "anti_bundles": [{"id": "A1", "classification": "anti_bundle", "name": "n",
                                 "why_judgment": "j", "understanding": "confirmed",
                                 "user_description": ""}],
               "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(out["bundles"][0]["user_description"],
                         "I enter each tracking number into the ERP.")
        self.assertEqual(out["bundles"][0]["understanding"], "corrected")
        self.assertEqual(out["anti_bundles"][0]["understanding"], "confirmed")
        self.assertEqual(out["anti_bundles"][0]["user_description"], "")

    def test_corrected_without_text_repaired(self):
        obj = {"bundles": [{"id": "B1", "classification": "bundle", "steps": ["s"],
                            "disposition": "approve_for_build",
                            "understanding": "corrected", "user_description": "   "}],
               "anti_bundles": [], "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(out["bundles"][0]["understanding"], "")
        self.assertEqual(out["bundles"][0]["user_description"], "")
        self.assertTrue(any("corrected" in e for e in self.rep.errors))

    def test_nonempty_text_forces_corrected(self):
        # a stale "confirmed" (or missing) marker never wins over actual correction text
        obj = {"bundles": [{"id": "B1", "classification": "bundle", "steps": ["s"],
                            "disposition": "approve_for_build",
                            "understanding": "confirmed",
                            "user_description": "  Actually I update the ERP.  "}],
               "anti_bundles": [], "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(out["bundles"][0]["understanding"], "corrected")
        self.assertEqual(out["bundles"][0]["user_description"], "Actually I update the ERP.")

    def test_correction_survives_reclassification(self):
        obj = {"anti_bundles": [{"id": "Y", "classification": "bundle", "name": "n",
                                 "steps": ["a"], "disposition": "approve_for_build",
                                 "reclassified_from": "anti_bundle",
                                 "user_description": "This is really data entry, not judgment.",
                                 "evidence_event_ids": ["e1"],
                                 "included_example_event_ids": ["e1"]}],
               "bundles": [], "notes": ""}
        out = review.validate_reviewed(obj, self.ids, self.rep)
        self.assertEqual(out["bundles"][0]["user_description"],
                         "This is really data entry, not judgment.")
        self.assertEqual(out["bundles"][0]["understanding"], "corrected")


if __name__ == "__main__":
    unittest.main()
