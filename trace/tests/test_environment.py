"""Review 2 — environment capture: request derivation (kind-agnostic, generic across
workflow types), per-kind extraction, contract round-trip, and status. Fixtures are
fresh and synthetic — no Marcy / gate-2 specifics — and vary the incidentals.
"""

import json
import os
import tempfile
import unittest
import zipfile

from trace.artifact import environment, extract


# --- two structurally-different reviewed bundles (different apps/kinds) -----
# Domain 1: a doc-read + spreadsheet-write workflow.
EVENTS_A = [
    {"id": "a1", "app": "Webmail", "window": "Inbox — req", "action": "read_email", "detail": "x"},
    {"id": "a2", "app": "Viewer", "window": "form-1001.pdf", "action": "open_document", "detail": "x"},
    {"id": "a3", "app": "Grid", "window": "Ledger — Tab1", "action": "enter_data", "detail": "x"},
    {"id": "a4", "app": "Viewer", "window": "form-1002.pdf", "action": "open_document", "detail": "x"},
    {"id": "a5", "app": "Grid", "window": "Ledger — Tab1", "action": "update_data", "detail": "x"},
    {"id": "a6", "app": "Webmail", "window": "Reply", "action": "send_email", "detail": "x"},
]
BUNDLE_A = {"id": "BA", "evidence_event_ids": ["a1", "a2", "a3", "a4", "a5", "a6"]}

# Domain 2: a totally different shape — a record system + a notes doc, no PDFs.
EVENTS_B = [
    {"id": "b1", "app": "Tracker", "window": "Candidate — Jordan", "action": "navigate", "detail": "x"},
    {"id": "b2", "app": "Tracker", "window": "Candidate — Jordan", "action": "enter_data", "detail": "x"},
    {"id": "b3", "app": "Notes", "window": "shortlist.txt", "action": "open_document", "detail": "x"},
    {"id": "b4", "app": "Tracker", "window": "Candidate — Dana", "action": "enter_data", "detail": "x"},
]
BUNDLE_B = {"id": "BB", "evidence_event_ids": ["b1", "b2", "b3", "b4"]}


class DeriveRequestsTest(unittest.TestCase):
    def test_domain_a_requests(self):
        reqs = environment.derive_requests(BUNDLE_A, environment.event_index(EVENTS_A))
        roles = sorted(r["role"] for r in reqs)
        self.assertEqual(roles, ["input", "output"])  # PDF read (input) + grid write (output)
        inp = next(r for r in reqs if r["role"] == "input")
        out = next(r for r in reqs if r["role"] == "output")
        self.assertEqual(inp["kind"], "pdf")                      # guessed from .pdf in title
        self.assertEqual(set(inp["derived_from_event_ids"]), {"a2", "a4"})  # both PDF instances collapse
        self.assertEqual(set(out["derived_from_event_ids"]), {"a3", "a5"})
        # email actions never produce an artifact request
        all_ids = {i for r in reqs for i in r["derived_from_event_ids"]}
        self.assertNotIn("a1", all_ids)
        self.assertNotIn("a6", all_ids)

    def test_domain_b_requests_differ_with_no_code_change(self):
        reqs = environment.derive_requests(BUNDLE_B, environment.event_index(EVENTS_B))
        roles = sorted(r["role"] for r in reqs)
        self.assertEqual(roles, ["input", "output"])
        out = next(r for r in reqs if r["role"] == "output")
        inp = next(r for r in reqs if r["role"] == "input")
        self.assertEqual(out["derived_from_event_ids"], ["b2", "b4"])  # Tracker writes collapse
        self.assertEqual(inp["derived_from_event_ids"], ["b3"])        # the notes doc
        # different apps/kinds than domain A, produced by the same code
        self.assertNotEqual({r["kind"] for r in reqs}, {r["kind"] for r in
                            environment.derive_requests(BUNDLE_A, environment.event_index(EVENTS_A))})


# --- per-kind extraction with fresh fixtures of each kind ------------------
def _make_xlsx(path, sheets, rows):
    """Build a minimal valid-enough xlsx (inline strings) for the stdlib reader."""
    wb = ('<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
          'spreadsheetml/2006/main"><sheets>' +
          "".join(f'<sheet name="{s}" sheetId="{i+1}"/>' for i, s in enumerate(sheets)) +
          '</sheets></workbook>')
    def cell(v):
        return f'<c t="inlineStr"><is><t>{v}</t></is></c>'
    sheet_rows = "".join("<row>" + "".join(cell(c) for c in r) + "</row>" for r in rows)
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
             f'spreadsheetml/2006/main"><sheetData>{sheet_rows}</sheetData></worksheet>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


class ExtractionTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_csv(self):
        p = os.path.join(self.d, "data.csv")
        with open(p, "w") as f:
            f.write("Name,Qty,Price\nAcme,3,30\nBeta,5,55\n")
        self.assertEqual(extract.detect_kind("data.csv"), "spreadsheet")
        ex = extract.extract("spreadsheet", p)
        self.assertEqual(ex["format"], "csv")
        self.assertEqual(ex["headers"], ["Name", "Qty", "Price"])
        self.assertEqual(ex["sample_rows"][0], ["Acme", "3", "30"])

    def test_xlsx_headers_tabs_and_format(self):
        p = os.path.join(self.d, "book.xlsx")
        _make_xlsx(p, ["Live", "Archive"], [["OrderId", "Customer", "Status"], ["1", "Acme", "open"]])
        self.assertEqual(extract.detect_kind("book.xlsx"), "spreadsheet")
        ex = extract.extract("spreadsheet", p)
        self.assertEqual(ex["format"], "xlsx")             # the format fact (xlsx vs native)
        self.assertEqual(ex["tab_names"], ["Live", "Archive"])
        self.assertEqual(ex["headers"], ["OrderId", "Customer", "Status"])

    def test_doc_text_and_field_layout(self):
        p = os.path.join(self.d, "po.txt")
        with open(p, "w") as f:
            f.write("Customer: Acme Foods\nShip Date: 2026-02-15\nItem: 3 widgets\n")
        self.assertEqual(extract.detect_kind("po.txt"), "doc")
        ex = extract.extract("doc", p)
        labels = [f["label"] for f in ex["field_layout"]]
        self.assertIn("Customer", labels)
        self.assertIn("Ship Date", labels)

    def test_unknown_kind_does_not_crash(self):
        p = os.path.join(self.d, "mystery.bin")
        with open(p, "wb") as f:
            f.write(b"\x00\x01\x02")
        ex = extract.extract(extract.detect_kind("mystery.bin", b"\x00\x01"), p)
        self.assertIn("format", ex)


# --- question derivation, contract round-trip, status ----------------------
class QuestionsAndContractTest(unittest.TestCase):
    def test_output_column_absent_from_source_yields_question(self):
        artifacts = [
            {"role": "input", "uploaded": True, "extracted": {"headers": ["Customer", "Item", "Qty"]}},
            {"role": "output", "uploaded": True,
             "extracted": {"headers": ["Customer", "Item", "Qty", "OrderId", "Status"], "tab_names": ["A", "B"]}},
        ]
        qs = environment.derive_questions(artifacts)
        joined = " ".join(qs).lower()
        self.assertIn("orderid", joined)        # present in output, absent in source
        self.assertIn("status", joined)
        self.assertIn("canonical", joined)      # multiple tabs -> which is canonical

    def test_validate_round_trip_and_status(self):
        rec = environment.seed_environment(BUNDLE_A, EVENTS_A)
        self.assertEqual(rec["status"], "incomplete")          # nothing uploaded yet
        rec2 = environment.validate_environment(json.loads(json.dumps(rec)))
        self.assertEqual(rec2["bundle_id"], "BA")
        self.assertEqual(len(rec2["artifacts"]), len(rec["artifacts"]))
        # uploaded but no instruction -> still incomplete (instruction is required)
        for a in rec2["artifacts"]:
            a["uploaded"] = True
        self.assertEqual(environment.validate_environment(rec2)["status"], "incomplete")
        # uploaded + instruction -> complete (extraction status is irrelevant)
        for a in rec2["artifacts"]:
            a["user_instruction"] = "use this file as the reference"
        self.assertEqual(environment.validate_environment(rec2)["status"], "complete")

    def test_attach_upload_extracts_and_updates(self):
        d = tempfile.mkdtemp()
        rec = environment.seed_environment(BUNDLE_A, EVENTS_A)
        out_idx = next(i for i, a in enumerate(rec["artifacts"]) if a["role"] == "output")
        p = os.path.join(d, "ledger.csv")
        with open(p, "w") as f:
            f.write("Customer,Item,OrderId\nAcme,widgets,1\n")
        environment.attach_upload(rec, out_idx, p, "ledger.csv")
        self.assertTrue(rec["artifacts"][out_idx]["uploaded"])
        self.assertEqual(rec["artifacts"][out_idx]["kind"], "spreadsheet")
        self.assertEqual(rec["artifacts"][out_idx]["extracted"]["headers"], ["Customer", "Item", "OrderId"])


class NoHardcodingTest(unittest.TestCase):
    def test_no_order_intake_specifics_in_module(self):
        import importlib
        for m in ("trace.artifact.environment", "trace.artifact.extract"):
            with open(importlib.import_module(m).__file__, encoding="utf-8") as f:
                src = f.read().lower()
            for term in ("purchase order", "po pdf", "erp", "open_orders", "order intake", "marcy"):
                self.assertNotIn(term, src, f"{m} contains run-specific term {term!r}")


if __name__ == "__main__":
    unittest.main()
