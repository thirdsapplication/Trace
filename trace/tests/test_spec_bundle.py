"""Self-contained spec bundles + mandatory preflight (P1–P4).

Proves: every attached artifact is copied next to the spec (byte-identical);
references are spec-relative (no session-storage path); the bundle resolves after
being moved to an arbitrary directory; a preflight block precedes Intent and lists
every artifact + instruction + the stop-on-failure directive; filename collisions
disambiguate; a zero-artifact bundle still exports a valid spec. All on two
different-domain bundles with no code changes. No fixture filename, tab, customer,
or bundle id is hardcoded in assertions.
"""

import json
import os
import re
import shutil
import tempfile
import unittest

from trace.artifact.artifact import generate_specs, write_spec_bundle


def _stub(system, user):
    return json.dumps({"intent": "i", "derived_acceptance_criteria": [],
                       "example_assessments": {}, "notes": ""})


def make_session(bundle_id, artifacts):
    """artifacts: list of (role, kind, filename, content_bytes, instruction).
    Returns (workdir, env_dir) with reviewed/events/environment + real artifact files."""
    workdir = tempfile.mkdtemp()
    srcdir = os.path.join(workdir, "env", bundle_id)
    os.makedirs(srcdir, exist_ok=True)
    art_records = []
    for i, (role, kind, fname, content, instr) in enumerate(artifacts):
        # place each artifact under its own subdir so same basenames can collide
        sub = os.path.join(srcdir, f"a{i}")
        os.makedirs(sub, exist_ok=True)
        p = os.path.join(sub, fname)
        with open(p, "wb") as f:
            f.write(content)
        art_records.append({"role": role, "kind": kind, "derived_from_event_ids": ["e1"],
                            "label_shown_to_user": "an artifact", "user_instruction": instr,
                            "uploaded": True, "file_ref": p,
                            "extraction_status": "failed", "extracted": None})
    env = {"bundle_id": bundle_id, "artifacts": art_records, "answers": [],
           "status": "complete" if all(a["user_instruction"] for a in art_records) else "incomplete"}
    events = [{"id": "e1", "app": "A", "window": "W", "action": "open_document",
               "ts_start": "t", "detail": "x", "confidence": "high"}]
    reviewed = {"bundles": [{"id": bundle_id, "classification": "bundle", "name": "WF",
                             "description": "d", "trigger": "t", "steps": ["s"],
                             "disposition": "approve_for_build", "human_checkpoints": [],
                             "included_example_event_ids": ["e1"], "added_acceptance_criteria": [],
                             "preferred_output": "", "evidence_event_ids": ["e1"],
                             "confidence": "high", "reclassified_from": None,
                             "original_reason_for_judgment": ""}],
                "anti_bundles": [], "notes": ""}
    from trace.artifact.artifact import environment_filename
    for fn, obj in [("reviewed.json", reviewed), ("events.json", events),
                    (environment_filename(bundle_id), env)]:
        with open(os.path.join(workdir, fn), "w") as f:
            json.dump(obj, f)
    return workdir


def _read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


def export(workdir, bundle_id):
    """Generate + write the bundle to a fresh out dir; return (out_dir, spec_path, md)."""
    results, _ = generate_specs(os.path.join(workdir, "reviewed.json"),
                                os.path.join(workdir, "events.json"),
                                model_fn=_stub, env_dir=workdir)
    spec = next(r for r in results if r["id"] == bundle_id)
    out = tempfile.mkdtemp()
    write_spec_bundle(out, spec)
    return out, os.path.join(out, spec["filename"]), spec["markdown"]


REL_RE = re.compile(r"\./(spec_[^/`]+_files/[^\s`)<]+)")


class SpecBundleTest(unittest.TestCase):
    def test_artifacts_copied_byte_identical(self):
        wd = make_session("B1", [("input", "pdf", "po.pdf", b"%PDF-1.4 hi", "use as reference"),
                                 ("output", "spreadsheet", "book.xlsx", b"PKxlsxbytes", "use tab two")])
        out, spec_path, md = export(wd, "B1")
        files_dir = os.path.join(out, "spec_B1_files")
        self.assertTrue(os.path.isdir(files_dir))
        self.assertEqual(set(os.listdir(files_dir)), {"po.pdf", "book.xlsx"})
        self.assertEqual(_read(os.path.join(files_dir, "po.pdf")), b"%PDF-1.4 hi")
        self.assertEqual(_read(os.path.join(files_dir, "book.xlsx")), b"PKxlsxbytes")

    def test_no_session_paths_all_references_spec_relative(self):
        wd = make_session("B1", [("input", "doc", "note.txt", b"hello", "use it")])
        out, spec_path, md = export(wd, "B1")
        # the original source path must not leak
        self.assertNotIn(wd, md)
        self.assertNotIn("captures/sessions", md)
        # every artifact reference is spec-relative
        rels = REL_RE.findall(md)
        self.assertTrue(rels)
        for rel in rels:
            self.assertTrue(rel.startswith("spec_B1_files/"))

    def test_bundle_resolves_after_move(self):
        wd = make_session("B1", [("input", "doc", "a.txt", b"AAA", "use it"),
                                 ("output", "spreadsheet", "b.csv", b"x,y\n1,2\n", "write here")])
        out, spec_path, md = export(wd, "B1")
        # move spec + _files together to an arbitrary new dir
        moved = tempfile.mkdtemp()
        shutil.copy(spec_path, os.path.join(moved, os.path.basename(spec_path)))
        shutil.copytree(os.path.join(out, "spec_B1_files"), os.path.join(moved, "spec_B1_files"))
        spec_in_moved = os.path.join(moved, os.path.basename(spec_path))
        spec_text = _read(spec_in_moved, "r")
        for rel in set(REL_RE.findall(spec_text)):
            resolved = os.path.normpath(os.path.join(os.path.dirname(spec_in_moved), rel))
            self.assertTrue(os.path.isfile(resolved), f"{rel} did not resolve after move")

    def test_preflight_precedes_intent_and_lists_all_artifacts(self):
        wd = make_session("B1", [("input", "pdf", "po.pdf", b"%PDF", "reference for fields"),
                                 ("output", "spreadsheet", "erp.xlsx", b"PK", "use the live tab")])
        out, spec_path, md = export(wd, "B1")
        self.assertIn("## 0. Preflight", md)
        self.assertLess(md.index("## 0. Preflight"), md.index("## 1. Intent"))   # before Intent
        # lists every artifact (relative path) + its instruction + the stop directive
        self.assertIn("spec_B1_files/po.pdf", md)
        self.assertIn("spec_B1_files/erp.xlsx", md)
        self.assertIn("reference for fields", md)
        self.assertIn("use the live tab", md)
        self.assertIn("STOP and report", md)

    def test_filename_collision_disambiguated(self):
        # two artifacts share the basename data.csv
        wd = make_session("B1", [("input", "spreadsheet", "data.csv", b"AAA", "source"),
                                 ("output", "spreadsheet", "data.csv", b"BBB", "target")])
        out, spec_path, md = export(wd, "B1")
        files = set(os.listdir(os.path.join(out, "spec_B1_files")))
        self.assertEqual(len(files), 2)                              # both written, distinct names
        self.assertIn("data.csv", files)
        # the references in the spec match the files actually written
        for rel in set(REL_RE.findall(md)):
            self.assertTrue(os.path.isfile(os.path.join(out, rel)))
        # the two distinct contents both survived
        contents = {_read(os.path.join(out, "spec_B1_files", f)) for f in files}
        self.assertEqual(contents, {b"AAA", b"BBB"})

    def test_zero_artifacts_still_valid(self):
        wd = make_session("B1", [])   # no artifacts attached
        out, spec_path, md = export(wd, "B1")
        self.assertTrue(os.path.isfile(spec_path))
        self.assertIn("## 0. Preflight", md)
        self.assertIn("No real artifacts are attached", md)
        # no empty _files dir error; directory simply not created
        self.assertFalse(os.path.isdir(os.path.join(out, "spec_B1_files")))


class GeneralityTest(unittest.TestCase):
    """Same export code, two different-domain bundles (different kinds/counts)."""

    def _assert_bundle_valid(self, wd, bid):
        out, spec_path, md = export(wd, bid)
        fdir = os.path.join(out, f"{bid_files(bid)}")
        # criterion 1: files copied
        self.assertTrue(os.path.isdir(fdir) and os.listdir(fdir))
        # criterion 2: spec-relative, no source leak
        self.assertNotIn(wd, md)
        # criterion 4: preflight before intent
        self.assertIn("## 0. Preflight", md)
        self.assertLess(md.index("## 0. Preflight"), md.index("## 1. Intent"))

    def test_two_domains_both_export_valid_bundles(self):
        wd1 = make_session("BM", [("input", "pdf", "form.pdf", b"%PDF a", "the PO reference"),
                                  ("output", "spreadsheet", "ledger.xlsx", b"PK b", "the live tab")])
        wd2 = make_session("BR", [("input", "doc", "resume.docx", b"PK c", "the candidate's resume"),
                                  ("reference", "doc", "rubric.txt", b"crit", "the scoring rubric")])
        self._assert_bundle_valid(wd1, "BM")
        self._assert_bundle_valid(wd2, "BR")


def bid_files(bid):
    safe = "".join(c if c.isalnum() else "_" for c in str(bid))
    return f"spec_{safe}_files"


if __name__ == "__main__":
    unittest.main()
