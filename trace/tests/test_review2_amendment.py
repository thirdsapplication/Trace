"""Review 2 amendment — courier + annotator model.

Proves: user_instruction is authoritative and reaches the spec; the raw file always
travels; auto-extraction is best-effort and non-fatal; criteria target the
user-designated location and never an auto-extracted schema; completeness keys off
uploads + instructions, not extraction. All proven on two different-domain
workflows with no code changes. No run-specific value (no real tab/column names)
appears in assertions.
"""

import json
import os
import tempfile
import unittest

from trace.artifact import environment
from trace.artifact.artifact import environment_filename, generate_specs


def _stub(system, user):
    return json.dumps({"intent": "i", "derived_acceptance_criteria": [],
                       "example_assessments": {}, "notes": ""})


def _run(events, bundle_id, env_record):
    """Write reviewed/events/environment into a temp dir and generate the spec."""
    d = tempfile.mkdtemp()
    reviewed = {"bundles": [{"id": bundle_id, "classification": "bundle", "name": "WF",
                             "description": "d", "trigger": "t", "steps": ["s"],
                             "disposition": "approve_for_build", "human_checkpoints": [],
                             "included_example_event_ids": [e["id"] for e in events],
                             "added_acceptance_criteria": [], "preferred_output": "",
                             "evidence_event_ids": [e["id"] for e in events],
                             "confidence": "high", "reclassified_from": None,
                             "original_reason_for_judgment": ""}],
                "anti_bundles": [], "notes": ""}
    with open(os.path.join(d, "reviewed.json"), "w") as f:
        json.dump(reviewed, f)
    with open(os.path.join(d, "events.json"), "w") as f:
        json.dump(events, f)
    with open(os.path.join(d, environment_filename(bundle_id)), "w") as f:
        json.dump(env_record, f)
    results, _ = generate_specs(os.path.join(d, "reviewed.json"),
                                os.path.join(d, "events.json"), model_fn=_stub, env_dir=d)
    spec = next(r for r in results if r["id"] == bundle_id)
    return spec


def _criteria_section(md):
    i = md.find("## 3. Acceptance criteria")
    return md[i:] if i != -1 else ""


def _artifact(role, instruction, uploaded=True, extracted=None, file_ref="/s/f.bin", kind="doc"):
    return {"role": role, "kind": kind, "derived_from_event_ids": ["x"],
            "label_shown_to_user": "an artifact", "user_instruction": instruction,
            "uploaded": uploaded, "file_ref": file_ref,
            "extraction_status": environment._extraction_status(extracted), "extracted": extracted}


# --- domain 1: a doc input + spreadsheet output ---------------------------
EVENTS_1 = [{"id": "p1", "app": "Viewer", "window": "doc.pdf", "action": "open_document", "detail": "x"},
            {"id": "p2", "app": "Grid", "window": "Book", "action": "enter_data", "detail": "x"}]
# --- domain 2: structurally different — a record system write + a notes ref
EVENTS_2 = [{"id": "q1", "app": "Tracker", "window": "Rec", "action": "enter_data", "detail": "x"},
            {"id": "q2", "app": "Notes", "window": "n.txt", "action": "open_document", "detail": "x"}]


class InstructionAuthoritativeTest(unittest.TestCase):
    def test_instruction_round_trips_to_spec(self):
        instr = "Use this file as the reference for where the order fields live."
        env = {"bundle_id": "B1", "artifacts": [_artifact("input", instr,
               extracted={"format": "pdf", "text": "some text", "field_layout": []})],
               "answers": [], "status": "complete"}
        spec = _run(EVENTS_1, "B1", env)
        self.assertIn(instr, spec["markdown"])                      # in Observed Examples
        self.assertIn(instr, _criteria_section(spec["markdown"]))   # drives the criterion

    def test_raw_file_always_travels_even_when_extraction_empty(self):
        # extraction empty/failed; the raw file + instruction still reach the spec
        env = {"bundle_id": "B1", "artifacts": [_artifact("input",
               "Use this as the reference.", extracted=None, file_ref="/s/po.pdf")],
               "answers": [], "status": "incomplete"}
        spec = _run(EVENTS_1, "B1", env)
        md = spec["markdown"]
        self.assertTrue(spec["built"])                  # never blocks
        self.assertIn("spec_B1_files/po.pdf", md)       # raw file referenced (spec-relative)
        self.assertNotIn("/s/po.pdf", md)               # never a session-storage path (P2)
        self.assertIn("Use this as the reference.", md)
        # complete because file + instruction present, despite failed extraction (R5)
        self.assertEqual(spec["environment_status"], "complete")
        self.assertNotIn("environment-incomplete", md)

    def test_extraction_failure_is_non_fatal_via_attach_upload(self):
        # a file the extractors can't read -> extraction_status failed, no raise
        d = tempfile.mkdtemp()
        rec = {"bundle_id": "B1", "artifacts": [_artifact("input", "", uploaded=False,
               extracted=None)], "answers": [], "status": "incomplete"}
        p = os.path.join(d, "weird.pdf")
        with open(p, "wb") as f:
            f.write(b"%PDF-1.4 no extractable text here")
        environment.attach_upload(rec, 0, p, "weird.pdf")
        self.assertEqual(rec["artifacts"][0]["extraction_status"], "failed")
        self.assertEqual(rec["artifacts"][0]["file_ref"], p)        # file kept


class InstructionOverridesExtractionTest(unittest.TestCase):
    """The wrong-target failure (extraction read the wrong location), stated
    generally with neutral names."""

    def _env(self, bundle_id, wrong_cols, instruction):
        return {"bundle_id": bundle_id, "artifacts": [_artifact("output", instruction, kind="spreadsheet",
                extracted={"format": "xlsx", "tab_names": ["Alpha", "Beta"], "headers": wrong_cols},
                file_ref="/s/book.xlsx")], "answers": [], "status": "complete"}

    def test_criteria_target_instruction_not_extracted_columns(self):
        # extraction landed on the wrong tab and recovered columns ColX/ColY;
        # the user designated a different location in the instruction.
        instr = "Write to the Beta tab; match the columns as labeled in that tab."
        spec = _run(EVENTS_1, "B1", self._env("B1", ["ColX", "ColY"], instr))
        crit = _criteria_section(spec["markdown"])
        self.assertIn(instr, crit)                       # the designated location wins
        self.assertNotIn("ColX", crit)                   # no wrong-target assertion
        self.assertNotIn("ColY", crit)
        # the extracted columns may appear only as an unverified aid in examples
        self.assertIn("UNVERIFIED", spec["markdown"])

    def test_generality_second_domain_doc_section(self):
        # different domain, different kind (doc), different designated sub-location
        instr = "Treat the 'Summary' section as the source; ignore the header block."
        env = {"bundle_id": "BX", "artifacts": [_artifact("reference", instr, kind="doc",
               extracted={"format": "docx", "text": "wrong header content",
                          "field_layout": [{"label": "ZZTopField", "value": "v"}]},
               file_ref="/s/notes.docx")], "answers": [], "status": "complete"}
        spec = _run(EVENTS_2, "BX", env)
        crit = _criteria_section(spec["markdown"])
        self.assertIn(instr, crit)
        self.assertNotIn("ZZTopField", crit)             # extracted field not asserted


class CompletenessRuleTest(unittest.TestCase):
    def test_missing_instruction_is_incomplete(self):
        rec = {"bundle_id": "B1", "artifacts": [_artifact("input", "")],  # uploaded, no instruction
               "answers": [], "status": "complete"}
        self.assertEqual(environment.validate_environment(rec)["status"], "incomplete")

    def test_failed_extraction_with_file_and_instruction_is_complete(self):
        rec = {"bundle_id": "B1", "artifacts": [_artifact("input", "use it", extracted=None)],
               "answers": [], "status": "incomplete"}
        out = environment.validate_environment(rec)
        self.assertEqual(out["artifacts"][0]["extraction_status"], "failed")
        self.assertEqual(out["status"], "complete")


if __name__ == "__main__":
    unittest.main()
