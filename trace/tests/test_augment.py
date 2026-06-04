"""Defect 2 — opening a document in a separate (native) application is recorded
as an "opened/viewed" action, symmetrically with browser documents, driven by a
persistence/duration heuristic — never an app allowlist or file-extension list.

Fixtures vary app names and file types and use only constructed values.
"""

import datetime
import unittest

from trace.reconstruct import augment, validate

_BASE = datetime.datetime(2026, 1, 1, 9, 0, 0)


def _raw(seq):
    """seq: list of ('window', app, title, dwell_s) | ('key', window, text, gap_s).
    Builds an ordered raw stream with timestamps spaced by the given durations."""
    rows = []
    t = 0.0
    for i, item in enumerate(seq):
        rid = f"r{i + 1:04d}"
        ts = (_BASE + datetime.timedelta(seconds=t)).isoformat(timespec="milliseconds")
        if item[0] == "window":
            rows.append({"id": rid, "ts": ts, "source": "window",
                         "payload": {"app": item[1], "title": item[2]}})
            t += item[3]
        else:
            rows.append({"id": rid, "ts": ts, "source": "keystroke",
                         "payload": {"text": item[2], "window": item[1], "ts_end": ts}})
            t += item[3]
    return rows


class SustainedOpenTest(unittest.TestCase):
    def test_several_native_apps_and_filetypes_each_get_open(self):
        # three different native apps / file types, each held in focus past the
        # threshold, none represented by an existing event -> three opens.
        rows = _raw([
            ("window", "ViewerOne", "contract.pdf", 30),
            ("window", "SheetTool", "ledger.xlsx", 30),
            ("window", "ReaderX", "manual.docx", 30),
            ("window", "Shell", "terminal", 1),
        ])
        out = augment.augment_document_opens([], rows, persistence_seconds=3.0)
        opened = {e["window"] for e in out if e["action"] == augment.OPEN_ACTION}
        self.assertEqual(opened, {"contract.pdf", "ledger.xlsx", "manual.docx"})
        # nothing fabricated beyond the title; evidence is the window event only
        for e in out:
            self.assertEqual(len(e["evidence"]), 1)
            self.assertIn(e["window"], e["detail"])

    def test_open_emitted_regardless_of_following_keystrokes(self):
        rows = _raw([
            ("window", "PdfApp", "spec.pdf", 20),   # sustained, no typing follows
            ("window", "Other", "x", 1),
        ])
        out = augment.augment_document_opens([], rows, persistence_seconds=3.0)
        self.assertEqual(sum(1 for e in out if e["action"] == augment.OPEN_ACTION), 1)


class FlickerSuppressionTest(unittest.TestCase):
    def test_rapid_oscillation_below_threshold_yields_no_opens(self):
        # A,B,A,B,A,B each held ~1s, threshold 3s -> no opens
        seq = []
        for _ in range(3):
            seq.append(("window", "AppA", "WinA", 1))
            seq.append(("window", "AppB", "WinB", 1))
        seq.append(("window", "AppC", "WinC", 0))  # final, dwell 0
        rows = _raw(seq)
        out = augment.augment_document_opens([], rows, persistence_seconds=3.0)
        self.assertEqual([e for e in out if e["action"] == augment.OPEN_ACTION], [])


class CoverageTest(unittest.TestCase):
    def test_browser_doc_already_represented_is_not_duplicated(self):
        rows = _raw([("window", "Browser", "Some Web Doc", 30), ("window", "X", "x", 1)])
        existing = [{"id": "e001", "ts_start": "t", "ts_end": "t", "app": "Browser",
                     "window": "Some Web Doc", "action": "navigate",
                     "detail": "opened web doc", "evidence": ["r0001"], "confidence": "high"}]
        out = augment.augment_document_opens(existing, rows, persistence_seconds=3.0)
        self.assertEqual(len(out), 1)  # no synthesized duplicate

    def test_incidental_evidence_does_not_count_as_represented(self):
        # native doc window cited only as incidental evidence on a data-entry event
        # in a *different* window must still get its own open action.
        rows = _raw([
            ("window", "PdfApp", "order.pdf", 30),
            ("window", "Sheet", "Book1", 5),
            ("key", "Book1", "typed the order", 1),
        ])
        existing = [{"id": "e001", "ts_start": "t", "ts_end": "t", "app": "Sheet",
                     "window": "Book1", "action": "enter_data",
                     "detail": "entered order", "evidence": ["r0001", "r0003"],  # cites the pdf window incidentally
                     "confidence": "high"}]
        out = augment.augment_document_opens(existing, rows, persistence_seconds=3.0)
        opens = [e for e in out if e["action"] == augment.OPEN_ACTION]
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["window"], "order.pdf")

    def test_repeated_consults_collapse_to_single_open(self):
        rows = _raw([
            ("window", "PdfApp", "ref.pdf", 30),
            ("window", "Sheet", "B", 5),
            ("window", "PdfApp", "ref.pdf", 30),  # returns to same doc
            ("window", "Sheet", "B", 1),
        ])
        out = augment.augment_document_opens([], rows, persistence_seconds=3.0)
        self.assertEqual(sum(1 for e in out if e["window"] == "ref.pdf"), 1)


class IntegrationWithResequenceTest(unittest.TestCase):
    def test_resequence_after_augment_orders_and_numbers(self):
        rows = _raw([("window", "PdfApp", "a.pdf", 30), ("window", "End", "end", 1)])
        out = validate.resequence(augment.augment_document_opens([], rows, 3.0))
        self.assertTrue(all(e["id"].startswith("e") for e in out))


if __name__ == "__main__":
    unittest.main()
